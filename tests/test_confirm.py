"""Acceptance tests for Phase 7 confirmation (cartographer.confirm) against
the Phase 0 synthetic model zoo.

`decompose()` finds candidate factor-set explanations using probes drawn
from a seed range the search controls; confirmation re-tests those
explanations on FRESH seeds (>= 10_000 by convention, disjoint from any
search-time seed_base < 10_000) and reports an effect size + p-value.
"""

from __future__ import annotations

from cartographer.confirm import ConfirmationResult, confirm_all, confirm_hypothesis
from cartographer.confirm.stats import holm_bonferroni, two_proportion_z_test, wilson_interval
from cartographer.decompose import decompose
from cartographer.decompose.hypothesis import FailureHypothesis, ProbeOutcome
from cartographer.synthetic import ZOO


def _evaluator_for(name: str):
    model = ZOO[name]
    return lambda d, s: model.evaluate(d, s)


def _hand_build_hypothesis(kind: str, factors: tuple[str, ...], failing_cfg: dict, baseline_cfg: dict) -> FailureHypothesis:
    """A minimal hypothesis with just the two evidence entries
    confirm_hypothesis needs (verify_failing, verify_baseline), same
    convention decompose() itself always produces first."""
    evidence = [
        ProbeOutcome(config=dict(failing_cfg), pass_rate=0.0, n_trials=1, trial_seeds=[0]),
        ProbeOutcome(config=dict(baseline_cfg), pass_rate=1.0, n_trials=1, trial_seeds=[1]),
    ]
    return FailureHypothesis(
        kind=kind, factors=factors, necessary_factors=factors, evidence=evidence, budget_spent=2,
    )


# --- real hypothesis from decompose confirms with effect size near 1.0 -----


def test_confirm_pairwise_interaction_hypothesis_confirmed_near_full_effect() -> None:
    evaluator = _evaluator_for("pairwise_interaction")
    failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    baseline = {"temporal": 1, "coreference": 1, "distractors": 1}

    hypothesis = decompose(evaluator, failing, baseline, seed_base=0)
    assert hypothesis.kind == "pairwise_interaction"

    result = confirm_hypothesis(hypothesis, evaluator, k_trials=9, n_fresh_seeds=5, seed_base=10_000)

    assert isinstance(result, ConfirmationResult)
    assert result.confirmed is True
    assert result.effect_size > 0.9, result.effect_size
    assert result.raw_p < 0.001
    assert result.factors == ("coreference", "temporal")


# --- hand-built FALSE hypothesis is correctly rejected ----------------------


def test_confirm_false_hypothesis_on_clean_threshold_not_confirmed() -> None:
    evaluator = _evaluator_for("clean_threshold")  # only depends on `temporal`
    # False claim: distractors cause the failure, at a config that actually
    # passes (temporal stays at baseline == 1, well under the cutoff of 6).
    failing_cfg = {"temporal": 1, "coreference": 0, "distractors": 100}
    baseline_cfg = {"temporal": 1, "coreference": 0, "distractors": 0}
    hypothesis = _hand_build_hypothesis(
        "single_sufficient", ("distractors",), failing_cfg, baseline_cfg
    )

    result = confirm_hypothesis(hypothesis, evaluator, k_trials=9, n_fresh_seeds=5, seed_base=10_000)

    assert result.confirmed is False
    assert abs(result.effect_size) < 0.2


# --- Holm-Bonferroni across a family: 1 real + 2 false -> only the real one


