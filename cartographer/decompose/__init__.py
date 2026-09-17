"""Phase 3: failure decomposition engine.

Given a failing dim configuration and a passing baseline, `decompose` finds
the minimal factor set (single / pairwise / higher-order) responsible, using
noise-robust multi-trial probing.
"""

from .engine import Evaluator, decompose, probe
from .hypothesis import FailureHypothesis, ProbeOutcome

__all__ = [
    "Evaluator",
    "decompose",
    "probe",
    "FailureHypothesis",
    "ProbeOutcome",
]
