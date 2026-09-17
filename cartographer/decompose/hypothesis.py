"""Data model for failure decomposition: probes and the hypothesis they support.

`ProbeOutcome` captures one noise-robust probe (k trials at a single config).
`FailureHypothesis` captures the engine's conclusion: which factor(s) explain
a failing config relative to a passing baseline, backed by the probe evidence
that led there.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProbeOutcome:
    """One noise-robust probe: `n_trials` evaluations of `config`.

    `pass_rate` is fraction of trials that passed. `trial_seeds` records the
    exact seeds used so the probe is fully reproducible.

    Invariant: len(trial_seeds) == n_trials.
    """

    config: dict[str, float]
    pass_rate: float
    n_trials: int
    trial_seeds: list[int]

    def to_json_dict(self) -> dict:
        return {
            "config": dict(self.config),
            "pass_rate": self.pass_rate,
            "n_trials": self.n_trials,
            "trial_seeds": list(self.trial_seeds),
        }


@dataclass(frozen=True)
class FailureHypothesis:
    """The engine's conclusion about what factor(s) cause a failing config.

    `kind` is one of "single_sufficient", "pairwise_interaction",
    "higher_order_interaction", "unexplained".

    `factors` is the sorted tuple of factors implicated by the hypothesis
    (e.g. all singles found sufficient, or the union of factors in
    interacting pairs/triples).

    `necessary_factors` is the sorted tuple of active factors whose
    relaxation (back to baseline) alone restores passing behavior — this is
    a separate, complementary notion of necessity computed regardless of
    which sufficiency kind is ultimately reported.

    `evidence` holds every ProbeOutcome gathered while building this
    hypothesis, in the order they were probed.

    `budget_spent` is the total number of trials consumed (== sum of
    n_trials across `evidence`).
    """

    kind: str
    factors: tuple[str, ...]
    necessary_factors: tuple[str, ...]
    evidence: list[ProbeOutcome] = field(default_factory=list)
    budget_spent: int = 0
    notes: str = ""

    def to_json_dict(self) -> dict:
        return {
            "kind": self.kind,
            "factors": list(self.factors),
            "necessary_factors": list(self.necessary_factors),
            "evidence": [e.to_json_dict() for e in self.evidence],
            "budget_spent": self.budget_spent,
            "notes": self.notes,
        }
