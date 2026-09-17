"""Phase 8: cross-model atlas -- pure-function HTML builder over a
`ResultsStore`, same pattern as `cartographer.viewer.serve`'s other pages.

Builds, per query_kind x dimension, an inline SVG line chart of pass rate
vs. dimension value for each model, plus a summary table. No JS libraries;
SVG is built directly as strings. Every db-sourced string is html.escape'd
before being placed in the page (SVG text nodes too).

Boundary detection: campaign runs only persist `ExperimentRecord`s, not the
scheduler's `CampaignState` (so no access to stored `boundaries`/`notes`).
Instead this module re-fits a logistic frontier per (model, dim) from the
binned records directly via `cartographer.schedule.estimate.fit_logistic`,
and draws a vertical dashed line at the crossing when a fit exists.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from ..schedule.estimate import fit_logistic
from ..store.store import ResultsStore

# Only look at records from campaign runs (adaptive scheduler runs), which
# is what run_id's "camp-" prefix denotes across this codebase (see
# run_state_campaign/run_belief_campaign's run_id conventions).
_CAMPAIGN_RUN_PREFIX = "camp-"

_CHART_W = 520
_CHART_H = 300
_MARGIN = 48

_LINE_COLORS = [
    "#2DD4BF", "#F87171", "#34D399", "#FBBF24", "#A78BFA", "#60A5FA", "#F472B6",
]
_CHART_BG = "#171b21"
_CHART_BORDER = "#2A2F36"
_CHART_AXIS = "#9aa4b2"
_CHART_TEXT = "#E8EAED"


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


@dataclass(frozen=True)
class _Point:
    x: float
    passed: bool


def _one_dim_varied_points(
    records: list, dim: str, baseline: dict[str, float]
) -> list[_Point]:
    """Records whose realized_dims differ from `baseline` in at most `dim`.

    Mirrors the `resume_from_records` idiom: a record counts toward this
    dim's curve if every OTHER known dim sits at (or very near) its
    baseline value. Baseline is approximated per-dim as the minimum
    observed value across all records passed in (the caller computes this
    once per model x dim group before calling).
    """
    out: list[_Point] = []
    for r in records:
        rd = r.realized_dims
        if dim not in rd:
            continue
        ok = True
        for other_dim, base_val in baseline.items():
            if other_dim == dim:
                continue
            val = rd.get(other_dim)
            if val is None:
                continue
            if abs(val - base_val) > 1e-9:
                ok = False
                break
        if ok:
            out.append(_Point(x=float(rd[dim]), passed=(r.verdict.name == "PASS")))
    return out


def _bin_points(points: list[_Point]) -> list[tuple[float, float, int]]:
    """Group points by exact x value -> (x, pass_rate, n)."""
    by_x: dict[float, list[bool]] = {}
    for p in points:
        by_x.setdefault(p.x, []).append(p.passed)
    out = []
    for x in sorted(by_x):
        vals = by_x[x]
        out.append((x, sum(vals) / len(vals), len(vals)))
    return out


def _svg_chart(
    title: str,
    series: dict[str, list[tuple[float, float, int]]],
    boundaries: dict[str, float],
) -> str:
    """Build one inline SVG line chart: pass rate (y, 0..1) vs dim value (x).

    `series`: model_short_name -> [(x, pass_rate, n), ...] sorted by x.
    `boundaries`: model_short_name -> logistic-fit midpoint x (if any),
    drawn as a vertical dashed line labeled with the value.
    """
    all_xs = [x for pts in series.values() for x, _, _ in pts]
    if not all_xs:
        return f"<p>{html.escape(title)}: no data.</p>"

    x_lo, x_hi = min(all_xs), max(all_xs)
    x_span = (x_hi - x_lo) or 1.0

    def sx(x: float) -> float:
        return _MARGIN + (x - x_lo) / x_span * (_CHART_W - 2 * _MARGIN)

    def sy(rate: float) -> float:
        return _CHART_H - _MARGIN - rate * (_CHART_H - 2 * _MARGIN)

    parts = [
        f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" xmlns="http://www.w3.org/2000/svg" '
        f'width="{_CHART_W}" height="{_CHART_H}">',
        f'<rect x="0" y="0" width="{_CHART_W}" height="{_CHART_H}" fill="{_CHART_BG}" '
        f'stroke="{_CHART_BORDER}"/>',
        # axes
        f'<line x1="{_MARGIN}" y1="{_CHART_H - _MARGIN}" x2="{_CHART_W - _MARGIN}" '
        f'y2="{_CHART_H - _MARGIN}" stroke="{_CHART_AXIS}"/>',
        f'<line x1="{_MARGIN}" y1="{_MARGIN}" x2="{_MARGIN}" '
        f'y2="{_CHART_H - _MARGIN}" stroke="{_CHART_AXIS}"/>',
        f'<text x="{_MARGIN}" y="20" font-size="14" fill="{_CHART_TEXT}" font-family="sans-serif">'
        f'{html.escape(title)}</text>',
        f'<text x="{_MARGIN - 10}" y="{_MARGIN - 5}" font-size="10" fill="{_CHART_AXIS}" '
        'text-anchor="start" font-family="sans-serif">1.0</text>',
        f'<text x="{_MARGIN - 10}" y="{_CHART_H - _MARGIN + 5}" font-size="10" fill="{_CHART_AXIS}" '
        'text-anchor="start" font-family="sans-serif">0.0</text>',
    ]

    legend_y = 34
    for i, (model, pts) in enumerate(sorted(series.items())):
        color = _LINE_COLORS[i % len(_LINE_COLORS)]
        poly_points = " ".join(f"{sx(x):.1f},{sy(rate):.1f}" for x, rate, _ in pts)
        parts.append(
            f'<polyline points="{poly_points}" fill="none" stroke="{color}" '
            'stroke-width="2"/>'
        )
        for x, rate, n in pts:
            parts.append(
                f'<circle cx="{sx(x):.1f}" cy="{sy(rate):.1f}" r="3" fill="{color}">'
                f'<title>{html.escape(model)} x={x} rate={rate:.2f} n={n}</title></circle>'
            )
        legend_x = _CHART_W - 150
        parts.append(
            f'<rect x="{legend_x}" y="{legend_y - 8}" width="10" height="10" fill="{color}"/>'
            f'<text x="{legend_x + 14}" y="{legend_y}" font-size="11" fill="{_CHART_TEXT}" '
            f'font-family="sans-serif">{html.escape(model)}</text>'
        )
        legend_y += 16

        mid = boundaries.get(model)
        if mid is not None and x_lo <= mid <= x_hi:
            bx = sx(mid)
            parts.append(
                f'<line x1="{bx:.1f}" y1="{_MARGIN}" x2="{bx:.1f}" '
                f'y2="{_CHART_H - _MARGIN}" stroke="{color}" stroke-dasharray="4,3"/>'
            )
            parts.append(
                f'<text x="{bx:.1f}" y="{_MARGIN - 8}" font-size="9" text-anchor="middle" '
                f'font-family="sans-serif" fill="{color}">{mid:.2f}</text>'
            )

    parts.append("</svg>")
    return "".join(parts)


def atlas_html(store: ResultsStore) -> str:
    """Cross-model comparison across every `camp-*` run in `store`.

    Pure function: reads the whole `experiments` table, groups by
    query_kind and dim, bins pass rate vs. dim value per model, fits a
    logistic frontier per (model, dim) via `fit_logistic`, and renders one
    SVG chart per (query_kind, dim) plus a model x query_kind summary
    table. Returns a graceful "no campaign data" page if there are no
    `camp-*` records at all.
    """
    from .serve import _shell  # local import: avoid a circular import at module load

    cur = store._conn.execute("SELECT * FROM experiments")
    from ..store.store import _row_to_record

    all_records = [_row_to_record(row) for row in cur.fetchall()]
    records = [r for r in all_records if r.run_id.startswith(_CAMPAIGN_RUN_PREFIX)]

    if not records:
        body = "<h1>Cross-model atlas</h1><p>No campaign data.</p>"
        return _shell("Cross-model atlas", "atlas", body)

    # Group by query_kind -> model -> records.
    by_kind_model: dict[str, dict[str, list]] = {}
    for r in records:
        kind = _query_kind_of(r)
        model = _short_model_name(r.model_name)
        by_kind_model.setdefault(kind, {}).setdefault(model, []).append(r)

    # Discover every dim seen anywhere so charts cover every dim any model
    # was probed on.
    all_dims: dict[str, set[str]] = {}
    for kind, by_model in by_kind_model.items():
        dims: set[str] = set()
        for recs in by_model.values():
            for r in recs:
                dims.update(r.realized_dims.keys())
        all_dims[kind] = dims

    chart_sections = []
    for kind in sorted(by_kind_model):
        by_model = by_kind_model[kind]
        chart_sections.append(f"<h2>Query kind: {html.escape(kind)}</h2>")
        for dim in sorted(all_dims[kind]):
            series: dict[str, list[tuple[float, float, int]]] = {}
            boundaries: dict[str, float] = {}
            for model, recs in by_model.items():
                # Approximate baseline as the minimum observed value per
                # dim for this model's records.
                baseline: dict[str, float] = {}
                for r in recs:
                    for d, v in r.realized_dims.items():
                        if d not in baseline or v < baseline[d]:
                            baseline[d] = v
                points = _one_dim_varied_points(recs, dim, baseline)
                if not points:
                    continue
                binned = _bin_points(points)
                if len(binned) < 1:
                    continue
                series[model] = binned

                fit = fit_logistic([(p.x, p.passed) for p in points])
                if fit is not None:
                    boundaries[model] = fit.midpoint

            if not series:
                continue
            svg = _svg_chart(f"{kind}: {dim}", series, boundaries)
            chart_sections.append(f'<div style="overflow-x:auto;">{svg}</div>')

    # Summary table: model x query_kind -> pass rate, n, total cost.
    summary_rows: dict[tuple[str, str], dict] = {}
    for r in records:
        kind = _query_kind_of(r)
        model = _short_model_name(r.model_name)
        key = (model, kind)
        agg = summary_rows.setdefault(key, {"n": 0, "pass": 0, "cost": 0.0})
        agg["n"] += 1
        agg["cost"] += r.cost_usd
        if r.verdict.name == "PASS":
            agg["pass"] += 1

    table_rows = []
    for (model, kind) in sorted(summary_rows):
        agg = summary_rows[(model, kind)]
        rate = (agg["pass"] / agg["n"] * 100.0) if agg["n"] else 0.0
        table_rows.append(
            "<tr>"
            f"<td>{html.escape(model)}</td>"
            f"<td>{html.escape(kind)}</td>"
            f"<td>{rate:.1f}%</td>"
            f'<td>{agg["n"]}</td>'
            f'<td>${agg["cost"]:.6f}</td>'
            "</tr>"
        )
    summary_table = (
        "<table><thead><tr><th>model</th><th>query_kind</th><th>pass rate</th>"
        "<th>n</th><th>total cost</th></tr></thead><tbody>"
        + "".join(table_rows)
        + "</tbody></table>"
    )

    body = (
        "<h1>Cross-model atlas</h1>"
        + "".join(chart_sections)
        + "<h2>Summary</h2>"
        + summary_table
    )
    return _shell("Cross-model atlas", "atlas", body)
