"""Rerun specific stored repair tasks (docs/spec-province-v-ambiguities.md B/C/E).

Each target is a (source run id, trial_seed) pair; the task is rebuilt from the
stored record's latent_params + task_seed (`make_repair_task`), and the stock
`CodeRepairEvaluator` runs it unchanged (prompt, retry, grading, record) --
only its `_task_seed` lookup is pinned so the evaluator draws the stored seed.

  python3 scripts/rerun_repair_seeds.py --mode postfix --dry-run
  python3 scripts/rerun_repair_seeds.py --mode postfix --model <id>
  python3 scripts/rerun_repair_seeds.py --mode determinism --model <id>

postfix: B (outside_subset:builtin_call tasks) + C (timeout tasks), pair_trace
  on, resample gate n=3, parallelism <= 4. Records land under
  `<source run>-postfix-001` with the source run's `-001`/`-002` suffix replaced
  (camp-code-repair-kinds-gemma4qat-postfix-001, ...-confirm-...-postfix-001).
determinism: E. Repair prompt only (pair_trace off), no gate; each target
  rerun `--reps` times serially (`<run_id>/serial`), then `--reps` times with all
  calls in a pool of 4 (`<run_id>/parallel`). Prints byte-level comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cartographer.codefix.tasks import make_repair_task  # noqa: E402
from cartographer.codefix.verify import render_repair_prompt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairtrace_xcheck import params_from_latent  # noqa: E402

DB = "results/campaign.sqlite"
KINDS, CONFIRM = "camp-code-repair-kinds-gemma4qat-001", "camp-code-repair-confirm-gemma4qat-001"
# B: outside_subset:builtin_call (kinds trial 1; confirm 800012 family = 800011/800012/800013/800015)
# C: api_error timeout (kinds trial 0; confirm 800007, 800011)
POSTFIX_TARGETS = [(KINDS, 0), (KINDS, 1), (CONFIRM, 800007), (CONFIRM, 800011), (CONFIRM, 800012),
                   (CONFIRM, 800013), (CONFIRM, 800015)]
DETERMINISM_TARGETS = [(CONFIRM, 800005), (CONFIRM, 800006), (CONFIRM, 800009)]
_IDX = "rerun_idx"  # not a _FIX_KNOWN_KEYS key: ignored by _apply_fix_config


def load_targets(db: str, targets) -> list[dict]:
    import sqlite3

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = []
    try:
        for rid, ts in targets:
            rows = con.execute("SELECT task_seed, latent_params, prompt_text FROM experiments "
                               "WHERE run_id IN (?, ?) AND trial_seed=? AND prompt_text IS NOT NULL",
                               (rid, rid + "/verify", ts)).fetchall()
            seeds = {r[0] for r in rows}
            if len(seeds) != 1:
                raise SystemExit(f"{rid} trial {ts}: expected one task_seed, got {seeds}")
            task_seed, latent, stored_prompt = rows[0][0], json.loads(rows[0][1]), rows[0][2]
            params = params_from_latent(latent)
            task = make_repair_task(params, task_seed)
            prompt = render_repair_prompt(task)
            stored_repair = [r[2] for r in rows if "solve" in (r[2] or "")]
            out.append({"source_run": rid, "trial_seed": ts, "task_seed": task_seed, "params": params,
                        "task": task, "prompt": prompt,
                        "prompt_matches_stored": prompt in stored_repair})
    finally:
        con.close()
    return out


@contextmanager
def _pinned_seeds(items):
    from cartographer.exec import evaluator as E

    real = E._task_seed

    def seed(config, trial_seed):
        return items[int(config[_IDX])]["task_seed"] if _IDX in config else real(config, trial_seed)

    E._task_seed = seed
    try:
        yield
    finally:
        E._task_seed = real


def postfix_run_id(source_run: str) -> str:
    return source_run.rsplit("-", 1)[0] + "-postfix-001"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["postfix", "determinism"], required=True)
    ap.add_argument("--model")
    ap.add_argument("--provider", default="modal", choices=["nebius", "modal", "openrouter", "groq"])
    ap.add_argument("--parallelism", type=int, default=4)
    ap.add_argument("--reps", type=int, default=5, help="determinism: reps per target per phase")
    ap.add_argument("--run-id", default="camp-determinism-probe-gemma4qat-001", help="determinism run id")
    ap.add_argument("--cost-cap", type=float, default=0.21)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    if args.parallelism > 4:
        ap.error("parallelism <= 4")

    items = load_targets(args.db, POSTFIX_TARGETS if args.mode == "postfix" else DETERMINISM_TARGETS)
    for i, it in enumerate(items):
        print(f"[{i}] {it['source_run']} trial={it['trial_seed']} task_seed={it['task_seed']} "
              f"bug_kinds={'+'.join(it['task'].bug_kinds)} prompt_matches_stored={it['prompt_matches_stored']}",
              flush=True)
    if not all(it["prompt_matches_stored"] for it in items):
        sys.exit("reconstructed prompt differs from the stored one; refusing to run")
    if args.dry_run:
        return
    if not args.model:
        ap.error("--model is required unless --dry-run")

    from run_campaign import _client

    from cartographer.exec.evaluator import CodeRepairEvaluator
    from cartographer.schedule.gate import ResampleGatedEvaluator
    from cartographer.store.store import ResultsStore

    client, store = _client(args.provider), ResultsStore(args.db)
    t0 = time.time()
    try:
        with _pinned_seeds(items):
            if args.mode == "postfix":
                evals = {}
                for it in items:
                    rid = postfix_run_id(it["source_run"])
                    if rid not in evals:
                        inner = CodeRepairEvaluator(client, args.model, store, rid, args.cost_cap,
                                                    base=it["params"], pair_trace=True)
                        evals[rid] = (inner, ResampleGatedEvaluator(inner, n=3))

                def one(i):
                    it = items[i]
                    inner, gate = evals[postfix_run_id(it["source_run"])]
                    inner.base = it["params"]  # all targets share one config; base is identical
                    s = time.time()
                    ok = gate({_IDX: float(i)}, it["trial_seed"])
                    row = {"idx": i, "source_run": it["source_run"], "trial_seed": it["trial_seed"],
                           "task_seed": it["task_seed"], "gated_pass": bool(ok),
                           "elapsed_s": round(time.time() - s, 1)}
                    print("ROW " + json.dumps(row), flush=True)
                    return row

                with ThreadPoolExecutor(args.parallelism) as pool:
                    list(pool.map(one, range(len(items))))
                for rid, (inner, gate) in evals.items():
                    print("DONE " + json.dumps({"run_id": rid, "spent_usd": round(inner.spent_usd, 4),
                                                "errors": inner.error_count,
                                                "gate": gate.note_fields()}), flush=True)
            else:
                ev = CodeRepairEvaluator(client, args.model, store, args.run_id, args.cost_cap,
                                         base=items[0]["params"], pair_trace=False)
                for i, it in enumerate(items):
                    assert it["params"] == items[0]["params"], "targets must share params"
                jobs = [i for i in range(len(items)) for _ in range(args.reps)]
                for i in jobs:  # serial: one request in flight at a time
                    ev({_IDX: float(i)}, items[i]["trial_seed"], run_suffix="/serial")
                print(f"serial done {time.time() - t0:.0f}s", flush=True)
                with ThreadPoolExecutor(4) as pool:
                    list(pool.map(lambda i: ev({_IDX: float(i)}, items[i]["trial_seed"],
                                               run_suffix="/parallel"), jobs))
                print("DONE " + json.dumps({"run_id": args.run_id, "spent_usd": round(ev.spent_usd, 4),
                                            "errors": ev.error_count}), flush=True)
    finally:
        store.close()
    print(f"minutes={(time.time() - t0) / 60:.1f}", flush=True)
    if args.mode == "determinism":
        summarize_determinism(args.db, args.run_id, items)


def summarize_determinism(db: str, run_id: str, items) -> None:
    import sqlite3

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for it in items:
        for phase in ("serial", "parallel"):
            rows = con.execute("SELECT response_text, verdict, verifier_detail FROM experiments "
                               "WHERE run_id=? AND trial_seed=? ORDER BY created_at",
                               (f"{run_id}/{phase}", it["trial_seed"])).fetchall()
            hashes = [hashlib.sha256((r[0] or "").encode()).hexdigest()[:10] for r in rows]
            dets = [r[2].split("detail=")[1].split(";")[0] if "detail=" in (r[2] or "") else r[1] for r in rows]
            print(f"E trial={it['trial_seed']} {phase}: n={len(rows)} distinct_bytes={len(set(hashes))} "
                  f"hashes={hashes} details={dets}", flush=True)
    con.close()


if __name__ == "__main__":
    main()
