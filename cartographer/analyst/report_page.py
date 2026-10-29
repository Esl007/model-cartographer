"""The /report viewer page -- renders the latest generated capability report.

The report itself is produced offline by `scripts/write_report.py`, which
writes `docs/capability-report.md` (the human artifact) and a JSON sidecar
`docs/capability-report.json` (the structured source this page reads). The
page renders, in order:

  * the diagnosis (executive summary + per-model + cross-model + next probes),
  * the audit flags -- PROMINENT if any figure was unsupported,
  * the literature corroborations with linked sources,
  * a "how this was made" provenance footer.

Empty state: "No report yet -- run scripts/write_report.py". Everything is
built with the existing design system (.card/.card-head, .status-tag chips,
the claim strip vocabulary) so /report looks like the rest of the viewer.

This module reads files only; it never calls a model or the store. It is
imported by the viewer purely to render, keeping the analyst package's LLM
code out of the request path.
"""

from __future__ import annotations

import html
import re
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SIDECAR = _REPO_ROOT / "docs" / "capability-report.json"

_RELATION_TAG = {
    "corroborates": ("confirmed", "corroborates"),
    "contradicts": ("retracted", "contradicts"),
    "no_clear_signal": ("needs-replication", "no clear signal"),
}


def load_report(sidecar_path: str | Path | None = None) -> dict | None:
    """Load the JSON sidecar, or None if no report has been generated."""
    path = Path(sidecar_path) if sidecar_path else _DEFAULT_SIDECAR
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _inline_md(line: str) -> str:
    """Escape, then render the two inline marks the analyst writes:
    **bold** and `code`."""
    out = html.escape(line)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r'<code class="mono">\1</code>', out)
    return out


