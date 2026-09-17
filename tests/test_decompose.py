"""Validation tests for the Phase 3 failure decomposition engine.

Exercises `cartographer.decompose.decompose` against the Phase 0 synthetic
model zoo (`cartographer.synthetic.ZOO`), checking that it correctly
recovers single/pairwise/higher-order failure causes, handles noise, respects
budget, and is deterministic.
"""

from __future__ import annotations

from cartographer.decompose import ProbeOutcome, decompose
from cartographer.synthetic import ZOO


def _evaluator_for(name: str):
    model = ZOO[name]
    return lambda d, s: model.evaluate(d, s)


def test_clean_threshold_single_sufficient() -> None:
    evaluator = _evaluator_for("clean_threshold")
    failing = {"temporal": 10, "coreference": 5, "distractors": 20}
    baseline = {"temporal": 1, "coreference": 0, "distractors": 0}

    result = decompose(evaluator, failing, baseline)

    assert result.kind == "single_sufficient"
    assert result.factors == ("temporal",)
    assert "temporal" in result.necessary_factors


def test_pairwise_interaction_model() -> None:
    evaluator = _evaluator_for("pairwise_interaction")
    failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    baseline = {"temporal": 1, "coreference": 1, "distractors": 1}

    result = decompose(evaluator, failing, baseline)

    assert result.kind == "pairwise_interaction"
    assert result.factors == ("coreference", "temporal")
    assert "distractors" not in result.factors


def test_three_way_interaction() -> None:
    evaluator = _evaluator_for("three_way_interaction")
    failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    baseline = {"temporal": 1, "coreference": 1, "distractors": 1}

    result = decompose(evaluator, failing, baseline)

    assert result.kind == "higher_order_interaction"
    assert result.factors == ("coreference", "distractors", "temporal")


def test_no_interaction_control_both_singles_sufficient() -> None:
    evaluator = _evaluator_for("no_interaction_control")
    failing = {"temporal": 12, "distractors": 80, "coreference": 5}
    baseline = {"temporal": 1, "distractors": 1, "coreference": 1}

    result = decompose(evaluator, failing, baseline)

    assert result.kind == "single_sufficient"
    assert "temporal" in result.factors
    assert "distractors" in result.factors
    assert "interaction" not in result.notes.lower()


def test_noisy_interaction_robust_across_seed_bases() -> None:
    evaluator = _evaluator_for("noisy_interaction")
    failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    baseline = {"temporal": 1, "coreference": 1, "distractors": 1}

    for seed_base in (0, 101, 5000):
        result = decompose(evaluator, failing, baseline, k_trials=9, seed_base=seed_base)
        assert result.kind == "pairwise_interaction", seed_base
        assert result.factors == ("coreference", "temporal"), seed_base


def test_unexplained_when_failing_config_does_not_fail() -> None:
    def always_pass(_d: dict, _s: int) -> bool:
        return True

    failing = {"temporal": 10, "coreference": 5, "distractors": 20}
    baseline = {"temporal": 1, "coreference": 0, "distractors": 0}

    result = decompose(always_pass, failing, baseline)

    assert result.kind == "unexplained"


def test_budget_exhaustion() -> None:
    evaluator = _evaluator_for("pairwise_interaction")
    failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    baseline = {"temporal": 1, "coreference": 1, "distractors": 1}

    result = decompose(evaluator, failing, baseline, budget=20)

    assert "budget_exhausted" in result.notes
    assert result.budget_spent <= 20


def test_determinism() -> None:
    evaluator = _evaluator_for("pairwise_interaction")
    failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    baseline = {"temporal": 1, "coreference": 1, "distractors": 1}

    first = decompose(evaluator, failing, baseline)
    second = decompose(evaluator, failing, baseline)

    assert first.to_json_dict() == second.to_json_dict()


def test_evidence_completeness() -> None:
    evaluator = _evaluator_for("pairwise_interaction")
    failing = {"temporal": 8, "coreference": 6, "distractors": 50}
    baseline = {"temporal": 1, "coreference": 1, "distractors": 1}
    active = {"temporal", "coreference", "distractors"}

    result = decompose(evaluator, failing, baseline)

    for outcome in result.evidence:
        assert isinstance(outcome, ProbeOutcome)
        for key in active:
            fv = failing.get(key, 0.0)
            bv = baseline.get(key, 0.0)
            cv = outcome.config.get(key, 0.0)
            if fv == bv:
                # Not an active factor: must equal shared value everywhere.
                assert cv == fv
            else:
                assert cv in (fv, bv)

    assert result.budget_spent == sum(e.n_trials for e in result.evidence)


def test_full_synthetic_suite_still_green() -> None:
    # Sanity: importing decompose alongside synthetic doesn't break the zoo.
    assert "clean_threshold" in ZOO
    assert "pairwise_interaction" in ZOO
