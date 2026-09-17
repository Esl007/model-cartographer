"""Failure decomposition engine: find the minimal factor set behind a failure.

Given a failing dim configuration and a passing baseline, the engine probes
intermediate configurations (single factors relaxed/raised, then pairs, then
triples) to determine which factor(s) are responsible, using multi-trial
probes to stay robust to per-trial noise (e.g. NoiseWrapper-wrapped rules).

Every stage is deterministic given (evaluator, failing, baseline, k_trials,
seed_base): probe ordering is always over `sorted(...)` collections, and
seeds are derived arithmetically from a monotonically increasing counter so
re-running with the same inputs reproduces byte-identical evidence.
"""

from __future__ import annotations

import itertools
from typing import Callable

from .hypothesis import FailureHypothesis, ProbeOutcome

Evaluator = Callable[[dict[str, float], int], bool]


class _BudgetExhausted(Exception):
    """Internal signal: budget ran out mid-stage."""

    def __init__(self, stage: str) -> None:
        super().__init__(stage)
        self.stage = stage


def probe(
    evaluator: Evaluator,
    config: dict[str, float],
    k_trials: int,
    seed_base: int,
) -> ProbeOutcome:
    """Run `k_trials` evaluations of `config` with seeds seed_base..seed_base+k-1.

    Each trial costs 1 unit against the caller's budget (accounted by the
    caller, not here). Returns the resulting pass rate and the exact seeds
    used, for full reproducibility.
    """
    seeds = [seed_base + i for i in range(k_trials)]
    passes = sum(1 for s in seeds if evaluator(config, s))
    pass_rate = passes / k_trials if k_trials > 0 else 0.0
    return ProbeOutcome(
        config=dict(config), pass_rate=pass_rate, n_trials=k_trials, trial_seeds=seeds
    )


class _Budget:
    """Tracks remaining trial budget and the running seed counter."""

    def __init__(self, budget: int, seed_base: int) -> None:
        self.remaining = budget
        self.spent = 0
        self._next_seed = seed_base

    def take_seed_block(self, k_trials: int) -> int:
        """Reserve a block of k_trials seeds and return its starting seed."""
        start = self._next_seed
        self._next_seed += k_trials
        return start

    def charge(self, k_trials: int, stage: str) -> None:
        if k_trials > self.remaining:
            raise _BudgetExhausted(stage)
        self.remaining -= k_trials
        self.spent += k_trials


def _run_probe(
    evaluator: Evaluator,
    config: dict[str, float],
    k_trials: int,
    budget: _Budget,
    stage: str,
) -> ProbeOutcome:
    budget.charge(k_trials, stage)
    seed_start = budget.take_seed_block(k_trials)
    return probe(evaluator, config, k_trials, seed_start)


def _classify(
    evaluator: Evaluator,
    config: dict[str, float],
    k_trials: int,
    pass_threshold: float,
    fail_threshold: float,
    budget: _Budget,
    stage: str,
    evidence: list[ProbeOutcome],
) -> tuple[str, ProbeOutcome]:
    """Probe `config`, classify as "pass"/"fail"/"ambiguous", with one retry round.

    On ambiguous (fail_threshold < pass_rate < pass_threshold), spend one
    more k_trials round on the SAME config (fresh seeds) and combine results
    before re-classifying. If still ambiguous, treat by nearest threshold
    and note it via the returned outcome (caller attaches notes).
    """
    outcome = _run_probe(evaluator, config, k_trials, budget, stage)
    evidence.append(outcome)
    verdict = _threshold_verdict(outcome.pass_rate, pass_threshold, fail_threshold)
    if verdict != "ambiguous":
        return verdict, outcome

    # One retry round: combine with fresh trials.
    retry = _run_probe(evaluator, config, k_trials, budget, stage)
    evidence.append(retry)
    total_trials = outcome.n_trials + retry.n_trials
    total_passes = round(outcome.pass_rate * outcome.n_trials) + round(
        retry.pass_rate * retry.n_trials
    )
    combined_rate = total_passes / total_trials if total_trials else 0.0
    combined = ProbeOutcome(
        config=dict(config),
        pass_rate=combined_rate,
        n_trials=total_trials,
        trial_seeds=outcome.trial_seeds + retry.trial_seeds,
    )
    verdict = _threshold_verdict(combined.pass_rate, pass_threshold, fail_threshold)
    if verdict == "ambiguous":
        # Nearest threshold: closer to pass_threshold -> pass, else fail.
        verdict = (
            "pass"
            if abs(combined.pass_rate - pass_threshold)
            <= abs(combined.pass_rate - fail_threshold)
            else "fail"
        )
    return verdict, combined


def _threshold_verdict(pass_rate: float, pass_threshold: float, fail_threshold: float) -> str:
    if pass_rate >= pass_threshold:
        return "pass"
    if pass_rate <= fail_threshold:
        return "fail"
    return "ambiguous"


def _active_factors(failing: dict[str, float], baseline: dict[str, float]) -> list[str]:
    keys = set(failing) | set(baseline)
    return sorted(k for k in keys if failing.get(k, 0.0) != baseline.get(k, 0.0))


