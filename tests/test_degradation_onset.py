"""Tests for degradation-onset reporting and the tool-count escalation ray."""

from __future__ import annotations

from cartographer.exec.evaluator import (
    _TOOL_COUNT_CEILING,
    default_tool_count_ray,
    default_tool_ray,
)
from cartographer.schedule.escalate import EscalationRay, RayDim, find_breaking_point


K = 10  # probe size; _CYCLE must divide it evenly for exact rates
_CYCLE = 10


class _Evaluator:
    """Pass rate is an EXACT function of t: 1.0 below `soft`, exactly
    `mid_rate` between `soft` and `hard`, 0.0 at/above `hard`.

    Probe seeds are consecutive integers (`seed_base + spent`), so keying on
    `seed % _CYCLE` walks a full cycle per probe of K=_CYCLE trials and
    yields the target rate exactly -- no sampling variance to make these
    tests flaky, and unaffected by probe parallelism (it depends only on the
    seed, never on completion order).
    """

    def __init__(self, soft: float, hard: float, mid_rate: float = 0.5) -> None:
        self.soft, self.hard, self.mid_rate = soft, hard, mid_rate

    def __call__(self, config: dict, seed: int) -> bool:
        t = config["x"]
        if t < self.soft:
            return True
        if t >= self.hard:
            return False
        return (seed % _CYCLE) < round(self.mid_rate * _CYCLE)


RAY = EscalationRay(dims=(RayDim("x", 0.0, 1.0, False),))


def test_degradation_onset_recorded_when_model_never_breaks():
    """The headline case: a model that degrades badly but never collapses
    below fail_threshold used to report found=False and nothing else."""
    ev = _Evaluator(soft=0.3, hard=99.0, mid_rate=0.5)
    bp = find_breaking_point(ev, RAY, budget=200, k_trials=K)
    assert bp.found is False
    assert bp.degradation_onset is not None
    assert bp.degradation_onset >= 0.3
    assert bp.baseline_rate == 1.0
    assert "x" in bp.config_at_degradation


def test_degradation_onset_precedes_break_when_both_exist():
    ev = _Evaluator(soft=0.3, hard=0.8, mid_rate=0.5)
    bp = find_breaking_point(ev, RAY, budget=200, k_trials=K)
    assert bp.found is True
    assert bp.degradation_onset is not None
    assert bp.t_star is not None
    assert bp.degradation_onset <= bp.t_star


def test_no_degradation_onset_for_a_flawless_model():
    ev = _Evaluator(soft=99.0, hard=99.0)
    bp = find_breaking_point(ev, RAY, budget=200, k_trials=K)
    assert bp.found is False
    assert bp.degradation_onset is None
    assert bp.config_at_degradation == {}


def test_degradation_drop_threshold_is_respected():
    """A shallow dip should not register as onset at a strict threshold."""
    ev = _Evaluator(soft=0.3, hard=99.0, mid_rate=0.9)  # only a 10pt drop
    strict = find_breaking_point(ev, RAY, budget=200, k_trials=K, degradation_drop=0.25)
    assert strict.degradation_onset is None
    lenient = find_breaking_point(
        _Evaluator(soft=0.3, hard=99.0, mid_rate=0.9),
        RAY,
        budget=200,
        k_trials=K,
        degradation_drop=0.05,
    )
    assert lenient.degradation_onset is not None


def test_degradation_fields_serialize():
    ev = _Evaluator(soft=0.3, hard=99.0, mid_rate=0.5)
    d = find_breaking_point(ev, RAY, budget=200, k_trials=K).to_json_dict()
    for key in ("degradation_onset", "config_at_degradation", "baseline_rate"):
        assert key in d


# --- tool-count ray --------------------------------------------------------


def test_tool_count_ray_is_single_dim_over_decoys():
    ray = default_tool_count_ray()
    assert len(ray.dims) == 1
    dim = ray.dims[0]
    assert dim.name == "decoy_tools"
    assert dim.is_int
    assert dim.base_value == 2
    assert dim.limit_value == _TOOL_COUNT_CEILING


def test_tool_count_ray_endpoints_and_monotonicity():
    ray = default_tool_count_ray()
    lo = ray.config_at(0.0)["decoy_tools"]
    hi = ray.config_at(1.0)["decoy_tools"]
    assert lo == 2
    assert hi == _TOOL_COUNT_CEILING
    vals = [ray.config_at(t / 10)["decoy_tools"] for t in range(11)]
    assert vals == sorted(vals)


def test_tool_count_ray_ceiling_exceeds_documented_safe_zone():
    """The published tool-selection safe zone is ~10-20 tools; the catalog is
    roughly decoys + 9 fixed tools, so the ceiling must clear it decisively
    or the sweep never leaves the easy regime (the bug this ray fixes)."""
    assert _TOOL_COUNT_CEILING >= 40


def test_trap_ray_unchanged():
    """The pre-existing trap ray must not have been disturbed."""
    names = {d.name for d in default_tool_ray().dims}
    assert "poison_traps" in names
    assert "decoy_tools" not in names