def test_holm_bonferroni_family_only_real_hypothesis_confirmed() -> None:
    evaluator = _evaluator_for("pairwise_interaction")
    real_failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    real_baseline = {"temporal": 1, "coreference": 1, "distractors": 1}
    real_hypothesis = decompose(evaluator, real_failing, real_baseline, seed_base=0)
    assert real_hypothesis.kind == "pairwise_interaction"

    # Two false hypotheses: claim `distractors` alone matters, at configs
    # where distractors changes but the actual rule (interaction of
    # temporal+coreference) is untouched, so pass rate doesn't move.
    false_hyp_1 = _hand_build_hypothesis(
        "single_sufficient", ("distractors",),
        {"temporal": 1, "coreference": 1, "distractors": 90}, {"temporal": 1, "coreference": 1, "distractors": 1},
    )
    false_hyp_2 = _hand_build_hypothesis(
        "single_sufficient", ("distractors",),
        {"temporal": 1, "coreference": 1, "distractors": 60}, {"temporal": 1, "coreference": 1, "distractors": 1},
    )

    results = confirm_all(
        [real_hypothesis, false_hyp_1, false_hyp_2], evaluator,
        alpha=0.05, k_trials=9, n_fresh_seeds=5, seed_base=10_000,
    )

    assert len(results) == 3
    confirmed_flags = [r.confirmed for r in results]
    assert confirmed_flags == [True, False, False], confirmed_flags
    # corrected thresholds are Holm-adjusted (<=  base alpha), not just 0.05 flat.
    assert all(r.corrected_alpha_threshold <= 0.05 for r in results)


def test_confirm_all_determinism() -> None:
    evaluator = _evaluator_for("pairwise_interaction")
    failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    baseline = {"temporal": 1, "coreference": 1, "distractors": 1}
    hypothesis = decompose(evaluator, failing, baseline, seed_base=0)

    first = confirm_all([hypothesis], evaluator, seed_base=10_000)
    second = confirm_all([hypothesis], evaluator, seed_base=10_000)

    assert [r.to_json_dict() for r in first] == [r.to_json_dict() for r in second]


def test_confirm_hypothesis_determinism() -> None:
    evaluator = _evaluator_for("pairwise_interaction")
    failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    baseline = {"temporal": 1, "coreference": 1, "distractors": 1}
    hypothesis = decompose(evaluator, failing, baseline, seed_base=0)

    first = confirm_hypothesis(hypothesis, evaluator, seed_base=10_000)
    second = confirm_hypothesis(hypothesis, evaluator, seed_base=10_000)

    assert first.to_json_dict() == second.to_json_dict()


def test_confirmation_result_to_json_dict_roundtrips_shape() -> None:
    evaluator = _evaluator_for("pairwise_interaction")
    failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    baseline = {"temporal": 1, "coreference": 1, "distractors": 1}
    hypothesis = decompose(evaluator, failing, baseline, seed_base=0)

    result = confirm_hypothesis(hypothesis, evaluator, seed_base=10_000)
    d = result.to_json_dict()

    assert set(d) == {
        "hypothesis_kind", "factors", "confirmed", "raw_p", "corrected_alpha_threshold",
        "effect_size", "intervals", "n_trials_spent", "baseline_config", "failing_config", "notes",
    }
    assert set(d["intervals"]) == {"baseline", "failing"}
    for interval in d["intervals"].values():
        assert set(interval) == {"point", "low", "high", "n"}


# --- unit tests for the stdlib stats primitives themselves -----------------


def test_wilson_interval_bounds_and_degenerate_cases() -> None:
    w = wilson_interval(9, 10)
    assert 0.0 <= w.low <= w.point <= w.high <= 1.0
    assert w.point == 0.9

    zero = wilson_interval(0, 0)
    assert zero.low == 0.0 and zero.high == 0.0 and zero.n == 0


def test_two_proportion_z_test_symmetric_and_null_case() -> None:
    p_equal = two_proportion_z_test(5, 10, 5, 10)
    assert p_equal == 1.0  # identical rates -> no evidence of difference

    p_diff = two_proportion_z_test(10, 10, 0, 10)
    assert p_diff < 0.01  # maximally different rates -> very small p


def test_holm_bonferroni_matches_hand_computation() -> None:
    # p-values 0.01, 0.02, 0.20 at alpha=0.05, m=3:
    # sorted: 0.01 (thresh 0.05/3=0.0167 -> reject), 0.02 (thresh 0.05/2=0.025 -> reject),
    # 0.20 (thresh 0.05/1=0.05 -> not reject)
    results = holm_bonferroni([0.02, 0.01, 0.20], alpha=0.05)
    rejected = [r for r, _ in results]
    assert rejected == [True, True, False]
