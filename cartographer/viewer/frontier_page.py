"""Capability map (`/`) and discovery page (`/frontier`).

The first screen a visitor sees: per model, where it breaks and why, in the
capability's own units. Every number, label, boundary, cause and verdict on
these pages comes from `cartographer.report.frontier.frontier_summaries`,
computed from the store at request time. Nothing here is curated: the only
words this module writes itself are UI labels and explanations of the
instrument (what a status or grade means), and those quote the thresholds
from `report.frontier` rather than restating them.

Two kinds of object are kept visually apart on purpose:

* a FINDING (BREAKS / DEGRADES / HOLDS / NOT TESTED, grade A/B/C) is a
  research conclusion over many trials;
* a TRIAL RESULT (PASS / FAIL) is one model answer.

The evidence ribbon only ticks what the row actually carries. Fresh-seed
confirmation and replication are not in `FrontierRow`, so they always read
"not run yet" and never get a check mark.
"""

from __future__ import annotations

import html
from collections import Counter
from urllib.parse import quote

from ..report.frontier import (
    BREAK_RATE,
    DEGRADE_RATE,
    HOLD_UPPER_BOUND,
    MIN_N,
    SPECS,
    STATUS_ORDER,
    SUSTAINED_BREAK_RATE,
    FrontierRow,
    ModelFrontier,
    frontier_summaries,
)
from .findings import _HEAT_STEPS, heat_class

_SPEC_BY_KEY = {s.key: s for s in SPECS}
_PROVINCES = list(dict.fromkeys(s.province for s in SPECS))

_GRADE_WORDS = {"A": "strong", "B": "moderate", "C": "thin"}
_SMALL_SAMPLE = 15

# Status -> (heat-ramp class, tiny curve glyph). The glyph is the shape of
# the pass-rate curve the status describes, so the chip reads without
# colour: flat (holds), sloping (degrades), a cliff (breaks), dashed (no
# evidence).
_STATUS_CLASS = {"BREAKS": "heat-1", "DEGRADES": "heat-2", "HOLDS": "heat-4", "NOT TESTED": "heat-none"}
_STATUS_GLYPH = {
    "BREAKS": '<path d="M1.5 4.5H8V12.5H14.5"/>',
    "DEGRADES": '<path d="M1.5 4.5H6L14.5 12"/>',
    "HOLDS": '<path d="M1.5 5H14.5"/>',
    "NOT TESTED": '<path d="M1.5 8.5H14.5" stroke-dasharray="2 2.2"/>',
}
_CAUSE_TITLE = {
    "BREAKS": "Why it breaks",
    "DEGRADES": "Why it slips",
    "HOLDS": "What the few failures have in common",
}
_KIND_WORDS = {
    "state": "state-tracking",
    "order": "event-order",
    "belief": "belief-tracking",
    "adversarial_belief": "misleading-story",
    "tool_plan": "tool-planning",
    "interactive": "interactive-execution",
}

