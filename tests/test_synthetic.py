"""Acceptance tests for the synthetic validation harness."""

from __future__ import annotations

from cartographer.synthetic import (
    ZOO,
    NoiseWrapper,
    ThresholdRule,
    bisection_strategy,
    grid_strategy,
    random_strategy,
    recovery_error,
)


def test_determinism_across_zoo() -> None:
    """Same (dims, trial_seed) must always yield the same outcome."""
    dims_variants = [
        {"temporal": 0}, {"temporal": 6}, {"temporal": 12},
        {"temporal": 5, "coreference": 2}, {"distractors": 70},
        {"temporal": 4, "coreference": 3, "distractors": 30},
        {}, {"temporal": 6.01},
    ]
    seeds = [0, 1, 7, 42, 999]
    count = 0
    for model in ZOO.values():
        for dims in dims_variants:
            for seed in seeds:
                first = model.evaluate(dims, trial_seed=seed)
                second = model.evaluate(dims, trial_seed=seed)
                assert first == second
                count += 1
    assert count >= 100


def test_threshold_semantics() -> None:
    model = ZOO["clean_threshold"]
    assert model.evaluate({"temporal": 6}, trial_seed=0) is True
    assert model.evaluate({"temporal": 6.01}, trial_seed=0) is False


def test_interaction_semantics() -> None:
    model = ZOO["pairwise_interaction"]
    assert model.evaluate({"temporal": 10}, trial_seed=0) is True
    assert model.evaluate({"coreference": 10}, trial_seed=0) is True
    assert model.evaluate({"temporal": 10, "coreference": 10}, trial_seed=0) is False


def test_no_interaction_control() -> None:
    model = ZOO["no_interaction_control"]
    assert model.evaluate({"temporal": 9}, trial_seed=0) is False
    assert model.evaluate({"distractors": 70}, trial_seed=0) is False


def test_logistic_calibration() -> None:
    model = ZOO["soft_boundary"]
    n = 2000

    def fail_rate(temporal: float) -> float:
        fails = sum(
            1 for seed in range(n) if not model.evaluate({"temporal": temporal}, trial_seed=seed)
        )
        return fails / n

    at_midpoint = fail_rate(6)
    assert 0.45 <= at_midpoint <= 0.55

    at_low = fail_rate(2)
    assert at_low < 0.05

    at_high = fail_rate(10)
    assert at_high > 0.95


def test_bisection_beats_baselines() -> None:
    model = ZOO["clean_threshold"]
    true_boundary = 6.0
    search_range = (0.0, 20.0)
    budget = 20

    bisection_result = recovery_error(
        model, "temporal", true_boundary, bisection_strategy(), search_range, budget
    )
    random_result = recovery_error(
        model, "temporal", true_boundary, random_strategy, search_range, budget
    )
    grid_result = recovery_error(
        model, "temporal", true_boundary, grid_strategy, search_range, budget
    )

    assert bisection_result.abs_error < 0.1
    assert bisection_result.abs_error <= random_result.abs_error
    assert bisection_result.abs_error <= grid_result.abs_error


def test_noise_breaks_naive_bisection_majority_vote_recovers() -> None:
    model = ZOO["noisy_threshold"]
    true_boundary = 6.0
    search_range = (0.0, 20.0)

    single_trial_found_bad = False
    for offset in range(5):
        naive_strategy = bisection_strategy(trials_per_point=1, seed_offset=offset)
        result = recovery_error(
            model, "temporal", true_boundary, naive_strategy, search_range, 40
        )
        if result.abs_error > 0.5:
            single_trial_found_bad = True
            break

    assert single_trial_found_bad

    majority_strategy = bisection_strategy(trials_per_point=9)
    majority_result = recovery_error(
        model, "temporal", true_boundary, majority_strategy, search_range, 90
    )
    assert majority_result.abs_error < 1.0


def test_noise_flip_rate() -> None:
    base = ThresholdRule("temporal", 6)
    p_flip = 0.15
    wrapped = NoiseWrapper(base, p_flip=p_flip)
    dims = {"temporal": 3}
    n = 2000
    flips = 0
    for seed in range(n):
        base_out = base.evaluate(dims, trial_seed=seed)
        wrapped_out = wrapped.evaluate(dims, trial_seed=seed)
        if base_out != wrapped_out:
            flips += 1
    measured = flips / n
    assert abs(measured - p_flip) <= 0.03
