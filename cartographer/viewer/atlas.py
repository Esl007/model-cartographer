"""Phase 8: cross-model atlas -- pure-function HTML builder over a
`ResultsStore`, same pattern as `cartographer.viewer.serve`'s other pages.

WHAT THIS PLOTS, AND WHY IT CHANGED
-----------------------------------
The first version of this page drew one polyline per model of pass rate
vs. dimension value, restricted to records that varied exactly ONE dim
away from a per-model baseline (the `resume_from_records` idiom). That
filter is correct for a grid sweep and wrong for this store's data: the
adaptive campaign scheduler moves several dims at once, so on the real
779-record store the filter admitted 3-8 records per (model, dim) and
binned them into ONE x value. A polyline with a single point renders
nothing at all. Six charts of empty axes is what "just raw projections"
meant -- the page was not under-styled, it was plotting almost no data.

This version bins by QUANTILE BUCKETS over every record of the selected
query kind instead, which puts all 779 records on screen with 5-250
trials behind each cell, and makes the primary visualization a
MODELS x DIFFICULTY-BUCKET HEATMAP:

  * A heatmap is the natural shape of the question. "Where does each
    model break" is a lookup over a model/difficulty grid; a heatmap is
    that grid, so the answer is a position rather than an inference from
    three overlapping polylines.
  * It degrades honestly at this data's real resolution. Bucket counts
    per cell vary 5..250; a line implies an interpolatable function
    between points that these adaptive, unevenly-sampled x values do not
    support. A cell claims only what it measured.
  * It scales with the store. Six models on shared axes is spaghetti; six
    heatmap rows is still six rows.
  * It is the same object as the "capability map" mental model the rest
    of the app is built around.

The response CURVE is kept as the secondary view, because a heatmap is
poor at the one job lines are good at -- reading two models' degradation
slopes against each other -- and "model vs model" is the page's other
job. It is now driven by the same buckets, so the two views can never
disagree, and it carries the per-model toggles.

Boundary detection: campaign runs only persist `ExperimentRecord`s, not
the scheduler's `CampaignState` (so no access to stored
`boundaries`/`notes`). This module re-fits a logistic frontier per
(model, kind, dim) from the raw (x, passed) points via
`cartographer.schedule.estimate.fit_logistic`, and marks the bucket the
crossing falls in -- but only when the crossing is inside the observed
range, since an extrapolated midpoint is a fit artifact, not a finding.

Every db-sourced string is html.escape'd before it is placed in the page
(SVG text nodes and data-* attributes too).
"""

from __future__ import annotations

import bisect
import html
from dataclasses import dataclass
from urllib.parse import urlencode

from ..schedule.estimate import fit_logistic
from ..store.store import ResultsStore
from .globe_data import display_name

# Only look at records from campaign runs (adaptive scheduler runs), which
# is what run_id's "camp-" prefix denotes across this codebase (see
# run_state_campaign/run_belief_campaign's run_id conventions).
_CAMPAIGN_RUN_PREFIX = "camp-"

# Target number of difficulty buckets per dimension. Dimensions with this
# many distinct values or fewer are bucketed by exact value instead (a
# 2-value dim should show 2 columns, not 6 mostly-empty ones).
_TARGET_BUCKETS = 6

# Below this scored pass rate a heatmap cell stops being achromatic and
# takes the trap color -- the same threshold the shared `_rate_bar` uses,
# so a cell and a table row never disagree about what counts as broken.
_BREAK_THRESHOLD = 0.60

_CELL_W = 96
_CELL_H = 46
_LABEL_W = 196
_HEAT_TOP = 30

_CURVE_W = 620
_CURVE_H = 300
_CURVE_M_L = 52
_CURVE_M_R = 148  # room for direct end-of-line labels
_CURVE_M_T = 22
_CURVE_M_B = 54

# Tokens, mirrored from _CSS's :root (SVG presentation attributes can't
# read CSS custom properties in every renderer, so they are duplicated
# here deliberately and named after the same concepts).
_INK = "#E7E5E2"
_INK_SECONDARY = "#A8A6A1"
_INK_MUTED = "#6E6C68"
_LINE = "rgba(255,255,255,0.08)"
_LINE_STRONG = "rgba(255,255,255,0.16)"
_SURFACE_1 = "#131415"
_SURFACE_2 = "#1a1b1d"
_CLAIM_BELIEF = "#D9A441"
_TRAP = "#C4544B"

# Neutral series get weight/dash to stay distinguishable without spending
# hue on them; hue is reserved for the focal series (see `_focal_model`).
_NEUTRAL_DASHES = ["", "5 3", "1 3", "7 2 2 2", "3 2"]


def _short_model_name(model_name: str) -> str:
    name = model_name
    for prefix in ("nvidia/", "meta-llama/", "mistralai/", "anthropic/", "openai/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name


def _query_kind_of(record) -> str:
    detail = record.verifier_detail or ""
    for part in detail.split(";"):
        if part.startswith("kind="):
            return part[len("kind="):]
    return "unknown"


def _fmt_num(v: float) -> str:
    """Compact axis/label formatting: integers stay integers, fractions
    get two places. Dimension values in this store range from 0.26 to 34."""
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}"


@dataclass(frozen=True)
class _Point:
    x: float
    passed: bool


