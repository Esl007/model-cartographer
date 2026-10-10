"""Evidence Fidelity Campaign live session on the self-hosted Gemma 4 QAT
endpoint (Modal), inside a hard GPU-time budget. Reuses run_vi_live's
deploy / warm / stop / watchdog machinery.

    python3 scripts/run_ef_live.py --hours 1.75 --seeds 10 --log <file> --summary <json>
    python3 scripts/run_ef_live.py --watchdog --deadline <epoch> --pid <pid> --log <file>

Families run cheapest first (belief, tools, comments, tickets, repair); a
family starts only if its estimated time fits before the soft deadline.
Single-shot families and tickets use the existing evaluators' defaults;
tickets and repair use the text tool protocol; repair uses the Province VI
turn budget (6k, no retry) and the 32k context guard. No resample gate.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_vi_live as V  # noqa: E402

FAMILY_ORDER = ("belief", "tools", "comments", "tickets", "repair")
EST_MIN_PER_SEED = {"belief": 0.3, "tools": 0.4, "comments": 1.0, "tickets": 2.0, "repair": 3.6}  # wall, par 4


def session(hours: float, seeds: int, fh, dry_run: bool, families) -> dict:
    from cartographer.campaigns.evidence_fidelity import hypothesis_summary, run_ef_family
    from cartographer.campaigns.registry import REGISTRY
    from cartographer.exec.client import Client, modal_provider
    from cartographer.exec.keys import load_keys
    from cartographer.store.store import ResultsStore

    store = ResultsStore(V.DB)
    run_ids = {f: V._fresh_run_id(REGISTRY[f"EF.{f}"], store) for f in families}
    summary: dict = {"run_ids": run_ids, "families": {}, "seeds": seeds}
    V.log(fh, f"run ids: {run_ids}")
    if dry_run:
        return summary
    keys = load_keys()
    t0 = time.time()
    hard = t0 + hours * 3600
    soft = hard - 10 * 60
    V.deploy_and_warm(fh, keys)
    client = Client(modal_provider(keys["modal_url"]), keys["modal"])
    V.tiny_chat(fh, client)
    results = {}
    for fam in families:
        need = EST_MIN_PER_SEED[fam] * seeds * 60
        left = soft - time.time()
        if left < need * 0.8:
            V.log(fh, f"{fam}: skipped -- needs ~{need / 60:.0f} min, {left / 60:.0f} min left before the soft deadline")
            summary["families"][fam] = {"skipped": True}
            continue
        kw = {}
        protocol = "native"
        if fam in ("tickets", "repair"):
            protocol = "text_fallback"
        if fam == "repair":
            kw = {"context_window": V.CONTEXT_WINDOW, "max_tokens": V.TURN_TOKENS, "retry_max_tokens": None,
                  "deadline": hard - 6 * 60, "episode_wall_s": V.EPISODE_WALL_S}
        ts = time.time()
        res = run_ef_family(client, store, run_ids[fam], V.MODEL, fam, n_trials=seeds, cost_cap=5.0,
                            parallelism=V.PARALLELISM, protocol=protocol, **kw)
        results[fam] = res
        summary["families"][fam] = {"minutes": round((time.time() - ts) / 60, 1), "note": res.note,
                                    "result": res.to_json_dict()}
        V.log(fh, f"{fam} done in {(time.time() - ts) / 60:.1f} min: {res.note}")
    if results:
        summary["hypotheses"] = hypothesis_summary(results)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1.75)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--families", default=",".join(FAMILY_ORDER))
    ap.add_argument("--log", required=True)
    ap.add_argument("--summary")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--watchdog", action="store_true")
    ap.add_argument("--deadline", type=float)
    ap.add_argument("--pid", type=int)
    a = ap.parse_args()
    fh = open(a.log, "a")
    if a.watchdog:
        V.watchdog(a.deadline, a.pid, fh)
        return
    summary: dict = {}
    t0 = time.time()
    try:
        summary = session(a.hours, a.seeds, fh, a.dry_run, tuple(a.families.split(",")))
    except Exception as e:  # noqa: BLE001 - the app must be stopped whatever happened
        V.log(fh, f"SESSION ERROR {type(e).__name__}: {e}")
        summary["error"] = f"{type(e).__name__}: {e}"
    finally:
        if not a.dry_run:
            V.stop_app(fh)
        summary["app_state_after"] = V.app_state() if not a.dry_run else "not deployed (dry run)"
        summary["session_minutes"] = round((time.time() - t0) / 60, 1)
        if a.summary:
            pathlib.Path(a.summary).write_text(json.dumps(summary, indent=1, default=str))
        V.log(fh, f"SESSION END ({summary['session_minutes']} min); app {summary['app_state_after']}")


if __name__ == "__main__":
    main()
