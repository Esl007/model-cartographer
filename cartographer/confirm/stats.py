"""Stdlib-only statistics for confirmation: Wilson intervals, a two-proportion
z-test, and Holm-Bonferroni correction.

No scipy/numpy -- everything here is closed-form arithmetic over `math`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_Z_95 = 1.959963984540054  # two-sided 95% critical value, math.erf-derived constant


@dataclass(frozen=True)
class WilsonInterval:
    """A Wilson score confidence interval for a binomial proportion.

    Preferred over the naive normal-approximation interval because it
    stays inside [0, 1] and remains sensible at small n / extreme p, both
    of which are common here (k_trials is typically single digits).
    """

    point: float
    low: float
    high: float
    n: int

    def to_json_dict(self) -> dict:
        return {"point": self.point, "low": self.low, "high": self.high, "n": self.n}


def wilson_interval(successes: int, n: int, z: float = _Z_95) -> WilsonInterval:
    """Wilson score interval for `successes` out of `n` trials.

    Formula (standard closed form):
        center = (phat + z^2/(2n)) / (1 + z^2/n)
        half_width = z * sqrt(phat*(1-phat)/n + z^2/(4n^2)) / (1 + z^2/n)
    where phat = successes/n. Degenerates to point=0.0, [0, 0] for n == 0
    (no data -- callers should treat this as "no evidence", not "confirmed
    zero").
    """
    if n <= 0:
        return WilsonInterval(point=0.0, low=0.0, high=0.0, n=0)
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    half_width = (z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))) / denom
    low = max(0.0, center - half_width)
    high = min(1.0, center + half_width)
    return WilsonInterval(point=phat, low=low, high=high, n=n)


def _norm_sf(x: float) -> float:
    """Survival function (1 - CDF) of the standard normal, via math.erf."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def two_proportion_z_test(successes_a: int, n_a: int, successes_b: int, n_b: int) -> float:
    """Two-sided p-value for H0: p_a == p_b, via the pooled two-proportion
    z-test:

        p_pool = (x_a + x_b) / (n_a + n_b)
        se = sqrt(p_pool * (1-p_pool) * (1/n_a + 1/n_b))
        z = (phat_a - phat_b) / se
        p_value = 2 * P(Z >= |z|)

    Returns 1.0 (no evidence of a difference) if either group has n == 0,
    or if se == 0 because p_pool is exactly 0 or 1 (both groups
    all-pass or all-fail identically -- no detectable difference).
    """
    if n_a <= 0 or n_b <= 0:
        return 1.0
    phat_a = successes_a / n_a
    phat_b = successes_b / n_b
    p_pool = (successes_a + successes_b) / (n_a + n_b)
    if p_pool <= 0.0 or p_pool >= 1.0:
        return 1.0 if phat_a == phat_b else 0.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1.0 / n_a + 1.0 / n_b))
    if se == 0.0:
        return 1.0 if phat_a == phat_b else 0.0
    z = (phat_a - phat_b) / se
    return 2.0 * _norm_sf(abs(z))


def holm_bonferroni(p_values: list[float], alpha: float = 0.05) -> list[tuple[bool, float]]:
    """Holm-Bonferroni step-down correction across a family of `p_values`.

    Returns, for each ORIGINAL index (order matches input), a tuple of
    (rejected_null, corrected_alpha_threshold_applied_to_it) -- i.e. whether
    that hypothesis's null is rejected at family-wise `alpha`, and the
    per-comparison alpha threshold it was tested against after the
    step-down procedure.

    Procedure: sort p-values ascending; the i-th smallest (1-indexed) is
    compared against alpha / (m - i + 1); reject if p <= that threshold AND
    every smaller p-value was also rejected (step-down: once one fails, all
    larger p-values are automatically not rejected).
    """
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    rejected = [False] * m
    thresholds = [0.0] * m
    still_rejecting = True
    for rank, idx in enumerate(order):  # rank is 0-indexed position in sorted order
        threshold = alpha / (m - rank)
        thresholds[idx] = threshold
        if still_rejecting and p_values[idx] <= threshold:
            rejected[idx] = True
        else:
            still_rejecting = False
            rejected[idx] = False
    return list(zip(rejected, thresholds))