def _prose_html(text: str) -> str:
    """Render analyst prose: paragraphs, `#`-headings, `-`/`*` bullet
    lists, **bold** and `code`. Everything is escaped first; nothing else
    the analyst writes is interpreted."""
    text = (text or "").strip()
    if not text:
        return ""
    parts: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush() -> None:
        if para:
            parts.append(f'<p class="prose-p">{"<br>".join(para)}</p>')
            para.clear()
        if items:
            parts.append('<ul class="prose-list">' + "".join(f"<li>{i}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        bullet = re.match(r"^[-*\u2022]\s+(.*)$", line)
        if heading:
            flush()
            parts.append(f'<h3 class="prose-h">{_inline_md(heading.group(2))}</h3>')
        elif bullet:
            if para:
                flush()
            items.append(_inline_md(bullet.group(1)))
        elif re.fullmatch(r"\*\*[^*]+\*\*:?", line):  # a bold-only line is a subheading
            flush()
            parts.append(f'<div class="prose-sub">{_inline_md(line)}</div>')
        else:
            if items:
                flush()
            para.append(_inline_md(line))
    flush()
    return "".join(parts)


def _diagnosis_card(report: dict) -> str:
    diag = report.get("diagnosis", {})
    model = diag.get("model", "unknown")
    sections = diag.get("sections", [])
    if not sections and diag.get("raw_text"):
        sections = [["Diagnosis", diag["raw_text"]]]

    section_html = []
    for title, body in sections:
        section_html.append(
            '<div class="card" style="margin-top:var(--sp-3);">'
            '<div class="card-head"><span class="card-title">'
            f"{html.escape(title)}</span></div>"
            f'<div class="card-body">{_prose_html(body)}</div></div>'
        )
    if not section_html:
        section_html.append(
            '<div class="card-body"><p class="meta">The analyst returned no '
            "diagnosis text.</p></div>"
        )

    return (
        '<div class="card">'
        '<div class="card-head"><span class="card-title">Capability diagnosis</span>'
        f'<span class="card-meta" title="The model that wrote this diagnosis, not a model under test">'
        f'written by <span class="chip chip-muted">{html.escape(model)}</span></span>'
        "</div>"
        '<div class="card-body">'
        '<p class="meta" style="margin-top:0;">Written by the analyst model, '
        "audited deterministically. Every number below traces to the "
        "deterministic evidence pack; unsupported figures are flagged in the "
        "next card.</p>"
        f"{''.join(section_html)}"
        "</div></div>"
    )


def _audit_card(report: dict) -> str:
    flags = report.get("audit_flags", [])
    n = len(flags)
    if n == 0:
        return (
            '<div class="card">'
            '<div class="card-head"><span class="card-title">Contamination audit</span>'
            '<span class="card-meta"><span class="status-tag confirmed">clean</span></span>'
            "</div>"
            '<div class="card-body"><p style="margin:0;">'
            "Every numeric figure in the diagnosis matched a value present in "
            "the evidence pack (at the pack&rsquo;s own rounding precision). No "
            "invented figures.</p>"
            '<p class="meta" style="margin:0.4rem 0 0;">This is the honesty '
            "check: the analyst narrates, but no number it writes becomes a "
            "claim unless the deterministic store already contains it.</p>"
            "</div></div>"
        )

    rows = []
    for fl in flags:
        figure = html.escape(str(fl.get("figure", "")))
        kind = html.escape(str(fl.get("kind", "")))
        context = html.escape(str(fl.get("context", "")))
        rows.append(
            "<tr>"
            f'<td><span class="status-tag retracted">unverified figure</span></td>'
            f'<td class="mono">{figure}</td>'
            f"<td>{kind}</td>"
            f'<td class="meta">&hellip;{context}&hellip;</td>'
            "</tr>"
        )
    return (
        '<div class="card">'
        '<div class="card-head"><span class="card-title">Contamination audit</span>'
        f'<span class="card-meta"><span class="status-tag retracted">'
        f"{n} unverified figure{'s' if n != 1 else ''}</span></span>"
        "</div>"
        '<div class="card-body">'
        '<p style="margin:0 0 var(--sp-3);">The deterministic audit found '
        f"{n} figure{'s' if n != 1 else ''} in the diagnosis that "
        "do not appear in the evidence pack. These are shown as written, "
        "flagged, and must not be trusted as measurements.</p>"
        "<table><thead><tr><th></th><th>figure</th><th>kind</th>"
        "<th>where it appears</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "</div></div>"
    )


def _corroboration_card(report: dict) -> str:
    corr = report.get("corroborations")
    lit = report.get("literature", {})
    if not corr:
        note = lit.get("note") or "literature check not run (no Tavily key)"
        return (
            '<div class="card">'
            '<div class="card-head"><span class="card-title">Literature corroboration</span>'
            '<span class="card-meta"><span class="status-tag verif-unverified">not run</span></span>'
            "</div>"
            f'<div class="card-body"><p style="margin:0;">{html.escape(note)}</p>'
            '<p class="meta" style="margin:0.4rem 0 0;">Add a Tavily key to '
            "cross-check each confirmed finding against public research; the "
            "diagnosis above stands on the measured store regardless.</p>"
            "</div></div>"
        )

    blocks = []
    for c in corr:
        relation = c.get("relation", "no_clear_signal")
        tag_cls, tag_label = _RELATION_TAG.get(relation, ("needs-replication", relation))
        sources = []
        for r in c.get("results", [])[:5]:
            url = r.get("url", "")
            title = r.get("title", "") or url
            snippet = html.escape((r.get("content", "") or "")[:220])
            if url:
                link = f'<a href="{html.escape(url)}" rel="nofollow noopener">{html.escape(title)}</a>'
            else:
                link = html.escape(title)
            sources.append(
                f'<li style="margin-bottom:var(--sp-2);">{link}'
                f'<br><span class="meta">{snippet}</span></li>'
            )
        sources_html = (
            f"<ul style=\"margin:var(--sp-2) 0 0;\">{''.join(sources)}</ul>"
            if sources
            else '<p class="meta" style="margin:var(--sp-2) 0 0;">No sources returned.</p>'
        )
        queries = ", ".join(html.escape(q) for q in c.get("queries", []))
        rationale = html.escape(c.get("rationale", ""))
        blocks.append(
            '<div class="card" style="margin-top:var(--sp-3);">'
            '<div class="card-head">'
            f'<span class="card-title card-title-name">{html.escape(c.get("finding_name",""))}</span>'
            f'<span class="card-meta"><span class="status-tag {tag_cls}">{html.escape(tag_label)}</span></span>'
            "</div>"
            '<div class="card-body">'
            f'<p class="meta" style="margin:0;">queries: {queries}</p>'
            + (f'<p style="margin:0.3rem 0 0;">{rationale}</p>' if rationale else "")
            + sources_html
            + "</div></div>"
        )

    return (
        '<div class="card">'
        '<div class="card-head"><span class="card-title">Literature corroboration</span>'
        f'<span class="card-meta">{len(corr)} finding(s) checked via Tavily</span>'
        "</div>"
        '<div class="card-body">'
        '<p class="meta" style="margin-top:0;">Each confirmed / needs-'
        "replication finding, searched against public research. The relation "
        "is the analyst model reading the snippets; the sources are kept so "
        "you can check the call yourself. This is corroboration only -- "
        "nothing here is a measurement.</p>"
        f"{''.join(blocks)}"
        "</div></div>"
    )


def _provenance_card(report: dict) -> str:
    meta = report.get("meta", {})
    generated = html.escape(str(meta.get("generated_at", "unknown")))
    analyst_model = html.escape(str(meta.get("analyst_model", "unknown")))
    totals = report.get("store_totals", {})
    n_trials = totals.get("n_trials", "?")
    n_scored = totals.get("n_scored", "?")
    spend = totals.get("spend_usd", "?")
    if isinstance(n_trials, int) and isinstance(n_scored, int):
        base_line = (
            f"<li>Evidence base: {n_trials:,} trials ({n_scored:,} scored), "
            f"${spend} spend. Generated {generated}.</li>"
        )
    else:
        base_line = (
            f"<li>Evidence base: {html.escape(str(n_trials))} trials "
            f"({html.escape(str(n_scored))} scored), ${html.escape(str(spend))} "
            f"spend. Generated {generated}.</li>"
        )
    return (
        '<div class="card">'
        '<div class="card-head"><span class="card-title">How this was made</span></div>'
        '<div class="card-body">'
        "<p style=\"margin:0 0 0.5rem;\">Diagnosis written by "
        f"<b>{analyst_model}</b>, audited deterministically; all numbers trace "
        "to the results store.</p>"
        '<ul style="margin:0;">'
        "<li>Numbers come from a deterministic evidence pack computed straight "
        "from the store under one arithmetic contract (scored trials only; "
        "infra errors and resample-noise failures excluded).</li>"
        "<li>The analyst model only narrates and interprets. A deterministic "
        "audit re-checks every figure it wrote against the pack.</li>"
        "<li>The literature layer only corroborates against public sources; "
        "no LLM output is ever written back as a measurement.</li>"
        f"{base_line}"
        "</ul></div></div>"
    )


def report_body(report: dict | None) -> str:
    """The inner HTML for /report (without the shell)."""
    if report is None:
        return (
            '<div class="page-head"><div class="page-head-main">'
            "<h1>Capability report</h1>"
            '<p class="subtitle">The analyst&rsquo;s capability diagnosis, '
            "audited against the measured store.</p></div></div>"
            '<div class="empty-state"><p>No report yet.</p>'
            '<p class="meta">Run <span class="mono">scripts/write_report.py</span> '
            "to generate one.</p></div>"
        )

    meta = report.get("meta", {})
    generated = html.escape(str(meta.get("generated_at", "")))
    n_flags = len(report.get("audit_flags", []))
    flag_pill = (
        f'<span class="status-tag retracted">{n_flags} unverified figure'
        f"{'s' if n_flags != 1 else ''}</span>"
        if n_flags
        else '<span class="status-tag confirmed">audit clean</span>'
    )
    head = (
        '<div class="page-head"><div class="page-head-main">'
        "<h1>Capability report</h1>"
        '<p class="subtitle">The analyst&rsquo;s capability diagnosis, '
        "audited against the measured store, cross-checked against public "
        "literature.</p>"
        f'<p class="meta">generated {generated} &middot; {flag_pill}</p>'
        "</div></div>"
    )
    return (
        head
        + _diagnosis_card(report)
        + _audit_card(report)
        + _corroboration_card(report)
        + _provenance_card(report)
    )
