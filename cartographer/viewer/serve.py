"""Local read-only HTML viewer over a `ResultsStore` sqlite db.

stdlib-only `http.server` app. Binds to 127.0.0.1 only. Every GET route
only reads from the store; the only mutating routes are `/launch` (spawns
a campaign thread) and `/stop` (sets that thread's cooperative-cancel
event -- see `cartographer.viewer.launch`), neither of which writes to
the results store directly.

Usage:
    python3 -m cartographer.viewer.serve [db_path] [port]

Defaults: db_path="results/pilot.sqlite", port=8788.
"""

from __future__ import annotations

import html
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..report.verdict import run_verdict
from ..store.schema import ExperimentRecord
from ..store.store import ResultsStore
from . import launch as launch_mod
from .atlas import atlas_html
from .coverage import coverage_html
from .findings import findings_html

_DEFAULT_DB_PATH = "results/campaign.sqlite"
_DEFAULT_PORT = 8788
_REPO_ROOT = Path(__file__).resolve().parents[2]

# run_id prefix -> known campaign budget (total scheduler trial budget),
# used only to render a progress bar on /live. Unknown prefixes fall back
# to "?" (bar hidden, raw count still shown). Budget is a CEILING, not a
# promise -- campaigns legitimately end early when explore finds nothing,
# so bars/labels below phrase this as a bound ("n / budget max").
_KNOWN_RUN_BUDGETS = {
    "camp-state-": 160,
    "camp-belief-": 700,
}

# ---------------------------------------------------------------------------
# Shared shell: design system + nav.
# ---------------------------------------------------------------------------

_NAV_GROUPS = [
    ("Results", [
        ("overview", "/", "Overview"),
        ("findings", "/findings", "Findings"),
        ("runs", "/runs", "Runs"),
    ]),
    ("Explore", [
        ("atlas", "/atlas", "Atlas"),
        ("coverage", "/coverage", "Coverage"),
    ]),
    ("Act", [
        ("launch", "/launch", "Launch"),
        ("live", "/live", "Live"),
    ]),
]
# Flat lookup kept for call sites that only need "is this key active".
_NAV_ITEMS = [item for _, items in _NAV_GROUPS for item in items]

