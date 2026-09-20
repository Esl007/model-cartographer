"""Generate the capability report: evidence pack -> analyst -> audit ->
(optional) Tavily corroboration -> docs/capability-report.md + JSON sidecar.

Main-guarded plain script. Opens `results/campaign.sqlite` READ-ONLY via
`ResultsStore.open_read_only` -- a paid campaign may be appending to it
concurrently, so this reader never takes a write lock or alters its schema.

Credibility rule: ALL numbers come from the deterministic evidence pack.
The analyst model only narrates; a deterministic audit re-checks every
figure it wrote. The Tavily layer only corroborates against public
literature and is attempted ONLY if a key is actually present -- otherwise
the report degrades to "literature check not run (no Tavily key)".

Live-run cost: bounded by a hard $0.20 cap on the single analyst call
(a `RetryPolicy` + `max_tokens` ceiling; there is no campaign loop here).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cartographer.analyst.analyst import audit_diagnosis, write_diagnosis  # noqa: E402
from cartographer.analyst.corroborate import (  # noqa: E402
    corroborate_findings,
    load_tavily_key,
)
from cartographer.analyst.evidence import build_evidence_pack  # noqa: E402
from cartographer.exec.client import NEBIUS, Client  # noqa: E402
from cartographer.exec.keys import load_keys  # noqa: E402
from cartographer.store.store import ResultsStore  # noqa: E402
from cartographer.viewer.findings import FINDINGS  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = _REPO_ROOT / "results" / "campaign.sqlite"
REPORT_MD = _REPO_ROOT / "docs" / "capability-report.md"
REPORT_JSON = _REPO_ROOT / "docs" / "capability-report.json"
TAVILY_CACHE = _REPO_ROOT / "results" / "tavily-cache.json"

# The Super model used elsewhere in scripts/ as the default analyst.
DEFAULT_ANALYST_MODEL = "nvidia/nemotron-3-super-120b-a12b"

# Hard ceiling on the single analyst call. max_tokens caps output; a low
# retry count keeps a transient failure from fanning out into more spend.
_ANALYST_MAX_TOKENS = 3000


def _build_report_dict(pack, diagnosis, flags, corroborations, analyst_model, tavily_used):
    literature = {}
    if not corroborations:
        literature["note"] = (
            "literature check not run (no Tavily key)"
            if not tavily_used
            else "no eligible findings to corroborate"
        )
    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "analyst_model": analyst_model,
            "schema": "capability_report_v1",
        },
        "store_totals": pack.get("store_totals", {}),
        "diagnosis": {
            "model": diagnosis.model,
            "raw_text": diagnosis.raw_text,
            "sections": [list(s) for s in diagnosis.sections()],
            "error": diagnosis.error,
        },
        "audit_flags": [f.to_json_dict() for f in flags],
        "corroborations": [c.to_json_dict() for c in corroborations] or None,
        "literature": literature,
        "evidence_pack": pack,
    }


def _render_markdown(report: dict) -> str:
    meta = report["meta"]
    lines = [
        "# Capability Report",
        "",
        f"_Diagnosis written by {meta['analyst_model']}, audited "
        "deterministically; all numbers trace to the results store._",
        "",
        f"Generated: {meta['generated_at']}",
        "",
    ]
    totals = report.get("store_totals", {})
    lines.append(
        f"Evidence base: {totals.get('n_trials','?')} trials "
        f"({totals.get('n_scored','?')} scored), ${totals.get('spend_usd','?')} spend."
    )
    lines.append("")

    diag = report["diagnosis"]
    if diag.get("error"):
        lines.append(f"> Analyst error: {diag['error']}")
        lines.append("")
    for title, body in diag.get("sections", []):
        lines.append(f"## {title}")
        lines.append("")
        lines.append(body)
        lines.append("")

    flags = report.get("audit_flags", [])
    lines.append("## Contamination Audit")
    lines.append("")
    if not flags:
        lines.append(
            "Clean: every numeric figure in the diagnosis matched a value "
            "present in the evidence pack. No invented figures."
        )
    else:
        lines.append(
            f"**{len(flags)} unverified figure(s)** -- stated by the analyst "
            "but absent from the evidence pack:"
        )
        lines.append("")
        for fl in flags:
            lines.append(
                f"- `{fl['figure']}` ({fl['kind']}) -- context: {fl['context']}"
            )
    lines.append("")

    corr = report.get("corroborations")
    lines.append("## Literature Corroboration")
    lines.append("")
    if not corr:
        lines.append(report.get("literature", {}).get("note", "not run"))
    else:
        for c in corr:
            lines.append(f"### {c['finding_name']} — {c['relation']}")
            lines.append("")
            if c.get("rationale"):
                lines.append(c["rationale"])
                lines.append("")
            for r in c.get("results", [])[:5]:
                lines.append(f"- [{r.get('title','')}]({r.get('url','')})")
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "How this was made: numbers come from a deterministic evidence pack "
        "computed straight from the store (scored trials only; infra errors "
        "and resample-noise failures excluded). The analyst model only "
        "narrates; a deterministic audit re-checks every figure. The "
        "literature layer only corroborates against public sources -- no LLM "
        "output is ever written back as a measurement."
    )
    lines.append("")
    return "\n".join(lines)


def generate_report(
    db_path=DEFAULT_DB_PATH,
    analyst_model: str = DEFAULT_ANALYST_MODEL,
    model_filter: str | None = None,
    allow_network: bool = True,
) -> dict:
    """Build and write the report. READ-ONLY on the store. Returns the
    report dict. When `allow_network` is False (or no Nebius key resolves),
    the analyst call is skipped and an empty-diagnosis report is written."""
    store = ResultsStore.open_read_only(db_path)
    try:
        pack = build_evidence_pack(store, model=model_filter)
        keys = load_keys() if allow_network else {}
        nebius_key = keys.get("nebius")

        if allow_network and nebius_key:
            client = Client(NEBIUS, nebius_key)
            diagnosis = write_diagnosis(
                client, analyst_model, pack, max_tokens=_ANALYST_MAX_TOKENS
            )
            flags = audit_diagnosis(diagnosis, pack)

            tavily_key = load_tavily_key()
            corroborations = corroborate_findings(
                FINDINGS,
                tavily_key,
                client=client,
                analyst_model=analyst_model,
                cache_path=str(TAVILY_CACHE),
            )
            tavily_used = bool(tavily_key)
        else:
            from cartographer.analyst.analyst import Diagnosis

            diagnosis = Diagnosis(
                model=analyst_model,
                raw_text="",
                error="analyst call skipped (no Nebius key / network disabled)",
            )
            flags = []
            corroborations = []
            tavily_used = False
    finally:
        store.close()

    report = _build_report_dict(
        pack, diagnosis, flags, corroborations, analyst_model, tavily_used
    )
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    REPORT_MD.write_text(_render_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    report = generate_report()
    diag = report["diagnosis"]
    print(f"Wrote {REPORT_MD} and {REPORT_JSON}")
    if diag.get("error"):
        print(f"Analyst: {diag['error']}")
    else:
        summary = ""
        for title, body in diag.get("sections", []):
            if "Executive" in title:
                summary = body
                break
        print("\n=== Executive Summary ===")
        print(summary or "(none)")
    print(f"\nAudit flags: {len(report['audit_flags'])}")


if __name__ == "__main__":
    main()
