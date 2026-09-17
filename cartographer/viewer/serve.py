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
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

from ..store.schema import ExperimentRecord
from ..store.store import ResultsStore

_DEFAULT_DB_PATH = "results/pilot.sqlite"
_DEFAULT_PORT = 8788

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


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
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
    body = f"<h1>Model Cartographer -- results viewer</h1>{table}"
    return _page("Model Cartographer results", body)


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

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def serve(db_path: str = _DEFAULT_DB_PATH, port: int = _DEFAULT_PORT) -> None:
    store = ResultsStore(db_path)
    handler_cls = type("BoundViewerHandler", (_ViewerHandler,), {"store": store})
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
