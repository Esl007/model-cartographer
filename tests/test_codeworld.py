"""Province IV (code tracing): interpreter, generator, renderer, measure,
foils, verifier, search -- all offline."""

from __future__ import annotations

import ast
import keyword

import pytest

from cartographer.codeworld import (
    FOILS,
    TRAP_FOILS,
    TRAP_KEYS,
    TRUE,
    CodeDimParams,
    GenerationFailed,
    answer,
    foil_answers,
    foil_divergence,
    generate,
    measure,
    parse_answer,
    render,
    run,
    search_adversarial_seeds,
    validate,
    verify_answer,
    without_distractors,
)
from cartographer.codeworld.interpret import FOIL_ERROR, Semantics
from cartographer.codeworld.model import (
    Assign,
    AugAssign,
    BinOp,
    Call,
    Compare,
    Const,
    ExprStmt,
    For,
    FuncDef,
    If,
    Len,
    ListLit,
    MethodCall,
    Name,
    Program,
    Query,
    RangeIter,
    Return,
)
from cartographer.store.schema import Verdict


def grid(i: int) -> CodeDimParams:
    """A deterministic walk over the whole dimension grid, trap subsets included."""
    h = lambda k, n: (i * 2654435761 + k * 97) % n  # noqa: E731
    traps = {k: bool((i >> j) & 1) for j, k in enumerate(TRAP_KEYS)}
    return CodeDimParams(
        n_statements=[4, 8, 15, 25, 40, 60][h(1, 6)], max_nesting=h(2, 4),
        n_variables=[2, 3, 5, 8, 12][h(3, 5)], distractor_fraction=[0, 0.2, 0.4, 0.6][h(4, 4)],
        identifier_confusability=[0, 0.3, 0.6, 1.0][h(5, 4)], **traps,
    )


def _prog(body, target="v0", kind="final_value") -> Program:
    return Program(body=tuple(body), query=Query(kind, target if kind == "final_value" else None),
                   params=CodeDimParams(), seed=0)


def _cpython(source: str):
    out: list[str] = []
    env = {"__builtins__": {"range": range, "len": len,
                            "print": lambda *a: out.append(" ".join(str(x) for x in a))}}
    exec(compile(source, "<generated>", "exec"), env)  # tests only: never on the live path
    return {k: v for k, v in env.items() if k != "__builtins__" and not callable(v)}, out


# --- acceptance 1: the interpreter IS Python on the subset --------------------


def test_interpreter_matches_cpython_on_5000_generated_programs():
    n, mismatches, failures = 5000, [], 0
    for i in range(n):
        try:
            prog = generate(grid(i), 1000 + i, query_kind="stdout" if i % 10 == 0 else "final_value")
        except GenerationFailed:
            failures += 1
            continue
        r = render(prog, i)
        env, out = _cpython(r.source)
        tr = run(prog, TRUE)
        mine = {r.name_map[k]: v for k, v in tr.globals.items()}
        if mine != env or out != tr.stdout:
            mismatches.append(i)
    assert mismatches == []
    assert failures / n < 0.02


# --- acceptance 2: traps are load-bearing; disabled traps are absent ------------


def test_enabled_traps_change_the_answer_and_disabled_ones_do_not():
    for i in range(600):
        p = grid(i)
        prog = generate(p, 7000 + i)
        truth = answer(prog)
        foils = foil_answers(prog)
        for trap, foil in TRAP_FOILS.items():
            if getattr(p, trap):
                assert foils[foil] != truth, (i, trap)
            else:
                assert foils[foil] == truth, (i, trap)


def test_generation_is_deterministic():
    p = grid(5)
    a, b = generate(p, 42), generate(p, 42)
    assert a == b
    assert render(a, 3).source == render(b, 3).source
    assert generate(p, 43) != a


def test_generation_failure_is_rare_and_explicit():
    fails = 0
    for i in range(400):
        try:
            generate(grid(i * 7 + 1), 90_000 + i)
        except GenerationFailed:
            fails += 1
    assert fails / 400 < 0.02


# --- acceptance 5: look-alike names are confusable, never equal ------------------


def test_lookalike_rendering_parses_and_never_reuses_a_name():
    seen_lookalike = 0
    for i in range(300):
        p = CodeDimParams(n_statements=10 + i % 30, n_variables=2 + i % 10, identifier_confusability=1.0,
                          max_nesting=i % 3, distractor_fraction=(i % 3) * 0.2)
        prog = generate(p, 300 + i)
        r = render(prog, i)
        ast.parse(r.source)
        names = list(r.name_map.values())
        assert len(names) == len(set(names))
        assert not any(keyword.iskeyword(n) for n in names)
        if r.naming == "lookalike":
            seen_lookalike += 1
            for a, b in prog.twin_pairs():
                assert r.name_map[a] != r.name_map[b]
    assert seen_lookalike == 300


