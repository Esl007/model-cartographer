"""X.consistency -- pass^k: does the model get the SAME task right every time?

tau-bench's reliability metric: run each task k times and ask whether all k
attempts pass. A model with pass^1 = 0.7 that fails independently at random
has pass^5 = 0.7**5 = 0.17; a model whose failures are all "this task is
beyond it" has pass^5 = pass^1. The gap between the two numbers is the
flakiness a single-try benchmark hides (measured once by hand on Nano: 63%
single-try vs 17% on all 5 tries).

Protocol: pick one fixed config (mid-difficulty by default), draw `m_tasks`
trial seeds, and send each (config, trial_seed) through the evaluator `k`
times. The evaluator derives the task from (config, trial_seed), so the k
repeats see the identical prompt; its records (kind unchanged) go under
`run_id` and are recognised as repeats by sharing `trial_seed` + config.

What this measures today: the evaluators call the model at the client's
default temperature (0.0), so the k repeats differ only by serving-side
non-determinism (batching, kernels, provider routing). That is the
reliability a deployed temperature-0 agent actually sees, but it is a lower
bound on sampling-temperature flakiness.

Outputs: a `ConsistencyResult` (pass^1, pass^k, Wilson CIs, per-task table,
flaky tasks) and a JSON side file. `frontier.SPECS["consistency"]` reads the
store, so it can show pass^1 over the repeats; pass^k needs the per-task
grouping and lives only in the result / side file.
"""

from __future__ import annotations

import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Callable

from ..confirm.stats import wilson_interval

Evaluator = Callable[[dict, int], bool]

# Mid-difficulty configs per evaluator kind: hard enough that a frontier
# model is not at 100%, easy enough that it is not at 0% (pass^k is
# uninformative at either end).
DEFAULT_CONFIGS: dict[str, dict[str, float]] = {
    "tool": {"plan_depth": 11, "id_collisions_on": 1},
    "interactive": {"plan_depth": 8, "value_links": 1.0},
    "code": {"n_statements": 20, "identifier_confusability": 1.0},
}


def tool_evaluator_factory(client, model, store, cost_cap, cancel_event=None) -> Callable[[str], Evaluator]:
    """Province II: `ToolBreakingPointEvaluator` (the default)."""
    from ..exec.evaluator import ToolBreakingPointEvaluator

    return lambda run_id: ToolBreakingPointEvaluator(client, model, store, run_id, cost_cap,
                                                     cancel_event=cancel_event)


def interactive_evaluator_factory(client, model, store, cost_cap, cancel_event=None,
                                  protocol: str = "native") -> Callable[[str], Evaluator]:
    """Province III: `InteractiveEvaluator` (multi-turn episodes)."""
    from ..interactive.campaign import InteractiveEvaluator

    return lambda run_id: InteractiveEvaluator(client, model, store, run_id, cost_cap,
                                               cancel_event=cancel_event, protocol=protocol)


def code_evaluator_factory(client, model, store, cost_cap, cancel_event=None) -> Callable[[str], Evaluator]:
    """Province IV: `CodeTraceEvaluator`."""
    from ..exec.evaluator import CodeTraceEvaluator

    return lambda run_id: CodeTraceEvaluator(client, model, store, run_id, cost_cap, cancel_event=cancel_event)


@dataclass
class ConsistencyResult:
    run_id: str
    model: str
    config: dict
    k: int
    m_tasks: int
    pass1: float | None
    pass_k: float | None
    wilson_pass1: tuple[float, float] | None
    wilson_passk: tuple[float, float] | None
    per_task: list[tuple[int, int]]  # (trial_seed, n_pass)
    flaky_tasks: list[int]  # trial seeds with 0 < n_pass < n_scored
    task_n_scored: list[int] = field(default_factory=list)  # parallel to per_task
    n_trials: int = 0
    n_scored: int = 0
    n_unscored: int = 0  # errors / cost-capped / cancelled
    tasks_complete: int = 0  # tasks with >= k scored repeats (the pass^k denominator)
    capped: bool = False
    side_file: str | None = None
    note: str = ""

    def to_json_dict(self) -> dict:
        return asdict(self)


def _pass_hat_k(c: int, n: int, k: int) -> float:
    """Unbiased per-task pass^k estimate from c passes in n >= k repeats
    (tau-bench): C(c, k) / C(n, k). Equals 1 iff all k of k passed when n == k."""
    return math.comb(c, k) / math.comb(n, k)


