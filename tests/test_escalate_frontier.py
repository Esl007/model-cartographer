"""Tests for the opt-in escalation frontier extras: early-stopped probes,
the reproducibility certificate, and ray scouting. Deterministic fakes only;
zero network calls, no LLM.
"""

from __future__ import annotations

import functools
import json

import pytest

from cartographer.confirm import ConfirmationResult
from cartographer.schedule.certificate import ReproducibilityCertificate, certify
from cartographer.schedule.escalate import (
    BreakingPoint,
    EscalationRay,
    RayDim,
    ScoutResult,
    _bucket_decided,
    find_breaking_point,
    scout_rays,
)
from cartographer.synthetic.rules import InteractionRule, LogisticRule

# --- fakes (mirroring tests/test_escalate.py and tests/test_degradation_onset.py)

K = 10
_CYCLE = 10
# Ramp/bisect seeds are seed_base + counter < 2_000; decompose starts at
# seed_base + 2_000 and confirmation at >= 10_000.
_SEARCH_SEED_CEIL = 2_000

RAY_X = EscalationRay(dims=(RayDim("x", 0.0, 1.0, False),))


class _StepEvaluator:
    """Pass rate is an exact function of t (1.0 below soft, mid_rate between,
    0.0 at/above hard), keyed on seed % _CYCLE so a K=_CYCLE probe hits the
    rate exactly and parallelism cannot change any outcome."""

    def __init__(self, soft: float, hard: float, mid_rate: float = 0.5) -> None:
        self.soft, self.hard, self.mid_rate = soft, hard, mid_rate

    def __call__(self, config: dict, seed: int) -> bool:
        t = config["x"]
        if t < self.soft:
            return True
        if t >= self.hard:
            return False
        return (seed % _CYCLE) < round(self.mid_rate * _CYCLE)


def _two_dim_ray() -> EscalationRay:
    return EscalationRay(dims=(RayDim("a", 0.0, 10.0, False), RayDim("b", 0.0, 20.0, False)))


_SUM_RULE = LogisticRule(dim="s", midpoint=7.7, slope=30.0)
_INTERACTION = InteractionRule(conditions={"a": 5.0, "b": 10.0})


def logistic_sum_evaluator(config: dict, seed: int) -> bool:
    return _SUM_RULE.evaluate({"s": 0.6 * config.get("a", 0.0) + 0.4 * config.get("b", 0.0)}, seed)


def interaction_evaluator(config: dict, seed: int) -> bool:
    return _INTERACTION.evaluate(config, seed)


