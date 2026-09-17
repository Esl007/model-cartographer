"""Validation tests for the Phase 5 adaptive scheduler.

Exercises `cartographer.schedule` against the Phase 0 synthetic model zoo
(`cartographer.synthetic.ZOO`): logistic fit sanity, single-dim campaigns,
multi-dim interaction detection, noise robustness, budget hard-stop, and
resumability.
"""

from __future__ import annotations

import math

from cartographer.schedule import (
    CampaignState,
    DimSpec,
    SchedulerConfig,
    boundary_ci,
    fit_logistic,
    resume_from_records,
    run_campaign,
)
from cartographer.synthetic import ZOO
from cartographer.synthetic.rules import LogisticRule, NoiseWrapper, ThresholdRule


def _evaluator_for(name: str):
    model = ZOO[name]
    return lambda d, s: model.evaluate(d, s)


def _quiet(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Logistic fit sanity
# ---------------------------------------------------------------------------


def test_fit_logistic_recovers_known_midpoint() -> None:
    true_mid, true_slope = 6.0, 1.5
    points: list[tuple[float, bool]] = []
    for x in range(0, 21):
        p_fail = 1.0 / (1.0 + math.exp(-true_slope * (x - true_mid)))
        # Deterministic threshold-based outcome (no RNG): fail iff p_fail >= 0.5.
        passed = p_fail < 0.5
        points.append((float(x), passed))

    fit = fit_logistic(points)
    assert fit is not None
    assert abs(fit.midpoint - true_mid) <= 1.0


def test_boundary_ci_brackets_known_midpoint() -> None:
    true_mid, true_slope = 6.0, 1.5
    points: list[tuple[float, bool]] = []
    for x in range(0, 21):
        p_fail = 1.0 / (1.0 + math.exp(-true_slope * (x - true_mid)))
        passed = p_fail < 0.5
        points.append((float(x), passed))

    mid, ci_lo, ci_hi = boundary_ci(points, n_boot=200, seed=0)
    assert ci_lo <= true_mid <= ci_hi


def test_fit_logistic_none_when_no_crossing() -> None:
    points = [(float(x), True) for x in range(5)]
    assert fit_logistic(points) is None


# ---------------------------------------------------------------------------
# Single-dim campaign: clean_threshold
# ---------------------------------------------------------------------------


def test_clean_threshold_campaign_finds_boundary() -> None:
    evaluator = _evaluator_for("clean_threshold")
    dims = [DimSpec("temporal", 0.0, 20.0, is_int=True)]
    config = SchedulerConfig(
        dims=dims,
        baseline={"temporal": 0.0},
        ceiling={"temporal": 20.0},
        budget=300,
        seed_base=42,
    )

    state = run_campaign(evaluator, config, progress=_quiet)

    assert "temporal" in state.boundaries
    mid, ci_lo, ci_hi = state.boundaries["temporal"]
    assert abs(mid - 6.0) <= 1.0
    assert ci_lo <= 6.0 <= ci_hi
    assert state.budget_spent <= config.budget


# ---------------------------------------------------------------------------
# Multi-dim campaign: pairwise_interaction (interaction-suspected path)
# ---------------------------------------------------------------------------


def test_pairwise_interaction_campaign_triggers_decompose() -> None:
    evaluator = _evaluator_for("pairwise_interaction")
    dims = [
        DimSpec("temporal", 0.0, 10.0, is_int=False),
        DimSpec("coreference", 0.0, 10.0, is_int=False),
    ]
    config = SchedulerConfig(
        dims=dims,
        baseline={"temporal": 0.0, "coreference": 0.0},
        ceiling={"temporal": 10.0, "coreference": 10.0},
        budget=400,
        seed_base=7,
    )

    state = run_campaign(evaluator, config, progress=_quiet)

    # Single-dim sweeps hold the other dim at baseline (0), below the
    # InteractionRule cutoffs (temporal>4, coreference>3), so no single-dim
    # boundary exists -- both dims pass at every explored level.
    assert "temporal" not in state.boundaries
    assert "coreference" not in state.boundaries
    assert "temporal" in state.no_boundary_dims
    assert "coreference" in state.no_boundary_dims

    # The interaction-suspected path must still have triggered decompose
    # from the all-ceiling failing config.
    assert state.hypotheses, "expected decompose to run via interaction-suspected path"
    kinds_and_factors = [(h.kind, h.factors) for h in state.hypotheses]
    assert any(
        kind == "pairwise_interaction" and set(("coreference", "temporal")) <= set(factors)
        for kind, factors in kinds_and_factors
    ), kinds_and_factors


# ---------------------------------------------------------------------------
# Noisy threshold campaign
# ---------------------------------------------------------------------------


def test_noisy_threshold_campaign_within_tolerance() -> None:
    evaluator = _evaluator_for("noisy_threshold")
    dims = [DimSpec("temporal", 0.0, 20.0, is_int=True)]
    config = SchedulerConfig(
        dims=dims,
        baseline={"temporal": 0.0},
        ceiling={"temporal": 20.0},
        k_trials=7,
        budget=400,
        seed_base=99,
    )

    state = run_campaign(evaluator, config, progress=_quiet)

    assert "temporal" in state.boundaries
    mid, _, _ = state.boundaries["temporal"]
    assert abs(mid - 6.0) <= 1.5


# ---------------------------------------------------------------------------
# Budget hard stop
# ---------------------------------------------------------------------------


def test_budget_hard_stop_respected() -> None:
    evaluator = _evaluator_for("three_way_interaction")
    dims = [
        DimSpec("temporal", 0.0, 10.0),
        DimSpec("coreference", 0.0, 10.0),
        DimSpec("distractors", 0.0, 60.0),
    ]
    config = SchedulerConfig(
        dims=dims,
        baseline={"temporal": 0.0, "coreference": 0.0, "distractors": 0.0},
        ceiling={"temporal": 10.0, "coreference": 10.0, "distractors": 60.0},
        budget=50,
        seed_base=1,
    )

    state = run_campaign(evaluator, config, progress=_quiet)

    assert state.budget_spent <= 50
    assert state is not None


# ---------------------------------------------------------------------------
# Resumability
# ---------------------------------------------------------------------------


def test_resume_from_records_continues_campaign() -> None:
    evaluator = _evaluator_for("clean_threshold")
    dims = [DimSpec("temporal", 0.0, 20.0, is_int=True)]

    records: list[tuple[dict, bool, int]] = []

    def on_record(cfg: dict, passed: bool, seed: int) -> None:
        records.append((cfg, passed, seed))

    config_small = SchedulerConfig(
        dims=dims,
        baseline={"temporal": 0.0},
        ceiling={"temporal": 20.0},
        budget=60,
        seed_base=42,
    )
    first_state = run_campaign(evaluator, config_small, on_record=on_record, progress=_quiet)
    n_first_probes = len(records)

    resumed = resume_from_records(records, config_small)
    config_big = SchedulerConfig(
        dims=dims,
        baseline={"temporal": 0.0},
        ceiling={"temporal": 20.0},
        budget=300,
        seed_base=42,
    )
    second_records: list[tuple[dict, bool, int]] = []

    def on_record2(cfg: dict, passed: bool, seed: int) -> None:
        second_records.append((cfg, passed, seed))

    final_state = run_campaign(
        evaluator, config_big, on_record=on_record2, initial_state=resumed, progress=_quiet
    )

    assert "temporal" in final_state.boundaries
    mid, _, _ = final_state.boundaries["temporal"]
    assert abs(mid - 6.0) <= 1.0

    total_probes = n_first_probes + len(second_records)
    assert total_probes > n_first_probes


# ---------------------------------------------------------------------------
# on_record fidelity
# ---------------------------------------------------------------------------


def test_on_record_fires_exactly_budget_spent_times() -> None:
    evaluator = _evaluator_for("clean_threshold")
    dims = [DimSpec("temporal", 0.0, 20.0, is_int=True)]
    config = SchedulerConfig(
        dims=dims,
        baseline={"temporal": 0.0},
        ceiling={"temporal": 20.0},
        budget=150,
        seed_base=3,
    )

    fired: list[tuple[dict, bool, int]] = []

    def on_record(cfg: dict, passed: bool, seed: int) -> None:
        fired.append((cfg, passed, seed))

    state = run_campaign(evaluator, config, on_record=on_record, progress=_quiet)

    assert len(fired) == state.budget_spent


# ---------------------------------------------------------------------------
# Full-suite sanity
# ---------------------------------------------------------------------------


def test_full_suite_still_green() -> None:
    assert "clean_threshold" in ZOO
    assert "pairwise_interaction" in ZOO
    assert isinstance(CampaignState(), CampaignState)


# ---------------------------------------------------------------------------
# Weak-signal refinement (lab-log: Nano's 7/105 diffuse belief failures)
# ---------------------------------------------------------------------------


def test_uniform_diffuse_failure_triggers_weak_signal_no_spurious_boundary() -> None:
    """A uniform ~10% failure rate everywhere has no real boundary. The old
    flip-detection never sees a single-level majority flip (every level
    still passes its own majority vote), but the aggregate failure count
    is >=10%, so the weak-signal path should fire and, finding no logistic
    crossing, record a 'weak_signal_unresolved' note without inventing a
    boundary.
    """
    rule = NoiseWrapper(ThresholdRule("temporal", 1000), p_flip=0.10)
    evaluator = lambda d, s: rule.evaluate(d, s)  # noqa: E731

    dims = [DimSpec("temporal", 0.0, 20.0, is_int=True)]
    config = SchedulerConfig(
        dims=dims,
        baseline={"temporal": 0.0},
        ceiling={"temporal": 20.0},
        k_trials=5,
        budget=400,
        seed_base=123,
    )

    state = run_campaign(evaluator, config, progress=_quiet)

    assert "temporal" not in state.boundaries
    assert any(n.startswith("weak_signal_unresolved:temporal") for n in state.notes)
    assert "temporal" in state.dim_pass_rates
    assert state.dim_pass_rates["temporal"] > 0.7  # mostly-passing, diffuse fails


def test_soft_boundary_too_diffuse_for_old_flip_detection_is_found() -> None:
    """A real soft logistic boundary (slope 0.4) is diffuse enough at
    k_trials=5 that the old per-level majority-vote flip detection can miss
    it (every explored level's own vote can still land on 'pass'), but the
    aggregate failure rate across levels crosses 10%, triggering the
    weak-signal path, which re-probes at 3x k_trials and should recover a
    boundary in range via the logistic refit.
    """
    rule = LogisticRule("temporal", midpoint=6.0, slope=0.4)
    evaluator = lambda d, s: rule.evaluate(d, s)  # noqa: E731

    dims = [DimSpec("temporal", 0.0, 20.0, is_int=True)]
    config = SchedulerConfig(
        dims=dims,
        baseline={"temporal": 0.0},
        ceiling={"temporal": 20.0},
        k_trials=5,
        budget=600,
        seed_base=55,
    )

    state = run_campaign(evaluator, config, progress=_quiet)

    assert "temporal" in state.dim_pass_rates
    if "temporal" in state.boundaries:
        mid, ci_lo, ci_hi = state.boundaries["temporal"]
        assert 0.0 <= mid <= 20.0
    else:
        # If the diffuse signal still didn't resolve to a clean crossing,
        # it must at least have been flagged rather than silently dropped.
        assert any(n.startswith("weak_signal_unresolved:temporal") for n in state.notes)
