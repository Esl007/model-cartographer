"""Mutators for the code-tracing province.

ANSWER-CHANGING injectors (`inject_*`) add one trap block to an existing
program -- the same block builders `generate` uses -- appended at the end
and folded into the target, then re-validated. They are how a decomposition
or a proposer turns one trap on in a program it already has.

ANSWER-PRESERVING ablations change only what the model reads, never a
value, and assert it:
  * `rename_descriptive` -- look-alike spellings replaced by unrelated
    descriptive names (tests H1 causally, like `pronoun_suppression` did
    for belief);
  * `remove_distractors` -- delete every statement that cannot affect any
    reading of the answer;
  * `annotate_aliasing` / `annotate_closures` -- a comment on the trap line
    stating the semantics, which separates "did not notice" from "noticed
    and still got it wrong".

Each raises `MutationUnavailable` when it cannot hold its invariant on this
particular program (the `AblationUnavailable` pattern in `ablate/`).
"""

from __future__ import annotations

import copy
import re
from dataclasses import replace

from .generate import GenerationFailed, _Builder, _Rng, validate, without_distractors
from .interpret import TRUE, Interpreter, answer
from .model import Program, iter_stmts
from .render import RenderedProgram, render

ANSWER_PRESERVING = {"rename_descriptive", "remove_distractors", "annotate_aliasing", "annotate_closures"}
ANSWER_CHANGING = {
    "inject_aliasing",
    "inject_late_binding",
    "inject_shadowing",
    "inject_inclusive_range_trap",
    "inject_negative_modulo",
    "inject_mutable_default",
}

_INJECTOR_TRAP = {
    "inject_aliasing": "aliasing_on",
    "inject_late_binding": "late_binding_on",
    "inject_shadowing": "shadowing_on",
    "inject_inclusive_range_trap": "inclusive_range_trap_on",
    "inject_negative_modulo": "negative_modulo_on",
    "inject_mutable_default": "mutable_default_on",
}


class MutationUnavailable(Exception):
    """This mutator cannot hold its invariant on this program."""


def _resume_builder(program: Program, tag: str) -> _Builder:
    """A `_Builder` positioned at the end of `program`, so a trap block can
    be appended with the same builders `generate` uses."""
    b = _Builder(program.params, program.seed, program.query.kind)
    b.rng = _Rng("codeworld-inject", program.seed, tag)
    b.body = list(program.body)
    b.n_nodes = sum(1 for _ in iter_stmts(program.body))
    b._sid = max((s.sid for s in iter_stmts(program.body)), default=0)
    b.vars = {v.id: v for v in program.variables}
    b.target = program.query.target or next(v.id for v in program.variables if v.role == "target")
    b.distractor_ids = set(program.distractor_ids)
    b.trap_sites = list(program.trap_sites)
    b.locals_used = list(program.locals_pool)
    for prefix in b._counters:
        nums = [int(m.group(1)) for vid in list(b.vars) + b.locals_used
                if (m := re.fullmatch(rf"{prefix}(\d+)", vid))]
        b._counters[prefix] = max(nums, default=-1) + 1
    it = Interpreter(TRUE)
    for s in program.body:
        it.exec_top(s)
    b.interp = it
    return b


def inject(program: Program, injector: str) -> Program:
    trap = _INJECTOR_TRAP[injector]
    if getattr(program.params, trap):
        raise MutationUnavailable(f"{trap} is already on")
    if program.query.kind != "final_value":
        raise MutationUnavailable("injectors target final_value programs")
    params = replace(program.params, **{trap: True})
    for attempt in range(8):
        b = _resume_builder(program, f"{injector}|{attempt}")
        builder = {
            "aliasing_on": b.trap_aliasing,
            "late_binding_on": b.trap_late_binding,
            "shadowing_on": b.trap_shadowing,
            "inclusive_range_trap_on": b.trap_inclusive_range,
            "negative_modulo_on": b.trap_negative_modulo,
            "mutable_default_on": b.trap_mutable_default,
        }[trap]
        try:
            stmts, site = builder()
        except GenerationFailed:
            continue
        if not b.try_emit(stmts, trap=trap, trap_index=site):
            continue
        new = replace(program, body=tuple(b.body), params=params, variables=tuple(b.vars.values()),
                      trap_sites=tuple(b.trap_sites), locals_pool=tuple(b.locals_used))
        if validate(new) is None and answer(new) != answer(program):
            return new
    raise MutationUnavailable(f"could not inject {trap} answer-relevantly")


def inject_aliasing(program: Program, seed: int = 0) -> Program:
    return inject(program, "inject_aliasing")


def inject_late_binding(program: Program, seed: int = 0) -> Program:
    return inject(program, "inject_late_binding")


def inject_shadowing(program: Program, seed: int = 0) -> Program:
    return inject(program, "inject_shadowing")


def inject_inclusive_range_trap(program: Program, seed: int = 0) -> Program:
    return inject(program, "inject_inclusive_range_trap")


def inject_negative_modulo(program: Program, seed: int = 0) -> Program:
    return inject(program, "inject_negative_modulo")


def inject_mutable_default(program: Program, seed: int = 0) -> Program:
    return inject(program, "inject_mutable_default")


def apply_ablation(program: Program, name: str, rendering_seed: int = 0) -> tuple[Program, RenderedProgram]:
    """(program, rendering) for an answer-preserving ablation. Asserts the
    answer is unchanged."""
    if name == "rename_descriptive":
        if not any(v.twin_of for v in program.variables):
            raise MutationUnavailable("no look-alike names to replace")
        out_prog, rendered = program, render(program, rendering_seed, naming="descriptive")
    elif name == "remove_distractors":
        if not program.distractor_ids:
            raise MutationUnavailable("no distractors")
        out_prog = without_distractors(program)
        rendered = render(out_prog, rendering_seed)
    elif name == "annotate_aliasing":
        if not program.params.aliasing_on:
            raise MutationUnavailable("no aliasing trap")
        out_prog, rendered = program, render(program, rendering_seed, annotate=("aliasing",))
    elif name == "annotate_closures":
        if not program.params.late_binding_on:
            raise MutationUnavailable("no late-binding trap")
        out_prog, rendered = program, render(program, rendering_seed, annotate=("closures",))
    else:
        raise KeyError(name)
    if answer(out_prog) != answer(program):  # the whole point of an ablation
        raise MutationUnavailable(f"{name} changed the answer")
    return copy.copy(out_prog), rendered
