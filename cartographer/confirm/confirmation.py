"""Phase 7: confirmation -- re-test a decompose `FailureHypothesis` on FRESH
seeds before trusting it.

`decompose()` (Phase 3) finds a candidate factor-set explanation for a
failure using probes drawn from a seed range the search process controls.
Confirmation is a second, independent check: probe the SAME implicated
config and its baseline again, on seeds that were never used during the
search that produced the hypothesis, and report an effect size + p-value
rather than a bare pass/fail classification.

Seed convention (must hold for the "fresh" guarantee to mean anything):
search-time probing (`cartographer.decompose.decompose` and anything that
calls it, e.g. explore/refine) always uses `seed_base < 10_000`. Confirmation
uses `seed_base >= 10_000` by construction (the default `seed_base=10_000`),
and any caller-supplied `seed_base` for `confirm_hypothesis`/`confirm_all`
must also be `>= 10_000` -- documented here rather than enforced at runtime
(this module has no visibility into what seed range a given decompose call
actually used), so a disjoint seed range is a design contract, not a
type-checked invariant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .stats import WilsonInterval, holm_bonferroni, two_proportion_z_test, wilson_interval

Evaluator = Callable[[dict[str, float], int], bool]


@dataclass(frozen=True)
class ConfirmationResult:
    """The outcome of re-testing one `FailureHypothesis` on fresh seeds.

    `confirmed` is the caller-facing verdict: True iff the (possibly
    multiple-comparison corrected) p-value is below `corrected_alpha_threshold`
    AND `effect_size` exceeds a minimum practical-significance bar (0.2 --
    a statistically significant but tiny effect isn't worth calling a
    confirmed capability boundary).

    `effect_size` = baseline_pass_rate - failing_pass_rate on the fresh
    trials (positive means the implicated config genuinely fails more than
    baseline; can be negative if the fresh data disagrees with the original
    hypothesis).

    `intervals` holds the Wilson 95% CI for both rates, keyed
    "baseline"/"failing", plus their raw counts, so a caller can inspect the
    evidence without re-deriving it.
    """

    hypothesis_kind: str
    factors: tuple[str, ...]
    confirmed: bool
    raw_p: float
    corrected_alpha_threshold: float
    effect_size: float
    intervals: dict[str, WilsonInterval]
    n_trials_spent: int
    baseline_config: dict[str, float] = field(default_factory=dict)
    failing_config: dict[str, float] = field(default_factory=dict)
    notes: str = ""

    def to_json_dict(self) -> dict:
        return {
            "hypothesis_kind": self.hypothesis_kind,
            "factors": list(self.factors),
            "confirmed": self.confirmed,
            "raw_p": self.raw_p,
            "corrected_alpha_threshold": self.corrected_alpha_threshold,
            "effect_size": self.effect_size,
            "intervals": {k: v.to_json_dict() for k, v in self.intervals.items()},
            "n_trials_spent": self.n_trials_spent,
            "baseline_config": dict(self.baseline_config),
            "failing_config": dict(self.failing_config),
            "notes": self.notes,
        }


_MIN_EFFECT_SIZE = 0.2


def _extract_configs(hypothesis) -> tuple[dict[str, float], dict[str, float]]:
    """Recover the (failing, baseline) configs the hypothesis was built
    from. `decompose()`'s first two evidence entries are always
    `verify_failing` and `verify_baseline` (see `cartographer/decompose/engine.py`),
    in that fixed order, regardless of hypothesis kind -- including
    "unexplained", so this works even for a hypothesis decompose gave up on.
    """
    if len(hypothesis.evidence) < 2:
        raise ValueError(
            "hypothesis has fewer than 2 evidence entries; cannot recover "
            "failing/baseline configs (expected verify_failing, verify_baseline "
            "as the first two probes)"
        )
    failing_config = dict(hypothesis.evidence[0].config)
    baseline_config = dict(hypothesis.evidence[1].config)
    return failing_config, baseline_config


def confirm_hypothesis(
    hypothesis,
    evaluator: Evaluator,
    k_trials: int = 9,
    n_fresh_seeds: int = 5,
    seed_base: int = 10_000,
) -> ConfirmationResult:
    """Re-test `hypothesis`'s implicated failing config and its baseline on
    fresh seeds, independent of whatever seeds `decompose()` used to build it.

    Probes `n_fresh_seeds` independent trial-seed blocks of `k_trials` each
    (so total trials per config = n_fresh_seeds * k_trials), computes a
    Wilson 95% CI for each rate, and a two-proportion z-test p-value for
    "are these two rates actually different." `seed_base` must be disjoint
    from whatever seed range the search that produced `hypothesis` used --
    by convention, search uses seed_base < 10_000 and confirmation uses
    seed_base >= 10_000 (the default here).
    """
    failing_config, baseline_config = _extract_configs(hypothesis)

    total_trials = n_fresh_seeds * k_trials
    failing_seeds = [seed_base + i for i in range(total_trials)]
    baseline_seeds = [seed_base + total_trials + i for i in range(total_trials)]

    failing_successes = sum(1 for s in failing_seeds if evaluator(failing_config, s))
    baseline_successes = sum(1 for s in baseline_seeds if evaluator(baseline_config, s))

    failing_pass_rate = failing_successes / total_trials if total_trials else 0.0
    baseline_pass_rate = baseline_successes / total_trials if total_trials else 0.0
    effect_size = baseline_pass_rate - failing_pass_rate

    raw_p = two_proportion_z_test(
        baseline_successes, total_trials, failing_successes, total_trials
    )

    intervals = {
        "baseline": wilson_interval(baseline_successes, total_trials),
        "failing": wilson_interval(failing_successes, total_trials),
    }

    confirmed = raw_p < 0.05 and effect_size > _MIN_EFFECT_SIZE

    return ConfirmationResult(
        hypothesis_kind=hypothesis.kind,
        factors=hypothesis.factors,
        confirmed=confirmed,
        raw_p=raw_p,
        corrected_alpha_threshold=0.05,  # overwritten by confirm_all's correction
        effect_size=effect_size,
        intervals=intervals,
        n_trials_spent=2 * total_trials,
        baseline_config=baseline_config,
        failing_config=failing_config,
        notes=(
            f"fresh probe: baseline_pass_rate={baseline_pass_rate:.3f} "
            f"failing_pass_rate={failing_pass_rate:.3f} over {total_trials} trials/config"
        ),
    )


def confirm_all(
    hypotheses: list,
    evaluator: Evaluator,
    alpha: float = 0.05,
    k_trials: int = 9,
    n_fresh_seeds: int = 5,
    seed_base: int = 10_000,
) -> list[ConfirmationResult]:
    """Confirm a family of hypotheses with Holm-Bonferroni correction across
    them, at family-wise error rate `alpha`.

    Each hypothesis gets its own disjoint fresh-seed block (so re-running
    is deterministic and no hypothesis's probing can influence another's),
    then Holm-Bonferroni is applied to the family of raw p-values; a
    hypothesis's final `confirmed` verdict is corrected_p_rejects AND
    effect_size > 0.2 -- both conditions applied, matching
    `confirm_hypothesis`'s single-hypothesis logic but with the
    Holm-corrected threshold substituted for the flat 0.05 one.
    """
    per_hypothesis_seed_span = 2 * n_fresh_seeds * k_trials
    raw_results: list[ConfirmationResult] = []
    for i, hyp in enumerate(hypotheses):
        this_seed_base = seed_base + i * per_hypothesis_seed_span
        raw_results.append(
            confirm_hypothesis(
                hyp, evaluator, k_trials=k_trials, n_fresh_seeds=n_fresh_seeds, seed_base=this_seed_base
            )
        )

    p_values = [r.raw_p for r in raw_results]
    corrections = holm_bonferroni(p_values, alpha=alpha)

    final: list[ConfirmationResult] = []
    for result, (rejected, threshold) in zip(raw_results, corrections):
        confirmed = rejected and result.effect_size > _MIN_EFFECT_SIZE
        final.append(
            ConfirmationResult(
                hypothesis_kind=result.hypothesis_kind,
                factors=result.factors,
                confirmed=confirmed,
                raw_p=result.raw_p,
                corrected_alpha_threshold=threshold,
                effect_size=result.effect_size,
                intervals=result.intervals,
                n_trials_spent=result.n_trials_spent,
                baseline_config=result.baseline_config,
                failing_config=result.failing_config,
                notes=result.notes,
            )
        )
    return final