@dataclass(frozen=True)
class _Bucket:
    """One difficulty column. `exact` buckets match a single value;
    range buckets are half-open (lo, hi] except the first, which is
    closed on both ends so the minimum observed value has a home."""

    lo: float
    hi: float
    exact: bool

    @property
    def label(self) -> str:
        if self.exact or self.lo == self.hi:
            return _fmt_num(self.lo)
        return f"{_fmt_num(self.lo)}–{_fmt_num(self.hi)}"

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2.0


@dataclass
class _Cell:
    n_scored: int = 0
    n_pass: int = 0
    n_error: int = 0

    @property
    def rate(self) -> float | None:
        return (self.n_pass / self.n_scored) if self.n_scored else None


def _build_buckets(values: list[float], target: int = _TARGET_BUCKETS) -> list[_Bucket]:
    """Quantile buckets over the observed value POPULATION (not its
    range), so every column carries a comparable number of trials rather
    than a comparable slice of the x axis -- which matters because the
    adaptive scheduler samples difficulty very unevenly.

    Falls back to exact-value buckets when a dim has few distinct values.
    Returns [] for empty input.
    """
    if not values:
        return []
    distinct = sorted(set(values))
    if len(distinct) <= target:
        return [_Bucket(v, v, True) for v in distinct]

    ordered = sorted(values)
    n = len(ordered)
    edges: list[float] = []
    for i in range(1, target):
        e = ordered[int(i * n / target)]
        if e > ordered[0] and (not edges or e > edges[-1]):
            edges.append(e)
    if not edges:
        return [_Bucket(distinct[0], distinct[-1], False)]

    out: list[_Bucket] = []
    lo = ordered[0]
    for e in edges:
        out.append(_Bucket(lo, e, False))
        lo = e
    out.append(_Bucket(lo, ordered[-1], False))
    return out


def _bucket_index(buckets: list[_Bucket], v: float) -> int | None:
    """Which column `v` belongs to, or None if it falls outside every
    bucket (only possible for exact-mode buckets)."""
    if not buckets:
        return None
    if buckets[0].exact:
        for i, b in enumerate(buckets):
            if b.lo == v:
                return i
        return None
    # Range mode: bisect on the upper edges.
    uppers = [b.hi for b in buckets]
    i = bisect.bisect_left(uppers, v)
    if i >= len(buckets):
        i = len(buckets) - 1
    return i


def _bucket_fraction(buckets: list[_Bucket], v: float) -> float | None:
    """Continuous column position of `v` (e.g. 2.4 = 40% into column 2),
    used to place a fitted boundary between columns rather than snapping
    it to one. None when `v` sits outside the plotted range."""
    if not buckets:
        return None
    if v < buckets[0].lo or v > buckets[-1].hi:
        return None
    i = _bucket_index(buckets, v)
    if i is None:
        return None
    b = buckets[i]
    span = b.hi - b.lo
    if span <= 0:
        return float(i)
    return i - 0.5 + (v - b.lo) / span


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------


@dataclass
class _Pane:
    """Everything needed to draw both views of one (kind, dim) selection."""

    kind: str
    dim: str
    models: list[str]
    buckets: list[_Bucket]
    cells: dict[tuple[str, int], _Cell]
    fits: dict[str, float]          # model -> midpoint, observed-range only
    raw_fits: dict[str, float]      # model -> midpoint, including extrapolated
    n_records: int


def _campaign_records(store: ResultsStore) -> list:
    """Campaign trials that count as evidence: the same primary set the Map
    and Findings use (no resample checks, replications, invalidated runs or
    renderer rejections), minus records that never stamped a query kind."""
    from .findings import _primary_records

    return [
        r
        for r in _primary_records(store)
        if r.run_id.startswith(_CAMPAIGN_RUN_PREFIX) and _query_kind_of(r) != "unknown"
    ]


_KIND_LABEL = {"state": "State", "order": "Order", "belief": "Belief", "adversarial_belief": "Adversarial belief",
               "tool_plan": "Tool plan", "interactive": "Interactive", "code_trace": "Code trace",
               "code_repair": "Code repair", "code_write": "Code writing", "repo_fix": "Repository fix"}


def _kind_label(kind: str) -> str:
    return _KIND_LABEL.get(kind, kind.replace("_", " ").capitalize())


def _dim_label(dim: str) -> str:
    return dim.replace("_", " ")


def _menu(records: list) -> dict[str, dict[str, int]]:
    """kind -> dim -> how many records carry that dim. Drives the two
    selectors (and their counts) without building every pane."""
    out: dict[str, dict[str, int]] = {}
    for r in records:
        kind = _query_kind_of(r)
        dims = out.setdefault(kind, {})
        for d in r.realized_dims:
            dims[d] = dims.get(d, 0) + 1
    return out


def _all_panes(records: list, menu: dict[str, dict[str, int]]) -> dict[tuple[str, str], _Pane]:
    """Every (kind, dim) pane, built once. Used for the frontier count,
    the default selection and the selected pane itself, so those three
    can never be computed from different bucketings."""
    out: dict[tuple[str, str], _Pane] = {}
    for kind, dims in menu.items():
        for dim in dims:
            pane = _build_pane(records, kind, dim)
            if pane is not None:
                out[(kind, dim)] = pane
    return out


_DENSE_CELL_N = 10