_CSS = """
.fm-lede { max-width: 70ch; }
.fm-verdicts { list-style: none; margin: 0; padding: 0; }
.fm-verdict {
  display: grid; grid-template-columns: minmax(0, 15rem) minmax(0, 1fr);
  gap: var(--sp-1) var(--sp-4); padding: var(--sp-3) var(--sp-5);
  border-top: 1px solid var(--line);
}
.fm-verdict:first-child { border-top: 0; }
.fm-verdict a { color: var(--ink); font-weight: 600; overflow-wrap: anywhere; }
.fm-verdict-text { color: var(--ink-secondary); }
.fm-verdict.is-selected { background: rgba(255,255,255,0.04); box-shadow: inset 2px 0 0 var(--ink); }
.fm-verdict.is-selected .fm-verdict-text { color: var(--ink); }

.fm-controls { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--sp-3); margin: var(--sp-5) 0 var(--sp-2); }
.fm-controls .model-tabs { margin: 0; border: 0; padding: 0; }

.fm-key { display: flex; flex-wrap: wrap; gap: var(--sp-2) var(--sp-5); margin: var(--sp-3) 0 var(--sp-5); font-size: 11.5px; color: var(--ink-muted); }
.fm-key-item { display: inline-flex; align-items: center; gap: 0.5em; }
.fm-key .cap-swatch { width: 14px; height: 10px; }

.fm-status {
  display: inline-flex; align-items: center; gap: 0.4em;
  font-family: var(--mono); font-size: 10.5px; font-weight: 600; letter-spacing: 0.05em;
  padding: 0.25em 0.6em; border-radius: 4px; white-space: nowrap; line-height: 1.4;
}
.fm-status svg { flex: none; display: block; }

a.fm-row {
  display: grid; grid-template-columns: 7.5rem minmax(0, 1fr) auto;
  gap: var(--sp-2) var(--sp-4); padding: var(--sp-4) var(--sp-5);
  border-top: 1px solid var(--line); color: var(--ink);
}
a.fm-row:first-of-type { border-top: 0; }
a.fm-row:hover { background: rgba(255,255,255,0.03); text-decoration: none; }
a.fm-row:focus-visible, a.fm-cmp-row:focus-visible { outline: 2px solid var(--ink); outline-offset: -2px; }
.fm-row-label { font-weight: 600; }
.fm-row-headline, .fm-row-where, .fm-row-label, .fm-cmp-text, .fm-ev-k, .fm-ev-v { display: block; }
.fm-row-headline { color: var(--ink-secondary); font-size: var(--fs-small); margin-top: 2px; }
.fm-row-where { font-family: var(--mono); font-size: 11.5px; color: var(--ink-muted); margin-top: var(--sp-2); }
.fm-row-where b { color: var(--ink); font-weight: 500; }
.fm-row-note { font-size: 11.5px; color: var(--ink-muted); margin-top: var(--sp-1); }
.fm-grade { font-family: var(--mono); font-size: 11px; color: var(--ink-muted); text-align: right; white-space: nowrap; }
.fm-grade b { color: var(--ink); font-weight: 600; font-size: 13px; }
.fm-row.is-untested .fm-row-label, .fm-row.is-untested .fm-row-headline,
.fm-cmp-row.is-untested .fm-cmp-text { color: var(--ink-muted); }
.fm-row.is-untested, .fm-cmp-row.is-untested { opacity: 0.72; }

.fm-strip-wrap { margin-top: var(--sp-3); max-width: 32rem; }
.fm-strip { display: flex; gap: 2px; }
.fm-cell { flex: 1 1 0; min-width: 4px; height: 14px; border-radius: 2px; position: relative; border: 1px solid transparent; }
.fm-cell.fm-thin { opacity: 0.45; }
.fm-cell.fm-gap { border: 1px dashed var(--line-strong); background: transparent; }
.fm-cell.fm-boundary { border-color: var(--ink); z-index: 1; }
.fm-cell.fm-boundary::after {
  content: "\\25B2"; position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
  margin-top: 2px; font-size: 8px; line-height: 1; color: var(--ink);
}
.fm-strip-cap { display: flex; justify-content: space-between; gap: var(--sp-3); margin-top: 13px; font-family: var(--mono); font-size: 10px; color: var(--ink-muted); }

.fm-cmp { padding: var(--sp-4) var(--sp-5); border-top: 1px solid var(--line); }
.fm-cmp:first-of-type { border-top: 0; }
.fm-cmp-label { font-weight: 600; margin-bottom: var(--sp-2); }
a.fm-cmp-row {
  display: grid; grid-template-columns: 12rem 7.5rem minmax(0, 1fr);
  gap: var(--sp-1) var(--sp-3); align-items: start; padding: var(--sp-2) var(--sp-2);
  margin: 0 calc(-1 * var(--sp-2)); border-radius: 8px; color: var(--ink);
}
a.fm-cmp-row:hover { background: rgba(255,255,255,0.03); text-decoration: none; }
.fm-cmp-model { font-size: var(--fs-small); color: var(--ink-secondary); overflow-wrap: anywhere; padding-top: 1px; }
.fm-cmp-text { font-size: var(--fs-small); color: var(--ink-secondary); }
.fm-cmp-text .fm-grade { display: inline; margin-left: var(--sp-2); }
.fm-cmp .fm-strip-wrap { max-width: none; margin-top: var(--sp-2); }
.fm-cmp-axis { display: grid; grid-template-columns: 12rem 7.5rem minmax(0, 1fr); gap: 0 var(--sp-3); }
.fm-cmp-axis .fm-strip-cap { grid-column: 3; margin-top: var(--sp-1); }

.fm-finding { margin: 0 0 var(--sp-5); }
.fm-kind { font-family: var(--mono); font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-muted); }
.fm-kind span { text-transform: none; letter-spacing: 0; }
.fm-finding-line {
  display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2) var(--sp-3);
  margin-top: var(--sp-2); font-family: var(--display); font-size: 1.3rem; font-weight: 600; letter-spacing: -0.01em;
}
.fm-finding-line .fm-status { font-size: 12.5px; padding: 0.3em 0.7em; }
.fm-finding-line .fm-sub { font-family: var(--sans); font-size: var(--fs-small); font-weight: 500; color: var(--ink-muted); letter-spacing: 0; }
.fm-finding-headline { margin: var(--sp-3) 0 0; font-size: 1rem; color: var(--ink); }
.fm-finding-where { margin: var(--sp-1) 0 0; font-family: var(--mono); font-size: 11.5px; color: var(--ink-muted); }
.fm-finding-where b { color: var(--ink); font-weight: 500; }

.fm-chart-scroll { overflow-x: auto; }
.fm-chart svg { display: block; width: 100%; height: auto; max-width: 780px; }
.fm-chart-scroll svg { min-width: 540px; }
.fm-chart .grid { stroke: var(--line); }
.fm-chart .ax { stroke: var(--line-strong); }
.fm-chart .thr { stroke: var(--ink-muted); stroke-dasharray: 3 4; opacity: 0.7; }
.fm-chart .t { fill: var(--ink-muted); font-family: var(--mono); font-size: 11px; }
.fm-chart .tx { fill: var(--ink-secondary); font-family: var(--mono); font-size: 11px; }
.fm-chart .line { fill: none; stroke: var(--ink-secondary); stroke-width: 1.5; }
.fm-chart .pt { fill: var(--ink); }
.fm-chart .pt.low { fill: var(--tag); }
.fm-chart .pt.thin { fill: var(--surface-instrument-0); stroke: var(--ink-muted); stroke-width: 1.5; }
.fm-chart .n { fill: var(--ink-secondary); font-family: var(--mono); font-size: 10.5px; }
.fm-chart .bnd { stroke: var(--tag); stroke-width: 1.5; stroke-dasharray: 5 4; }
.fm-chart .bnd-t { fill: var(--tag); font-family: var(--sans); font-size: 12.5px; font-weight: 600; }
.fm-chart .ci { fill: rgba(255,255,255,0.10); stroke: var(--ink-muted); }
.fm-chart .track { fill: rgba(255,255,255,0.04); }
.fm-big { font-family: var(--display); font-size: 2rem; font-weight: 600; letter-spacing: -0.02em; line-height: 1.1; }
.fm-chart-note { font-size: 11.5px; color: var(--ink-muted); margin: var(--sp-3) 0 0; }
table.fm-levels tr.is-boundary td { color: var(--ink); font-weight: 600; }
table.fm-levels tr.is-boundary td:first-child { box-shadow: inset 3px 0 0 0 var(--tag); }
table.fm-levels td.dim { color: var(--ink-muted); }

.fm-cause { font-size: 1rem; color: var(--ink); margin: 0; }
.fm-cause-share { font-family: var(--mono); font-size: 11.5px; color: var(--ink-muted); margin: var(--sp-2) 0 0; }

.fm-trial { background: var(--well); border: 1px dashed var(--line-strong); border-radius: 6px; padding: var(--sp-4); }
.fm-trial .claim { padding: var(--sp-2) 0 0; }
.fm-trial .claim .evidence { font-family: var(--mono); font-size: 11.5px; overflow-wrap: anywhere; }
.fm-trial pre.block { margin: var(--sp-3) 0 0; max-height: 14rem; overflow: auto; }
.fm-links { display: flex; flex-wrap: wrap; gap: var(--sp-2) var(--sp-5); margin-top: var(--sp-3); font-size: var(--fs-small); }

.fm-ribbon { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: var(--sp-2); }
.fm-ev { display: flex; gap: var(--sp-2); padding: var(--sp-3); border: 1px solid var(--line); border-radius: 10px; background: var(--module); min-width: 0; }
.fm-ev-mark { font-family: var(--mono); font-weight: 700; width: 1.1em; flex: none; text-align: center; line-height: 1.5; }
.fm-ev-mark.ok { color: var(--verdict-pass); }
.fm-ev-mark.warn { color: var(--tag); }
.fm-ev-mark.none { color: var(--ink-muted); }
.fm-ev-k { font-size: 11px; color: var(--ink-muted); }
.fm-ev-v { font-size: var(--fs-small); color: var(--ink); overflow-wrap: anywhere; }
.fm-ev-v .meta { display: block; }
.fm-ev.is-pending { border-style: dashed; background: transparent; }
.fm-ev.is-pending .fm-ev-v { color: var(--ink-muted); }

.fm-raw { margin: 0; padding-left: 1.05rem; font-size: var(--fs-small); }
.fm-raw li + li { margin-top: var(--sp-2); }
.fm-raw .mono { font-size: 11.5px; }

@media (max-width: 760px) {
  main.shell-main { padding: 16px 16px 48px; }
}
@media (max-width: 640px) {
  .fm-verdict { grid-template-columns: minmax(0, 1fr); padding: var(--sp-3) var(--sp-4); }
  a.fm-row { grid-template-columns: minmax(0, 1fr) auto; padding: var(--sp-4); }
  .fm-row-status { grid-column: 1 / -1; }
  .fm-cmp { padding: var(--sp-4); }
  a.fm-cmp-row { grid-template-columns: minmax(0, 1fr) auto; }
  a.fm-cmp-row .fm-cmp-main { grid-column: 1 / -1; }
  .fm-cmp-axis { grid-template-columns: minmax(0, 1fr); }
  .fm-cmp-axis .fm-strip-cap { grid-column: 1; }
}
"""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _e(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _q(value: str) -> str:
    return quote(value, safe="")


def _map_href(model: str | None = None, compare: bool = False) -> str:
    if compare:
        return "/?compare=1"
    return f"/?model={_q(model)}" if model else "/"


def _frontier_href(model: str, key: str) -> str:
    return f"/frontier?model={_q(model)}&cap={_q(key)}"


def _resolve(summaries: list[ModelFrontier], wanted: str | None) -> ModelFrontier | None:
    """Match a `?model=` value against the full model id or its short name."""
    if not wanted:
        return None
    w = wanted.strip().lower()
    for mf in summaries:
        if mf.model.lower() == w or mf.name.lower() == w:
            return mf
    return None


def _default_model(summaries: list[ModelFrontier]) -> ModelFrontier | None:
    """The model with the most capabilities tested (first one on a tie)."""
    if not summaries:
        return None
    return max(summaries, key=lambda mf: sum(r.status != "NOT TESTED" for r in mf.rows))


def _verdict_text(mf: ModelFrontier) -> str:
    """The verdict sentence without its leading "name: " (the name is shown
    beside it already)."""
    prefix = f"{mf.name}: "
    text = mf.verdict[len(prefix):] if mf.verdict.startswith(prefix) else mf.verdict
    return text[:1].upper() + text[1:]


def _status_chip(status: str) -> str:
    cls = _STATUS_CLASS.get(status, "heat-none")
    glyph = _STATUS_GLYPH.get(status, _STATUS_GLYPH["NOT TESTED"])
    return (
        f'<span class="fm-status {cls}" data-status="{_e(status)}">'
        '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
        f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{glyph}</svg>'
        f"{_e(status)}</span>"
    )


def _grade(row: FrontierRow) -> str:
    if not row.confidence:
        return ""
    words = _GRADE_WORDS.get(row.confidence, "")
    return (
        f'<span class="fm-grade" title="Confidence grade: A strong, B moderate, C thin">'
        f"grade <b>{_e(row.confidence)}</b> {_e(words)}</span>"
    )


def _where(row: FrontierRow) -> str:
    """Boundary (or how far it was tested) in the capability's own units."""
    if row.boundary:
        out = f"Boundary <b>{_e(row.boundary)}</b>"
        if row.held_through:
            out += f" &middot; held through {_e(row.held_through)}"
        return out
    if row.held_through:
        if row.held_through.startswith("tested up to "):
            return "Tested up to " + _e(row.held_through[len("tested up to "):])
        return f"Held through <b>{_e(row.held_through)}</b>"
    return ""


def _level_rate(c: dict) -> float | None:
    return c["n_pass"] / c["n_scored"] if c["n_scored"] else None


def _level_tip(c: dict) -> str:
    if not c["n_scored"]:
        tip = f'{c["label"]}: no scored trials'
    else:
        tip = f'{c["label"]}: {c["n_pass"]}/{c["n_scored"]} pass ({c["n_pass"] / c["n_scored"]:.0%})'
        if c["n_scored"] < MIN_N:
            tip += f", fewer than {MIN_N} scored, not used for the call"
    if c["n_error"]:
        tip += f', {c["n_error"]} errored'
    return tip


def _strip(curve: list[dict], boundary: str | None, columns: list[float] | None = None,
           labels: dict | None = None) -> str:
    """One cell per curve level, shaded by pass rate, boundary outlined and
    marked with a caret. `columns` puts the cells on a shared axis (compare
    view): a level this model was never tested at is an empty dashed cell."""
    by_val = {c["value"]: c for c in curve}
    cols = columns if columns is not None else [c["value"] for c in curve]
    cells = []
    for v in cols:
        c = by_val.get(v)
        if c is None:
            name = (labels or {}).get(v, "")
            cells.append(f'<span class="fm-cell fm-gap" title="{_e(name)}: not tested at this level"></span>')
            continue
        cls = heat_class(_level_rate(c))
        if 0 < c["n_scored"] < MIN_N:
            cls += " fm-thin"
        if boundary is not None and c["label"] == boundary:
            cls += " fm-boundary"
        cells.append(f'<span class="fm-cell {cls}" title="{_e(_level_tip(c))}"></span>')
    summary = "; ".join(_level_tip(by_val[v]) for v in cols if v in by_val)
    return f'<span class="fm-strip" role="img" aria-label="{_e(summary)}">{"".join(cells)}</span>'


def _where_span(where: str) -> str:
    return f'<span class="fm-row-where">{where}</span>' if where else ""


def _strip_caption(first: str, last: str) -> str:
    return f'<span class="fm-strip-cap"><span>{_e(first)}</span><span>{_e(last)}</span></span>'


def _css() -> str:
    # The shell has no viewport meta; browsers honour one in the body, and
    # without it a phone lays the page out at desktop width.
    return f'<meta name="viewport" content="width=device-width, initial-scale=1"><style>{_CSS}</style>'


def _empty(title: str, message: str, extra: str = "") -> str:
    return (
        f'<div class="page-head"><div class="page-head-main"><h1>{_e(title)}</h1></div></div>'
        f'<div class="empty-state"><p>{message}</p>{extra}</div>'
    )


# ---------------------------------------------------------------------------
# A. Capability map
# ---------------------------------------------------------------------------


def _key_html() -> str:
    """Legend: what each status and each shade means, quoting the thresholds
    `report.frontier` actually uses."""
    meanings = {
        "BREAKS": f"a level fell to {BREAK_RATE:.0%} pass or below and stayed low "
                  f"(at most {SUSTAINED_BREAK_RATE:.0%} from there up)",
        "DEGRADES": f"a level fell to {DEGRADE_RATE:.0%} or below; for single-rate rows, "
                    f"the 95% interval sits below {HOLD_UPPER_BOUND:.0%}",
        "HOLDS": "neither, at every level tested",
        "NOT TESTED": "no evidence yet",
    }
    status = "".join(
        f'<span class="fm-key-item">{_status_chip(s)}<span>{_e(meanings[s])}</span></span>'
        for s in sorted(STATUS_ORDER, key=STATUS_ORDER.get)
    )
    shades = "".join(
        f'<span class="fm-key-item"><span class="cap-swatch {cls}"></span>{_e(label)}</span>'
        for _upper, cls, label in _HEAT_STEPS
    )
    shades += (
        '<span class="fm-key-item"><span class="cap-swatch heat-none"></span>no scored trials</span>'
        f'<span class="fm-key-item">faded cell: fewer than {MIN_N} scored trials</span>'
        '<span class="fm-key-item">&#9650; boundary level</span>'
        '<span class="fm-key-item">grade A strong &middot; B moderate &middot; C thin</span>'
    )
    return (
        f'<div class="fm-key" aria-label="Status key">{status}</div>'
        f'<div class="fm-key" aria-label="Strip key"><span class="fm-key-item">Strip cells, pass rate per level:</span>{shades}</div>'
    )


def _row_html(mf: ModelFrontier, row: FrontierRow) -> str:
    untested = row.status == "NOT TESTED"
    where = _where(row)
    strip = ""
    if len(row.curve) >= 2:
        strip = (
            f'<div class="fm-strip-wrap">{_strip(row.curve, row.boundary)}'
            f'{_strip_caption(row.curve[0]["label"], row.curve[-1]["label"])}</div>'
        )
    note = f'<div class="fm-row-note">{_e(row.note)}</div>' if row.note else ""
    return (
        f'<a class="fm-row{" is-untested" if untested else ""}" href="{_e(_frontier_href(mf.model, row.key))}" '
        f'data-cap="{_e(row.key)}" data-status="{_e(row.status)}">'
        f'<div class="fm-row-status">{_status_chip(row.status)}</div>'
        f'<div class="fm-row-main"><span class="fm-row-label">{_e(row.label)}</span>'
        f'<span class="fm-row-headline">{_e(row.headline)}</span>'
        f'{_where_span(where)}{note}{strip}</div>'
        f"{_grade(row)}</a>"
    )


def _province_card(title: str, rows_html: str, meta: str) -> str:
    return (
        f'<section class="card" aria-label="{_e(title)}"><div class="card-head">'
        f'<span class="card-title">{_e(title)}</span><span class="card-meta">{_e(meta)}</span></div>'
        f"{rows_html}</section>"
    )


def _single_model_body(mf: ModelFrontier) -> str:
    out = []
    for province in _PROVINCES:
        rows = [r for r in mf.rows if r.province == province]
        if not rows:
            continue
        rows.sort(key=lambda r: STATUS_ORDER.get(r.status, 99))  # stable: SPECS order within a status
        tested = sum(r.status != "NOT TESTED" for r in rows)
        out.append(_province_card(province, "".join(_row_html(mf, r) for r in rows),
                                  f"{tested} of {len(rows)} tested"))
    return "".join(out)


def _compare_body(summaries: list[ModelFrontier]) -> str:
    out = []
    for province in _PROVINCES:
        blocks = []
        for spec in (s for s in SPECS if s.province == province):
            pairs = [(mf, next(r for r in mf.rows if r.key == spec.key)) for mf in summaries]
            # Shared axis: every level any model was tested at, so the
            # boundaries line up column for column.
            labels: dict[float, str] = {}
            for _mf, r in pairs:
                for c in r.curve:
                    labels.setdefault(c["value"], c["label"])
            columns = sorted(labels)
            show_strip = any(len(r.curve) >= 2 for _mf, r in pairs)
            lines = []
            for mf, r in pairs:
                where = _where(r)
                strip = f'<div class="fm-strip-wrap">{_strip(r.curve, r.boundary, columns, labels)}</div>' if show_strip and r.curve else ""
                lines.append(
                    f'<a class="fm-cmp-row{" is-untested" if r.status == "NOT TESTED" else ""}" '
                    f'href="{_e(_frontier_href(mf.model, r.key))}" data-model="{_e(mf.model)}" data-cap="{_e(r.key)}">'
                    f'<div class="fm-cmp-model">{_e(mf.name)}</div>'
                    f"<div>{_status_chip(r.status)}</div>"
                    f'<div class="fm-cmp-main"><span class="fm-cmp-text">{_e(r.headline)}{_grade(r)}</span>'
                    f'{_where_span(where)}{strip}</div></a>'
                )
            axis = ""
            if show_strip and columns:
                axis = (f'<div class="fm-cmp-axis">'
                        f"{_strip_caption(labels[columns[0]], labels[columns[-1]])}</div>")
            blocks.append(
                f'<div class="fm-cmp" data-cap="{_e(spec.key)}"><div class="fm-cmp-label">{_e(spec.label)}</div>'
                f'{"".join(lines)}{axis}</div>'
            )
        out.append(_province_card(province, "".join(blocks), f"{len(summaries)} models"))
    return "".join(out)


def capability_map_html(store, model: str | None = None, compare: bool = False) -> str:
    """`/` -- per model, where it breaks and why. `?model=` picks the model
    (full id or short name), `?compare=1` stacks every model per capability."""
    from .serve import _shell, _trial_count

    summaries = frontier_summaries(store)
    trials = _trial_count(store)
    if not summaries:
        body = _css() + _empty(
            "Capability map",
            "No trials in the store yet, so there is nothing to map.",
            '<p class="meta"><a href="/launch">Launch a campaign &rarr;</a></p>',
        )
        return _shell("Capability map", "capmap", body, trial_count=trials)

    selected = _resolve(summaries, model)
    unknown = bool(model) and selected is None and not compare
    if selected is None:
        selected = _default_model(summaries)

    verdicts = "".join(
        f'<li class="fm-verdict{" is-selected" if (mf is selected and not compare and not unknown) else ""}">'
        f'<a href="{_e(_map_href(mf.model))}">{_e(mf.name)}</a>'
        f'<span class="fm-verdict-text">{_e(_verdict_text(mf))}</span></li>'
        for mf in summaries
    )
    head = (
        '<div class="page-head"><div class="page-head-main"><h1>Capability map</h1>'
        '<p class="subtitle fm-lede">Each row is one capability, scored against a deterministic simulation '
        "that knows the right answer. Where a capability has a difficulty dial, the dial is turned up until "
        "the model stops passing, and the point where it stopped is reported in the task&rsquo;s own units.</p>"
        "</div></div>"
        f'<section class="card" aria-label="Verdicts"><div class="card-head"><span class="card-title">Verdict per model</span>'
        f'<span class="card-meta">{len(summaries)} models</span></div><ul class="fm-verdicts">{verdicts}</ul></section>'
    )
    tabs = "".join(
        f'<a class="model-tab{" active" if (mf is selected and not compare and not unknown) else ""}" '
        f'href="{_e(_map_href(mf.model))}">{_e(mf.name)}</a>'
        for mf in summaries
    )
    one_href = _map_href(selected.model) if selected else "/"
    controls = (
        '<div class="fm-controls">'
        f'<nav class="model-tabs" aria-label="Model">{tabs}</nav>'
        '<span class="seg-control" role="group" aria-label="View">'
        f'<a class="seg-item{"" if compare else " active"}" href="{_e(one_href)}">One model</a>'
        f'<a class="seg-item{" active" if compare else ""}" href="/?compare=1">Compare all models</a>'
        "</span></div>"
    )
    if compare:
        main = _compare_body(summaries)
    elif unknown:
        main = (f'<div class="empty-state"><p>No model called &ldquo;{_e(model)}&rdquo; has trials in the store.</p>'
                '<p class="meta">Pick a model above.</p></div>')
    else:
        main = _single_model_body(selected)
    body = _css() + head + controls + _key_html() + main
    return _shell("Capability map", "capmap", body, trial_count=trials)


# ---------------------------------------------------------------------------
# B. Discovery page
# ---------------------------------------------------------------------------


def _fmt_p(p: float) -> str:
    if p >= 0.001:
        return f"p = {p:.3f}"
    mant, exp = f"{p:.1e}".split("e")
    return f"p = {mant}&times;10<sup>{int(exp)}</sup>"


def _curve_chart(row: FrontierRow) -> str:
    """Pass rate per tested level, x drawn to scale on the axis value, the
    status thresholds as dashed reference lines, the boundary marked."""
    curve = row.curve
    W, H, L, R, T, B = 760, 340, 96, 24, 50, 96
    pw, ph = W - L - R, H - T - B
    vals = [c["value"] for c in curve]
    lo, hi = min(vals), max(vals)

    pad = 22  # keeps the first/last point and its n label off the axes

    def x(v: float) -> float:
        return L + pad + (pw - 2 * pad) * (v - lo) / (hi - lo) if hi > lo else L + pw / 2

    def y(r: float) -> float:
        return T + ph * (1 - r)

    parts = [f'<line class="ax" x1="{L}" y1="{y(0):.1f}" x2="{W - R}" y2="{y(0):.1f}"/>']
    for r in (0.5, 1.0):
        parts.append(f'<line class="grid" x1="{L}" y1="{y(r):.1f}" x2="{W - R}" y2="{y(r):.1f}"/>')
    for r in (BREAK_RATE, DEGRADE_RATE):
        parts.append(f'<line class="thr" x1="{L}" y1="{y(r):.1f}" x2="{W - R}" y2="{y(r):.1f}"/>')
    for r in sorted({0.0, BREAK_RATE, 0.5, DEGRADE_RATE, 1.0}):
        parts.append(f'<text class="t" x="{L - 8}" y="{y(r) + 4:.1f}" text-anchor="end">{r:.0%}</text>')

    bnd = next((c for c in curve if row.boundary and c["label"] == row.boundary), None)
    if bnd is not None:
        bx = x(bnd["value"])
        word = "Breaks" if row.status == "BREAKS" else "Degrades"
        anchor = "end" if bx > L + pw * 0.6 else "start"
        dx = -6 if anchor == "end" else 6
        parts.append(f'<line class="bnd" x1="{bx:.1f}" y1="{T - 22}" x2="{bx:.1f}" y2="{y(0):.1f}"/>')
        parts.append(f'<text class="bnd-t" x="{bx + dx:.1f}" y="{T - 26}" text-anchor="{anchor}">'
                     f"Boundary: {word.lower()} at {_e(bnd['label'])}</text>")

    scored = [c for c in curve if c["n_scored"]]
    if len(scored) >= 2:
        pts = " ".join(f"{x(c['value']):.1f},{y(_level_rate(c)):.1f}" for c in scored)
        parts.append(f'<polyline class="line" points="{pts}"/>')
    for c in curve:
        cx = x(c["value"])
        short = c["label"].split(", ")[0]
        parts.append(f'<text class="tx" transform="translate({cx:.1f},{y(0) + 14:.1f}) rotate(-38)" '
                     f'text-anchor="end">{_e(short)}</text>')
        if not c["n_scored"]:
            parts.append(f'<text class="n" x="{cx:.1f}" y="{y(0) - 8:.1f}" text-anchor="middle">n/a</text>')
            continue
        rate = _level_rate(c)
        cls = "pt"
        if c["n_scored"] < MIN_N:
            cls += " thin"
        elif rate <= BREAK_RATE:
            cls += " low"
        rad = 6 if c is bnd else 4.5
        parts.append(f'<circle class="{cls}" cx="{cx:.1f}" cy="{y(rate):.1f}" r="{rad}">'
                     f"<title>{_e(_level_tip(c))}</title></circle>")
        # The boundary's own label sits left of the dashed boundary line.
        nx, anchor = (cx - 9, "end") if c is bnd else (cx, "middle")
        parts.append(f'<text class="n" x="{nx:.1f}" y="{y(rate) - 11:.1f}" text-anchor="{anchor}">'
                     f'{c["n_pass"]}/{c["n_scored"]}</text>')
    label = f"Pass rate by level for {row.label}"
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{_e(label)}" xmlns="http://www.w3.org/2000/svg">'
            f'<title>{_e(label)}</title>{"".join(parts)}</svg>')


