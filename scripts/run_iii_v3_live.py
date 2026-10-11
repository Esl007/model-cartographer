"""Live session for docs/spec-iii-protocol-v3-and-belief-power.md (Tasks 2 + 3)
on the self-hosted Gemma 4 QAT endpoint (Modal), hard-capped. Reuses
run_vi_live's deploy / warm / JSON-listing stop / watchdog.

    python3 scripts/run_iii_v3_live.py --hours 2.5 --log <file> --summary <json>
    python3 scripts/run_iii_v3_live.py --watchdog --deadline <epoch> --pid <pid> --log <file>

Stages, each with its own time cap (remaining trials of a capped stage are
recorded as cancelled ERRORs, never as model results):
  T3  belief power run: +20 paired seeds on camp-ef-belief-gemma4qat-001 (A/B/C/D), resample gate on
  T2b EF ticket arm under v3: camp-ef-tickets-gemma4qat-002, 10 paired seeds (A/B/C/D/X)
  T2a III.values default ray under v3, standard tier, gate on: camp-interactive-gemma4qat-v3-001
  T2c III.fault default ray under v3, standard tier, gate on: camp-interactive-fault-gemma4qat-v3-001
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_vi_live as V  # noqa: E402

STAGE_CAP_MIN = {"T3": 12, "T2b": 45, "T2a": 45, "T2c": 40}


def _timer(minutes: float, soft: float) -> threading.Event:
    ev = threading.Event()
    until = min(time.time() + minutes * 60, soft)
    t = threading.Timer(max(1.0, until - time.time()), ev.set)
    t.daemon = True  # a stage cap must never keep the process (and its app-stop / report waiters) alive
    t.start()
    return ev


def session(hours: float, fh, dry_run: bool) -> dict:
    from cartographer.campaigns.evidence_fidelity import run_ef_family
    from cartographer.campaigns.registry import REGISTRY
    from cartographer.campaigns.tiers import apply_tier
    from cartographer.exec.client import Client, modal_provider
    from cartographer.exec.keys import load_keys
    from cartographer.interactive.campaign import run_interactive_breaking_point_campaign
    from cartographer.store.store import ResultsStore

    store = ResultsStore(V.DB)
    ids = {"T3": "camp-ef-belief-gemma4qat-001", "T2b": "camp-ef-tickets-gemma4qat-002",
           "T2a": "camp-interactive-gemma4qat-v3-001", "T2c": "camp-interactive-fault-gemma4qat-v3-001"}
    for k in ("T2b", "T2a", "T2c"):
        if store.by_run(ids[k]) or any(store.by_run(f"{ids[k]}/{a}") for a in "ABCDX"):
            raise RuntimeError(f"run id {ids[k]} already has records")
    summary: dict = {"run_ids": ids, "stages": {}}
    V.log(fh, f"run ids: {ids}")
    if dry_run:
        return summary
    keys = load_keys()
    t0 = time.time()
    hard = t0 + hours * 3600
    soft = hard - 8 * 60
    V.deploy_and_warm(fh, keys)
    client = Client(modal_provider(keys["modal_url"]), keys["modal"])
    V.tiny_chat(fh, client)

    def stage(name, fn):
        if soft - time.time() < 5 * 60:
            V.log(fh, f"{name}: skipped, {(soft - time.time()) / 60:.0f} min left")
            summary["stages"][name] = {"skipped": True}
            return
        ts = time.time()
        try:
            out = fn(_timer(STAGE_CAP_MIN[name], soft))
            note = getattr(out, "note", "")
            summary["stages"][name] = {"minutes": round((time.time() - ts) / 60, 1), "note": note,
                                       "result": out.to_json_dict() if hasattr(out, "to_json_dict") else repr(out)}
            V.log(fh, f"{name} done in {(time.time() - ts) / 60:.1f} min: {note}")
        except Exception as e:  # noqa: BLE001 -- one stage's failure must not stop the others
            summary["stages"][name] = {"error": f"{type(e).__name__}: {e}"}
            V.log(fh, f"{name} ERROR {type(e).__name__}: {e}")

    def t3(cancel):
        from cartographer.campaigns import evidence_fidelity as EFM

        ev = EFM.make_evaluator("belief", client, V.MODEL, EFM._StampingStore(store, ""), ids["T3"], 5.0,
                                on_task=EFM._chained_hook("belief", None), cancel_event=cancel)
        return run_ef_family(client, store, ids["T3"], V.MODEL, "belief", n_trials=20, seed_base=10,
                             parallelism=V.PARALLELISM, resample_gate=True, evaluator=ev)

    def t2b(cancel):
        return run_ef_family(client, store, ids["T2b"], V.MODEL, "tickets", n_trials=10, parallelism=V.PARALLELISM,
                             protocol="text_fallback", cost_cap=5.0, protocol_version=3, cancel_event=cancel)

    def ray(spec_id, rid):
        def run(cancel):
            spec = REGISTRY[spec_id]
            kw = apply_tier(spec, "standard")
            kw.update(ray=spec.ray_factory(), protocol="text_fallback", protocol_version=3, cancel_event=cancel,
                      resample_gate=True)
            kw.pop("verify", None)
            return run_interactive_breaking_point_campaign(client, store, rid, V.MODEL, **kw)
        return run

    stage("T3", t3)
    stage("T2b", t2b)
    stage("T2a", ray("III.values", ids["T2a"]))
    stage("T2c", ray("III.fault", ids["T2c"]))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.5)
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
        summary = session(a.hours, fh, a.dry_run)
    except Exception as e:  # noqa: BLE001
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