def _pane_rank(pane: _Pane) -> tuple:
    """How good a first impression a pane makes.

    Ranking by record count is useless here: in this store every record
    carries all five dims, so all five dims of a kind tie at n=689 and
    the winner is whichever dict order happens to surface. Ranking by
    bucket COUNT is little better -- a dim the scheduler barely moved can
    still produce six exact-value buckets with five trials in each.

    What makes a readable map is cells with enough trials behind them to
    be worth comparing, so that is the primary key: how many cells clear
    `_DENSE_CELL_N`. Fitted frontiers break ties (a dim where something
    actually broke beats one where nothing did), then raw trials, then
    the dim name so the choice is deterministic.
    """
    dense = sum(1 for c in pane.cells.values() if c.n_scored >= _DENSE_CELL_N)
    return (dense, len(pane.fits), pane.n_records, pane.dim)


def _default_selection(panes: dict[tuple[str, str], _Pane]) -> tuple[str, str]:
    if not panes:
        return "", ""
    key = max(panes, key=lambda k: _pane_rank(panes[k]))
    return key[0], key[1]


def _build_pane(records: list, kind: str, dim: str) -> _Pane | None:
    subset = [r for r in records if _query_kind_of(r) == kind and dim in r.realized_dims]
    if not subset:
        return None

    buckets = _build_buckets([float(r.realized_dims[dim]) for r in subset])
    if not buckets:
        return None

    cells: dict[tuple[str, int], _Cell] = {}
    points_by_model: dict[str, list[_Point]] = {}
    for r in subset:
        model = _short_model_name(r.model_name)
        v = float(r.realized_dims[dim])
        idx = _bucket_index(buckets, v)
        if idx is None:
            continue
        cell = cells.setdefault((model, idx), _Cell())
        name = r.verdict.name
        if name in ("PASS", "FAIL"):
            cell.n_scored += 1
            if name == "PASS":
                cell.n_pass += 1
            points_by_model.setdefault(model, []).append(_Point(v, name == "PASS"))
        else:
            cell.n_error += 1

    # Drop columns nothing landed in (exact-mode buckets built from the
    # pooled population can be empty for an individual selection).
    used = sorted({i for (_m, i) in cells})
    if not used:
        return None
    remap = {old: new for new, old in enumerate(used)}
    buckets = [buckets[i] for i in used]
    cells = {(m, remap[i]): c for (m, i), c in cells.items()}

    fits: dict[str, float] = {}
    raw_fits: dict[str, float] = {}
    for model, pts in points_by_model.items():
        fit = fit_logistic([(p.x, p.passed) for p in pts])
        if fit is None:
            continue
        raw_fits[model] = fit.midpoint
        if buckets[0].lo <= fit.midpoint <= buckets[-1].hi:
            fits[model] = fit.midpoint

    models = sorted({m for (m, _i) in cells})
    # Worst overall first: the reader should meet the model that breaks
    # before the one that doesn't.
    def _overall(m: str) -> float:
        scored = sum(c.n_scored for (mm, _i), c in cells.items() if mm == m)
        passed = sum(c.n_pass for (mm, _i), c in cells.items() if mm == m)
        return (passed / scored) if scored else 1.0

    models.sort(key=lambda m: (_overall(m), m))
    return _Pane(kind, dim, models, buckets, cells, fits, raw_fits, len(subset))


def _focal_model(pane: _Pane) -> str | None:
    """The one series that gets hue. Focal-point discipline (design-notes
    + the admin-dashboard reference: every bar identical except the one
    that IS the point) applied along the axis this page is actually about
    -- not "most recent" but "breaks earliest". The model whose fitted
    frontier sits at the lowest difficulty is the finding; everything else
    is context and stays achromatic."""
    if not pane.fits:
        return None
    return min(pane.fits, key=lambda m: pane.fits[m])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _cell_fill(rate: float | None) -> str:
    """Achromatic luminance ramp down to the break threshold, trap red
    below it. One hue total; the ramp itself carries magnitude."""
    if rate is None:
        return "none"
    if rate >= _BREAK_THRESHOLD:
        # 0.60 -> faintest, 1.00 -> brightest. Kept low-contrast on
        # purpose: a passing cell is background, not signal.
        t = (rate - _BREAK_THRESHOLD) / (1.0 - _BREAK_THRESHOLD)
        alpha = 0.05 + 0.13 * t
        return f"rgba(231,229,226,{alpha:.3f})"
    t = 1.0 - (rate / _BREAK_THRESHOLD)  # 0 at threshold, 1 at zero pass
    alpha = 0.16 + 0.44 * t
    return f"rgba(196,84,75,{alpha:.3f})"


def _cell_text_color(rate: float | None) -> str:
    if rate is None:
        return _INK_MUTED
    return _INK if rate < _BREAK_THRESHOLD else _INK_SECONDARY


def _trials_href(kind: str, dim: str, bucket_i: int, model: str | None = None) -> str:
    params = {"kind": kind, "dim": dim, "b": str(bucket_i)}
    if model:
        params["model"] = model
    return "/trials?" + urlencode(params)


