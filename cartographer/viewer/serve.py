"""Local read-only HTML viewer over a `ResultsStore` sqlite db.

stdlib-only `http.server` app. Binds to 127.0.0.1 only. No write endpoints
of any kind -- every route only reads from the store.

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
# to "?" (bar hidden, raw count still shown).
_KNOWN_RUN_BUDGETS = {
    "camp-state-": 160,
    "camp-belief-": 700,
}

_CSS = """
body { font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2rem; color: #1a1a1a; background: #fafafa; }
h1, h2 { margin-top: 0; }
a { color: #2554a8; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; background: #fff; }
th, td { border: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.9rem; }
th { background: #f0f0f0; }
.verdict-PASS { color: #157a15; font-weight: bold; }
.verdict-FAIL { color: #b30000; font-weight: bold; }
.verdict-ERROR { color: #b8860b; font-weight: bold; }
.verdict-REJECTED_BY_FIDELITY_GATE { color: #666; font-weight: bold; }
pre.block { white-space: pre-wrap; word-wrap: break-word; background: #fff; border: 1px solid #ddd;
            padding: 0.75rem; font-family: "SF Mono", Consolas, monospace; font-size: 0.85rem; }
details summary { cursor: pointer; font-weight: bold; margin: 0.5rem 0; }
.meta { color: #555; font-size: 0.9rem; }
nav { margin-bottom: 1rem; }
"""

_VERDICT_CLASS_FALLBACK = "meta"


def _page(title: str, body: str, refresh: bool = False) -> str:
    refresh_tag = '<meta http-equiv="refresh" content="5">' if refresh else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"{refresh_tag}"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def _verdict_span(verdict_name: str) -> str:
    safe = html.escape(verdict_name)
    css_class = f"verdict-{safe}"
    return f'<span class="{css_class}">{safe}</span>'


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


def index_html(store: ResultsStore) -> str:
    cur = store._conn.execute(
        "SELECT run_id, verdict, COUNT(*) as n FROM experiments GROUP BY run_id, verdict"
    )
    by_run: dict[str, dict[str, int]] = {}
    for row in cur.fetchall():
        by_run.setdefault(row["run_id"], {}).setdefault(row["verdict"], 0)
        by_run[row["run_id"]][row["verdict"]] += row["n"]

    rows_html = []
    for run_id in sorted(by_run.keys()):
        counts = by_run[run_id]
        total = sum(counts.values())
        passn = counts.get("PASS", 0)
        failn = counts.get("FAIL", 0)
        errn = counts.get("ERROR", 0)
        safe_run_id = html.escape(run_id)
        rows_html.append(
            "<tr>"
            f'<td><a href="/run/{safe_run_id}">{safe_run_id}</a></td>'
            f"<td>{total}</td>"
            f'<td class="verdict-PASS">{passn}</td>'
            f'<td class="verdict-FAIL">{failn}</td>'
            f'<td class="verdict-ERROR">{errn}</td>'
            "</tr>"
        )

    table = (
        "<table><thead><tr><th>run_id</th><th>records</th><th>pass</th>"
        "<th>fail</th><th>error</th></tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>"
    )
    body = (
        "<h1>Model Cartographer -- results viewer</h1>"
        '<p><a href="/live">Live campaign monitor</a> &middot; '
        '<a href="/atlas">Cross-model atlas</a> &middot; '
        '<a href="/launch">Launch a campaign</a></p>'
        f"{table}"
    )
    return _page("Model Cartographer results", body)


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


def _now_utc() -> datetime:
    """Split out so tests can monkeypatch 'now'."""
    return datetime.now(timezone.utc)


def _age_seconds(newest: datetime) -> float:
    if newest.tzinfo is None:
        # created_at strings without an offset are treated as UTC (that's
        # how ExperimentRecord stamps them).
        newest = newest.replace(tzinfo=timezone.utc)
    return (_now_utc() - newest).total_seconds()


def live_html(store: ResultsStore) -> str:
    """Live monitor for the most-recently-created campaign run (by max
    `created_at` across all records) -- progress bar, tallies, and the last
    15 records newest-first. Pure function over the store; auto-refreshes
    via a `<meta http-equiv="refresh">` tag so no JS is needed.

    If the newest record is older than `_COMPLETE_AFTER_S` (3 minutes), the
    run is rendered as complete: full-width green bar, "RUN COMPLETE --
    finished at N calls", no ETA, tallies kept.
    """
    cur = store._conn.execute(
        "SELECT run_id FROM experiments ORDER BY created_at DESC, record_id DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        body = (
            '<nav><a href="/">&larr; runs</a></nav>'
            "<h1>Live campaign monitor</h1><p>No records found.</p>"
        )
        return _page("Live campaign monitor", body, refresh=True)

    run_id = row["run_id"]
    safe_run_id = html.escape(run_id)
    records = store.by_run(run_id)

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
    rate_per_min = (total / (elapsed_s / 60.0)) if elapsed_s > 0 else 0.0

    complete = bool(timestamps) and _age_seconds(max(timestamps)) > _COMPLETE_AFTER_S

    budget = _budget_for_run(run_id)
    if complete:
        pct_str = ""
        if budget is not None and budget > 0:
            pct_str = f" ({min(total / budget * 100.0, 100.0):.1f}% of {budget} budget)"
        bar_html = (
            '<div style="border:1px solid #157a15;background:#eee;width:100%;height:1.5rem;'
            'border-radius:4px;overflow:hidden;">'
            '<div style="background:#157a15;height:100%;width:100%;"></div></div>'
            f'<p class="meta"><strong>RUN COMPLETE</strong> &mdash; finished at '
            f"{total} calls{pct_str}</p>"
        )
    elif budget is not None and budget > 0:
        pct = min(total / budget * 100.0, 100.0)
        remaining = max(budget - total, 0)
        eta_min = (remaining / rate_per_min) if rate_per_min > 0 else None
        eta_str = f"{eta_min:.1f} min" if eta_min is not None else "?"
        bar_html = (
            f'<div style="border:1px solid #999;background:#eee;width:100%;height:1.5rem;'
            f'border-radius:4px;overflow:hidden;">'
            f'<div style="background:#2554a8;height:100%;width:{pct:.1f}%;"></div></div>'
            f'<p class="meta">{total} / {budget} records ({pct:.1f}%) &middot; ETA: {eta_str}</p>'
        )
    else:
        bar_html = f'<p class="meta">{total} records (budget unknown for this run_id)</p>'

    tallies_html = (
        "<ul>"
        f"<li>PASS: {passn}</li>"
        f"<li>FAIL: {failn}</li>"
        f"<li>ERROR: {errn}</li>"
        f"<li>Pass rate: {pass_rate:.1f}%</li>"
        f"<li>Total cost: ${total_cost:.6f}</li>"
        f"<li>Elapsed: {elapsed_s:.1f} s</li>"
        f"<li>Rate: {rate_per_min:.2f} records/min</li>"
        "</ul>"
    )

    recent = list(reversed(records))[:15]
    rows_html = []
    for r in recent:
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(r.created_at or '-')}</td>"
            f"<td>{_verdict_span(r.verdict.name)}</td>"
            f"<td>{html.escape(_config_summary(r.realized_dims))}</td>"
            "</tr>"
        )
    recent_table = (
        "<table><thead><tr><th>time</th><th>verdict</th><th>config</th></tr></thead>"
        "<tbody>" + "".join(rows_html) + "</tbody></table>"
    )

    body = f"""
    <nav><a href="/">&larr; runs</a> | <a href="/run/{safe_run_id}">full run detail</a></nav>
    <h1>Live: {safe_run_id}</h1>
    {bar_html}
    <h2>Tallies</h2>
    {tallies_html}
    <h2>Last 15 records (newest first)</h2>
    {recent_table}
    """
    return _page(f"Live: {run_id}", body, refresh=not complete)


def run_html(store: ResultsStore, run_id: str) -> str:
    records = store.by_run(run_id)
    safe_run_id = html.escape(run_id)

    if not records:
        body = f'<nav><a href="/">&larr; runs</a></nav><h1>Run {safe_run_id}</h1><p>No records found.</p>'
        return _page(f"Run {run_id}", body)

    rows_html = []
    for r in records:
        detail = _parse_verifier_detail(r.verifier_detail)
        kind = detail.get("kind", "-")
        # difficulty is not always encoded in verifier_detail; fall back to "-".
        difficulty = detail.get("difficulty", "-")
        rows_html.append(
            "<tr>"
            f"<td>{html.escape(r.model_name)}</td>"
            f"<td>{_verdict_span(r.verdict.name)}</td>"
            f"<td>{html.escape(kind)}</td>"
            f"<td>{html.escape(difficulty)}</td>"
            f"<td>{r.cost_usd:.6f}</td>"
            f"<td>{r.tokens_in}/{r.tokens_out}</td>"
            f'<td><a href="/record/{html.escape(r.record_id)}">detail</a></td>'
            "</tr>"
        )

    table = (
        "<table><thead><tr><th>model</th><th>verdict</th><th>kind</th>"
        "<th>difficulty</th><th>cost ($)</th><th>tokens in/out</th><th></th>"
        "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>"
    )
    body = f'<nav><a href="/">&larr; runs</a></nav><h1>Run {safe_run_id}</h1>{table}'
    return _page(f"Run {run_id}", body)


def record_html(store: ResultsStore, record_id: str) -> str:
    cur = store._conn.execute("SELECT * FROM experiments WHERE record_id = ?", (record_id,))
    row = cur.fetchone()
    safe_record_id = html.escape(record_id)

    if row is None:
        body = f'<nav><a href="/">&larr; runs</a></nav><h1>Record {safe_record_id}</h1><p>Not found.</p>'
        return _page(f"Record {record_id}", body)

    from ..store.store import _row_to_record

    record: ExperimentRecord = _row_to_record(row)

    prompt_block = html.escape(record.prompt_text or "")
    response_block = html.escape(record.response_text or "")
    reasoning_block = html.escape(record.reasoning_text or "")

    reasoning_html = (
        "<details><summary>Model reasoning</summary>"
        f'<pre class="block">{reasoning_block}</pre></details>'
        if record.reasoning_text
        else "<p class=\"meta\">(no reasoning captured)</p>"
    )

    realized_rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"
        for k, v in sorted(record.realized_dims.items())
    )
    latent_rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"
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
    <nav><a href="/">&larr; runs</a> | <a href="/run/{html.escape(record.run_id)}">&larr; run {html.escape(record.run_id)}</a></nav>
    <h1>Record {safe_record_id}</h1>
    <p class="meta">
      model: {html.escape(record.model_name)} &middot;
      verdict: {_verdict_span(record.verdict.name)} &middot;
      finish_reason: {html.escape(record.finish_reason or "-")} &middot;
      tokens in/out: {record.tokens_in}/{record.tokens_out} &middot;
      cost: ${record.cost_usd:.6f} &middot;
      latency: {record.latency_ms:.1f} ms
    </p>
    <p class="meta">verifier_detail: {html.escape(record.verifier_detail or "-")}</p>
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
    return _page(f"Record {record_id}", body)


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
            self._send_html(_page("Launch a campaign", body))
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

        self._send_html(_page("Not found", "<h1>404</h1><p>Not found.</p>"), status=404)

    def do_POST(self) -> None:  # noqa: N802 -- required BaseHTTPRequestHandler name
        path = urlsplit(self.path).path
        if path != "/launch":
            self._send_html(_page("Not found", "<h1>404</h1><p>Not found.</p>"), status=404)
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        form = {
            k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()
        }

        step = form.get("step", "")
        if step == "models":
            body = launch_mod.handle_models_step(form, self.launch_deps)
            self._send_html(_page("Launch a campaign", body))
            return
        if step == "start":
            started, payload = launch_mod.handle_start(form, self.launch_deps)
            if started:
                self.send_response(303)
                self.send_header("Location", "/live")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_html(_page("Launch a campaign", payload))
            return

        self._send_html(
            _page("Launch a campaign", launch_mod.error_html("unknown form step")),
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
