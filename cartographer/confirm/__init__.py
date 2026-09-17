"""Phase 7: confirmation -- re-test decompose hypotheses on fresh seeds
before trusting them, with effect sizes, Wilson intervals, and
Holm-Bonferroni-corrected significance across a family of hypotheses.
"""

from .confirmation import ConfirmationResult, confirm_all, confirm_hypothesis
from .stats import WilsonInterval, holm_bonferroni, two_proportion_z_test, wilson_interval

__all__ = [
    "ConfirmationResult",
    "confirm_all",
    "confirm_hypothesis",
    "WilsonInterval",
    "holm_bonferroni",
    "two_proportion_z_test",
    "wilson_interval",
]
