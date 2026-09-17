"""Logistic frontier estimation with bootstrap confidence intervals.

Given (dim_value, passed) points collected along one dimension, `fit_logistic`
fits P(pass) = 1 / (1 + exp(slope * (x - midpoint))) by a deterministic
grid-then-refine search over (midpoint, slope) minimizing log loss (stdlib
math only -- no numpy/scipy). `boundary_ci` wraps this in a sha256-seeded
deterministic bootstrap (same idiom as `cartographer/synthetic/rules.py`'s
`_stable_float`) to produce a midpoint estimate with a (lo, hi) interval.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

_EPS = 1e-9


@dataclass(frozen=True)
class LogisticFit:
    """A fitted logistic frontier: P(pass) = 1/(1+exp(slope*(x-midpoint)))."""

    midpoint: float
    slope: float
    log_loss: float


def _stable_float(*parts: str) -> float:
    """Deterministic float in [0, 1) from sha256 over stable string parts."""
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    as_int = int(digest[:15], 16)
    return as_int / float(16**15)


def _log_loss(points: list[tuple[float, bool]], midpoint: float, slope: float) -> float:
    total = 0.0
    for x, passed in points:
        p_pass = 1.0 / (1.0 + math.exp(slope * (x - midpoint)))
        p_pass = min(max(p_pass, _EPS), 1.0 - _EPS)
        total += -math.log(p_pass) if passed else -math.log(1.0 - p_pass)
    return total / len(points) if points else 0.0


def _has_crossing(points: list[tuple[float, bool]]) -> bool:
    passed = {p for _, p in points}
    return len(passed) > 1


def fit_logistic(points: list[tuple[float, bool]]) -> LogisticFit | None:
    """Fit a logistic frontier to (dim_value, passed) points.

    Returns None if there is no pass/fail crossing (all points pass, all
    points fail, or fewer than 2 points) -- there is no boundary to locate.

    Search strategy: a coarse grid over midpoint (observed x range) crossed
    with a log-spaced slope grid in [0.1, 10], then one local refinement
    pass around the best grid point (finer midpoint/slope grids centered on
    it). Deterministic and dependency-free (stdlib math only).
    """
    if len(points) < 2 or not _has_crossing(points):
        return None

    xs = [x for x, _ in points]
    lo, hi = min(xs), max(xs)
    span = hi - lo if hi > lo else 1.0

    # Coarse grid.
    n_mid = 25
    midpoints = [lo + i * span / (n_mid - 1) for i in range(n_mid)]
    n_slope = 15
    log_lo, log_hi = math.log(0.1), math.log(10.0)
    slopes = [math.exp(log_lo + i * (log_hi - log_lo) / (n_slope - 1)) for i in range(n_slope)]

    best_mid, best_slope, best_loss = midpoints[0], slopes[0], math.inf
    for m in midpoints:
        for s in slopes:
            loss = _log_loss(points, m, s)
            if loss < best_loss:
                best_mid, best_slope, best_loss = m, s, loss

    # Local refinement: finer grid centered on the best point.
    mid_window = max(span / (n_mid - 1), 1e-6)
    refine_mids = [
        best_mid + k * mid_window / 5.0 for k in range(-5, 6)
    ]
    slope_lo = max(best_slope * 0.5, 0.05)
    slope_hi = best_slope * 2.0
    refine_slopes = [slope_lo + k * (slope_hi - slope_lo) / 10.0 for k in range(11)]

    for m in refine_mids:
        for s in refine_slopes:
            if s <= 0:
                continue
            loss = _log_loss(points, m, s)
            if loss < best_loss:
                best_mid, best_slope, best_loss = m, s, loss

    return LogisticFit(midpoint=best_mid, slope=best_slope, log_loss=best_loss)


def boundary_ci(
    points: list[tuple[float, bool]], n_boot: int = 200, seed: int = 0
) -> tuple[float, float, float]:
    """Bootstrap a (midpoint_estimate, ci_lo, ci_hi) triple from `points`.

    Deterministic sha256-seeded resampling (same idiom as
    `synthetic/rules.py`): resample index i of bootstrap round b is chosen
    via `_stable_float("boundary_ci", seed, b, i)` mapped into `[0, n)`.
    Each resample is refit; degenerate resamples (no crossing -> None fit)
    are skipped. The 5th/95th percentile of the surviving midpoints forms
    the CI. If more than half of resamples are degenerate, the CI widens to
    (min(x), max(x)) of the observed points as a conservative fallback.
    """
    point_fit = fit_logistic(points)
    if point_fit is None:
        xs = [x for x, _ in points]
        lo, hi = (min(xs), max(xs)) if xs else (0.0, 0.0)
        mid = (lo + hi) / 2.0
        return mid, lo, hi

    n = len(points)
    boot_mids: list[float] = []
    n_degenerate = 0
    for b in range(n_boot):
        resample = []
        for i in range(n):
            u = _stable_float("boundary_ci", str(seed), str(b), str(i))
            idx = min(int(u * n), n - 1)
            resample.append(points[idx])
        fit = fit_logistic(resample)
        if fit is None:
            n_degenerate += 1
            continue
        boot_mids.append(fit.midpoint)

    if n_degenerate > n_boot // 2 or not boot_mids:
        xs = [x for x, _ in points]
        lo, hi = min(xs), max(xs)
        return point_fit.midpoint, lo, hi

    boot_mids.sort()
    m = len(boot_mids)

    def _pct(p: float) -> float:
        idx = min(int(round(p * (m - 1))), m - 1)
        return boot_mids[idx]

    ci_lo = _pct(0.05)
    ci_hi = _pct(0.95)
    return point_fit.midpoint, ci_lo, ci_hi
