"""Province IV: code tracing (execution prediction). See docs/province-iv-spec.md.

A generated Python program (our own AST, our own interpreter for ground
truth) is shown to the model, which predicts a variable's final value.
Foils name the shallow reading behind a wrong answer.
"""

from .foils import FOIL_TRAPS, FOILS, TRAP_FOILS, CodeFoilProfile, foil_answers, foil_divergence, foils_for
from .generate import GenerationFailed, generate, validate, without_distractors
from .interpret import FOIL_ERROR, STEP_CAP, TRUE, Semantics, answer, run, semantics_for
from .measure import measure
from .model import TRAP_KEYS, CodeDimParams, Program, Query
from .mutate import ANSWER_CHANGING, ANSWER_PRESERVING, MutationUnavailable, apply_ablation, inject
from .render import RenderedProgram, render
from .search import search_adversarial_seeds
from .verify import TraceVerification, parse_answer, verify_answer

__all__ = [
    "ANSWER_CHANGING", "ANSWER_PRESERVING", "FOILS", "FOIL_ERROR", "FOIL_TRAPS", "STEP_CAP", "TRAP_FOILS",
    "TRAP_KEYS", "TRUE", "CodeDimParams", "CodeFoilProfile", "GenerationFailed", "MutationUnavailable",
    "Program", "Query", "RenderedProgram", "Semantics", "TraceVerification", "answer", "apply_ablation",
    "foil_answers", "foil_divergence", "foils_for", "generate", "inject", "measure", "parse_answer", "render", "run",
    "search_adversarial_seeds", "semantics_for", "validate", "verify_answer", "without_distractors",
]