def _levels_table(row: FrontierRow) -> str:
    body = []
    for c in row.curve:
        is_b = bool(row.boundary) and c["label"] == row.boundary
        rate = _level_rate(c)
        notes = []
        if is_b:
            notes.append("&#9650; boundary")
        if 0 < c["n_scored"] < MIN_N:
            notes.append(f"fewer than {MIN_N} scored, not used for the call")
        if c["n_error"]:
            notes.append(f'{c["n_error"]} errored (excluded)')
        body.append(
            f'<tr class="{"is-boundary" if is_b else ""}"><td>{_e(c["label"])}</td>'
            f'<td class="num">{c["n_pass"]}/{c["n_scored"]}</td>'
            f'<td class="num">{f"{rate:.0%}" if rate is not None else "&mdash;"}</td>'
            f'<td class="dim">{"; ".join(notes)}</td></tr>'
        )
    return ('<table class="fm-levels"><thead><tr><th>Level</th><th class="num">Passed</th>'
            f'<th class="num">Pass rate</th><th>Note</th></tr></thead><tbody>{"".join(body)}</tbody></table>')


def _rate_chart(row: FrontierRow) -> str:
    """Single-rate rows: the rate, its Wilson interval, and the line the
    interval must reach for HOLDS."""
    W, H, L, R = 760, 74, 12, 12
    pw = W - L - R

    def x(r: float) -> float:
        return L + pw * r

    rate = row.n_pass / row.n_scored
    parts = [f'<rect class="track" x="{L}" y="22" width="{pw}" height="16" rx="3"/>']
    if row.ci:
        parts.append(f'<rect class="ci" x="{x(row.ci[0]):.1f}" y="22" width="{x(row.ci[1]) - x(row.ci[0]):.1f}" height="16" rx="2"/>')
    cls = "pt low" if rate <= BREAK_RATE else "pt"
    parts.append(f'<circle class="{cls}" cx="{x(rate):.1f}" cy="30" r="5.5"/>')
    for r, name in ((BREAK_RATE, f"breaks &le; {BREAK_RATE:.0%}"), (HOLD_UPPER_BOUND, f"holds if interval reaches {HOLD_UPPER_BOUND:.0%}")):
        parts.append(f'<line class="thr" x1="{x(r):.1f}" y1="14" x2="{x(r):.1f}" y2="46"/>')
        anchor = "end" if r > 0.5 else "start"
        parts.append(f'<text class="t" x="{x(r):.1f}" y="10" text-anchor="{anchor}">{name}</text>')
    for r in (0.0, 0.5, 1.0):
        anchor = {0.0: "start", 1.0: "end"}.get(r, "middle")
        parts.append(f'<text class="t" x="{x(r):.1f}" y="62" text-anchor="{anchor}">{r:.0%}</text>')
    label = f"{row.n_pass} of {row.n_scored} correct"
    return (f'<div class="fm-big">{rate:.0%}</div><p class="meta" style="margin:0 0 var(--sp-3)">'
            f"{row.n_pass}/{row.n_scored} scored trials passed"
            + (f" &middot; 95% interval {row.ci[0]:.0%}&ndash;{row.ci[1]:.0%}" if row.ci else "")
            + f'</p><svg viewBox="0 0 {W} {H}" role="img" aria-label="{_e(label)}" xmlns="http://www.w3.org/2000/svg">'
            f'{"".join(parts)}</svg>')


