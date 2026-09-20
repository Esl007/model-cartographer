"""Tool-call planning province: a second capability family for the cartographer.

Deterministic seeded generation of a devops-flavored latent state + typed tool
catalog with real precondition dependency chains, decoys, and destructive
tools; a bounded-search verifier that proves solvability and grades model plans
by simulation; a foil model + adversarial seed search for trap-dense selection;
and realized-dim measurement -- following the same architecture contract as
`cartographer.world`.
"""

from .foils import ToolFoilProfile, foil_divergence, foil_plans
from .generate import ToolDimParams, generate
from .measure import measure
from .model import (
    Artifact,
    Call,
    Flag,
    Goal,
    GoalClause,
    Param,
    Service,
    State,
    Tool,
    ToolWorld,
    make_state,
)
from .render import RenderedToolTask, render
from .search import search_adversarial_seeds
from .simulate import apply_call, optimal_depth, simulate_plan, validate
from .verify import PlanVerification, parse_plan, verify_plan

__all__ = [
    "Artifact",
    "Call",
    "Flag",
    "Goal",
    "GoalClause",
    "Param",
    "Service",
    "State",
    "Tool",
    "ToolWorld",
    "make_state",
    "ToolDimParams",
    "generate",
    "measure",
    "render",
    "RenderedToolTask",
    "verify_plan",
    "parse_plan",
    "PlanVerification",
    "simulate_plan",
    "apply_call",
    "optimal_depth",
    "validate",
    "foil_divergence",
    "foil_plans",
    "ToolFoilProfile",
    "search_adversarial_seeds",
]