def _heatmap_svg(pane: _Pane) -> str:
    """Models (rows) x difficulty buckets (columns), cell = scored pass
    rate. Each cell is a focusable <a> that drills into its trials and
    carries both a native <title> (works with JS off) and a data-tip (the
    styled readout the inline JS shows)."""
    n_cols = len(pane.buckets)
    n_rows = len(pane.models)
    width = _LABEL_W + n_cols * _CELL_W + 8
    height = _HEAT_TOP + n_rows * _CELL_H + 30
    focal = _focal_model(pane)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Pass rate by model and {html.escape(pane.dim)} bucket">'
    ]

    # Column headers.
    for c, b in enumerate(pane.buckets):
        cx = _LABEL_W + c * _CELL_W + _CELL_W / 2
        parts.append(
            f'<text x="{cx:.1f}" y="{_HEAT_TOP - 12}" font-size="10.5" fill="{_INK_MUTED}" '
            f'text-anchor="middle" font-family="monospace">{html.escape(b.label)}</text>'
        )
    parts.append(
        f'<text x="{_LABEL_W}" y="12" font-size="9.5" fill="{_INK_MUTED}" '
        f'font-family="monospace" letter-spacing="0.06em">'
        f'{html.escape(_dim_label(pane.dim).upper())} → HARDER</text>'
    )

    for r_i, model in enumerate(pane.models):
        y = _HEAT_TOP + r_i * _CELL_H
        row_scored = sum(c.n_scored for (m, _i), c in pane.cells.items() if m == model)
        row_pass = sum(c.n_pass for (m, _i), c in pane.cells.items() if m == model)
        row_rate = (row_pass / row_scored) if row_scored else None
        is_focal = model == focal
        label_color = _CLAIM_BELIEF if is_focal else _INK_SECONDARY
        label_weight = "650" if is_focal else "500"
        parts.append(f'<g data-series="{html.escape(model)}">')
        parts.append(
            f'<text x="0" y="{y + _CELL_H / 2 - 2}" font-size="11.5" fill="{label_color}" '
            f'font-family="monospace" font-weight="{label_weight}">'
            f"{html.escape(display_name(model)[:26])}</text>"
        )
        if row_rate is not None:
            parts.append(
                f'<text x="0" y="{y + _CELL_H / 2 + 12}" font-size="9.5" fill="{_INK_MUTED}" '
                f'font-family="monospace">overall {row_rate * 100:.0f}% · n={row_scored}</text>'
            )

        boundary_frac = _bucket_fraction(pane.buckets, pane.fits[model]) if model in pane.fits else None

        for c_i, b in enumerate(pane.buckets):
            cell = pane.cells.get((model, c_i))
            x = _LABEL_W + c_i * _CELL_W
            rate = cell.rate if cell else None
            fill = _cell_fill(rate)
            if cell is None or cell.n_scored == 0:
                parts.append(
                    f'<rect class="hc-bg" x="{x + 2}" y="{y + 2}" width="{_CELL_W - 4}" '
                    f'height="{_CELL_H - 6}" fill="none" stroke="{_LINE}" '
                    'stroke-dasharray="3 3" rx="2"/>'
                    f'<text x="{x + _CELL_W / 2:.1f}" y="{y + _CELL_H / 2 + 3}" font-size="11" '
                    f'fill="{_INK_MUTED}" text-anchor="middle" font-family="monospace">—</text>'
                )
                continue

            tip = (
                f"{model}|{pane.dim} {b.label}|pass {rate * 100:.1f}%  "
                f"({cell.n_pass}/{cell.n_scored})"
            )
            if cell.n_error:
                tip += f"|{cell.n_error} infra error(s)"
            tip += "|click → these trials"
            href = _trials_href(pane.kind, pane.dim, c_i, model)
            parts.append(
                f'<a class="heat-cell" href="{html.escape(href)}" '
                f'data-tip="{html.escape(tip)}" tabindex="0">'
                f"<title>{html.escape(f'{model} - {pane.dim} {b.label}: {rate * 100:.1f}% pass (n={cell.n_scored})')}</title>"
                f'<rect class="hc-bg" x="{x + 2}" y="{y + 2}" width="{_CELL_W - 4}" '
                f'height="{_CELL_H - 6}" fill="{fill}" stroke="{_LINE}" rx="2"/>'
                f'<text x="{x + _CELL_W / 2:.1f}" y="{y + _CELL_H / 2 - 1}" font-size="13" '
                f'font-weight="650" fill="{_cell_text_color(rate)}" text-anchor="middle" '
                f'font-family="monospace">{rate * 100:.0f}%</text>'
                f'<text x="{x + _CELL_W / 2:.1f}" y="{y + _CELL_H / 2 + 12}" font-size="9" '
                f'fill="{_INK_MUTED}" text-anchor="middle" font-family="monospace">'
                f"n={cell.n_scored}</text>"
                "</a>"
            )

        # Fitted frontier: a single amber edge on this row, placed between
        # columns. The ONE thing on the row allowed to be colored.
        if boundary_frac is not None:
            bx = _LABEL_W + (boundary_frac + 0.5) * _CELL_W
            bx = max(_LABEL_W, min(_LABEL_W + n_cols * _CELL_W, bx))
            frontier_title = html.escape(
                f"{model}: fitted frontier at {pane.dim}={_fmt_num(pane.fits[model])}"
            )
            parts.append(
                f'<line x1="{bx:.1f}" y1="{y + 1}" x2="{bx:.1f}" y2="{y + _CELL_H - 5}" '
                f'stroke="{_CLAIM_BELIEF}" stroke-width="2">'
                f"<title>{frontier_title}</title></line>"
            )
        parts.append("</g>")

    parts.append("</svg>")
    return "".join(parts)