def _n_failures(row: FrontierRow, spec) -> int | None:
    """How many failures the cause was drawn from (same scope as
    `report.frontier`): every failure for single-rate rows; at/after the
    boundary for a break; every eligible level for an axis row that holds."""
    if spec.mode == "rate":
        return row.n_scored - row.n_pass
    eligible = [c for c in row.curve if c["n_scored"] >= MIN_N]
    if row.boundary:
        idx = next((i for i, c in enumerate(eligible) if c["label"] == row.boundary), None)
        if idx is None:
            return None
        eligible = eligible[idx:]
    return sum(c["n_scored"] - c["n_pass"] for c in eligible)


def _cause_card(row: FrontierRow, spec) -> str:
    title = _CAUSE_TITLE.get(row.status, "Why it fails")
    if row.cause_share is None:
        inner = '<p class="fm-cause">No failures recorded in the trials behind this row.</p>'
    else:
        sentence = row.cause or "The dominant failure has no plain-words description yet."
        # The share is computed over every failure in scope (row.cause_n).
        n = row.cause_n or _n_failures(row, spec)
        scope = " at and beyond the boundary" if row.boundary else ""
        if n:
            share = f"in {row.cause_share:.0%} of failures ({n} failures{scope})"
        else:
            share = f"in {row.cause_share:.0%} of failures"
        inner = (f'<p class="fm-cause">The model {_e(sentence)}.</p>'
                 f'<p class="fm-cause-share">{share}</p>')
    return (f'<section class="card"><div class="card-head"><span class="card-title">{_e(title)}</span></div>'
            f'<div class="card-body">{inner}</div></section>')