class _Counting:
    """Wraps an evaluator; records every (config, seed) call."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[tuple[tuple, int]] = []

    def __call__(self, config: dict, seed: int) -> bool:
        self.calls.append((tuple(sorted(config.items())), seed))
        return self.inner(config, seed)

    @property
    def search_calls(self) -> int:
        return sum(1 for _, s in self.calls if s < _SEARCH_SEED_CEIL)


# (name, evaluator factory, ray, kwargs) -- every deterministic fake the
# escalate/degradation suites use.
_FAKES = [
    ("step_break", lambda: _StepEvaluator(0.3, 0.8, 0.5), RAY_X, dict(budget=200, k_trials=K)),
    ("step_never_breaks", lambda: _StepEvaluator(0.3, 99.0, 0.5), RAY_X, dict(budget=200, k_trials=K)),
    ("step_flawless", lambda: _StepEvaluator(99.0, 99.0), RAY_X, dict(budget=200, k_trials=K)),
    ("step_shallow_dip", lambda: _StepEvaluator(0.3, 99.0, 0.9), RAY_X, dict(budget=200, k_trials=K)),
    ("step_cliff", lambda: _StepEvaluator(0.5, 0.5), RAY_X, dict(budget=200, k_trials=K)),
    ("logistic_sum", lambda: logistic_sum_evaluator, _two_dim_ray(), dict(budget=350)),
    ("interaction", lambda: interaction_evaluator, _two_dim_ray(), dict(budget=350)),
]
_FAKE_IDS = [f[0] for f in _FAKES]
_FAKE_BY_NAME = {f[0]: f for f in _FAKES}


@functools.lru_cache(maxsize=None)
def _run(name: str, early_stop: bool = False, parallelism: int = 1) -> tuple[BreakingPoint, _Counting]:
    """One escalation per (fake, mode), shared across tests: the fakes are
    deterministic and the bootstrap CI makes each found=True run cost
    seconds. Callers must not mutate the returned objects."""
    _, make, ray, kw = _FAKE_BY_NAME[name]
    ev = _Counting(make())
    bp = find_breaking_point(ev, ray, early_stop=early_stop, parallelism=parallelism, **kw)
    return bp, ev


def _probed_ts(bp: BreakingPoint) -> list[float]:
    return [t for t, _ in bp.pass_curve]


# --- early stop: the decision rule -----------------------------------------


def test_bucket_decided_matches_the_brief_example():
    cuts = [("ge", 0.7), ("le", 0.3)]
    # k=7: 5 passes already -> reaches .7 and can no longer fall to .3.
    assert _bucket_decided(5, 5, 7, cuts)
    # 3 fails, 0 passes: cannot reach .7, but .3 is still reachable either way.
    assert not _bucket_decided(0, 3, 7, cuts)
    # 3 passes + 3 fails: final 3/7 or 4/7 -- the middle bucket is fixed.
    assert _bucket_decided(3, 6, 7, cuts)
    # 5 fails: at most 2/7 <= .3 -- fail bucket fixed.
    assert _bucket_decided(0, 5, 7, cuts)
    # Nothing run yet is never decided.
    assert not _bucket_decided(0, 0, 7, cuts)
    # An extra cut can only delay the decision.
    assert not _bucket_decided(5, 5, 7, cuts + [("le", 0.75)])


# --- early stop: saving -----------------------------------------------------


def test_early_stop_saves_evaluator_calls_on_every_fake():
    total_full = total_es = 0
    for name in _FAKE_IDS:
        _, full_ev = _run(name)
        _, es_ev = _run(name, True)
        assert es_ev.search_calls < full_ev.search_calls, name
        total_full += full_ev.search_calls
        total_es += es_ev.search_calls
    # Measured ~17% on these fakes (10-22% per fake); guard a floor.
    assert total_es <= 0.9 * total_full


def test_early_stop_counts_on_the_degradation_fake_are_exact():
    """Pin the measured saving on the canonical break fake (soft .3, hard .8,
    k=10): 100 ramp+bisect trials full vs 84 early-stopped."""
    full, full_ev = _run("step_break")
    es, es_ev = _run("step_break", True)
    assert (full_ev.search_calls, es_ev.search_calls) == (100, 84)
    assert full.probe_sizes == [K] * len(full.pass_curve)
    assert es.probe_sizes[0] == K  # the t=0 baseline always runs in full
    assert all(1 <= n <= K for n in es.probe_sizes)
    assert sum(es.probe_sizes) == es_ev.search_calls


# --- early stop: result invariance -----------------------------------------


@pytest.mark.parametrize("name", _FAKE_IDS)
def test_early_stop_never_changes_the_search_path(name):
    full, _ = _run(name)
    for par in (1, 4):
        es, _ = _run(name, True, par)
        assert es.found == full.found
        assert es.config_at_break == full.config_at_break
        # Same probed t sequence => same ramp stop and same bisect bracket.
        assert _probed_ts(es) == _probed_ts(full)
        assert es.degradation_onset == full.degradation_onset
        if full.note.startswith("frontier bracket"):
            assert es.note == full.note  # the bracket itself, verbatim
        # Every early-stopped rate sits in the same bucket as the full rate.
        for (_, r_full), (_, r_es) in zip(full.pass_curve, es.pass_curve):
            assert (r_es >= 0.7) == (r_full >= 0.7)
            assert (r_es <= 0.3) == (r_full <= 0.3)


def test_early_stopped_trials_are_a_prefix_of_the_full_probe():
    """Seeds stay pre-assigned: each early-stopped probe ran the first n of
    exactly the seeds the full run used for that probe."""
    full, full_ev = _run("step_break")
    es, es_ev = _run("step_break", True)
    full_search = [c for c in full_ev.calls if c[1] < _SEARCH_SEED_CEIL]
    es_search = [c for c in es_ev.calls if c[1] < _SEARCH_SEED_CEIL]
    i = j = 0
    for n_full, n_es in zip(full.probe_sizes, es.probe_sizes):
        assert es_search[j : j + n_es] == full_search[i : i + n_es]
        i += n_full
        j += n_es
    assert j == len(es_search)


def test_early_stop_budget_accounting_and_cost_cap_discard():
    class Capping:
        def __init__(self, cap_after: int) -> None:
            self.calls, self.cap_after = 0, cap_after
            self.capped, self.error_count = False, 0

        def __call__(self, config, seed):
            self.calls += 1
            if self.calls > self.cap_after:
                self.capped = True
                self.error_count += 1
                return False
            return True

    # Budget accounting: budget_spent == evaluator calls with early stop on.
    counting = _Counting(logistic_sum_evaluator)
    bp = find_breaking_point(counting, _two_dim_ray(), budget=120, early_stop=True)
    assert bp.budget_spent == len(counting.calls) <= 120

    # A probe cut short by the cost cap is discarded (points, curve, size).
    ev = Capping(cap_after=K + 3 * 7 + 2)
    bp = find_breaking_point(ev, RAY_X, budget=200, k_trials=K, early_stop=True)
    assert bp.found is False
    assert "cost cap" in bp.note
    assert all(rate == 1.0 for _, rate in bp.pass_curve)
    assert len(bp.probe_sizes) == len(bp.pass_curve)
    assert bp.degradation_onset is None


# --- reproducibility certificate --------------------------------------------


def _confirmation(confirmed: bool, p: float = 0.001, effect: float = 0.6) -> ConfirmationResult:
    return ConfirmationResult(
        hypothesis_kind="single_factor",
        factors=("x",),
        confirmed=confirmed,
        raw_p=p,
        corrected_alpha_threshold=0.05,
        effect_size=effect,
        intervals={},
        n_trials_spent=42,
    )


def test_certificate_found_confirmed_compact_is_reproducible():
    bp, _ = _run("interaction")
    assert bp.confirmation is not None and bp.confirmation.confirmed
    cert = bp.certificate  # filled by find_breaking_point for free
    assert cert is not None
    assert cert.reproducible is True
    assert cert.reasons == []
    assert cert.fresh_seed_confirmed is True
    assert cert.confirmation_p == bp.confirmation.raw_p
    assert cert.compact is True and cert.boundary_steps is None
    # Re-certified with an in-limit step count and a mostly-stable gate.
    again = certify(bp, gate_stable=4, gate_noise=1, boundary_steps=12)
    assert again.reproducible is True


def test_certificate_found_but_24_steps_is_not_reproducible():
    bp = BreakingPoint(found=True, confirmation=_confirmation(True))
    cert = certify(bp, boundary_steps=24)
    assert cert.reproducible is False
    assert cert.compact is False
    assert cert.reasons == ["boundary needs 24 steps, limit 12 (compounding, not structural)"]


def test_certificate_gate_mostly_noise_is_not_reproducible():
    bp = BreakingPoint(found=True, confirmation=_confirmation(True))
    noisy = certify(bp, gate_stable=1, gate_noise=4)
    assert noisy.reproducible is False
    assert len(noisy.reasons) == 1 and "noise" in noisy.reasons[0]
    # Boundary: exactly 60% stable passes; no gated FAILs passes trivially.
    assert certify(bp, gate_stable=3, gate_noise=2).reproducible is True
    assert certify(bp, gate_stable=0, gate_noise=0).reproducible is True


def test_certificate_lists_every_failing_condition():
    # Not found, never confirmed, noisy gate, sprawling boundary: four reasons.
    cert = certify(BreakingPoint(found=False), gate_stable=0, gate_noise=3, boundary_steps=30)
    assert cert.reproducible is False
    assert cert.fresh_seed_confirmed is None and cert.confirmation_p is None
    assert cert.reasons[0] == "no breaking point found"
    assert cert.reasons[1] == "no fresh-seed confirmation"
    assert "noise" in cert.reasons[2]
    assert cert.reasons[3].startswith("boundary needs 30 steps, limit 12")
    # A confirmation that ran and failed is named as such.
    failed = certify(BreakingPoint(found=True, confirmation=_confirmation(False, p=0.4, effect=0.05)))
    assert failed.fresh_seed_confirmed is False
    assert failed.confirmation_p == 0.4
    assert len(failed.reasons) == 1 and failed.reasons[0].startswith("fresh-seed confirmation failed")


def test_certificate_is_attached_on_not_found_paths_too():
    bp, _ = _run("step_flawless")
    assert bp.found is False
    assert bp.certificate is not None
    assert bp.certificate.reproducible is False
    assert "no breaking point found" in bp.certificate.reasons


def test_to_json_dict_round_trips_certificate_and_probe_sizes():
    bp, _ = _run("interaction", True)
    back = json.loads(json.dumps(bp.to_json_dict()))
    assert ReproducibilityCertificate.from_json_dict(back["certificate"]) == bp.certificate
    assert back["probe_sizes"] == bp.probe_sizes
    assert len(back["probe_sizes"]) == len(back["pass_curve"])
    # The pass_curve stays (t, rate) pairs for back-compat.
    assert all(len(pair) == 2 for pair in back["pass_curve"])
    # Unset certificate -> key omitted entirely.
    assert "certificate" not in BreakingPoint(found=False).to_json_dict()


# --- scout ------------------------------------------------------------------


def _scout_evaluator(config: dict, seed: int) -> bool:
    """`brittle` fails from its midpoint on; `solid` never fails."""
    if "brittle" in config:
        return config["brittle"] < 0.5
    return True


_SCOUT_RAYS = {
    "a_solid": EscalationRay(dims=(RayDim("solid", 0.0, 1.0, False),)),
    "z_brittle": EscalationRay(dims=(RayDim("brittle", 0.0, 1.0, False),)),
}


def test_scout_ranks_the_breaking_ray_first():
    counting = _Counting(_scout_evaluator)
    ranked = scout_rays(counting, _SCOUT_RAYS, k=3)
    assert [r.name for r in ranked] == ["z_brittle", "a_solid"]  # not alphabetical
    brittle, solid = ranked
    assert brittle.pass_rates == {0.5: 0.0, 1.0: 0.0}
    assert brittle.signal == 1.0
    assert solid.pass_rates == {0.5: 1.0, 1.0: 1.0}
    assert solid.signal == 0.0
    # 2k trials per ray, nothing more.
    assert len(counting.calls) == 2 * 3 * len(_SCOUT_RAYS)
    # Seed discipline: each ray uses seed_base + 0..2k-1 in t_points order.
    assert sorted({s for _, s in counting.calls}) == list(range(6))


def test_scout_is_deterministic_and_parallelism_invariant():
    def cyc(config: dict, seed: int) -> bool:
        key = next(iter(config))
        if config[key] < 0.5:
            return True
        return (seed % 4) < {"p": 1, "q": 3}[key]

    rays = {n: EscalationRay(dims=(RayDim(n, 0.0, 1.0, False),)) for n in ("p", "q")}
    a = scout_rays(cyc, rays, k=4, seed_base=8)
    b = scout_rays(cyc, rays, k=4, seed_base=8, parallelism=4)
    assert [r.to_json_dict() for r in a] == [r.to_json_dict() for r in b]
    assert [r.name for r in a] == ["p", "q"]
    # A ray's result does not depend on which other rays are scouted with it.
    alone = scout_rays(cyc, {"q": rays["q"]}, k=4, seed_base=8)
    assert alone[0] == a[1]


def test_scout_ties_broken_by_earliest_failure():
    def ev(config: dict, seed: int) -> bool:
        if "early" in config:  # half fail at t=0.5, all pass at t=1.0
            return not (config["early"] == 0.5 and seed % 2 == 0)
        return not (config["late"] == 1.0 and seed % 2 == 0)

    # Names chosen so alphabetical order disagrees with the tie-break.
    rays = {
        "a_late": EscalationRay(dims=(RayDim("late", 0.0, 1.0, False),)),
        "z_early": EscalationRay(dims=(RayDim("early", 0.0, 1.0, False),)),
    }
    ranked = scout_rays(ev, rays, k=2)
    assert ranked[0].signal == ranked[1].signal == 0.25
    assert [r.name for r in ranked] == ["z_early", "a_late"]
    assert isinstance(ranked[0], ScoutResult)
