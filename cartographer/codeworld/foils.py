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

# Round-2 foils (docs/spec-code-hunt.md WP-G). They join a program's readings
# only when one of its round-2 dials is on (`CodeDimParams.round2_on`), so
# every program generated with those dials off reads exactly as before.
ROUND2_FOILS: dict[str, Semantics] = {
    "augassign_rebinds": Semantics(augassign_list="rebind"),
    "concat_mutates": Semantics(list_concat="mutate"),
    "slice_aliases": Semantics(slice_copy="alias"),
    "param_copies": Semantics(param_passing="copy"),
    "predicate_frozen": Semantics(loop_predicate="frozen"),
    "stale_read": Semantics(path_updates="skip_one"),
    "slice_end_inclusive": Semantics(slice_end="inclusive"),
}
# stack_depth = k: the foil of each of the first k stacked steps (model.STACK_STEPS).
STACK_FOILS = ("augassign_rebinds", "concat_mutates", "slice_aliases", "param_copies")


def dial_foils(params) -> tuple[set[str], set[str]]:
    """(required, permitted) foil names for these params' round-2 dials.
    `required` foils must diverge from the truth (each enabled mechanism is
    load-bearing); `permitted` foils may diverge although no toggle of their
    own is on (a stacked alias also fools `value_semantics`). Every other
    round-2 foil, and every trap foil whose trap is off, must agree with the
    truth (`generate.validate`)."""
    required: set[str] = set()
    permitted: set[str] = set()
    if params.chain_depth:
        required.add("stale_read")
    if params.stack_depth:
        required.update(STACK_FOILS[:params.stack_depth])
        permitted.add("value_semantics")
    if params.loop_branch or params.while_on:
        required.update(("predicate_frozen", "loop_once"))
    if params.sign_chain:
        required.add("truncating_modulo")
    if params.index_math:
        required.add("slice_end_inclusive")
    if params.aliasing_on:
        permitted.add("augassign_rebinds")  # the aliasing trap may mutate through `alias += [..]`
    return required, permitted | required


def _delta(sem: Semantics) -> dict[str, str]:
    return {f.name: getattr(sem, f.name) for f in fields(Semantics) if getattr(sem, f.name) != getattr(TRUE, f.name)}


def foils_for(program: Program) -> dict[str, Semantics]:
    """The foil readings of THIS program, relative to its declared truth.
    Plain Python programs get exactly `FOILS`."""
    truth = program.semantics
    pool = {**FOILS, **ROUND2_FOILS} if program.params.round2_on() else FOILS
    if truth == TRUE:
        return pool
    declared = _delta(truth)
    out: dict[str, Semantics] = {PYTHON_PRIOR: TRUE}
    for name, sem in pool.items():
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