def _exemplar_card(row: FrontierRow) -> str:
    ex = row.exemplar
    if not ex:
        return ""
    at = f"at {row.boundary}" if row.boundary else ""
    detail = f'<span class="evidence">{_e(ex.get("detail"))}</span>' if ex.get("detail") else ""
    excerpt = f'<pre class="block">{_e(ex.get("excerpt"))}</pre>' if ex.get("excerpt") else ""
    # The exemplar is the first failure at the boundary, which is not always
    # an instance of the dominant cause. Say so rather than let the two
    # cards imply it is.
    code = (ex.get("detail") or "").split(":", 1)[0]
    mismatch = ""
    if code and row.cause_code and code != row.cause_code:
        where = "at the boundary" if row.boundary else "for this row"
        mismatch = ('<p class="fm-chart-note">This trial failed a different check from the dominant cause '
                    f"above. It is shown because it is the first failure recorded {where}.</p>")
    links = []
    if ex.get("record_id"):
        links.append(f'<a href="/record/{_e(_q(ex["record_id"]))}">Open this trial &rarr;</a>')
    if ex.get("run_id"):
        links.append(f'<a href="/run/{_e(_q(ex["run_id"]))}">Open the run it came from &rarr;</a>')
    return (
        '<section class="card"><div class="card-head"><span class="card-title">Example failure</span>'
        f'<span class="card-meta">{_e(at)}</span></div><div class="card-body">'
        '<div class="fm-trial"><div class="fm-kind">Trial result: FAIL <span>(one model answer)</span></div>'
        f'<div class="claim v-fail-trap"><span class="mark" aria-hidden="true">&#10005;</span>'
        f'<span class="verdict-word">FAIL</span>{detail}</div>{excerpt}{mismatch}'
        f'<div class="fm-links">{"".join(links)}</div></div></div></section>'
    )