def test_target_twin_differs_and_is_rendered_alike():
    prog = generate(CodeDimParams(n_statements=12, identifier_confusability=1.0), 11)
    twin = prog.twin_of_target()
    assert twin is not None
    assert run(prog).globals[twin] != answer(prog)
    r = render(prog)
    assert r.lookalike_of(prog, prog.query.target) == twin
    assert render(prog, naming="descriptive").naming == "descriptive"


# --- interpreter semantics, one flag at a time ------------------------------------


def test_aliasing_vs_copy():
    body = [Assign(1, "v1", ListLit((Const(1),))), Assign(2, "v2", Name("v1")),
            ExprStmt(3, MethodCall("v2", "append", (Const(5),))), Assign(4, "v0", Len(Name("v1")))]
    p = _prog(body)
    assert answer(p) == 2
    assert answer(p, Semantics(list_assignment="copy")) == 1


def test_late_vs_early_binding():
    body = [Assign(1, "v1", Const(2)),
            FuncDef(2, "f0", ("p0",), (), (Return(3, BinOp("*", Name("p0"), Name("v1"))),)),
            Assign(4, "v1", Const(5)), Assign(5, "v0", Call("f0", (Const(3),)))]
    p = _prog(body)
    assert answer(p) == 15
    assert answer(p, Semantics(closure_binding="early")) == 6


def test_lexical_vs_flat_scope():
    body = [Assign(1, "v1", Const(4)),
            FuncDef(2, "f0", ("p0",), (), (Assign(3, "v1", Name("p0")), Return(4, Name("v1")))),
            Assign(5, "v2", Call("f0", (Const(9),))), Assign(6, "v0", Name("v1"))]
    p = _prog(body)
    assert answer(p) == 4
    assert answer(p, Semantics(scoping="flat")) == 9


def test_range_modulo_and_defaults():
    loop = _prog([Assign(1, "v0", Const(0)),
                  For(2, "i0", RangeIter(None, 4), (AugAssign(3, "v0", "+", Name("i0")),))])
    assert answer(loop) == 6
    assert answer(loop, Semantics(range_upper="inclusive")) == 10
    mod = _prog([Assign(1, "v1", Const(-7)), Assign(2, "v0", BinOp("%", Name("v1"), Const(3)))])
    assert answer(mod) == 2
    assert answer(mod, Semantics(modulo="truncate")) == -1
    div = _prog([Assign(1, "v1", Const(-7)), Assign(2, "v0", BinOp("//", Name("v1"), Const(2)))])
    assert answer(div) == -4
    assert answer(div, Semantics(modulo="truncate")) == -3
    fd = FuncDef(1, "f0", ("p0", "p1"), (ListLit(()),),
                 (ExprStmt(2, MethodCall("p1", "append", (Name("p0"),))), Return(3, Len(Name("p1")))))
    dflt = _prog([fd, Assign(4, "v1", Call("f0", (Const(1),))), Assign(5, "v0", Call("f0", (Const(2),)))])
    assert answer(dflt) == 2
    assert answer(dflt, Semantics(default_args="fresh")) == 1


def test_control_flow_blind_foils():
    branchy = _prog([Assign(1, "v0", Const(1)),
                     If(2, Compare(">", Name("v0"), Const(5)), (Assign(3, "v0", Const(10)),),
                        (Assign(4, "v0", Const(20)),))])
    assert answer(branchy) == 20
    assert answer(branchy, Semantics(conditionals="all_branches")) == 20
    one_armed = _prog([Assign(1, "v0", Const(1)),
                       If(2, Compare(">", Name("v0"), Const(5)), (Assign(3, "v0", Const(10)),))])
    assert answer(one_armed) == 1
    assert answer(one_armed, Semantics(conditionals="all_branches")) == 10
    loop = _prog([Assign(1, "v1", ListLit((Const(1), Const(2), Const(3)))), Assign(2, "v0", Const(0)),
                  For(3, "x0", Name("v1"), (AugAssign(4, "v0", "+", Name("x0")),))])
    assert answer(loop) == 6
    assert answer(loop, Semantics(loops="once")) == 1


def test_runaway_readings_crash_instead_of_eating_memory():
    """A foil may read a program in a way that explodes (here: a list that
    doubles itself 200 times); that must be a crash, not an OOM."""
    body = [Assign(1, "v0", ListLit((Const(1),))),
            For(2, "i0", RangeIter(None, 200), (AugAssign(3, "v0", "+", Name("v0")),))]
    assert answer(_prog(body)) is FOIL_ERROR
    spin = _prog([Assign(1, "v0", Const(0)),
                  For(2, "i0", RangeIter(None, 9000), (AugAssign(3, "v0", "+", Const(1)),))])
    assert run(spin).error is not None  # step cap


# --- distractors, measure, foils, search --------------------------------------------


def test_removing_distractors_changes_no_reading():
    checked = 0
    for i in range(200):
        prog = generate(CodeDimParams(n_statements=20, distractor_fraction=0.5, max_nesting=i % 3,
                                      aliasing_on=bool(i % 2), inclusive_range_trap_on=bool(i % 3 == 0)), i)
        if not prog.distractor_ids:
            continue
        checked += 1
        stripped = without_distractors(prog)
        assert answer(stripped) == answer(prog)
        for sem in FOILS.values():
            assert answer(stripped, sem) == answer(prog, sem)
    assert checked > 150