def _curve_svg(pane: _Pane) -> str:
    """Pass rate vs. difficulty bucket, one series per model. Same buckets
    as the heatmap. Points are focusable links into their trials."""
    n_cols = len(pane.buckets)
    focal = _focal_model(pane)
    plot_w = _CURVE_W - _CURVE_M_L - _CURVE_M_R
    plot_h = _CURVE_H - _CURVE_M_T - _CURVE_M_B

    def sx(col: float) -> float:
        if n_cols <= 1:
            return _CURVE_M_L + plot_w / 2
        return _CURVE_M_L + (col / (n_cols - 1)) * plot_w

    def sy(rate: float) -> float:
        return _CURVE_M_T + (1.0 - rate) * plot_h

    parts = [
        f'<svg viewBox="0 0 {_CURVE_W} {_CURVE_H}" width="{_CURVE_W}" height="{_CURVE_H}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Pass rate curve per model over {html.escape(pane.dim)}">'
    ]

    # Gridlines + y labels at 0 / 50 / 100%.
    for frac, label in ((1.0, "100%"), (0.5, "50%"), (0.0, "0%")):
        gy = sy(frac)
        parts.append(
            f'<line x1="{_CURVE_M_L}" y1="{gy:.1f}" x2="{_CURVE_M_L + plot_w}" y2="{gy:.1f}" '
            f'stroke="{_LINE}"/>'
            f'<text x="{_CURVE_M_L - 8}" y="{gy + 3.5:.1f}" font-size="10" fill="{_INK_MUTED}" '
            f'text-anchor="end" font-family="monospace">{label}</text>'
        )
    # The break threshold is a real line in this product's vocabulary, so
    # it is drawn, not implied.
    ty = sy(_BREAK_THRESHOLD)
    parts.append(
        f'<line x1="{_CURVE_M_L}" y1="{ty:.1f}" x2="{_CURVE_M_L + plot_w}" y2="{ty:.1f}" '
        f'stroke="{_TRAP}" stroke-opacity="0.45" stroke-dasharray="4 4"/>'
        f'<text x="{_CURVE_M_L + plot_w + 6}" y="{ty + 3.5:.1f}" font-size="9" fill="{_TRAP}" '
        f'fill-opacity="0.8" font-family="monospace">break</text>'
    )

    # X axis labels.
    for c, b in enumerate(pane.buckets):
        parts.append(
            f'<text x="{sx(c):.1f}" y="{_CURVE_H - _CURVE_M_B + 18}" font-size="10" '
            f'fill="{_INK_MUTED}" text-anchor="middle" font-family="monospace">'
            f"{html.escape(b.label)}</text>"
        )
    parts.append(
        f'<text x="{_CURVE_M_L + plot_w / 2:.1f}" y="{_CURVE_H - 8}" font-size="9.5" '
        f'fill="{_INK_MUTED}" text-anchor="middle" font-family="monospace" '
        f'letter-spacing="0.06em">{html.escape(_dim_label(pane.dim).upper())}</text>'
    )

    neutral_i = 0
    for model in pane.models:
        is_focal = model == focal
        if is_focal:
            color, dash, sw = _CLAIM_BELIEF, "", "2.4"
        else:
            color, sw = _INK_MUTED, "1.5"
            dash = _NEUTRAL_DASHES[neutral_i % len(_NEUTRAL_DASHES)]
            neutral_i += 1

        pts = [
            (c, pane.cells[(model, c)].rate)
            for c in range(n_cols)
            if (model, c) in pane.cells and pane.cells[(model, c)].n_scored
        ]
        if not pts:
            continue

        parts.append(f'<g class="series" data-series="{html.escape(model)}">')
        if len(pts) >= 2:
            poly = " ".join(f"{sx(c):.1f},{sy(rate):.1f}" for c, rate in pts)
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            parts.append(
                f'<polyline points="{poly}" fill="none" stroke="{color}" '
                f'stroke-width="{sw}" stroke-linejoin="round"{dash_attr}/>'
            )

        for c, rate in pts:
            cell = pane.cells[(model, c)]
            b = pane.buckets[c]
            tip = (
                f"{model}|{pane.dim} {b.label}|pass {rate * 100:.1f}%  "
                f"({cell.n_pass}/{cell.n_scored})|click → these trials"
            )
            # Radius encodes sample size: a 5-trial point should not look
            # as load-bearing as a 250-trial one.
            radius = 3.0 + min(3.0, cell.n_scored / 60.0)
            href = _trials_href(pane.kind, pane.dim, c, model)
            parts.append(
                f'<a class="series-dot" href="{html.escape(href)}" '
                f'data-tip="{html.escape(tip)}" tabindex="0">'
                f"<title>{html.escape(f'{model} - {pane.dim} {b.label}: {rate * 100:.1f}% pass (n={cell.n_scored})')}</title>"
                f'<circle cx="{sx(c):.1f}" cy="{sy(rate):.1f}" r="{radius:.1f}" fill="{color}"/>'
                "</a>"
            )

        # Direct end-of-line label -- beats a legend key the eye has to
        # round-trip through.
        last_c, last_rate = pts[-1]
        parts.append(
            f'<text x="{sx(last_c) + 9:.1f}" y="{sy(last_rate) + 3.5:.1f}" font-size="10" '
            f'fill="{color}" font-family="monospace" '
            f'font-weight="{"650" if is_focal else "400"}">{html.escape(display_name(model)[:17])}</text>'
        )

        frac = _bucket_fraction(pane.buckets, pane.fits[model]) if model in pane.fits else None
        if frac is not None and is_focal:
            bx = sx(max(0.0, min(float(n_cols - 1), frac)))
            parts.append(
                f'<line x1="{bx:.1f}" y1="{_CURVE_M_T}" x2="{bx:.1f}" '
                f'y2="{_CURVE_M_T + plot_h}" stroke="{color}" stroke-dasharray="4 3" '
                'stroke-opacity="0.7"/>'
                f'<text x="{bx:.1f}" y="{_CURVE_M_T - 6}" font-size="9" text-anchor="middle" '
                f'font-family="monospace" fill="{color}">frontier {_fmt_num(pane.fits[model])}</text>'
            )
        parts.append("</g>")

    parts.append("</svg>")
    return "".join(parts)


