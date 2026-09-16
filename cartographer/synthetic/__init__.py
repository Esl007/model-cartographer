"""Synthetic validation harness: ground-truth rules, models, and recovery tools."""

from .rules import (
    CompositeRule,
    DimVector,
    InteractionRule,
    LogisticRule,
    NoiseWrapper,
    Rule,
    ThresholdRule,
)
from .models import SyntheticModel, ZOO
from .recovery import (
    ProbeStrategy,
    RecoveryResult,
    bisection_strategy,
    compare_strategies,
    grid_strategy,
    random_strategy,
    recovery_error,
)

__all__ = [
    "CompositeRule",
    "DimVector",
    "InteractionRule",
    "LogisticRule",
    "NoiseWrapper",
    "Rule",
    "ThresholdRule",
    "SyntheticModel",
    "ZOO",
    "ProbeStrategy",
    "RecoveryResult",
    "bisection_strategy",
    "compare_strategies",
    "grid_strategy",
    "random_strategy",
    "recovery_error",
]