def _ev(mark: str, cls: str, key: str, value: str, pending: bool = False, slug: str = "") -> str:
    return (
        f'<li class="fm-ev{" is-pending" if pending else ""}" data-ev="{_e(slug)}">'
        f'<span class="fm-ev-mark {cls}" aria-hidden="true">{mark}</span>'
        f'<span><span class="fm-ev-k">{_e(key)}</span>'
        f'<span class="fm-ev-v">{value}</span></span></li>'
    )


def _ribbon(row: FrontierRow) -> str:
    """Only what the row supports. Fresh-seed confirmation and replication
    are not carried by `FrontierRow`, so they never get a check mark."""
    items = [_ev("&#10003;", "ok", "Ground truth", "Deterministic simulation: the right answer is computed, not judged",
                 slug="ground-truth")]
    stats = []
    if row.ci:
        stats.append(f"Wilson 95% interval {row.ci[0]:.0%}&ndash;{row.ci[1]:.0%}")
    if row.p_value is not None:
        stats.append(f"{_fmt_p(row.p_value)}, before vs after the boundary (one-sided Fisher exact)")
    if row.confidence:
        stats.append(f"Grade {_e(row.confidence)}: {_e(_GRADE_WORDS.get(row.confidence, ''))} evidence")
    if stats:
        ok = row.confidence in ("A", "B")
        items.append(_ev("&#10003;" if ok else "!", "ok" if ok else "warn", "Statistics",
                         "".join(f'<span class="meta">{s}</span>' if i else s for i, s in enumerate(stats)),
                         slug="statistics"))
    if row.n_scored:
        scope = "at the boundary" if row.boundary else "in total"
        err = f'<span class="meta">{row.n_error} errored trials excluded</span>' if row.n_error else ""
        if row.n_scored < _SMALL_SAMPLE:
            items.append(_ev("!", "warn", "Sample size",
                             f"{row.n_scored} scored trials {scope}: small sample (under {_SMALL_SAMPLE}){err}",
                             slug="sample-size"))
        else:
            items.append(_ev("&#10003;", "ok", "Sample size", f"{row.n_scored} scored trials {scope}{err}",
                             slug="sample-size"))
    items.append(_ev("&#9675;", "none", "Fresh-seed confirmation", "Not run yet", pending=True, slug="fresh-seed"))
    items.append(_ev("&#9675;", "none", "Replication", "Not run yet", pending=True, slug="replication"))
    return (
        '<section class="card" aria-label="Evidence"><div class="card-head"><span class="card-title">Evidence</span>'
        '<span class="card-meta">what this finding rests on</span></div>'
        f'<div class="card-body"><ul class="fm-ribbon">{"".join(items)}</ul></div></section>'
    )


