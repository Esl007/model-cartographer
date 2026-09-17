"""Phase 5: the adaptive scheduler -- wave-based campaigns over the model zoo.

Explores dims for pass/fail boundaries, refines them via bisection with
bootstrap-CI stopping, and hands each boundary to `decompose` to explain
what else combines with it.
"""

from .estimate import LogisticFit, boundary_ci, fit_logistic
from .scheduler import (
    CampaignState,
    DimSpec,
    SchedulerConfig,
    TaskEvaluator,
    resume_from_records,
    run_campaign,
)

__all__ = [
    "LogisticFit",
    "fit_logistic",
    "boundary_ci",
    "CampaignState",
    "DimSpec",
    "SchedulerConfig",
    "TaskEvaluator",
    "run_campaign",
    "resume_from_records",
]
