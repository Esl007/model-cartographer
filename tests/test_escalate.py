"""Tests for cartographer.schedule.escalate: escalation-mode breaking-point
search against the synthetic zoo. Zero network calls.
"""

from __future__ import annotations

from cartographer.schedule.escalate import (
    BreakingPoint,
    EscalationRay,
    RayDim,
    find_breaking_point,
)
from cartographer.synthetic.rules import InteractionRule, LogisticRule


# --- synthetic evaluators --------------------------------------------------


def _two_dim_ray() -> EscalationRay:
    return EscalationRay(
        dims=(
            RayDim("a", 0.0, 10.0, False),
            RayDim("b", 0.0, 20.0, False),
        )
    )


# Weighted-sum logistic: failure depends on s = 0.6*a + 0.4*b through a sharp
# logistic. Along _two_dim_ray, s(t) = 0.6*10t + 0.4*20t = 14t, so the
# analytic P(fail)=0.5 crossing at s = midpoint = 7.7 sits at t* = 0.55.
_SUM_RULE = LogisticRule(dim="s", midpoint=7.7, slope=30.0)
_ANALYTIC_T_STAR = 7.7 / 14.0  # 0.55


def logistic_sum_evaluator(config: dict[str, float], trial_seed: int) -> bool:
    s = 0.6 * config.get("a", 0.0) + 0.4 * config.get("b", 0.0)
    return _SUM_RULE.evaluate({"s": s}, trial_seed)


def always_pass(config: dict[str, float], trial_seed: int) -> bool:
    return True


def always_fail(config: dict[str, float], trial_seed: int) -> bool:
    return False


# Fails iff BOTH a > 5 and b > 10 (i.e. both dims past their t=0.5 value):
# a genuine pairwise interaction for decompose to find at the frontier.
_INTERACTION = InteractionRule(conditions={"a": 5.0, "b": 10.0})


def interaction_evaluator(config: dict[str, float], trial_seed: int) -> bool:
    return _INTERACTION.evaluate(config, trial_seed)


# --- ray geometry ----------------------------------------------------------


def test_ray_endpoints_and_interpolation():
    ray = EscalationRay(
        dims=(RayDim("x", 2, 25, True), RayDim("y", 0.0, 0.9, False))
    )
    assert ray.config_at(0.0) == {"x": 2.0, "y": 0.0}
    assert ray.config_at(1.0) == {"x": 25.0, "y": 0.9}
    mid = ray.config_at(0.5)
    assert mid["x"] == round(2 + 0.5 * 23)  # ints rounded
    assert abs(mid["y"] - 0.45) < 1e-12


# --- ramp + bisect accuracy -------------------------------------------------


def test_finds_breaking_point_near_analytic_crossing():
    bp = find_breaking_point(logistic_sum_evaluator, _two_dim_ray(), budget=350)
    assert bp.found is True
    assert bp.t_star is not None
    assert abs(bp.t_star - _ANALYTIC_T_STAR) < 0.1
    assert bp.t_ci is not None
    lo, hi = bp.t_ci
    assert lo <= hi
    # config_at_break is the first-failing ramp/bisect config, on the hard side.
    assert bp.config_at_break["a"] > 0
    # pass_curve starts at t=0 with a passing rate.
    assert bp.pass_curve[0][0] == 0.0
    assert bp.pass_curve[0][1] >= 0.7


def test_always_pass_returns_honest_not_found():
    bp = find_breaking_point(always_pass, _two_dim_ray(), budget=200)
    assert bp.found is False
    assert bp.note == "model exceeds generator limits"
    assert bp.ceiling_pass_rate == 1.0
    assert bp.t_star is None
    assert bp.hypothesis is None
    # Ramp probed t=0 through t=1.0 inclusive.
    assert bp.pass_curve[-1][0] == 1.0


def test_degenerate_baseline_fails_at_t_zero():
    bp = find_breaking_point(always_fail, _two_dim_ray(), budget=200)
    assert bp.found is False
    assert "degenerate" in bp.note
    assert bp.budget_spent <= 7  # one probe only


# --- budget ----------------------------------------------------------------


def test_budget_respected():
    calls = {"n": 0}

    def counting(config: dict[str, float], trial_seed: int) -> bool:
        calls["n"] += 1
        return logistic_sum_evaluator(config, trial_seed)

    budget = 120
    bp = find_breaking_point(counting, _two_dim_ray(), budget=budget)
    assert calls["n"] <= budget
    assert bp.budget_spent == calls["n"]


def test_tiny_budget_gives_up_honestly():
    bp = find_breaking_point(logistic_sum_evaluator, _two_dim_ray(), budget=3)
    assert bp.found is False
    assert "budget" in bp.note


# --- decompose + confirm at the frontier ------------------------------------


def test_decompose_fires_at_frontier_and_hypothesis_attaches():
    bp = find_breaking_point(interaction_evaluator, _two_dim_ray(), budget=350)
    assert bp.found is True
    assert bp.hypothesis is not None
    assert bp.hypothesis.kind == "pairwise_interaction"
    assert set(bp.hypothesis.factors) == {"a", "b"}
    # A non-unexplained hypothesis triggers fresh-seed confirmation.
    assert bp.confirmation is not None
    assert bp.confirmation.confirmed is True
    assert bp.confirmation.effect_size > 0.2


# --- determinism -----------------------------------------------------------


def test_two_runs_are_byte_identical():
    bp1 = find_breaking_point(interaction_evaluator, _two_dim_ray(), budget=350)
    bp2 = find_breaking_point(interaction_evaluator, _two_dim_ray(), budget=350)
    assert bp1.to_json_dict() == bp2.to_json_dict()

    bp3 = find_breaking_point(logistic_sum_evaluator, _two_dim_ray(), budget=350)
    bp4 = find_breaking_point(logistic_sum_evaluator, _two_dim_ray(), budget=350)
    assert bp3.to_json_dict() == bp4.to_json_dict()


def test_to_json_dict_round_trips_through_json():
    import json

    bp = find_breaking_point(interaction_evaluator, _two_dim_ray(), budget=350)
    payload = json.dumps(bp.to_json_dict())
    back = json.loads(payload)
    assert back["found"] is True
    assert back["hypothesis"]["kind"] == "pairwise_interaction"


def test_fit_logistic_survives_wide_range_dims():
    """Regression: /atlas crashed with OverflowError in _log_loss on the
    decoy_tools 2..60 sweep (steep candidate slope x wide x range)."""
    from cartographer.schedule.estimate import fit_logistic

    pts = [(float(x), x < 30) for x in range(2, 61)] * 3
    fit = fit_logistic(pts)
    assert fit is not None
    assert 20.0 <= fit.midpoint <= 40.0
