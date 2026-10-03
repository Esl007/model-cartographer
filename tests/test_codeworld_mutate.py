"""Province IV mutators: answer-preserving ablations never change the answer;
answer-changing injectors change it or are rejected (acceptance item 3)."""

from __future__ import annotations

from cartographer.codeworld import (
    ANSWER_CHANGING,
    ANSWER_PRESERVING,
    TRAP_FOILS,
    CodeDimParams,
    MutationUnavailable,
    answer,
    apply_ablation,
    foil_answers,
    generate,
    inject,
    render,
    validate,
)
from cartographer.codeworld.mutate import _INJECTOR_TRAP


def _programs(n: int):
    for i in range(n):
        yield generate(CodeDimParams(
            n_statements=8 + i % 25, max_nesting=i % 4, n_variables=2 + i % 6,
            distractor_fraction=(i % 4) * 0.15, identifier_confusability=(i % 3) / 2,
            aliasing_on=i % 5 == 0, late_binding_on=i % 7 == 0,
        ), 20_000 + i)


def test_answer_preserving_ablations_never_change_the_answer():
    applied = {name: 0 for name in ANSWER_PRESERVING}
    for prog in _programs(500):
        truth = answer(prog)
        for name in sorted(ANSWER_PRESERVING):
            try:
                out, rendered = apply_ablation(prog, name)
            except MutationUnavailable:
                continue
            applied[name] += 1
            assert answer(out) == truth
            assert rendered.source
    assert all(applied[n] > 20 for n in applied), applied


def test_answer_changing_injectors_change_the_answer_or_are_rejected():
    attempted = changed = 0
    for prog in _programs(500):
        for name in sorted(ANSWER_CHANGING):
            if getattr(prog.params, _INJECTOR_TRAP[name]):
                continue
            attempted += 1
            try:
                new = inject(prog, name)
            except MutationUnavailable:
                continue
            assert answer(new) != answer(prog)
            assert validate(new) is None
            trap = _INJECTOR_TRAP[name]
            assert getattr(new.params, trap)
            assert foil_answers(new)[TRAP_FOILS[trap]] != answer(new)
            changed += 1
    assert changed / attempted >= 0.95


def test_injecting_an_enabled_trap_is_unavailable():
    prog = generate(CodeDimParams(n_statements=10, aliasing_on=True), 1)
    try:
        inject(prog, "inject_aliasing")
    except MutationUnavailable:
        return
    raise AssertionError("expected MutationUnavailable")


def test_rename_descriptive_removes_lookalike_spellings():
    prog = generate(CodeDimParams(n_statements=12, identifier_confusability=1.0), 8)
    assert render(prog).naming == "lookalike"
    _, plain = apply_ablation(prog, "rename_descriptive")
    assert plain.naming == "descriptive"
    assert plain.source != render(prog).source


def test_annotations_state_the_semantics_on_the_trap_line():
    prog = generate(CodeDimParams(n_statements=10, aliasing_on=True, late_binding_on=True), 2)
    _, ra = apply_ablation(prog, "annotate_aliasing")
    assert "now refer to the same list" in ra.source
    _, rc = apply_ablation(prog, "annotate_closures")
    assert "when it is called, not when it is defined" in rc.source
    plain = generate(CodeDimParams(n_statements=10), 2)
    for name in ("annotate_aliasing", "annotate_closures"):
        try:
            apply_ablation(plain, name)
        except MutationUnavailable:
            continue
        raise AssertionError(name)
