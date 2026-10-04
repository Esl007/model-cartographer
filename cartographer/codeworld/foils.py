"""Shallow-reader foils for the code-tracing province.

Each foil is the SAME interpreter with one semantic belief flipped to the
common wrong prior (lists copy on assignment, closures bind early, `range`
is inclusive, ...). A foil is therefore a perfect reader with exactly one
misconception, and "the model's answer equals foil X's answer" names the
misconception it used -- the code analogue of the tool province's
`unknown_tool` detail code.

Six foils pair with the six trap toggles (`TRAP_FOILS`); two more
(`all_branches`, `loop_once`) are control-flow-blind readers reported only
as salience, never toggled.

A program that declares a counterfactual DIALECT (`CodeDimParams.dialect`)
is read relative to that dialect: truth is `program.semantics`, the plain
Python reading is the `python_prior` foil (listed first, so it is the
reported cause), and every other foil flips its one field on top of the
dialect truth (`foils_for`). A foil whose field the dialect itself declares
is dropped -- its reading would be the truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace

from .interpret import FOIL_ERROR, TRUE, Semantics, answer_from_trace, run
from .model import Program

FOILS: dict[str, Semantics] = {
    "value_semantics": Semantics(list_assignment="copy"),
    "early_binding": Semantics(closure_binding="early"),
    "flat_scope": Semantics(scoping="flat"),
    "inclusive_range": Semantics(range_upper="inclusive"),
    "truncating_modulo": Semantics(modulo="truncate"),
    "fresh_default": Semantics(default_args="fresh"),
    "all_branches": Semantics(conditionals="all_branches"),
    "loop_once": Semantics(loops="once"),
    "pattern_completion": Semantics(literals="pattern"),
}

TRAP_FOILS: dict[str, str] = {
    "aliasing_on": "value_semantics",
    "late_binding_on": "early_binding",
    "shadowing_on": "flat_scope",
    "inclusive_range_trap_on": "inclusive_range",
    "negative_modulo_on": "truncating_modulo",
    "mutable_default_on": "fresh_default",
}
# Foils for traps outside the six-trap family (model.EXTRA_TRAP_KEYS).
EXTRA_TRAP_FOILS: dict[str, str] = {"opaque_literal_on": "pattern_completion"}
ALL_TRAP_FOILS: dict[str, str] = {**TRAP_FOILS, **EXTRA_TRAP_FOILS}
FOIL_TRAPS = {v: k for k, v in ALL_TRAP_FOILS.items()}
# The reader who ignores a declared dialect and reads the code as plain Python.
PYTHON_PRIOR = "python_prior"


def _delta(sem: Semantics) -> dict[str, str]:
    return {f.name: getattr(sem, f.name) for f in fields(Semantics) if getattr(sem, f.name) != getattr(TRUE, f.name)}


def foils_for(program: Program) -> dict[str, Semantics]:
    """The foil readings of THIS program, relative to its declared truth.
    Plain Python programs get exactly `FOILS`."""
    truth = program.semantics
    if truth == TRUE:
        return FOILS
    declared = _delta(truth)
    out: dict[str, Semantics] = {PYTHON_PRIOR: TRUE}
    for name, sem in FOILS.items():
        d = _delta(sem)
        if any(k in declared for k in d):
            continue
        out[name] = replace(truth, **d)
    return out


def foil_answers(program: Program) -> dict[str, object]:
    """Every foil's answer to the program's query (`FOIL_ERROR` if that
    reading crashes)."""
    return {name: answer_from_trace(program, run(program, sem)) for name, sem in foils_for(program).items()}


@dataclass(frozen=True)
class CodeFoilProfile:
    """How much this program traps a shallow reader. `n_diverging` counts
    foils whose answer differs from the truth; `divergence_score` weights
    each by how early its target trace departs from the true one (a foil
    that is wrong from the first statement on is a more salient trap than
    one that only goes wrong on the last line)."""

    truth: object
    n_diverging: int = 0
    divergence_score: float = 0.0
    trap_kinds: tuple[str, ...] = ()
    foil_answers: dict = field(default_factory=dict)


def foil_divergence(program: Program) -> CodeFoilProfile:
    true_trace = run(program, program.semantics)
    truth = answer_from_trace(program, true_trace)
    n = max(len(program.body), 1)
    answers: dict[str, object] = {}
    diverging: list[str] = []
    score = 0.0
    for name, sem in foils_for(program).items():
        tr = run(program, sem)
        a = answer_from_trace(program, tr)
        answers[name] = a
        if a == truth:
            continue
        diverging.append(name)
        first = next(
            (i for i, (x, y) in enumerate(zip(tr.target_track, true_trace.target_track)) if x != y),
            None,
        )
        if a is FOIL_ERROR or first is None:
            score += 0.5
        else:
            score += max(0.0, 1.0 - first / n)
    return CodeFoilProfile(
        truth=truth,
        n_diverging=len(diverging),
        divergence_score=score,
        trap_kinds=tuple(sorted(diverging)),
        foil_answers=answers,
    )
