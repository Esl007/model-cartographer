"""Boundary-recovery strategies and error measurement against known ground truth.

A probe strategy estimates where, along one dimension, a synthetic model's
pass/fail boundary lies, holding all other dimensions at 0. Comparing the
estimate to the model's known true boundary quantifies how well a given
probing method recovers ground truth under a fixed evaluation budget.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from .models import SyntheticModel

ProbeStrategy = Callable[[SyntheticModel, str, tuple[float, float], int], float]


def _seeded_floats(seed: int, count: int) -> list[float]:
    """Deterministic sequence of `count` floats in [0, 1) derived from `seed`."""
    out = []
    for i in range(count):
        digest = hashlib.sha256(f"probe|{seed}|{i}".encode("utf-8")).hexdigest()
        out.append(int(digest[:15], 16) / float(16**15))
    return out


def _dims_at(dim: str, x: float) -> dict[str, float]:
    return {dim: x}


def _estimate_from_samples(
    samples: list[tuple[float, bool]], lo: float, hi: float
) -> float:
    """Midpoint between the highest passing x and the lowest failing x.

    Invariant: if all samples pass, estimate is `hi`; if all fail, estimate
    is `lo`; otherwise it is the midpoint of the tightest bracketing pair.
    """
    samples = sorted(samples, key=lambda s: s[0])
    passing = [x for x, ok in samples if ok]
    failing = [x for x, ok in samples if not ok]
    if not failing:
        return hi
    if not passing:
        return lo
    return (max(passing) + min(failing)) / 2.0


def random_strategy(
    model: SyntheticModel,
    dim: str,
    search_range: tuple[float, float],
    budget: int,
    seed: int = 0,
) -> float:
    """Sample `budget` deterministic pseudo-random points in `search_range`."""
    lo, hi = search_range
    draws = _seeded_floats(seed, budget)
    samples = []
    for i, u in enumerate(draws):
        x = lo + u * (hi - lo)
        ok = model.evaluate(_dims_at(dim, x), trial_seed=seed * 100_003 + i)
        samples.append((x, ok))
    return _estimate_from_samples(samples, lo, hi)


def grid_strategy(
    model: SyntheticModel,
    dim: str,
    search_range: tuple[float, float],
    budget: int,
) -> float:
    """Sample `budget` evenly spaced points across `search_range`."""
    lo, hi = search_range
    if budget == 1:
        xs = [(lo + hi) / 2.0]
    else:
        step = (hi - lo) / (budget - 1)
        xs = [lo + i * step for i in range(budget)]
    samples = []
    for i, x in enumerate(xs):
        ok = model.evaluate(_dims_at(dim, x), trial_seed=i)
        samples.append((x, ok))
    return _estimate_from_samples(samples, lo, hi)


def bisection_strategy(trials_per_point: int = 1, seed_offset: int = 0) -> ProbeStrategy:
    """Build a bisection probe strategy with majority vote per evaluated point.

    Each evaluated point costs `trials_per_point` from the budget (one
    distinct trial_seed per trial); the point's outcome is the majority
    vote (ties broken as fail) over those trials. `seed_offset` shifts the
    trial_seed sequence so distinct offsets sample distinct noise
    realizations from the same noisy rule.
    """

    def _strategy(
        model: SyntheticModel,
        dim: str,
        search_range: tuple[float, float],
        budget: int,
    ) -> float:
        lo, hi = search_range

        def _vote(x: float, base_seed: int) -> bool:
            passes = 0
            for t in range(trials_per_point):
                ok = model.evaluate(
                    _dims_at(dim, x),
                    trial_seed=(seed_offset * 1_000_003 + base_seed) * 1000 + t,
                )
                passes += 1 if ok else 0
            return passes * 2 > trials_per_point

        remaining = budget
        seed_counter = 0

        left, right = lo, hi
        if remaining >= trials_per_point:
            remaining -= trials_per_point
            left_ok = _vote(lo, seed_counter)
            seed_counter += 1
        else:
            left_ok = True

        while remaining >= trials_per_point:
            mid = (left + right) / 2.0
            remaining -= trials_per_point
            ok = _vote(mid, seed_counter)
            seed_counter += 1
            if ok:
                left = mid
            else:
                right = mid

        return (left + right) / 2.0

    return _strategy


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of comparing an estimated boundary to the known true boundary."""

    estimated: float
    true: float
    abs_error: float
    evaluations_used: int


def recovery_error(
    model: SyntheticModel,
    dim: str,
    true_boundary: float,
    strategy: ProbeStrategy,
    search_range: tuple[float, float],
    budget: int,
) -> RecoveryResult:
    """Run `strategy` and score its estimate against `true_boundary`."""
    estimated = strategy(model, dim, search_range, budget)
    return RecoveryResult(
        estimated=estimated,
        true=true_boundary,
        abs_error=abs(estimated - true_boundary),
        evaluations_used=budget,
    )


def compare_strategies(
    model: SyntheticModel,
    dim: str,
    true_boundary: float,
    budget: int,
    strategies: dict[str, ProbeStrategy],
    search_range: tuple[float, float] = (0.0, 20.0),
) -> dict[str, RecoveryResult]:
    """Run every named strategy under the same budget and range; collect results."""
    return {
        name: recovery_error(model, dim, true_boundary, fn, search_range, budget)
        for name, fn in strategies.items()
    }