def summarize(run_id: str, model: str, config: dict, k: int, seeds: list[int],
              outcomes: dict[int, list[bool | None]]) -> ConsistencyResult:
    """Fold per-repeat outcomes (True pass / False fail / None unscored) into
    pass^1 and pass^k. pass^1 pools every scored repeat; its Wilson CI treats
    repeats as independent, which overstates precision when tasks differ --
    read it next to pass^k, whose CI is over tasks."""
    per_task, scored_n, flaky = [], [], []
    tot_pass = tot_scored = unscored = 0
    hats = []
    for s in seeds:
        res = outcomes.get(s, [])
        scored = [x for x in res if x is not None]
        c, n = sum(scored), len(scored)
        unscored += len(res) - n
        per_task.append((s, c))
        scored_n.append(n)
        tot_pass += c
        tot_scored += n
        if 0 < c < n:
            flaky.append(s)
        if n >= k:
            hats.append(_pass_hat_k(c, n, k))
    pass1 = tot_pass / tot_scored if tot_scored else None
    pass_k = sum(hats) / len(hats) if hats else None
    w1 = wilson_interval(tot_pass, tot_scored) if tot_scored else None
    wk = wilson_interval(round(sum(hats)), len(hats)) if hats else None
    note = (f"pass^{k} over {len(hats)} complete tasks; pass^1 over {tot_scored} scored repeats. "
            "Repeats run at the evaluator's temperature (0.0), so flakiness here is serving-side "
            "non-determinism.")
    return ConsistencyResult(
        run_id=run_id, model=model, config=dict(config), k=k, m_tasks=len(seeds),
        pass1=pass1, pass_k=pass_k,
        wilson_pass1=(w1.low, w1.high) if w1 else None,
        wilson_passk=(wk.low, wk.high) if wk else None,
        per_task=per_task, flaky_tasks=flaky, task_n_scored=scored_n,
        n_trials=sum(len(outcomes.get(s, [])) for s in seeds), n_scored=tot_scored, n_unscored=unscored,
        tasks_complete=len(hats), note=note,
    )


def _outcomes_from_store(store, run_id: str, seeds: list[int], config: dict) -> dict[int, list[bool | None]] | None:
    """Re-read verdicts from the records the evaluator wrote (PASS/FAIL scored,
    anything else unscored). None when the evaluator wrote nothing we can match
    (e.g. a test fake) -- the caller then keeps the evaluator's return values."""
    if store is None or not hasattr(store, "by_run"):
        return None
    try:
        from ..store.schema import Verdict

        recs = store.by_run(run_id)
    except Exception:  # noqa: BLE001 - reconciliation is best-effort
        return None
    wanted = set(seeds)
    out: dict[int, list[bool | None]] = {s: [] for s in seeds}
    hit = False
    for r in recs:
        if r.trial_seed not in wanted:
            continue
        if any(abs(float(r.latent_params.get(key, v)) - float(v)) > 1e-9 for key, v in config.items()):
            continue
        hit = True
        out[r.trial_seed].append(True if r.verdict == Verdict.PASS else False if r.verdict == Verdict.FAIL else None)
    return out if hit else None


def run_consistency_campaign(
    client,
    store,
    run_id: str,
    model: str,
    evaluator_factory: Callable[[str], Evaluator] | None = None,
    config: dict | None = None,
    m_tasks: int = 12,
    k: int = 5,
    cost_cap: float = 0.5,
    parallelism: int = 4,
    evaluator_kind: str = "tool",
    protocol: str = "native",
    seed_base: int = 1,
    side_file: str | None = None,
    cancel_event: threading.Event | None = None,
) -> ConsistencyResult:
    """Run m_tasks x k repeats and return pass^1 / pass^k.

    `evaluator_factory(run_id) -> Evaluator` builds the per-trial evaluator;
    when None, `evaluator_kind` picks a default ("tool" = Province II
    `ToolBreakingPointEvaluator`, "interactive", "code") and `config`
    defaults to that kind's mid-difficulty point in `DEFAULT_CONFIGS`.
    `side_file`: JSON path for the per-task table (default
    `results/consistency/<run_id>.json`; "" = don't write one).
    """
    if evaluator_factory is None:
        if evaluator_kind == "interactive":
            evaluator_factory = interactive_evaluator_factory(client, model, store, cost_cap, cancel_event, protocol)
        elif evaluator_kind == "code":
            evaluator_factory = code_evaluator_factory(client, model, store, cost_cap, cancel_event)
        else:
            evaluator_factory = tool_evaluator_factory(client, model, store, cost_cap, cancel_event)
    if config is None:
        config = DEFAULT_CONFIGS.get(evaluator_kind, DEFAULT_CONFIGS["tool"])
    config = dict(config)
    ev = evaluator_factory(run_id)
    seeds = [seed_base + i for i in range(m_tasks)]
    outcomes: dict[int, list[bool | None]] = {s: [] for s in seeds}
    lock = threading.Lock()

    def _stopped() -> bool:
        return bool(getattr(ev, "capped", False) or getattr(ev, "cancelled", False)
                    or (cancel_event is not None and cancel_event.is_set()))

    def one(seed: int) -> None:
        if _stopped():
            return
        ok = bool(ev(dict(config), seed))
        # A trial refused by the evaluator's own cap/cancel returns False
        # without asking the model: unscored, not a failure.
        result: bool | None = ok if (ok or not _stopped()) else None
        with lock:
            outcomes[seed].append(result)

    # Task-major order: if the cost cap binds, finished tasks are complete
    # (pass^k needs all k repeats of a task more than it needs more tasks).
    jobs = [s for s in seeds for _ in range(k)]
    if parallelism <= 1:
        for s in jobs:
            one(s)
    else:
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            list(pool.map(one, jobs))

    reconciled = _outcomes_from_store(store, run_id, seeds, config)
    res = summarize(run_id, model, config, k, seeds, reconciled or outcomes)
    res.capped = bool(getattr(ev, "capped", False))
    if side_file is None:
        side_file = os.path.join("results", "consistency", run_id.replace("/", "_") + ".json")
    if side_file:
        os.makedirs(os.path.dirname(side_file) or ".", exist_ok=True)
        with open(side_file, "w") as f:
            json.dump(res.to_json_dict(), f, indent=1, default=str)
        res.side_file = side_file
    return res