def _legend_html(pane: _Pane) -> str:
    """Per-model show/hide. Buttons, not links: they change how the SAME
    plotted data is displayed, which is a view state, not a new view.

    Renders nothing for a single-model pane. The toggle handler refuses to
    hide the last visible series (blanking the chart is never what the
    reader meant), so with one model the control could never do anything
    -- and a button that can't act is worse than no button, it's the
    unfinished-interface tell. A single-model chart is already "narrowed";
    there is no comparison to narrow.
    """
    if len(pane.models) < 2:
        return ""
    focal = _focal_model(pane)
    neutral_i = 0
    items = []
    for model in pane.models:
        if model == focal:
            swatch = _CLAIM_BELIEF
        else:
            swatch = _INK_MUTED
            neutral_i += 1
        items.append(
            f'<button type="button" class="legend-toggle" aria-pressed="true" '
            f'data-toggle-model="{html.escape(model)}">'
            f'<span class="swatch" style="background:{swatch}"></span>'
            f"{html.escape(display_name(model))}</button>"
        )
    return f'<div class="legend js-only">{"".join(items)}</div>'


def _selector_html(
    label: str, options: list[tuple[str, str, int]], active: str, param: str, other: tuple[str, str]
) -> str:
    """A segmented control of real links -- every selection has a URL and
    survives a reload, and the page needs no JS to change what is plotted.

    `options` is [(value, label, n_records)]; `other` is the (param,
    value) pair to carry through unchanged.
    """
    items = []
    for value, text, n in options:
        params = {other[0]: other[1], param: value}
        href = "/atlas?" + urlencode(params)
        cls = "seg-item active" if value == active else "seg-item"
        aria = ' aria-current="true"' if value == active else ""
        items.append(
            f'<a class="{cls}" href="{html.escape(href)}"{aria} '
            f'title="{n} record(s)">{html.escape(text)}</a>'
        )
    return (
        f'<div class="control-row-item">'
        f'<span class="control-label">{html.escape(label)}</span> '
        f'<span class="seg-control">{"".join(items)}</span></div>'
    )


