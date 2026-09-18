"""Noise filter: resample a single reported failure before trusting it.

This module exists because of a real mistake made and corrected in one
session (see `docs/lab-log.md`, "Ultra pronoun-actor-tracking bug ...
earlier '3-for-3 tiers' claim partially retracted"). A single-trial FAIL
against `nvidia/Nemotron-3-Ultra-550b-a55b` (the "Priya" case) was
initially reported as a confirmed finding matching a pattern seen in other
model tiers. It did not reproduce: 4 of 5 total samples of that exact
prompt (the original campaign trial plus 4 fresh resamples) came back
correct. Ultra is a 550B mixture-of-experts model with known real
run-to-run output variance even at temperature 0, from expert-routing
effects -- so a single FAIL can be pure sampling noise, not a stable
capability gap.

The lesson, made mechanical: before spending any budget on the expensive
causal-attribution step (regenerating worlds, running an ablation catalog,
running significance tests), first resample the *exact same* config+seed a
handful of times and check the failure is stable. This is deliberately the
cheapest possible check -- same prompt, no ablation catalog, no fresh
seeds -- so it should run first and gate everything downstream.
`cartographer.ablate.attribute.attribute_failure` calls this gate before
doing anything else, and returns early (`stable=False`) if the failure
evaporates on resample, which is exactly the class of error the Priya case
was.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..confirm.stats import wilson_interval

# `(config, task_seed) -> passed` -- same contract as
# `cartographer.schedule.scheduler.TaskEvaluator` / `PipelineEvaluator`.
Evaluator = Callable[[dict[str, float], int], bool]


@dataclass(frozen=True)
class ResampleResult:
    """Outcome of resampling one (config, task_seed) pair `n` times.

    `is_stable_failure` is the gate: True iff `pass_rate <= stable_threshold`,
    i.e. the failure held up under repeated sampling rather than evaporating.
    A pass_rate near 0.5 with small `n` (like fake model C in the tests) is
    the canonical case this gate is meant to catch and refuse to escalate.
    """

    config: dict[str, float]
    task_seed: int
    n: int
    n_pass: int
    pass_rate: float
    is_stable_failure: bool
    wilson_lo: float
    wilson_hi: float

    def to_json_dict(self) -> dict:
        return {
            "config": dict(self.config),
            "task_seed": self.task_seed,
            "n": self.n,
            "n_pass": self.n_pass,
            "pass_rate": self.pass_rate,
            "is_stable_failure": self.is_stable_failure,
            "wilson_lo": self.wilson_lo,
            "wilson_hi": self.wilson_hi,
        }


def resample_failure(
    evaluator: Evaluator,
    config: dict[str, float],
    task_seed: int,
    n: int = 5,
    stable_threshold: float = 0.5,
) -> ResampleResult:
    """Re-run the SAME `config` + `task_seed` through `evaluator` `n` times.

    This deliberately does NOT vary the seed -- the point is to measure
    the model's own run-to-run variance on one exact prompt, not to sample
    fresh tasks. `is_stable_failure = pass_rate <= stable_threshold`: the
    default 0.5 means "at least half the resamples still failed," which
    is exactly the bar the Priya case failed to clear (4/5 passed).
    """
    n_pass = 0
    for _ in range(n):
        # Deliberately the SAME (config, task_seed) every call -- this
        # measures the model's own run-to-run variance on one exact
        # prompt (e.g. MoE expert-routing noise at temp=0), not variety
        # across fresh tasks. A real evaluator wraps a live model call
        # that can return different answers to an identical prompt; a
        # fake test evaluator that is fully deterministic on
        # (config, task_seed) alone should build its own noise in (see
        # fake model C in tests/test_ablate.py, a seeded coin flip keyed
        # on an internal call counter) to exercise this gate honestly.
        ok = evaluator(config, task_seed)
        n_pass += bool(ok)
    pass_rate = n_pass / n if n else 0.0
    interval = wilson_interval(n_pass, n)
    return ResampleResult(
        config=dict(config),
        task_seed=task_seed,
        n=n,
        n_pass=n_pass,
        pass_rate=pass_rate,
        is_stable_failure=pass_rate <= stable_threshold,
        wilson_lo=interval.low,
        wilson_hi=interval.high,
    )