_CSS = """
/* ---------------------------------------------------------------------
   1. TOKENS -- spacing scale, 4-step type scale, 3 surface levels, and
   ONE accent color used only for links/primary-action/active-nav (see
   docs/design-notes.md principles 10-11).
   ------------------------------------------------------------------- */
:root {
  --bg: #111418;
  --surface-1: #161a20;
  --surface-2: #1c2128;
  --bg-raised: #161a20; /* back-compat alias for --surface-1 */
  --border: #262c34;
  --border-strong: #333a44;

  --text: #E8EAED;
  --text-secondary: #b7c0cc;
  --text-muted: #7c8794;

  --accent: #2DD4BF;
  --accent-dim: #1f9484;
  --pass: #34D399;
  --fail: #F87171;
  --error: #FBBF24;

  --mono: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;

  /* spacing scale */
  --sp-1: 0.25rem; --sp-2: 0.5rem; --sp-3: 0.75rem; --sp-4: 1rem;
  --sp-5: 1.5rem; --sp-6: 2rem; --sp-7: 3rem;

  /* type scale: 4 sizes only -- display / h2 / body / small */
  --fs-display: 1.75rem;
  --fs-h2: 1.25rem;
  --fs-body: 0.95rem;
  --fs-small: 0.8125rem;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  background: var(--bg);
  color: var(--text);
  line-height: 1.55;
  font-size: var(--fs-body);
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* Numbers: tabular figures everywhere a number appears in a column or
   stat, so digits line up instead of jittering (principle 19). */
.num, td.num, .stat-num, .pill-num {
  font-variant-numeric: tabular-nums;
}

/* ---------------------------------------------------------------------
   2. SHELL -- header, grouped nav, breadcrumb slot.
   ------------------------------------------------------------------- */
header.shell-header {
  display: flex; align-items: center; gap: var(--sp-6);
  padding: var(--sp-3) var(--sp-5);
  border-bottom: 1px solid var(--border);
  background: var(--surface-1);
}
header.shell-header .brand {
  font-weight: 700; font-size: 1rem; color: var(--text);
  letter-spacing: -0.01em;
}
header.shell-header .brand:hover { text-decoration: none; color: var(--accent); }

nav.shell-nav { display: flex; gap: var(--sp-6); flex: 1; }
nav.shell-nav .nav-group { display: flex; align-items: baseline; gap: var(--sp-3); }
nav.shell-nav .nav-group-label {
  color: var(--text-muted); font-size: 10px; text-transform: uppercase;
  letter-spacing: 0.06em; font-weight: 600; margin-right: var(--sp-1);
}
nav.shell-nav a {
  color: var(--text-secondary); font-size: var(--fs-small);
  padding: 0.3rem 0; border-bottom: 2px solid transparent;
}
nav.shell-nav a.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
nav.shell-nav a:hover { color: var(--text); text-decoration: none; }

main.shell-main { padding: var(--sp-6) var(--sp-5) var(--sp-7); max-width: 1120px; margin: 0 auto; }

.breadcrumb {
  color: var(--text-muted); font-size: var(--fs-small); margin: 0 0 var(--sp-4);
}
.breadcrumb a { color: var(--text-secondary); }
.breadcrumb a:hover { color: var(--accent); }
.breadcrumb .sep { margin: 0 var(--sp-2); color: var(--border-strong); }

/* ---------------------------------------------------------------------
   3. TYPE SCALE -- 4 sizes, used consistently.
   ------------------------------------------------------------------- */
h1 {
  font-size: var(--fs-display); font-weight: 650; margin: 0 0 var(--sp-1);
  letter-spacing: -0.015em;
}
.subtitle { color: var(--text-secondary); font-size: var(--fs-body); margin: 0 0 var(--sp-5); }
h2 {
  font-size: var(--fs-h2); font-weight: 600; margin: var(--sp-6) 0 var(--sp-3);
  color: var(--text); letter-spacing: -0.005em;
}
h3 { font-size: var(--fs-body); font-weight: 650; margin: var(--sp-4) 0 var(--sp-2); color: var(--text); }

.meta { color: var(--text-muted); font-size: var(--fs-small); }
.mono { font-family: var(--mono); }
.eyebrow {
  text-transform: uppercase; letter-spacing: 0.06em; font-size: 10px;
  font-weight: 700; color: var(--text-muted);
}

/* ---------------------------------------------------------------------
   4. SURFACES -- elevation via background steps, not borders on
   everything (principle 12). .panel is the ONE reusable card pattern.
   ------------------------------------------------------------------- */
.panel {
  background: var(--surface-1);
  border-radius: 10px;
  padding: var(--sp-5);
}
.panel + .panel { margin-top: var(--sp-4); }
.panel-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: var(--sp-4);
  margin: var(--sp-4) 0 var(--sp-6);
}
.panel-grid .panel { padding: var(--sp-4); }

.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: var(--sp-3); margin: var(--sp-3) 0 var(--sp-5); }
.stat-box { background: var(--surface-1); border-radius: 8px; padding: var(--sp-4); }
.stat-box .stat-num { font-family: var(--sans); font-size: 1.6rem; font-weight: 650; }
.stat-box .stat-label { color: var(--text-muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.05em; margin-top: var(--sp-1); }

/* ---------------------------------------------------------------------
   5. TABLES -- numeric columns right-aligned + tabular, headers
   uppercase-tracking (used ONLY here and in pills/labels), rules
   between rows instead of boxing the whole table.
   ------------------------------------------------------------------- */
table { border-collapse: collapse; width: 100%; margin: var(--sp-3) 0 var(--sp-5); font-size: var(--fs-small); }
th, td { padding: 0.6rem var(--sp-3); text-align: left; border-bottom: 1px solid var(--border); }
th {
  color: var(--text-muted); text-transform: uppercase; font-size: 10.5px;
  letter-spacing: 0.05em; font-weight: 650; border-bottom: 1px solid var(--border-strong);
}
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.mono, td .mono { font-family: var(--mono); }
tbody tr:hover { background: var(--surface-1); }

/* ---------------------------------------------------------------------
   6. VERDICT PILLS -- dot + label, not a solid saturated chip
   (principle 17: keeps semantic color as accent, not a color block).
   ------------------------------------------------------------------- */
.pill {
  display: inline-flex; align-items: center; gap: 0.4em;
  font-family: var(--sans); font-size: var(--fs-small); font-weight: 600;
  color: var(--text-secondary);
}
.pill::before {
  content: ""; display: inline-block; width: 7px; height: 7px; border-radius: 999px;
  background: var(--text-muted);
}
.pill-PASS::before { background: var(--pass); }
.pill-PASS { color: var(--pass); }
.pill-FAIL::before { background: var(--fail); }
.pill-FAIL { color: var(--fail); }
.pill-ERROR::before { background: var(--error); }
.pill-ERROR { color: var(--error); }
.pill-REJECTED_BY_FIDELITY_GATE::before { background: var(--text-muted); }

/* Stage tracker for full_map runs on /live (principle 24). */
.stage-track { display: flex; align-items: center; gap: var(--sp-2); margin: var(--sp-3) 0 var(--sp-4); flex-wrap: wrap; }
.stage-step {
  display: flex; align-items: center; gap: 0.4em; font-size: var(--fs-small);
  color: var(--text-muted); padding: 0.3rem 0.65rem; border-radius: 999px;
  background: var(--surface-1);
}
.stage-step .dot { width: 8px; height: 8px; border-radius: 999px; background: var(--border-strong); }
.stage-step.done { color: var(--pass); }
.stage-step.done .dot { background: var(--pass); }
.stage-step.active { color: var(--accent); background: var(--surface-2); font-weight: 650; }
.stage-step.active .dot { background: var(--accent); }
.stage-arrow { color: var(--border-strong); }

/* ---------------------------------------------------------------------
   7. PROGRESS -- accent reserved for in-progress fill; complete state
   switches to semantic pass-green so accent stays meaningful.
   ------------------------------------------------------------------- */
.progress-track { background: var(--border); border-radius: 999px; height: 8px; width: 100%; overflow: hidden; }
.progress-fill { background: var(--accent); height: 100%; border-radius: 999px; transition: width 0.3s ease; }
.progress-fill.complete { background: var(--pass); }
.progress-row { display: flex; align-items: center; gap: var(--sp-3); margin: var(--sp-2) 0 var(--sp-1); min-height: 8px; }
.progress-row .progress-track { flex: 1; }
.progress-text { font-size: var(--fs-small); color: var(--text-muted); white-space: nowrap; font-variant-numeric: tabular-nums; }

/* ---------------------------------------------------------------------
   8. CODE / DETAILS -- long mono blobs stay collapsed by default
   (principle 7: progressive disclosure).
   ------------------------------------------------------------------- */
pre.block {
  white-space: pre-wrap; word-wrap: break-word;
  background: var(--surface-1);
  border-radius: 8px;
  padding: var(--sp-4);
  font-family: var(--mono);
  font-size: var(--fs-small);
  color: var(--text-secondary);
}
details { margin: var(--sp-2) 0; }
details summary {
  cursor: pointer; font-weight: 600; margin: var(--sp-2) 0; color: var(--text-secondary);
  font-size: var(--fs-small); list-style: none;
}
details summary::-webkit-details-marker { display: none; }
details summary::before { content: "\\25b8  "; color: var(--text-muted); }
details[open] summary::before { content: "\\25be  "; }
details[open] summary { color: var(--text); }

/* ---------------------------------------------------------------------
   9. FORMS -- radio-card preset pattern for /launch (principle 23).
   ------------------------------------------------------------------- */
form p { margin: var(--sp-3) 0; }
label { font-size: var(--fs-small); color: var(--text-secondary); }
input, select, button, textarea {
  font-family: var(--sans);
  background: var(--surface-2);
  color: var(--text);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  padding: 0.45rem 0.6rem;
  font-size: var(--fs-body);
}
button {
  background: var(--accent);
  color: #0b0d10;
  font-weight: 650;
  cursor: pointer;
  border: none;
  padding: 0.6rem 1.1rem;
}
button:hover { opacity: 0.9; }
button.btn-primary { font-size: var(--fs-body); }

.preset-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: var(--sp-3); margin: var(--sp-3) 0; }
.preset-card {
  display: block; background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 10px; padding: var(--sp-4); cursor: pointer;
}
.preset-card:has(input:checked) { border-color: var(--accent); background: var(--surface-2); }
.preset-card .preset-title { font-weight: 650; display: flex; align-items: center; gap: var(--sp-2); }
.preset-card .preset-desc { color: var(--text-muted); font-size: var(--fs-small); margin-top: var(--sp-1); }
.badge {
  font-size: 9.5px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase;
  padding: 0.1rem 0.4rem; border-radius: 4px;
}
.badge-recommended { background: var(--accent); color: #0b0d10; }
.badge-new { background: var(--surface-2); color: var(--accent); border: 1px solid var(--accent-dim); }

/* ---------------------------------------------------------------------
   10. EMPTY STATES (principle 6) -- helpful pointer, not a bare box.
   ------------------------------------------------------------------- */
.empty-state {
  background: var(--surface-1); border-radius: 10px; padding: var(--sp-6);
  text-align: center; color: var(--text-secondary);
}
.empty-state .meta { margin-top: var(--sp-2); }

/* ---------------------------------------------------------------------
   11. CTA -- the one primary action on / (principle 22).
   ------------------------------------------------------------------- */
.cta-row { margin: var(--sp-4) 0 var(--sp-6); }
.cta-primary {
  display: inline-block; background: var(--accent); color: #0b0d10;
  font-weight: 650; font-size: var(--fs-body); padding: 0.75rem 1.4rem;
  border-radius: 8px;
}
.cta-primary:hover { text-decoration: none; opacity: 0.92; }
"""

