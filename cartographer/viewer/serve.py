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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from ..store.schema import ExperimentRecord
from ..store.store import ResultsStore
from . import launch as launch_mod
from .atlas import atlas_html

_DEFAULT_DB_PATH = "results/pilot.sqlite"
_DEFAULT_PORT = 8788

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

_NAV_ITEMS = [
    ("runs", "/", "Runs"),
    ("live", "/live", "Live"),
    ("atlas", "/atlas", "Atlas"),
    ("launch", "/launch", "Launch"),
]

_CSS = """
:root {
  --bg: #111418;
  --bg-raised: #171b21;
  --text: #E8EAED;
  --text-muted: #9aa4b2;
  --border: #2A2F36;
  --accent: #2DD4BF;
  --pass: #34D399;
  --fail: #F87171;
  --error: #FBBF24;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

header.shell-header {
  display: flex; align-items: center; gap: 1.5rem;
  padding: 0.9rem 1.5rem;
  border-bottom: 1px solid var(--border);
  background: var(--bg-raised);
}
header.shell-header .brand {
  font-weight: 700; font-size: 1.05rem; color: var(--text);
}
header.shell-header .brand:hover { text-decoration: none; color: var(--accent); }
nav.shell-nav { display: flex; gap: 1.1rem; }
nav.shell-nav a { color: var(--text-muted); font-size: 0.92rem; padding: 0.2rem 0; }
nav.shell-nav a.active { color: var(--accent); border-bottom: 2px solid var(--accent); }

main.shell-main { padding: 1.5rem 1.75rem 3rem; max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 1rem; }
h2 { font-size: 1.05rem; margin: 1.75rem 0 0.75rem; color: var(--text); }

.meta { color: var(--text-muted); font-size: 0.88rem; }
.mono { font-family: var(--mono); }

table {
  border-collapse: collapse;
  width: 100%;
  margin: 0.75rem 0 1.25rem;
  font-size: 13px;
}
th, td { padding: 0.55rem 0.75rem; text-align: left; border-bottom: 1px solid var(--border); }
th {
  color: var(--text-muted);
  text-transform: uppercase;
  font-size: 11px;
  letter-spacing: 0.03em;
  font-weight: 600;
  border-bottom: 1px solid var(--border);
}
td.mono, td .mono { font-family: var(--mono); }

.pill {
  display: inline-block;
  font-family: var(--sans);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
  padding: 0.15rem 0.55rem;
  border-radius: 999px;
  color: #0b0d10;
}
.pill-PASS { background: var(--pass); }
.pill-FAIL { background: var(--fail); }
.pill-ERROR { background: var(--error); }
.pill-REJECTED_BY_FIDELITY_GATE { background: var(--text-muted); }

.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 0.75rem;
  margin: 0.75rem 0 1.25rem;
}
.stat-box {
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.9rem 1rem;
}
.stat-box .stat-num { font-family: var(--mono); font-size: 1.5rem; font-weight: 600; }
.stat-box .stat-label { color: var(--text-muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; margin-top: 0.2rem; }

.progress-track {
  background: var(--border);
  border-radius: 999px;
  height: 8px;
  width: 100%;
  overflow: hidden;
}
.progress-fill { background: var(--accent); height: 100%; border-radius: 999px; }
.progress-fill.complete { background: var(--pass); }
.progress-row { display: flex; align-items: center; gap: 0.9rem; margin: 0.4rem 0 0.25rem; }
.progress-row .progress-track { flex: 1; }
.progress-text { font-size: 0.85rem; color: var(--text-muted); white-space: nowrap; }

pre.block {
  white-space: pre-wrap; word-wrap: break-word;
  background: var(--bg-raised);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.85rem;
  font-family: var(--mono);
  font-size: 0.85rem;
}
details summary { cursor: pointer; font-weight: 600; margin: 0.5rem 0; color: var(--text); }
details[open] summary { color: var(--accent); }

form p { margin: 0.6rem 0; }
input, select, button {
  font-family: var(--sans);
  background: var(--bg-raised);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.4rem 0.55rem;
}
button {
  background: var(--accent);
  color: #0b0d10;
  font-weight: 600;
  cursor: pointer;
  border: none;
}
button:hover { opacity: 0.9; }
"""

_VERDICT_CLASS_FALLBACK = "meta"