def atlas_html(store: ResultsStore, kind: str | None = None, dim: str | None = None) -> str:
    """Cross-model atlas for one (query_kind, dimension) selection.

    `kind`/`dim` come from `/atlas?kind=...&dim=...`; unknown or missing
    values fall back to the densest varying selection, so the bare
    `/atlas` URL (and every existing caller) still works.
    """
    from .serve import _rate_bar, _shell, _trial_count  # local: avoid a circular import

    records = _campaign_records(store)

    page_head_actions = (
        '<div class="page-head-actions">'
        '<a class="cta-primary" href="/launch">Map another model &rarr;</a>'
        "</div>"
    )

    if not records:
        body = (
            '<div class="page-head"><div class="page-head-main">'
            "<h1>Cross-model atlas</h1>"
            '<p class="subtitle">Where each model breaks, as a capability map.</p>'
            f"</div>{page_head_actions}</div>"
            '<div class="empty-state"><p>No campaign data.</p>'
            '<p class="meta"><a href="/launch">Launch a campaign to populate this page &rarr;</a></p></div>'
        )
        return _shell("Cross-model atlas", "atlas", body)

    menu = _menu(records)
    panes = _all_panes(records, menu)
    def_kind, def_dim = _default_selection(panes)
    sel_kind = kind if kind in menu else def_kind
    sel_dim = dim if (sel_kind, dim) in panes else None
    if sel_dim is None:
        if sel_kind == def_kind:
            sel_dim = def_dim
        else:
            in_kind = {k: p for k, p in panes.items() if k[0] == sel_kind}
            sel_dim = _default_selection(in_kind)[1] if in_kind else ""

    pane = panes.get((sel_kind, sel_dim))

    kind_options = [
        (k, _kind_label(k), sum(dims.values())) for k, dims in sorted(menu.items())
    ]
    dim_options = [
        (d, _dim_label(d), n) for d, n in sorted(menu.get(sel_kind, {}).items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    controls = (
        '<div class="control-row">'
        + _selector_html("Query kind", kind_options, sel_kind, "kind", ("dim", sel_dim))
        + _selector_html("Difficulty dial", dim_options, sel_dim, "dim", ("kind", sel_kind))
        + "</div>"
    )

    # Headline stats, computed across the WHOLE store (they describe the
    # atlas, not the current selection).
    summary_rows: dict[tuple[str, str], dict] = {}
    for r in records:
        key = (_short_model_name(r.model_name), _query_kind_of(r))
        agg = summary_rows.setdefault(key, {"n": 0, "pass": 0, "scored": 0, "cost": 0.0})
        agg["n"] += 1
        agg["cost"] += r.cost_usd
        if r.verdict.name in ("PASS", "FAIL"):
            agg["scored"] += 1
            if r.verdict.name == "PASS":
                agg["pass"] += 1

    n_models_total = len({m for (m, _k) in summary_rows})
    n_pass_total = sum(a["pass"] for a in summary_rows.values())
    n_scored_total = sum(a["scored"] for a in summary_rows.values())
    n_total = sum(a["n"] for a in summary_rows.values())
    atlas_rate = (n_pass_total / n_scored_total * 100.0) if n_scored_total else 0.0

    # Boundaries across every (kind, dim, model) -- the count of real
    # frontiers this store has located, in-range fits only.
    n_boundaries_total = sum(len(p.fits) for p in panes.values())

    stat_grid = (
        '<div class="stat-grid">'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_models_total}</div></div>'
        '<div class="stat-label">Models compared</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{len(menu)}</div></div>'
        '<div class="stat-label">Query kinds</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{atlas_rate:.0f}%</div>'
        f'<div class="stat-delta">{n_pass_total:,}/{n_scored_total:,}</div></div>'
        '<div class="stat-label">Campaign pass rate</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_boundaries_total}</div></div>'
        '<div class="stat-label">Boundaries fitted</div></div>'
        "</div>"
    )

    if pane is None:
        pane_html = (
            '<div class="empty-state"><p>No trials recorded for '
            f"{html.escape(sel_kind)} / {html.escape(sel_dim)}.</p></div>"
        )
    else:
        focal = _focal_model(pane)
        if focal:
            headline = (
                f"{html.escape(display_name(focal))} breaks first: its pass rate crosses 50% at "
                f"{html.escape(_dim_label(pane.dim))} = {_fmt_num(pane.fits[focal])}."
            )
        else:
            headline = "No model crosses a fitted frontier on this dimension — nothing broke here."
        n_cells = len(pane.cells)
        pane_html = (
            '<div class="card instrument"><div class="card-head">'
            f'<span class="card-title">Pass rate by difficulty &middot; {html.escape(_kind_label(pane.kind))} / {html.escape(_dim_label(pane.dim))}</span>'
            f'<span class="card-meta">{len(pane.models)} models &times; {len(pane.buckets)} buckets '
            f"&middot; {pane.n_records:,} trials</span>"
            '</div><div class="card-body">'
            f'<p class="meta" style="margin:0 0 var(--sp-3);">{headline} '
            "Darker red = lower pass rate. Click any cell for the trials behind it.</p>"
            f'<div class="chart-wrap">{_heatmap_svg(pane)}</div>'
            f"{_legend_html(pane)}"
            f'<p class="heat-note">{n_cells} populated cells &middot; '
            f"{len(pane.fits)} fitted frontier(s) in range &middot; hover a cell for exact counts</p>"
            "</div></div>"
            '<div class="card instrument"><div class="card-head">'
            '<span class="card-title">Degradation curve &middot; model vs. model</span>'
            f'<span class="card-meta">same buckets</span>'
            '</div><div class="card-body">'
            '<p class="meta" style="margin:0 0 var(--sp-3);">The same cells as slopes, for reading two '
            "models against each other. Toggle a model off to narrow the comparison.</p>"
            f'<div class="chart-wrap">{_curve_svg(pane)}</div>'
            f"{_legend_html(pane)}"
            "</div></div>"
        )

    # Summary table: now a bar per rate, not a bare number.
    table_rows = []
    for (model, kind_name) in sorted(summary_rows):
        agg = summary_rows[(model, kind_name)]
        rate = (agg["pass"] / agg["scored"] * 100.0) if agg["scored"] else 0.0
        sub = "{0}/{1}".format(agg["pass"], agg["scored"])
        table_rows.append(
            "<tr>"
            f'<td title="{html.escape(model)}">{html.escape(display_name(model))}</td>'
            f'<td>{_chip_link(kind_name, sel_dim)}</td>'
            f'<td class="num">{_rate_bar(rate, sub=sub, decimals=1)}</td>'
            f'<td class="num">{agg["n"]}</td>'
            f'<td class="mono num">${agg["cost"]:.6f}</td>'
            "</tr>"
        )
    summary_table = (
        '<table><thead><tr><th>model</th><th>query_kind</th><th class="num">pass rate</th>'
        '<th class="num">n</th><th class="num">total cost</th></tr></thead><tbody>'
        + "".join(table_rows)
        + "</tbody></table>"
    )

    body = (
        '<div class="page-head"><div class="page-head-main">'
        "<h1>Cross-model atlas</h1>"
        '<p class="subtitle">Where each model breaks. Pick a query kind and a difficulty '
        "dimension; every model with campaign data is binned into the same difficulty "
        "buckets so the cells are directly comparable.</p>"
        f"</div>{page_head_actions}</div>"
        + stat_grid
        + controls
        + pane_html
        + '<div class="card"><div class="card-head">'
        + '<span class="card-title">Summary &middot; model x query kind</span>'
        + f'<span class="card-meta">{len(summary_rows)} pairs</span>'
        + "</div>"
        + summary_table
        + "</div>"
    )
    return _shell("Cross-model atlas", "atlas", body, trial_count=_trial_count(store))


def _chip_link(kind_name: str, current_dim: str) -> str:
    from .serve import _chip

    return _chip(kind_name, "chip-muted", href="/atlas?" + urlencode({"kind": kind_name, "dim": current_dim}))


# ---------------------------------------------------------------------------
# /trials -- the drill-down target every heatmap cell and curve point
# links to. Same bucketing code path as the chart, so a cell claiming
# "43%  n=124" and the list it opens can never disagree.
# ---------------------------------------------------------------------------

_TRIALS_LIMIT = 200


def trials_html(store: ResultsStore, params: dict[str, str]) -> str:
    from .serve import (
        _chip,
        _chip_set,
        _claim_strip,
        _parse_verifier_detail,
        _rate_bar,
        _shell,
        _short_answer,
        _trial_count,
    )

    kind = params.get("kind", "")
    dim = params.get("dim", "")
    model = params.get("model", "")
    try:
        bucket_i = int(params.get("b", ""))
    except (TypeError, ValueError):
        bucket_i = -1

    records = _campaign_records(store)
    pane = _build_pane(records, kind, dim) if (kind and dim) else None

    back = f'<a href="/atlas?{html.escape(urlencode({"kind": kind, "dim": dim}))}">Atlas</a>'
    breadcrumb = f"{back}<span class=\"sep\">/</span>trials"

    if pane is None or not (0 <= bucket_i < len(pane.buckets)):
        body = (
            '<div class="page-head"><div class="page-head-main"><h1>Trials</h1>'
            '<p class="subtitle">That slice of the atlas does not exist in this store.</p>'
            '</div></div><div class="empty-state"><p>No such selection.</p>'
            '<p class="meta"><a href="/atlas">Back to the atlas &rarr;</a></p></div>'
        )
        return _shell("Trials", "atlas", body, breadcrumb=breadcrumb, trial_count=_trial_count(store))

    bucket = pane.buckets[bucket_i]
    matched = []
    for r in records:
        if _query_kind_of(r) != kind or dim not in r.realized_dims:
            continue
        if model and _short_model_name(r.model_name) != model:
            continue
        if _bucket_index(pane.buckets, float(r.realized_dims[dim])) != bucket_i:
            continue
        matched.append(r)

    n_scored = sum(1 for r in matched if r.verdict.name in ("PASS", "FAIL"))
    n_pass = sum(1 for r in matched if r.verdict.name == "PASS")
    rate = (n_pass / n_scored * 100.0) if n_scored else 0.0

    rows = []
    for r in matched[:_TRIALS_LIMIT]:
        detail = _parse_verifier_detail(r.verifier_detail)
        answer = _short_answer(r.response_text, max_chars=56)
        if r.verdict.name == "PASS":
            evidence = f'answered <b>"{html.escape(answer)}"</b>' if answer else "matched ground truth"
        elif r.verdict.name == "FAIL":
            evidence = f'answered <b>"{html.escape(answer)}"</b>' if answer else None
        else:
            evidence = html.escape(detail.get("error", "") or r.finish_reason or "infra failure")
        # PASS rows carry no edge and no class attribute at all -- see
        # _CSS section 13 on why an edge on every row is no edge.
        sev = {"FAIL": "row-sev row-sev-fail", "ERROR": "row-sev row-sev-error"}.get(r.verdict.name, "")
        row_attr = f' class="{sev}"' if sev else ""
        traps = _chip_set(detail.get("traps", ""), "chip-trap")
        rows.append(
            f"<tr{row_attr}>"
            f'<td class="mono">{html.escape(_short_model_name(r.model_name))}</td>'
            f'<td class="claim-cell">{_claim_strip(r.verdict.name, evidence)}</td>'
            f'<td class="mono num">{_fmt_num(float(r.realized_dims[dim]))}</td>'
            f"<td>{traps or _chip(detail.get('sim', ''), 'chip-muted')}</td>"
            f'<td class="mono"><a href="/run/{html.escape(r.run_id)}">{html.escape(r.run_id)}</a></td>'
            f'<td><a href="/record/{html.escape(r.record_id)}">transcript &rarr;</a></td>'
            "</tr>"
        )

    truncated = (
        f'<p class="meta">Showing the first {_TRIALS_LIMIT} of {len(matched):,} matching trials.</p>'
        if len(matched) > _TRIALS_LIMIT
        else ""
    )
    who = _chip(model or "all models", "chip-muted")
    body = (
        '<div class="page-head"><div class="page-head-main">'
        f"<h1>Trials &middot; {html.escape(dim)} {html.escape(bucket.label)}</h1>"
        f'<p class="subtitle">Every {html.escape(kind)} trial behind one cell of the atlas: '
        f"{html.escape(dim)} in {html.escape(bucket.label)}.</p>"
        f'<p class="meta">{who} {_chip(kind, "chip-muted")} '
        f'{_chip(f"{dim} {bucket.label}", "chip-muted")}</p>'
        '</div><div class="page-head-actions">'
        f'<a href="/atlas?{html.escape(urlencode({"kind": kind, "dim": dim}))}">&larr; back to the map</a>'
        "</div></div>"
        '<div class="stat-grid">'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{len(matched):,}</div></div>'
        '<div class="stat-label">Trials in this cell</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{rate:.0f}%</div>'
        f'<div class="stat-delta">{n_pass:,}/{n_scored:,} scored</div></div>'
        '<div class="stat-label">Pass rate</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">'
        f'{len({_short_model_name(r.model_name) for r in matched})}</div></div>'
        '<div class="stat-label">Models</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">'
        f'${sum(r.cost_usd for r in matched):.4f}</div></div>'
        '<div class="stat-label">Cell cost</div></div>'
        "</div>"
        '<div class="card"><div class="card-head">'
        '<span class="card-title">Trials</span>'
        f'<span class="card-meta">{_rate_bar(rate, sub=f"{n_pass}/{n_scored}")}</span>'
        "</div>"
        '<table><thead><tr><th>model</th><th>verdict</th>'
        f'<th class="num">{html.escape(dim)}</th><th>seeded traps</th><th>run</th><th></th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        + truncated
    )
    return _shell(
        f"Trials {dim} {bucket.label}", "atlas", body, breadcrumb=breadcrumb, trial_count=_trial_count(store)
    )


__all__ = ["atlas_html", "trials_html"]