_VERDICT_CLASS_FALLBACK = "meta"


def _shell(
    title: str,
    active_nav: str,
    body: str,
    refresh: bool = False,
    breadcrumb: str | None = None,
) -> str:
    """Render `body` inside the shared header/nav/style shell.

    `active_nav` is one of the keys in `_NAV_ITEMS` ("runs", "live",
    "atlas", "launch") or "" for none highlighted. Nav is grouped into
    Results / Explore / Act (see docs/design-notes.md principle 2).
    `breadcrumb`, if given, is raw HTML rendered just above the page body
    (principle 3: recognition over recall on detail pages).
    """
    refresh_tag = '<meta http-equiv="refresh" content="5">' if refresh else ""
    groups_html = []
    for group_label, items in _NAV_GROUPS:
        links = "".join(
            f'<a href="{href}"{" class=\"active\"" if key == active_nav else ""}>{label}</a>'
            for key, href, label in items
        )
        groups_html.append(
            f'<span class="nav-group"><span class="nav-group-label">{group_label}</span>{links}</span>'
        )
    nav_links = "".join(groups_html)
    header = (
        '<header class="shell-header">'
        '<a class="brand" href="/">Model Cartographer</a>'
        f'<nav class="shell-nav">{nav_links}</nav>'
        "</header>"
    )
    breadcrumb_html = f'<p class="breadcrumb">{breadcrumb}</p>' if breadcrumb else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"{refresh_tag}"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{header}<main class=\"shell-main\">{breadcrumb_html}{body}</main></body></html>"
    )


# Backwards-compatible alias used by atlas.py and launch.py call sites that
# don't need nav highlighting (kept internal to this module's API surface).
def _page(title: str, body: str, refresh: bool = False, active_nav: str = "") -> str:
    return _shell(title, active_nav, body, refresh=refresh)


def _verdict_pill(verdict_name: str) -> str:
    safe = html.escape(verdict_name)
    return f'<span class="pill pill-{safe}">{safe}</span>'


# Kept as an alias: some call sites/tests refer to the old name.
_verdict_span = _verdict_pill


def _parse_verifier_detail(detail: str | None) -> dict[str, str]:
    """Parse a `;`-separated `key=value` string, e.g. 'kind=state;sim=near_duplicate'.
    Tolerant of missing/malformed detail -- returns an empty dict rather than
    raising, since this is display-only.
    """
    out: dict[str, str] = {}
    if not detail:
        return out
    for part in detail.split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