def _row_runs(store, model: str, spec) -> list[tuple[str, int]]:
    """Runs whose trials feed this row, with trial counts -- the same record
    filter `report.frontier` applies (hygiene included)."""
    if spec.mode == "absent":
        return []
    from ..report.frontier import _matches
    from .findings import _primary_records

    counts = Counter(r.run_id for r in _primary_records(store) if r.model_name == model and _matches(spec, r))
    return sorted(counts.items())


def _raw_card(store, mf: ModelFrontier, row: FrontierRow, spec) -> str:
    items = []
    runs = _row_runs(store, mf.model, spec)
    if runs:
        run_links = "".join(
            f'<li><a class="mono" href="/run/{_e(_q(rid))}">{_e(rid)}</a> '
            f'<span class="meta">{n} trial{"s" if n != 1 else ""}</span></li>'
            for rid, n in runs
        )
        items.append(f"<li>Runs behind this row<ul class=\"fm-raw\" style=\"margin-top:var(--sp-2)\">{run_links}</ul></li>")
    if spec.kind:
        words = _KIND_WORDS.get(spec.kind, "matching")
        href = f"/trials?model={_q(mf.model)}&axis=kind&key={_q(spec.kind)}"
        items.append(f'<li><a href="{_e(href)}">Every {_e(words)} trial for this model &rarr;</a>'
                     ' <span class="meta">broader than this row</span></li>')
    if row.exemplar and row.exemplar.get("record_id"):
        items.append(f'<li><a href="/record/{_e(_q(row.exemplar["record_id"]))}">The example failure, in full &rarr;</a></li>')
    if not items:
        return ""
    return ('<section class="card" aria-label="Raw evidence"><div class="card-head"><span class="card-title">Raw evidence</span>'
            f'<span class="card-meta">runs and trials</span></div><div class="card-body"><ul class="fm-raw">{"".join(items)}</ul></div></section>')