def _shell(title: str, active_nav: str, body: str, refresh: bool = False) -> str:
    """Render `body` inside the shared header/nav/style shell.

    `active_nav` is one of the keys in `_NAV_ITEMS` ("runs", "live",
    "atlas", "launch") or "" for none highlighted.
    """
    refresh_tag = '<meta http-equiv="refresh" content="5">' if refresh else ""
    nav_links = "".join(
        f'<a href="{href}"{" class=\"active\"" if key == active_nav else ""}>{label}</a>'
        for key, href, label in _NAV_ITEMS
    )
    header = (
        '<header class="shell-header">'
        '<a class="brand" href="/">Model Cartographer</a>'
        f'<nav class="shell-nav">{nav_links}</nav>'
        "</header>"
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"{refresh_tag}"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{header}<main class=\"shell-main\">{body}</main></body></html>"
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


def index_html(store: ResultsStore) -> str:
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
        failn = counts.get("FAIL", 0) + counts.get("ERROR", 0)
        pass_rate = (passn / total * 100.0) if total else 0.0
        newest = None
        if agg["created_ats"]:
            newest = _parse_created_at(max(agg["created_ats"]))
        is_live = run_id == active_run_id or (
            newest is not None and _age_seconds(newest) <= _COMPLETE_AFTER_S
        )
        status = "live" if is_live else "complete"
        detail = _parse_verifier_detail(None)  # placeholder for kind lookup below
        kind = "-"
        safe_run_id = html.escape(run_id)
        model_short = html.escape(_short_model(agg["model"]))
        rows_html.append(
            "<tr>"
            f'<td class="mono"><a href="/run/{safe_run_id}">{safe_run_id}</a></td>'
            f'<td class="mono">{model_short}</td>'
            f"<td>{total}</td>"
            f'<td><span class="pill pill-PASS">{pass_rate:.0f}%</span></td>'
            f'<td class="mono">${agg["cost"]:.4f}</td>'
            f"<td>{html.escape(_relative_time(newest))}</td>"
            f"<td>{status}</td>"
            "</tr>"
        )

    table = (
        "<table><thead><tr><th>run_id</th><th>model</th><th>records</th>"
        "<th>pass rate</th><th>cost</th><th>started</th><th>status</th>"
        "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>"
    )
    body = f"<h1>Runs</h1>{table}"
    return _shell("Model Cartographer results", "runs", body)


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
            body = "<h1>Live campaign monitor</h1><p>No records found.</p>"
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
    body = f"""
    <h1>Live: {safe_run_id}</h1>
    <p><a href="/run/{safe_run_id}">full run detail &rarr;</a></p>
    {bar_html}
    {_stop_button_html(is_active_run, stopping)}
    <h2>Tallies</h2>
    {stat_grid}
    <h2>Last 15 records (newest first)</h2>
    {recent_table}
    """
    return _shell(f"Live: {run_id}", "live", body, refresh=not complete)


def run_html(store: ResultsStore, run_id: str) -> str:
    records = store.by_run(run_id)
    safe_run_id = html.escape(run_id)

    if not records:
        body = f"<h1>Run {safe_run_id}</h1><p>No records found.</p>"
        return _shell(f"Run {run_id}", "runs", body)

    rows_html = []
    for r in records:
        detail = _parse_verifier_detail(r.verifier_detail)
        kind = detail.get("kind", "-")
        # difficulty is not always encoded in verifier_detail; fall back to "-".
        difficulty = detail.get("difficulty", "-")
        rows_html.append(
            "<tr>"
            f'<td class="mono">{html.escape(r.model_name)}</td>'
            f"<td>{_verdict_pill(r.verdict.name)}</td>"
            f"<td>{html.escape(kind)}</td>"
            f"<td>{html.escape(difficulty)}</td>"
            f'<td class="mono">{r.cost_usd:.6f}</td>'
            f'<td class="mono">{r.tokens_in}/{r.tokens_out}</td>'
            f'<td><a href="/record/{html.escape(r.record_id)}">detail</a></td>'
            "</tr>"
        )

    table = (
        "<table><thead><tr><th>model</th><th>verdict</th><th>kind</th>"
        "<th>difficulty</th><th>cost ($)</th><th>tokens in/out</th><th></th>"
        "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>"
    )
    body = f"<h1>Run {safe_run_id}</h1>{table}"
    return _shell(f"Run {run_id}", "runs", body)


def record_html(store: ResultsStore, record_id: str) -> str:
    cur = store._conn.execute("SELECT * FROM experiments WHERE record_id = ?", (record_id,))
    row = cur.fetchone()
    safe_record_id = html.escape(record_id)

    if row is None:
        body = f"<h1>Record {safe_record_id}</h1><p>Not found.</p>"
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

    body = f"""
    <p><a href="/run/{html.escape(record.run_id)}">&larr; run {html.escape(record.run_id)}</a></p>
    <h1>Record {safe_record_id}</h1>
    <p class="meta">
      model: <span class="mono">{html.escape(record.model_name)}</span> &middot;
      verdict: {_verdict_pill(record.verdict.name)} &middot;
      finish_reason: {html.escape(record.finish_reason or "-")} &middot;
      tokens in/out: <span class="mono">{record.tokens_in}/{record.tokens_out}</span> &middot;
      cost: <span class="mono">${record.cost_usd:.6f}</span> &middot;
      latency: <span class="mono">{record.latency_ms:.1f} ms</span>
    </p>
    <p class="meta">verifier_detail: <span class="mono">{html.escape(record.verifier_detail or "-")}</span></p>
    <p class="meta">lineage: {lineage_links}</p>

    <h2>Prompt</h2>
    <pre class="block">{prompt_block}</pre>

    <h2>Response</h2>
    <pre class="block">{response_block}</pre>

    <h2>Reasoning</h2>
    {reasoning_html}

    <h2>Realized dims</h2>
    <table><thead><tr><th>dim</th><th>value</th></tr></thead><tbody>{realized_rows}</tbody></table>

    <h2>Latent params</h2>
    <table><thead><tr><th>param</th><th>value</th></tr></thead><tbody>{latent_rows}</tbody></table>
    """
    return _shell(f"Record {record_id}", "runs", body)


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
    serve(db_path, port)


if __name__ == "__main__":
    main()