def _relative_time(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    age_s = _age_seconds(ts)
    if age_s < 0:
        age_s = 0
    if age_s < 60:
        return f"{int(age_s)}s ago"
    if age_s < 3600:
        return f"{int(age_s // 60)}m ago"
    if age_s < 86400:
        return f"{int(age_s // 3600)}h ago"
    return f"{int(age_s // 86400)}d ago"


def _verdict_headline_safe(store: ResultsStore, run_id: str) -> str:
    """`run_verdict`'s headline, defensively -- never let a verdict
    computation (which regenerates worlds) break a listing page."""
    try:
        return run_verdict(store, run_id).headline
    except Exception:
        return ""


def verdict_panel_html(store: ResultsStore, run_id: str) -> str:
    """Renders the "what this run revealed" panel: headline + paragraphs +
    up to 3 notable failure examples. Meant to sit at the TOP of the run
    detail page, above the raw per-trial table."""
    try:
        verdict = run_verdict(store, run_id)
    except Exception as exc:  # never let a verdict crash the run page
        return (
            '<div class="stat-box" style="grid-column: 1 / -1;">'
            '<p class="meta">verdict unavailable for this run '
            f"({html.escape(repr(exc))}).</p></div>"
        )

    paragraphs_html = "".join(f"<p>{html.escape(p)}</p>" for p in verdict.paragraphs)

    failures_html = ""
    if verdict.notable_failures:
        items = []
        for f in verdict.notable_failures:
            items.append(
                "<li>"
                f'<a href="/record/{html.escape(f.record_id)}">record {html.escape(f.record_id[:8])}</a>: '
                f'<span class="meta">asked</span> "{html.escape(f.question)}" &middot; '
                f'<span class="meta">model said</span> "{html.escape(f.model_answer)}" &middot; '
                f'<span class="meta">correct</span> "{html.escape(f.correct_answer)}" &mdash; '
                f"{html.escape(f.why_wrong)}"
                "</li>"
            )
        failures_html = (
            "<h3>Notable failures</h3><ul>" + "".join(items) + "</ul>"
        )

    return (
        '<div class="stat-box" style="grid-column: 1 / -1;" data-testid="verdict-panel">'
        f'<h2 style="margin-top:0;">{html.escape(verdict.headline)}</h2>'
        f"{paragraphs_html}"
        f"{failures_html}"
        "</div>"
    )


def _runs_table_rows(store: ResultsStore) -> tuple[list[str], dict[str, dict]]:
    cur = store._conn.execute(
        "SELECT run_id, model_name, verdict, cost_usd, created_at FROM experiments"
    )
    by_run: dict[str, dict] = {}
    for row in cur.fetchall():
        run_id = row["run_id"]
        agg = by_run.setdefault(
            run_id,
            {"counts": {}, "model": row["model_name"], "cost": 0.0, "created_ats": []},
        )
        agg["counts"][row["verdict"]] = agg["counts"].get(row["verdict"], 0) + 1
        agg["cost"] += row["cost_usd"] or 0.0
        if row["created_at"]:
            agg["created_ats"].append(row["created_at"])

    # full_map runs record no rows directly against the parent run_id --
    # every trial lives under f"{parent}/{stage}" child run_ids. Fold those
    # child rows' aggregates up into a synthetic parent entry (and drop the
    # raw child rows from the top-level table) so /runs shows one grouped
    # row per full_map launch instead of five disconnected stage rows.
    parents: dict[str, dict] = {}
    child_run_ids: set[str] = set()
    for run_id in list(by_run.keys()):
        if "/" in run_id:
            parent, stage = run_id.rsplit("/", 1)
            child_run_ids.add(run_id)
            agg = by_run[run_id]
            p = parents.setdefault(
                parent, {"counts": {}, "model": agg["model"], "cost": 0.0, "created_ats": []}
            )
            for verdict_name, n in agg["counts"].items():
                p["counts"][verdict_name] = p["counts"].get(verdict_name, 0) + n
            p["cost"] += agg["cost"]
            p["created_ats"].extend(agg["created_ats"])
    for parent, agg in parents.items():
        by_run[parent] = agg
    for child in child_run_ids:
        by_run.pop(child, None)

    active = launch_mod.current_run()
    active_run_id = active["run_id"] if active else None

    def sort_key(run_id: str) -> str:
        ats = by_run[run_id]["created_ats"]
        return max(ats) if ats else ""

    rows_html = []
    for run_id in sorted(by_run.keys(), key=sort_key, reverse=True):
        agg = by_run[run_id]
        counts = agg["counts"]
        total = sum(counts.values())
        passn = counts.get("PASS", 0)
        pass_rate = (passn / total * 100.0) if total else 0.0
        newest = None
        if agg["created_ats"]:
            newest = _parse_created_at(max(agg["created_ats"]))
        is_live = run_id == active_run_id or (
            newest is not None and _age_seconds(newest) <= _COMPLETE_AFTER_S
        )
        status = "live" if is_live else "complete"
        safe_run_id = html.escape(run_id)
        model_short = html.escape(_short_model(agg["model"]))
        headline = _verdict_headline_safe(store, run_id)
        headline_html = (
            f'<div class="meta" style="margin-top:0.15rem;">{html.escape(headline)}</div>'
            if headline
            else ""
        )
        rows_html.append(
            "<tr>"
            f'<td class="mono"><a href="/run/{safe_run_id}">{safe_run_id}</a>{headline_html}</td>'
            f'<td class="mono">{model_short}</td>'
            f'<td class="num">{total}</td>'
            f'<td class="num"><span class="pill pill-PASS">{pass_rate:.0f}%</span></td>'
            f'<td class="mono num">${agg["cost"]:.4f}</td>'
            f"<td>{html.escape(_relative_time(newest))}</td>"
            f"<td>{status}</td>"
            "</tr>"
        )
    return rows_html, by_run


def runs_html(store: ResultsStore) -> str:
    """The raw per-run table (formerly the home page)."""
    rows_html, by_run = _runs_table_rows(store)
    if not rows_html:
        body = (
            "<h1>Runs</h1>"
            '<p class="subtitle">Every campaign or pilot run recorded in this store.</p>'
            '<div class="empty-state"><p>No runs yet.</p>'
            '<p class="meta"><a href="/launch">Launch your first campaign &rarr;</a></p></div>'
        )
        return _shell("Runs", "runs", body)
    table = (
        "<table><thead><tr><th>run_id</th><th>model</th><th class=\"num\">records</th>"
        "<th class=\"num\">pass rate</th><th class=\"num\">cost</th><th>started</th><th>status</th>"
        "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>"
    )
    body = (
        "<h1>Runs</h1>"
        '<p class="subtitle">Every campaign/pilot run recorded in this store, one row each '
        '(full_map runs are grouped under their parent run_id). Click a run_id for the full '
        "report and per-trial table.</p>"
        f"{table}"
    )
    return _shell("Runs", "runs", body)


_QUICK_LINKS = [
    ("/findings", "Findings", "The capability map -- confirmed failure modes, mechanisms, and evidence."),
    ("/coverage", "Coverage", "What we test (query kinds, difficulty dims, foil traps) and what we don't."),
    ("/runs", "Runs", "Raw table of every campaign/pilot run recorded in this store."),
    ("/live", "Live", "Watch an in-progress campaign's trials come in, with ETA."),
    ("/atlas", "Atlas", "Cross-model pass-rate charts per query kind and dimension."),
    ("/launch", "Launch", "Start a new campaign against a model from the browser."),
]


def index_html(store: ResultsStore) -> str:
    """Overview dashboard: what this instrument does, three summary cards
    computed live from the store, and quick links into the rest of the
    site. The raw per-run table lives at /runs.
    """
    cur = store._conn.execute("SELECT verdict, cost_usd, verifier_detail, model_name FROM experiments")
    rows = cur.fetchall()

    total_trials = len(rows)
    total_spend = sum(r["cost_usd"] or 0.0 for r in rows)

    by_model_kind: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        kind = _query_kind_from_detail(r["verifier_detail"])
        model = _short_model(r["model_name"])
        agg = by_model_kind.setdefault((model, kind), {"n": 0, "pass": 0})
        agg["n"] += 1
        if r["verdict"] == "PASS":
            agg["pass"] += 1

    model_kind_rows = "".join(
        f'<tr><td class="mono">{html.escape(model)}</td><td>{html.escape(kind)}</td>'
        f'<td>{agg["pass"]}/{agg["n"]}</td>'
        f'<td>{(agg["pass"] / agg["n"] * 100.0 if agg["n"] else 0.0):.0f}%</td></tr>'
        for (model, kind), agg in sorted(by_model_kind.items())
        if kind != "-"
    )
    model_kind_table = (
        "<table><thead><tr><th>model</th><th>query kind</th><th>pass/total</th>"
        "<th>rate</th></tr></thead><tbody>"
        + (model_kind_rows or '<tr><td colspan="4" class="meta">No trials recorded yet.</td></tr>')
        + "</tbody></table>"
    )

    findings_summary = "".join(
        f"<li>{html.escape(f.name)} <span class=\"meta\">({html.escape(f.status)})</span></li>"
        for f in _confirmed_findings_preview()
    )

    _, by_run_for_headlines = _runs_table_rows(store)

    def _headline_sort_key(rid: str) -> str:
        ats = by_run_for_headlines[rid]["created_ats"]
        return max(ats) if ats else ""

    recent_run_ids = sorted(by_run_for_headlines.keys(), key=_headline_sort_key, reverse=True)[:5]
    recent_verdicts_items = []
    for rid in recent_run_ids:
        headline = _verdict_headline_safe(store, rid)
        if not headline:
            continue
        recent_verdicts_items.append(
            f'<li><a href="/run/{html.escape(rid)}"><span class="mono">{html.escape(rid)}</span></a>'
            f" &mdash; {html.escape(headline)}</li>"
        )
    recent_verdicts_html = (
        "".join(recent_verdicts_items)
        if recent_verdicts_items
        else '<li class="meta">no runs yet</li>'
    )

    # Task-first overview (docs/design-notes.md principle 22): a first-time
    # visitor's 3 jobs, in order, above the fold -- (a) what is this,
    # (b) what did it find, (c) one primary CTA to run a test. Everything
    # else (raw stat counts, per-model matrix, quick links) is demoted
    # below as "see evidence" / "where to go" material.
    body = f"""
    <h1>Model Cartographer</h1>
    <p class="subtitle">A synthetic story world with a known ground truth
    is used to probe a model's grasp of object state, belief, and event
    order -- so capability boundaries and failure modes can be mapped and
    audited, not just guessed at.</p>

    <div class="panel">
      <p class="eyebrow">What we've found so far</p>
      <ul style="margin:0.5rem 0 0; padding-left: 1.1rem;">{findings_summary or '<li class="meta">No confirmed findings yet -- launch a campaign to start mapping.</li>'}</ul>
      <p class="meta" style="margin-top:0.75rem;"><a href="/findings">see the full capability map &rarr;</a></p>
    </div>

    <div class="cta-row">
      <a class="cta-primary" href="/launch">Map a model &rarr;</a>
    </div>

    <h2>Models tested &middot; pass rates by query kind</h2>
    {model_kind_table}

    <div class="stat-grid">
      <div class="stat-box"><div class="stat-num">{total_trials}</div><div class="stat-label">Total trials</div></div>
      <div class="stat-box"><div class="stat-num">${total_spend:.4f}</div><div class="stat-label">Total spend</div></div>
    </div>

    <h2>What recent runs revealed</h2>
    <ul style="margin:0; padding-left: 1.1rem;">{recent_verdicts_html}</ul>
    <p class="meta" style="margin-top:0.5rem;"><a href="/runs">all runs &rarr;</a></p>

    <h2>Where to go</h2>
    <div class="panel-grid">
      {"".join(
          f'<div class="panel"><p style="margin:0 0 0.3rem;"><a href="{href}"><strong>{html.escape(label)}</strong></a></p>'
          f'<p class="meta" style="margin:0;">{html.escape(desc)}</p></div>'
          for href, label, desc in _QUICK_LINKS
      )}
    </div>
    """
    return _shell("Model Cartographer", "overview", body)


def _query_kind_from_detail(detail: str | None) -> str:
    parsed = _parse_verifier_detail(detail)
    return parsed.get("kind", "-")


def _confirmed_findings_preview():
    from .findings import FINDINGS

    return [f for f in FINDINGS if f.status == "confirmed"][:4]


def _short_model(model_name: str) -> str:
    return model_name.split("/")[-1] if model_name else "-"


def _budget_for_run(run_id: str) -> int | None:
    for prefix, budget in _KNOWN_RUN_BUDGETS.items():
        if run_id.startswith(prefix):
            return budget
    return None


def _parse_created_at(created_at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None


def _config_summary(realized_dims: dict) -> str:
    if not realized_dims:
        return "-"
    parts = [f"{k}={v}" for k, v in sorted(realized_dims.items())]
    return ", ".join(parts)


# A run whose newest record is older than this is considered finished:
# /live renders a "RUN COMPLETE" state (full green bar, no ETA) instead of
# a frozen partial progress bar with a bogus ETA.
_COMPLETE_AFTER_S = 180.0

# ETA is computed over a RECENT window only -- not the whole run's average
# rate, which stalls or fast bursts easily poison. Window = last N records
# or last M minutes of records, whichever gives the LARGER sample.
_ETA_WINDOW_RECORDS = 25
_ETA_WINDOW_MINUTES = 3.0
_ETA_MIN_SAMPLE = 10


def _now_utc() -> datetime:
    """Split out so tests can monkeypatch 'now'."""
    return datetime.now(timezone.utc)


def _age_seconds(newest: datetime) -> float:
    if newest.tzinfo is None:
        # created_at strings without an offset are treated as UTC (that's
        # how ExperimentRecord stamps them).
        newest = newest.replace(tzinfo=timezone.utc)
    return (_now_utc() - newest).total_seconds()


def _recent_rate_per_min(records: list[ExperimentRecord]) -> tuple[float | None, int]:
    """Rate (records/min) computed over a RECENT window, plus the sample
    size used. Returns (None, n) if there's no usable time span.

    Window selection: take the records from the last `_ETA_WINDOW_MINUTES`
    minutes (relative to the newest record's own timestamp, not wall
    clock, so this works fine in tests). A long stall followed by a fast
    burst should reflect the fast burst's rate, not the whole run's
    average -- so the time-based window is preferred whenever it already
    has enough samples to be meaningful. Only when that time-based window
    is too thin (a slow-but-steady run with few records per minute) do we
    widen to the last `_ETA_WINDOW_RECORDS` records instead, so the
    estimate still has a decent sample size rather than reading "estimating..."
    unnecessarily.
    """
    dated = [
        (r, t) for r, t in ((r, _parse_created_at(r.created_at)) for r in records) if t is not None
    ]
    if len(dated) < 2:
        return None, len(dated)
    dated.sort(key=lambda pair: pair[1])

    newest_t = dated[-1][1]
    cutoff = newest_t.timestamp() - _ETA_WINDOW_MINUTES * 60.0
    by_time = [pair for pair in dated if pair[1].timestamp() >= cutoff]

    if len(by_time) >= _ETA_MIN_SAMPLE:
        window = by_time
    else:
        by_count = dated[-_ETA_WINDOW_RECORDS:]
        window = by_count if len(by_count) >= len(by_time) else by_time

    if len(window) < 2:
        return None, len(window)

    span_s = (window[-1][1] - window[0][1]).total_seconds()
    if span_s <= 0:
        return None, len(window)
    rate = (len(window) - 1) / (span_s / 60.0) if span_s > 0 else None
    if rate is None or rate <= 0:
        return None, len(window)
    return rate, len(window)


def _format_eta(remaining: int, rate_per_min: float | None, sample_n: int) -> str:
    if sample_n < _ETA_MIN_SAMPLE:
        return "estimating..."
    if rate_per_min is None or rate_per_min <= 0:
        return "estimating..."
    eta_min = remaining / rate_per_min
    if eta_min > 120:
        return "at most &gt; 2h at current rate"
    return f"at most ~{eta_min:.1f} min at current rate"


def _stop_button_html(is_active: bool, stopping: bool) -> str:
    """Renders a "Stop campaign" form-POST button when a campaign is
    actively registered, or a "stopping..." notice once its cancel_event
    has been set but the thread hasn't exited yet. Renders nothing when no
    campaign is active -- there's nothing to stop.
    """
    if not is_active:
        return ""
    if stopping:
        return '<p class="meta"><strong>stopping&hellip;</strong> waiting for the current trial to finish.</p>'
    return (
        '<form method="post" action="/stop" style="display:inline">'
        '<button type="submit">Stop campaign</button></form>'
    )


def live_html(store: ResultsStore) -> str:
    """Live monitor. Prefers the ACTIVE registered campaign run_id (from
    `launch_mod.current_run()`) even if it has zero records yet -- a
    just-launched campaign has no records for its first seconds-to-minutes
    (world gen + first API call latency), and without this the page would
    keep showing the previous run. Falls back to the run with the newest
    record only when nothing is actively registered.
    """
    active = launch_mod.current_run()
    run_id: str | None = active["run_id"] if active else None
    stopping = launch_mod.is_stopping() if active else False

    if run_id is None:
        cur = store._conn.execute(
            "SELECT run_id FROM experiments ORDER BY created_at DESC, record_id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            body = (
                "<h1>Live campaign monitor</h1>"
                '<div class="empty-state"><p>No records found.</p>'
                '<p class="meta"><a href="/launch">Launch your first campaign &rarr;</a></p></div>'
            )
            return _shell("Live campaign monitor", "live", body, refresh=True)
        run_id = row["run_id"]

    safe_run_id = html.escape(run_id)
    records = store.by_run(run_id)

    if not records:
        body = f"""
        <h1>Live: {safe_run_id}</h1>
        <p class="meta">starting&hellip; 0 records</p>
        <div class="progress-track"><div class="progress-fill" style="width:0%"></div></div>
        {_stop_button_html(active is not None, stopping)}
        """
        return _shell(f"Live: {run_id}", "live", body, refresh=True)

    total = len(records)
    passn = sum(1 for r in records if r.verdict.name == "PASS")
    failn = sum(1 for r in records if r.verdict.name == "FAIL")
    errn = sum(1 for r in records if r.verdict.name == "ERROR")
    pass_rate = (passn / total * 100.0) if total else 0.0
    total_cost = sum(r.cost_usd for r in records)

    timestamps = [t for t in (_parse_created_at(r.created_at) for r in records) if t is not None]
    if len(timestamps) >= 2:
        elapsed_s = max((max(timestamps) - min(timestamps)).total_seconds(), 0.0)
    else:
        elapsed_s = 0.0

    complete = bool(timestamps) and _age_seconds(max(timestamps)) > _COMPLETE_AFTER_S
    # A run still actively registered is never "complete" even if its
    # newest record happens to be stale (e.g. a slow phase) -- it's still
    # in-flight from the launcher's point of view.
    if active is not None and run_id == active["run_id"]:
        complete = False

    budget = _budget_for_run(run_id)
    overall_rate_per_min = (total / (elapsed_s / 60.0)) if elapsed_s > 0 else 0.0

    if complete:
        pct_str = ""
        if budget is not None and budget > 0:
            pct_str = f" ({min(total / budget * 100.0, 100.0):.1f}% of {budget} budget max)"
        bar_html = (
            '<div class="progress-track"><div class="progress-fill complete" '
            'style="width:100%"></div></div>'
            f'<p class="meta"><strong>RUN COMPLETE</strong> &mdash; finished at '
            f"{total} calls{pct_str}</p>"
        )
    elif budget is not None and budget > 0:
        pct = min(total / budget * 100.0, 100.0)
        remaining = max(budget - total, 0)
        rate_per_min, sample_n = _recent_rate_per_min(records)
        eta_str = _format_eta(remaining, rate_per_min, sample_n)
        bar_html = (
            '<div class="progress-row">'
            f'<div class="progress-track"><div class="progress-fill" style="width:{pct:.1f}%"></div></div>'
            f'<span class="progress-text">{total} / {budget} max ({pct:.1f}%)</span>'
            "</div>"
            f'<p class="meta">ETA: {eta_str}</p>'
        )
    else:
        bar_html = f'<p class="meta">{total} records (budget unknown for this run_id)</p>'

    stat_grid = (
        '<div class="stat-grid">'
        f'<div class="stat-box"><div class="stat-num">{passn}</div><div class="stat-label">Pass</div></div>'
        f'<div class="stat-box"><div class="stat-num">{failn}</div><div class="stat-label">Fail</div></div>'
        f'<div class="stat-box"><div class="stat-num">{errn}</div><div class="stat-label">Error</div></div>'
        f'<div class="stat-box"><div class="stat-num">{pass_rate:.1f}%</div><div class="stat-label">Pass rate</div></div>'
        f'<div class="stat-box"><div class="stat-num">${total_cost:.4f}</div><div class="stat-label">Total cost</div></div>'
        f'<div class="stat-box"><div class="stat-num">{overall_rate_per_min:.1f}/min</div><div class="stat-label">Rate</div></div>'
        "</div>"
    )

    recent = list(reversed(records))[:15]
    rows_html = []
    for r in recent:
        rows_html.append(
            "<tr>"
            f'<td class="mono">{html.escape(r.created_at or "-")}</td>'
            f"<td>{_verdict_pill(r.verdict.name)}</td>"
            f'<td class="mono">{html.escape(_config_summary(r.realized_dims))}</td>'
            "</tr>"
        )
    recent_table = (
        "<table><thead><tr><th>time</th><th>verdict</th><th>config</th></tr></thead>"
        "<tbody>" + "".join(rows_html) + "</tbody></table>"
    )

    is_active_run = active is not None and run_id == active["run_id"]
    stage_html = _stage_track_html(run_id, records)
    body = f"""
    <h1>Live: {safe_run_id}</h1>
    <p class="subtitle">Auto-refreshing monitor for an in-progress (or just-finished) campaign run.</p>
    <p class="meta"><a href="/run/{safe_run_id}">full run detail &rarr;</a></p>
    {stage_html}
    {bar_html}
    {_stop_button_html(is_active_run, stopping)}
    <h2>Tallies</h2>
    {stat_grid}
    <h2>Last 15 records (newest first)</h2>
    {recent_table}
    """
    return _shell(f"Live: {run_id}", "live", body, refresh=not complete)


def _stage_track_html(run_id: str, records: list[ExperimentRecord]) -> str:
    """For a full_map campaign (run_id or its records' run_id carries a
    "/<stage>" suffix), render a fixed step tracker: state (done) ->
    order -> belief -> ... so progress reads at a glance without parsing
    numbers (design-notes principle 24). Returns "" for non-full_map runs
    so it never appears for a plain state/belief/order campaign.
    """
    seen_stages = {
        _stage_from_run_id_local(r.run_id) for r in records if "/" in r.run_id
    }
    seen_stages.discard(None)
    if not seen_stages:
        return ""
    steps = []
    # the CURRENT stage is the last one with any records; everything
    # before it is "done", everything after is upcoming.
    reached = [s for s in _FULL_MAP_RUN_STAGES if s in seen_stages]
    current = reached[-1] if reached else None
    for i, stage in enumerate(_FULL_MAP_RUN_STAGES):
        cls = "done" if stage in seen_stages and stage != current else ""
        cls = "active" if stage == current else cls
        steps.append(f'<span class="stage-step {cls}"><span class="dot"></span>{html.escape(stage)}</span>')
        if i < len(_FULL_MAP_RUN_STAGES) - 1:
            steps.append('<span class="stage-arrow">&rarr;</span>')
    return f'<div class="stage-track">{"".join(steps)}</div>'


_FULL_MAP_RUN_STAGES = ["state", "order", "belief", "adversarial_belief", "breaking_point"]


_VERIFY_STATUS_LABEL = {
    "stable": "verified-stable",
    "noise": "noise",
    "unverified": "unverified",
}


def run_html(store: ResultsStore, run_id: str) -> str:
    records = store.by_run(run_id)
    safe_run_id = html.escape(run_id)

    # A full_map PARENT run_id has no records of its own -- every trial
    # lives under f"{run_id}/{stage}" child run_ids. Pull those in (labeled
    # by stage) so the table still shows every trial for a full_map launch.
    is_full_map_parent = False
    if not records:
        for stage in _FULL_MAP_RUN_STAGES:
            child = store.by_run(f"{run_id}/{stage}")
            if child:
                is_full_map_parent = True
                records.extend(child)

    breadcrumb = f'<a href="/runs">Runs</a><span class="sep">/</span>{safe_run_id}'

    if not records:
        body = (
            f"<h1>Run {safe_run_id}</h1>"
            f"{verdict_panel_html(store, run_id)}"
            '<div class="empty-state"><p>No records found.</p></div>'
        )
        return _shell(f"Run {run_id}", "runs", body, breadcrumb=breadcrumb)

    # Verification status per FAIL record_id, keyed by the run_id its
    # verify trials actually live under (f"{run_id}/verify" for a plain
    # run, or f"{run_id}/{stage}/verify" per stage for a full_map parent --
    # breaking_point is never auto-verified, see run_full_map_campaign).
    from ..exec.evaluator import classify_all_verified_failures

    verify_status_by_record: dict[str, str] = {}
    verify_source_run_ids = (
        [f"{run_id}/{s}" for s in _FULL_MAP_RUN_STAGES] if is_full_map_parent else [run_id]
    )
    for src in verify_source_run_ids:
        for record_id, fv in classify_all_verified_failures(store, src).items():
            verify_status_by_record[record_id] = fv.status

    rows_html = []
    for r in records:
        detail = _parse_verifier_detail(r.verifier_detail)
        kind = detail.get("kind", "-")
        # difficulty is not always encoded in verifier_detail; fall back to "-".
        difficulty = detail.get("difficulty", "-")
        traps = detail.get("traps", "")
        traps_html = f' <span class="meta mono">traps={html.escape(traps)}</span>' if traps else ""
        stage_html = ""
        if is_full_map_parent:
            stage = _stage_from_run_id_local(r.run_id)
            if stage:
                stage_html = f'<td class="mono">{html.escape(stage)}</td>'
        if r.verdict.name == "FAIL":
            status = verify_status_by_record.get(r.record_id, "unverified")
            verify_html = f'<td class="meta">{html.escape(_VERIFY_STATUS_LABEL[status])}</td>'
        else:
            verify_html = '<td class="meta">-</td>'
        rows_html.append(
            "<tr>"
            f'<td class="mono">{html.escape(r.model_name)}</td>'
            f"{stage_html}"
            f"<td>{_verdict_pill(r.verdict.name)}</td>"
            f"{verify_html}"
            f"<td>{html.escape(kind)}{traps_html}</td>"
            f"<td>{html.escape(difficulty)}</td>"
            f'<td class="mono">{r.cost_usd:.6f}</td>'
            f'<td class="mono">{r.tokens_in}/{r.tokens_out}</td>'
            f'<td><a href="/record/{html.escape(r.record_id)}">full transcript &rarr;</a></td>'
            "</tr>"
        )

    stage_header = "<th>stage</th>" if is_full_map_parent else ""
    table = (
        "<table><thead><tr><th>model</th>"
        f"{stage_header}"
        "<th>verdict</th><th>verified</th><th>kind</th>"
        "<th>difficulty</th><th class=\"num\">cost ($)</th><th class=\"num\">tokens in/out</th><th></th>"
        "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>"
    )

    n_unverified_fails = sum(
        1
        for r in records
        if r.verdict.name == "FAIL"
        and verify_status_by_record.get(r.record_id, "unverified") == "unverified"
        and (not is_full_map_parent or _stage_from_run_id_local(r.run_id) != "breaking_point")
    )
    verify_now_html = ""
    if n_unverified_fails:
        verify_now_html = f"""
        <details style="margin-top:1rem;">
          <summary>Verify {n_unverified_fails} failure(s) now
            <span class="meta">(re-tests each ~3x against the model; est. a few percent of the original run's cost)</span>
          </summary>
          <form method="post" action="/verify">
            <input type="hidden" name="run_id" value="{safe_run_id}">
            <p><label>Provider:
              <select name="provider">{launch_mod._provider_options()}</select></label></p>
            <p><label>API key:
              <input type="password" name="api_key" value="" autocomplete="off"></label>
              <span class="meta">leave empty to use env/ key file</span></p>
            <p><label>Model:
              <input name="model" value="{html.escape(records[0].model_name)}"></label></p>
            <p><button type="submit">Verify failures now</button></p>
          </form>
        </details>
        """

    body = (
        f"<h1>Run {safe_run_id}</h1>"
        '<p class="subtitle">What this run revealed, then every individual trial as evidence.</p>'
        f"{verdict_panel_html(store, run_id)}"
        f'<p class="meta" style="margin-top:1.25rem;"><a href="/live?run={safe_run_id}">watch live &rarr;</a> &middot; '
        f'<a href="/atlas">compare across models &rarr;</a></p>'
        f"{verify_now_html}"
        "<details><summary>Per-trial table "
        f'<span class="meta">({len(records)} trials -- prompt, reasoning, verdict, answer key)</span></summary>'
        f"{table}</details>"
    )
    return _shell(f"Run {run_id}", "runs", body, breadcrumb=breadcrumb)


def _stage_from_run_id_local(run_id: str) -> str | None:
    if "/" in run_id:
        return run_id.rsplit("/", 1)[-1]
    return None


def record_html(store: ResultsStore, record_id: str) -> str:
    cur = store._conn.execute("SELECT * FROM experiments WHERE record_id = ?", (record_id,))
    row = cur.fetchone()
    safe_record_id = html.escape(record_id)

    if row is None:
        body = f'<h1>Record {safe_record_id}</h1><div class="empty-state"><p>Not found.</p></div>'
        return _shell(f"Record {record_id}", "runs", body)

    from ..store.store import _row_to_record

    record: ExperimentRecord = _row_to_record(row)

    prompt_block = html.escape(record.prompt_text or "")
    response_block = html.escape(record.response_text or "")
    reasoning_block = html.escape(record.reasoning_text or "")

    reasoning_html = (
        "<details><summary>Model reasoning</summary>"
        f'<pre class="block mono">{reasoning_block}</pre></details>'
        if record.reasoning_text
        else "<p class=\"meta\">(no reasoning captured)</p>"
    )

    realized_rows = "".join(
        f'<tr><td class="mono">{html.escape(str(k))}</td><td class="mono">{html.escape(str(v))}</td></tr>'
        for k, v in sorted(record.realized_dims.items())
    )
    latent_rows = "".join(
        f'<tr><td class="mono">{html.escape(str(k))}</td><td class="mono">{html.escape(str(v))}</td></tr>'
        for k, v in sorted(record.latent_params.items())
    )

    lineage_links = ""
    try:
        chain = store.lineage(record.record_id)
        lineage_links = " &rarr; ".join(
            f'<a href="/record/{html.escape(rec.record_id)}">{html.escape(rec.record_id[:8])}</a>'
            for rec in chain
        )
    except KeyError:
        lineage_links = "(lineage unavailable)"

    detail = _parse_verifier_detail(record.verifier_detail)
    traps = detail.get("traps", "")
    trap_line = (
        f'<p class="meta">trap kind(s) this world was seeded for: <span class="mono">{html.escape(traps)}</span></p>'
        if traps
        else ""
    )

    # prev/next trial within the same run -- lateral navigation so a
    # dead-end detail page never strands the reader (design-notes principle 4).
    siblings = store.by_run(record.run_id)
    sib_ids = [s.record_id for s in siblings]
    prev_next_html = ""
    if record.record_id in sib_ids and len(sib_ids) > 1:
        idx = sib_ids.index(record.record_id)
        prev_id = sib_ids[idx - 1] if idx > 0 else None
        next_id = sib_ids[idx + 1] if idx < len(sib_ids) - 1 else None
        parts = []
        if prev_id:
            parts.append(f'<a href="/record/{html.escape(prev_id)}">&larr; prev trial</a>')
        parts.append(f'<span class="meta">trial {idx + 1} of {len(sib_ids)}</span>')
        if next_id:
            parts.append(f'<a href="/record/{html.escape(next_id)}">next trial &rarr;</a>')
        prev_next_html = f'<p class="meta" style="display:flex; gap:1rem; align-items:center;">{" &middot; ".join(parts)}</p>'

    breadcrumb = (
        f'<a href="/runs">Runs</a><span class="sep">/</span>'
        f'<a href="/run/{html.escape(record.run_id)}">{html.escape(record.run_id)}</a>'
        f'<span class="sep">/</span>{safe_record_id[:8]}'
    )

    body = f"""
    <h1>Record {safe_record_id}</h1>
    <p class="subtitle">Verdict and provenance first; raw prompt/response/reasoning below, collapsed.</p>

    <div class="panel">
      <p style="margin:0 0 0.5rem;">
        {_verdict_pill(record.verdict.name)}
        <span class="meta">&middot; model <span class="mono">{html.escape(record.model_name)}</span></span>
      </p>
      <p class="meta" style="margin:0;">
        finish_reason: <span class="mono">{html.escape(record.finish_reason or "-")}</span> &middot;
        tokens in/out: <span class="mono num">{record.tokens_in}/{record.tokens_out}</span> &middot;
        cost: <span class="mono num">${record.cost_usd:.6f}</span> &middot;
        latency: <span class="mono num">{record.latency_ms:.1f} ms</span>
      </p>
      <p class="meta">verifier_detail: <span class="mono">{html.escape(record.verifier_detail or "-")}</span></p>
      {trap_line}
      <p class="meta">lineage: {lineage_links}</p>
    </div>

    {prev_next_html}

    <h2>Prompt</h2>
    <pre class="block">{prompt_block}</pre>

    <h2>Response</h2>
    <pre class="block">{response_block}</pre>

    <h2>Reasoning</h2>
    {reasoning_html}

    <h2>Realized dims &amp; latent params</h2>
    <h3>Realized dims</h3>
    <table><thead><tr><th>dim</th><th>value</th></tr></thead><tbody>{realized_rows}</tbody></table>

    <h3>Latent params</h3>
    <table><thead><tr><th>param</th><th>value</th></tr></thead><tbody>{latent_rows}</tbody></table>
    """
    return _shell(f"Record {record_id}", "runs", body, breadcrumb=breadcrumb)


class _ViewerHandler(BaseHTTPRequestHandler):
    store: ResultsStore  # set on the class before serving
    launch_deps: launch_mod.LaunchDeps  # set on the class before serving

    def _send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 -- required BaseHTTPRequestHandler name
        path = urlsplit(self.path).path

        if path == "/":
            self._send_html(index_html(self.store))
            return

        if path == "/runs":
            self._send_html(runs_html(self.store))
            return

        if path == "/findings":
            self._send_html(findings_html())
            return

        if path == "/coverage":
            self._send_html(coverage_html())
            return

        if path == "/live":
            self._send_html(live_html(self.store))
            return

        if path == "/launch":
            body = launch_mod.handle_launch_get(self.launch_deps)
            self._send_html(_shell("Launch a campaign", "launch", body))
            return

        if path == "/atlas":
            self._send_html(atlas_html(self.store))
            return

        if path.startswith("/run/"):
            run_id = path[len("/run/"):]
            if run_id:
                self._send_html(run_html(self.store, run_id))
                return

        if path.startswith("/record/"):
            record_id = path[len("/record/"):]
            if record_id:
                self._send_html(record_html(self.store, record_id))
                return

        self._send_html(_shell("Not found", "", "<h1>404</h1><p>Not found.</p>"), status=404)

    def do_POST(self) -> None:  # noqa: N802 -- required BaseHTTPRequestHandler name
        path = urlsplit(self.path).path

        if path == "/stop":
            launch_mod.request_stop()
            self.send_response(303)
            self.send_header("Location", "/live")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/verify":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            form = {
                k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()
            }
            run_id = form.get("run_id", "")
            started, payload = launch_mod.handle_verify_now(
                run_id,
                form.get("provider", ""),
                form.get("api_key", ""),
                form.get("model", ""),
                self.launch_deps,
            )
            if started:
                self.send_response(303)
                self.send_header("Location", f"/run/{run_id}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_html(_shell("Verify failures", "runs", payload))
            return

        if path != "/launch":
            self._send_html(_shell("Not found", "", "<h1>404</h1><p>Not found.</p>"), status=404)
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        form = {
            k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()
        }

        step = form.get("step", "")
        if step == "models":
            body = launch_mod.handle_models_step(form, self.launch_deps)
            self._send_html(_shell("Launch a campaign", "launch", body))
            return
        if step == "start":
            started, payload = launch_mod.handle_start(form, self.launch_deps)
            if started:
                self.send_response(303)
                self.send_header("Location", "/live")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_html(_shell("Launch a campaign", "launch", payload))
            return

        self._send_html(
            _shell("Launch a campaign", "launch", launch_mod.error_html("unknown form step")),
            status=400,
        )

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def serve(db_path: str = _DEFAULT_DB_PATH, port: int = _DEFAULT_PORT) -> None:
    store = ResultsStore(db_path)
    handler_cls = type(
        "BoundViewerHandler",
        (_ViewerHandler,),
        {"store": store, "launch_deps": launch_mod.LaunchDeps(db_path=str(db_path))},
    )
    httpd = HTTPServer(("127.0.0.1", port), handler_cls)
    print(f"Serving results viewer for {db_path!r} at http://127.0.0.1:{port}/ (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_DB_PATH
    port = int(sys.argv[2]) if len(sys.argv) > 2 else _DEFAULT_PORT

    # Resolve relative paths against the repo root, not the caller's cwd,
    # so `modelcart` works the same from anywhere on disk.
    resolved = Path(db_path)
    if not resolved.is_absolute() and not resolved.exists():
        candidate = _REPO_ROOT / db_path
        if candidate.exists():
            resolved = candidate
    db_path = str(resolved)

    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    serve(db_path, port)


if __name__ == "__main__":
    main()