def test_measure_reports_realised_dims():
    prog = generate(CodeDimParams(n_statements=20, max_nesting=2, distractor_fraction=0.3,
                                  identifier_confusability=1.0, aliasing_on=True, late_binding_on=True), 5)
    m = measure(prog)
    for key in ("n_statements", "max_nesting", "n_variables", "trace_length", "distractor_fraction",
                "n_live_aliases", "n_closures", "traps_on", "identifier_confusability", "answer_size"):
        assert key in m
    assert m["traps_on"] == 2
    assert m["n_live_aliases"] >= 1
    assert m["n_closures"] >= 1
    assert m["n_statements"] >= 20
    assert 0 < m["identifier_confusability"] <= 1


def test_foil_divergence_names_the_enabled_traps():
    prog = generate(CodeDimParams(n_statements=10, aliasing_on=True, negative_modulo_on=True), 9)
    prof = foil_divergence(prog)
    assert {"value_semantics", "truncating_modulo"} <= set(prof.trap_kinds)
    assert prof.n_diverging >= 2 and prof.divergence_score > 0


def test_search_returns_best_first():
    res = search_adversarial_seeds(CodeDimParams(n_statements=12, max_nesting=2), 7, n_candidates=15, top_k=4)
    scores = [p.divergence_score for _, p in res]
    assert scores == sorted(scores, reverse=True) and len(res) == 4


def test_validate_rejects_a_trap_that_does_not_matter():
    prog = generate(CodeDimParams(n_statements=8), 3)
    from dataclasses import replace

    lying = replace(prog, params=replace(prog.params, aliasing_on=True))
    assert "not answer-relevant" in validate(lying)


# --- verifier ------------------------------------------------------------------------


@pytest.mark.parametrize("text,ok,value", [
    ("ANSWER: 42", True, 42),
    ("reasoning...\nso the final ANSWER: [1, 2]", True, [1, 2]),
    ("**ANSWER:** 7", True, 7),
    ("ANSWER: `5`.", True, 5),
    ("ANSWER: 3\nANSWER: 4", True, 4),
    ("ANSWER: (1, 2)", True, [1, 2]),
    ("ANSWER: 'ab'", True, "ab"),
    ("the answer is nine", False, None),
    ("", False, None),
])
def test_parse_answer(text, ok, value):
    got_ok, got, _ = parse_answer(text)
    assert got_ok == ok and got == value


def test_verify_names_the_failure_signature():
    prog = generate(CodeDimParams(n_statements=12, identifier_confusability=1.0, aliasing_on=True), 21)
    r = render(prog)
    truth = answer(prog)
    assert verify_answer(f"ANSWER: {truth!r}", prog, r).verdict == Verdict.PASS
    copy_reading = foil_answers(prog)["value_semantics"]
    v = verify_answer(f"ANSWER: {copy_reading!r}", prog, r)
    assert v.verdict == Verdict.FAIL and "foil:" in v.detail and "value_semantics" in v.detail
    twin_val = run(prog).globals[prog.twin_of_target()]
    v = verify_answer(f"ANSWER: {twin_val!r}", prog, r)
    assert v.detail.startswith("confused_identifier:")
    assert verify_answer("I could not work it out", prog, r).detail == "malformed_answer"
    assert verify_answer("", prog, r).detail == "empty_answer"


def test_verify_is_strict_about_bool_vs_int():
    prog = _prog([Assign(1, "v0", Const(1))])
    r = render(prog)
    assert verify_answer("ANSWER: True", prog, r).verdict == Verdict.FAIL
    assert verify_answer("ANSWER: 1", prog, r).verdict == Verdict.PASS


def test_stdout_query():
    prog = generate(CodeDimParams(n_statements=10), 4, query_kind="stdout")
    r = render(prog)
    assert "print(" in r.source and "What does this program print" in r.prompt
    lines = run(prog).stdout
    assert verify_answer(f"ANSWER: {lines!r}", prog, r).verdict == Verdict.PASS


def test_frontier_cause_words_for_code_failures():
    from types import SimpleNamespace

    from cartographer.report.frontier import CAUSE_WORDS, SPECS, _cause_code

    spec = next(s for s in SPECS if s.key == "code_traps")
    rec = lambda d: SimpleNamespace(verifier_detail=f"kind=code_trace;campaign=1;query=final_value;detail={d};n_stmts=9")  # noqa: E731
    assert _cause_code(spec, rec("foil:value_semantics,loop_once")) == "foil_value_semantics"
    assert _cause_code(spec, rec("confused_identifier:totl+foil:early_binding")) == "confused_identifier"
    assert _cause_code(spec, rec("wrong_value")) == "wrong_value"
    for code in ("confused_identifier", "wrong_value", "malformed_answer",
                 *(f"foil_{f}" for f in FOILS)):
        assert code in CAUSE_WORDS