def decompose(
    evaluator: Evaluator,
    failing: dict[str, float],
    baseline: dict[str, float],
    k_trials: int = 7,
    pass_threshold: float = 0.7,
    fail_threshold: float = 0.3,
    budget: int = 500,
    seed_base: int = 0,
) -> FailureHypothesis:
    """Find the minimal factor set responsible for `failing`'s failure.

    Proceeds in stages of increasing interaction order (single -> pair ->
    triple), stopping as soon as a stage explains the failure. Every probe
    is noise-robust (k_trials repeats, classified via pass/fail thresholds
    with one ambiguity-resolving retry round). Budget is a hard cap on total
    trials; if exhausted mid-stage the best evidence gathered so far is
    returned with an explanatory note.
    """
    evidence: list[ProbeOutcome] = []
    b = _Budget(budget, seed_base)
    active = _active_factors(failing, baseline)
    necessary_so_far: list[str] = []

    def finish(kind: str, factors: tuple[str, ...], necessary: tuple[str, ...], notes: str) -> FailureHypothesis:
        return FailureHypothesis(
            kind=kind,
            factors=tuple(sorted(factors)),
            necessary_factors=tuple(sorted(necessary)),
            evidence=list(evidence),
            budget_spent=b.spent,
            notes=notes,
        )

    try:
        # Step 0: verify failing fails and baseline passes.
        fail_verdict, _ = _classify(
            evaluator, failing, k_trials, pass_threshold, fail_threshold, b, "verify_failing", evidence
        )
        base_verdict, _ = _classify(
            evaluator, baseline, k_trials, pass_threshold, fail_threshold, b, "verify_baseline", evidence
        )
        if fail_verdict != "fail":
            return finish(
                "unexplained", (), (), "failing config did not fail under probing"
            )
        if base_verdict != "pass":
            return finish(
                "unexplained", (), (), "baseline config did not pass under probing"
            )

        if not active:
            return finish(
                "unexplained", (), (), "no active factors differ between failing and baseline"
            )

        # Step 1: NECESSITY — relax each active factor to baseline, one at a time.
        necessary: list[str] = necessary_so_far
        for f in active:
            relaxed = dict(failing)
            relaxed[f] = baseline.get(f, 0.0)
            verdict, _ = _classify(
                evaluator, relaxed, k_trials, pass_threshold, fail_threshold, b,
                f"necessity_{f}", evidence,
            )
            if verdict == "pass":
                necessary.append(f)

        # Step 2: SUFFICIENCY singles — raise each active factor alone from baseline.
        single_results: dict[str, str] = {}
        for f in active:
            raised = dict(baseline)
            raised[f] = failing.get(f, 0.0)
            verdict, _ = _classify(
                evaluator, raised, k_trials, pass_threshold, fail_threshold, b,
                f"sufficiency_single_{f}", evidence,
            )
            single_results[f] = verdict

        sufficient_singles = sorted(f for f, v in single_results.items() if v == "fail")
        if sufficient_singles:
            notes = "; ".join(f"{f} alone reproduces failure" for f in sufficient_singles)
            return finish("single_sufficient", tuple(sufficient_singles), tuple(necessary), notes)

        # Step 3: PAIRS — only factors whose singles passed (didn't suffice alone).
        pair_candidates = sorted(f for f, v in single_results.items() if v == "pass")
        failing_pairs: list[tuple[str, str]] = []
        for f1, f2 in itertools.combinations(pair_candidates, 2):
            raised = dict(baseline)
            raised[f1] = failing.get(f1, 0.0)
            raised[f2] = failing.get(f2, 0.0)
            verdict, _ = _classify(
                evaluator, raised, k_trials, pass_threshold, fail_threshold, b,
                f"sufficiency_pair_{f1}_{f2}", evidence,
            )
            if verdict == "fail":
                failing_pairs.append((f1, f2))

        if failing_pairs:
            union_factors: set[str] = set()
            pair_names = []
            for f1, f2 in failing_pairs:
                union_factors.update((f1, f2))
                pair_names.append(f"({f1}, {f2})")
            notes = "pairwise interaction(s): " + ", ".join(pair_names)
            return finish(
                "pairwise_interaction", tuple(sorted(union_factors)), tuple(necessary), notes
            )

        # Step 4: HIGHER-ORDER — triples, only if <=5 active factors.
        if len(active) > 5:
            return finish(
                "unexplained",
                (),
                tuple(necessary),
                "skipped higher-order stage: more than 5 active factors",
            )

        triple_candidates = sorted(active)
        failing_triples: list[tuple[str, str, str]] = []
        for f1, f2, f3 in itertools.combinations(triple_candidates, 3):
            raised = dict(baseline)
            raised[f1] = failing.get(f1, 0.0)
            raised[f2] = failing.get(f2, 0.0)
            raised[f3] = failing.get(f3, 0.0)
            verdict, _ = _classify(
                evaluator, raised, k_trials, pass_threshold, fail_threshold, b,
                f"sufficiency_triple_{f1}_{f2}_{f3}", evidence,
            )
            if verdict == "fail":
                failing_triples.append((f1, f2, f3))

        if failing_triples:
            union_factors = set()
            triple_names = []
            for f1, f2, f3 in failing_triples:
                union_factors.update((f1, f2, f3))
                triple_names.append(f"({f1}, {f2}, {f3})")
            notes = "higher-order interaction(s): " + ", ".join(triple_names)
            return finish(
                "higher_order_interaction", tuple(sorted(union_factors)), tuple(necessary), notes
            )

        return finish(
            "unexplained",
            (),
            tuple(necessary),
            "no single, pair, or triple of active factors reproduced the failure",
        )

    except _BudgetExhausted as exc:
        return finish(
            "unexplained",
            (),
            tuple(necessary_so_far),
            f"budget_exhausted at stage {exc.stage}",
        )
