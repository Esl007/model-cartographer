"""A1.2 cross-evaluator rerun (docs/spec-province-v-ambiguities.md): take the
BUGGY programs whose base paired /trace record was FAIL in the given repair
campaigns, rebuild them via `make_repair_task(latent_params, task_seed)`, and
run each through the round-2 `CodeTraceEvaluator` verbatim (same `render`
prompt, same `codeworld.verify` grading, fresh single-message chats) under
the resample gate.

  python3 scripts/pairtrace_xcheck.py --dry-run
  python3 scripts/pairtrace_xcheck.py --provider modal --model <id> --label gemma4qat

Default source runs: camp-code-repair-gemma4qat-001,
camp-code-repair-kinds-gemma4qat-001, camp-code-repair-confirm-gemma4qat-001.
`--dry-run` only reads the sqlite (read-only) and prints each program's task
seed and prompt: no client, no spend. Records are written under
`--run-id` (default camp-code-pairtrace-xcheck-<label>-001), task_seed = the
repair task's seed so they join back to the source records.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cartographer.codefix.tasks import CodeFixParams, make_repair_task  # noqa: E402
from cartographer.codefix.verify import render_trace_prompt  # noqa: E402

DB = "results/campaign.sqlite"
DEFAULT_RUNS = ("camp-code-repair-gemma4qat-001", "camp-code-repair-kinds-gemma4qat-001",
                "camp-code-repair-confirm-gemma4qat-001")
_INT_KEYS = {"n_statements", "n_bugs", "hidden_inputs", "edge_cases", "spec_examples", "n_params"}
_IDX = "xcheck_idx"  # config key; not a CodeDimParams key, so CodeTraceEvaluator ignores it for params


def params_from_latent(latent: dict) -> CodeFixParams:
    """Inverse of evaluator._fix_latent (bug_distance 0 = None = any site)."""
    kw = {}
    for k, v in latent.items():
        if k in _INT_KEYS:
            kw[k] = int(round(v))
        elif k == "bug_distance":
            kw[k] = int(round(v)) or None
        elif k == "misleading_comment":
            kw[k] = bool(v >= 0.5)
        elif k in ("bug_mix", "identifier_confusability"):
            kw[k] = float(v)
    return CodeFixParams(**kw)


def traced_wrong(db: str, run_ids) -> list[dict]:
    """One entry per base /trace FAIL record (resample / verify sub-runs excluded)."""
    from cartographer.store.schema import Verdict
    from cartographer.store.store import ResultsStore

    store = ResultsStore(db, read_only=True)
    out = []
    try:
        for rid in run_ids:
            for r in store.by_run(f"{rid}/trace"):
                if r.verdict != Verdict.FAIL:
                    continue
                params = params_from_latent(r.latent_params)
                task = make_repair_task(params, r.task_seed)
                program, rendered = render_trace_prompt(task)
                out.append({"source_run": rid, "task_seed": r.task_seed, "trial_seed": r.trial_seed,
                            "task": task, "program": program, "rendered": rendered,
                            "old_prompt": r.prompt_text})
    finally:
        store.close()
    return out


@contextmanager
def _pinned_programs(items):
    """Make CodeTraceEvaluator's own `build_code_task` / `_task_seed` hand back
    our reconstructed program (indexed by config[_IDX]); the evaluator body
    (chat, retry, grading, record) runs unchanged."""
    from cartographer.exec import evaluator as E

    real_build, real_seed = E.build_code_task, E._task_seed

    def build(config, trial_seed, base=None, adversarial=False, adversarial_candidates=20):
        if _IDX not in config:
            return real_build(config, trial_seed, base, adversarial, adversarial_candidates)
        it = items[int(config[_IDX])]
        return it["program"].params, it["program"], it["rendered"], it["task_seed"]

    def seed(config, trial_seed):
        return items[int(config[_IDX])]["task_seed"] if _IDX in config else real_seed(config, trial_seed)

    E.build_code_task, E._task_seed = build, seed
    try:
        yield
    finally:
        E.build_code_task, E._task_seed = real_build, real_seed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", nargs="+", default=list(DEFAULT_RUNS), help="source repair campaign run ids")
    ap.add_argument("--model")
    ap.add_argument("--label", default="gemma4qat")
    ap.add_argument("--provider", default="modal", choices=["nebius", "modal", "openrouter", "groq"])
    ap.add_argument("--run-id", help="default camp-code-pairtrace-xcheck-<label>-001")
    ap.add_argument("--k", type=int, default=5, help="trials per program (temp 0 replays)")
    ap.add_argument("--resample-n", type=int, default=3)
    ap.add_argument("--cost-cap", type=float, default=1.0, help="per-program USD cap")
    ap.add_argument("--max-tokens", type=int)
    ap.add_argument("--dry-run", action="store_true", help="read-only: print seeds + prompts, no client")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    run_id = args.run_id or f"camp-code-pairtrace-xcheck-{args.label}-001"

    items = traced_wrong(args.db, args.runs)
    print(f"{len(items)} traced-wrong programs from {', '.join(args.runs)} -> {run_id}", flush=True)
    for i, it in enumerate(items):
        t = it["task"]
        print(f"[{i}] {it['source_run']} task_seed={it['task_seed']} trial_seed={it['trial_seed']} "
              f"bug_kinds={'+'.join(t.bug_kinds)} visible={t.visible_input!r} buggy_out={t.visible_wrong!r} "
              f"old_prompt_matches_new={it['old_prompt'] == it['rendered'].prompt}")
        if args.dry_run:
            print(it["rendered"].prompt + "\n" + "-" * 72)
    if args.dry_run:
        return
    if not args.model:
        ap.error("--model is required unless --dry-run")
    from cartographer.exec.pricing import PRICES

    if args.model not in PRICES:
        sys.exit(f"{args.model} has no entry in cartographer/exec/pricing.py; cost caps would not bind.")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from run_campaign import _client  # same provider wiring as scripts/run_campaign.py

    from cartographer.exec.evaluator import CodeTraceEvaluator
    from cartographer.schedule.gate import ResampleGatedEvaluator
    from cartographer.store.store import ResultsStore

    client, store = _client(args.provider), ResultsStore(args.db)
    t0, rows, spent = time.time(), [], 0.0
    try:
        with _pinned_programs(items):
            for i, it in enumerate(items):
                inner = CodeTraceEvaluator(client, args.model, store, run_id, args.cost_cap,
                                           base=it["program"].params, max_tokens=args.max_tokens)
                gate = ResampleGatedEvaluator(inner, n=args.resample_n)
                passes = sum(bool(gate({_IDX: float(i)}, ts)) for ts in range(args.k))
                spent += inner.spent_usd
                rows.append({"idx": i, "source_run": it["source_run"], "task_seed": it["task_seed"],
                             "passes": passes, "k": args.k, "errors": inner.error_count,
                             "gate_stable": gate.stable, "gate_noise": gate.noise,
                             "gate_unresolved": getattr(gate, "unresolved", None)})
                print("ROW " + json.dumps(rows[-1]), flush=True)
    finally:
        store.close()
    print("DONE " + json.dumps({"run_id": run_id, "programs": len(items), "spent_usd": round(spent, 4),
                                "programs_all_pass": sum(r["passes"] == r["k"] for r in rows),
                                "minutes": round((time.time() - t0) / 60, 1)}), flush=True)


if __name__ == "__main__":
    main()