def frontier_html(store, model: str | None = None, cap: str | None = None) -> str:
    """`/frontier?model=<model>&cap=<key>` -- one capability row in full:
    the curve, why it breaks, one failing trial, and what the call rests on."""
    from .serve import _shell, _trial_count

    trials = _trial_count(store)
    back = '<p class="meta"><a href="/">Back to the capability map &rarr;</a></p>'
    summaries = frontier_summaries(store)
    mf = _resolve(summaries, model)
    spec = _SPEC_BY_KEY.get(cap or "")
    if mf is None or spec is None:
        if mf is None:
            msg = (f"No model called &ldquo;{_e(model)}&rdquo; has trials in the store." if model
                   else "No model chosen.")
        else:
            msg = (f"No capability called &ldquo;{_e(cap)}&rdquo;." if cap else "No capability chosen.")
        body = _css() + _empty("Capability not found", msg, back)
        return _shell("Capability not found", "capmap", body, trial_count=trials)

    row = next(r for r in mf.rows if r.key == spec.key)
    crumb = (f'<a href="/">Capability map</a><span class="sep">/</span>'
             f'<a href="{_e(_map_href(mf.model))}">{_e(mf.name)}</a><span class="sep">/</span>{_e(row.label)}')
    where = _where(row)
    head = (
        '<div class="page-head"><div class="page-head-main">'
        f'<div class="eyebrow">{_e(row.province)}</div><h1>{_e(row.label)}</h1>'
        f'<p class="subtitle" style="margin:0">{_e(mf.name)}</p></div></div>'
        '<section class="panel fm-finding" aria-label="Finding">'
        f'<div class="fm-finding-line"><span>Finding:</span>{_status_chip(row.status)}'
        + (f'<span title="Confidence grade: A strong, B moderate, C thin">&middot; grade {_e(row.confidence)}</span>'
           if row.confidence else "")
        + ('<span class="fm-sub">(no evidence yet)</span></div>' if row.status == "NOT TESTED"
           else '<span class="fm-sub">(research conclusion)</span></div>')
        + f'<p class="fm-finding-headline">{_e(row.headline)}</p>'
        + (f'<p class="fm-finding-where">{where}</p>' if where else "")
        + (f'<p class="fm-finding-where">{_e(row.note)}</p>' if row.note else "")
        + "</section>"
    )

    if row.status == "NOT TESTED" and not row.curve:
        main = ('<div class="empty-state"><p>Not tested yet: there is no evidence about this capability for this '
                "model in the store.</p>"
                '<p class="meta">That gap is itself a result: it marks what we do not know.</p></div>')
        body = _css() + head + main + back
        return _shell(f"{row.label} - {mf.name}", "capmap", body, breadcrumb=crumb, trial_count=trials)

    if row.curve:
        thin = any(0 < c["n_scored"] < MIN_N for c in row.curve)
        chart = (
            '<section class="card" aria-label="Pass rate by level"><div class="card-head">'
            '<span class="card-title">Pass rate by level</span>'
            f'<span class="card-meta">{len(row.curve)} levels tested</span></div>'
            f'<div class="card-body fm-chart"><div class="fm-chart-scroll">{_curve_chart(row)}</div>'
            f'<p class="fm-chart-note">Dashed lines: a level at or below {BREAK_RATE:.0%} pass counts toward BREAKS, '
            f'at or below {DEGRADE_RATE:.0%} toward DEGRADES. Labels above points: passed/scored.'
            + (f" Hollow points: fewer than {MIN_N} scored trials, not used for the call." if thin else "")
            + "</p>"
            + f"</div>{_levels_table(row)}</section>"
        )
    elif row.n_scored:
        chart = ('<section class="card" aria-label="Pass rate"><div class="card-head"><span class="card-title">Pass rate</span>'
                 '<span class="card-meta">single rate, no difficulty dial</span></div>'
                 f'<div class="card-body fm-chart">{_rate_chart(row)}</div></section>')
    else:
        chart = ""

    cause = _cause_card(row, spec) if row.status != "NOT TESTED" else ""
    ribbon = _ribbon(row) if row.status != "NOT TESTED" else ""
    body = (_css() + head + chart + cause + _exemplar_card(row) + ribbon
            + _raw_card(store, mf, row, spec) + back)
    return _shell(f"{row.label} - {mf.name}", "capmap", body, breadcrumb=crumb, trial_count=trials)
