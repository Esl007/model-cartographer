"""Province V parser + harness (docs/province-v-spec.md §3 items 1 and 5, §4.1).

Round-trip method: each generated program `p` is rendered with
`render(p_without_comments, seed, naming="descriptive")`, parsed back with
`parse_program(source, keep_names=True)` (so the ids ARE the rendered
identifiers), and re-rendered with `render_source(parsed)` (render.py's own
statement layout under the identity name map). That text must equal the
original rendering byte for byte, and the parsed program's `run()` must give
the original's globals (keyed by rendered name), stdout and error. Comments
are stripped before rendering because the parser drops comments; they never
affect names (`build_names` does not read them).
"""

from __future__ import annotations

import dataclasses
import os
import time

import pytest

from cartographer.codefix import (
    CONSTRUCTS,
    CodeParseError,
    HarnessResult,
    SubsetViolation,
    call_entry,
    equal_outputs,
    outputs_on,
    parse_program,
    render_source,
    values_equal,
)
from cartographer.codeworld import TRAP_KEYS, TRUE, CodeDimParams, GenerationFailed, generate, render, run
from cartographer.codeworld.model import (
    BinOp,
    Compare,
    Const,
    ExprStmt,
    FuncDef,
    MethodCall,
    Name,
    Program,
    Query,
    Return,
    While,
)

_FIELDS = {f.name for f in dataclasses.fields(CodeDimParams)}


def _dials(**kw) -> dict:
    """Only the dials this CodeDimParams has (getattr guard for older/newer models)."""
    return {k: v for k, v in kw.items() if k in _FIELDS}


def grid(i: int) -> CodeDimParams:
    """tests/test_codeworld.py `grid`: the round-1 grid, trap subsets included."""
    h = lambda k, n: (i * 2654435761 + k * 97) % n  # noqa: E731
    traps = {k: bool((i >> j) & 1) for j, k in enumerate(TRAP_KEYS)}
    return CodeDimParams(
        n_statements=[4, 8, 15, 25, 40, 60][h(1, 6)], max_nesting=h(2, 4),
        n_variables=[2, 3, 5, 8, 12][h(3, 5)], distractor_fraction=[0, 0.2, 0.4, 0.6][h(4, 4)],
        identifier_confusability=[0, 0.3, 0.6, 1.0][h(5, 4)], **traps,
    )


def r2_grid(i: int) -> CodeDimParams:
    """tests/test_codeworld_round2.py `r2_grid` (round-2 dials, comments included)."""
    h = lambda k, n: (i * 2654435761 + k * 97) % n  # noqa: E731
    return CodeDimParams(
        n_statements=[8, 15, 22, 30][h(1, 4)], max_nesting=h(2, 3), n_variables=[2, 3, 5][h(3, 3)],
        distractor_fraction=[0, 0.2, 0.4][h(4, 3)], identifier_confusability=[0, 0.3, 0.6][h(5, 3)],
        **_dials(chain_depth=[0, 0, 3, 8, 15][h(6, 5)], stack_depth=h(7, 5), loop_branch=bool(h(8, 2)),
                 while_on=bool(h(9, 2)), negative_modulo_on=h(10, 7) == 0, sign_chain=h(11, 4),
                 index_math=h(12, 4), misleading_comments=h(13, 4)),
    )


def dialect_grid(i: int) -> CodeDimParams:
    rules = ("inclusive_range", "truncating_modulo", "copy_on_assign", "fresh_defaults")
    k = i % 4 + 1
    return CodeDimParams(n_statements=[8, 15][i % 2], max_nesting=i % 3, dialect=rules[:k],
                         **_dials(while_on=bool(i % 2), opaque_literal_on=i % 5 == 0))


def _params(i: int) -> CodeDimParams:
    return (grid, r2_grid, dialect_grid)[i % 3](i)


# --- acceptance 1: round trip on 3,000 generated programs --------------------------


def test_round_trip_source_and_execution_on_3000_generated_programs():
    n, failures, ok, n_while, n_def, n_default = 3000, 0, 0, 0, 0, 0
    for i in range(n):
        try:
            prog = generate(_params(i), 20_000 + i, query_kind="stdout" if i % 10 == 0 else "final_value")
        except GenerationFailed:
            failures += 1
            continue
        clean = dataclasses.replace(prog, comments=())
        r = render(clean, i, naming="descriptive")
        parsed = parse_program(r.source, keep_names=True)
        assert render_source(parsed) == r.source, (i, r.source)
        a, b = run(prog, TRUE), run(parsed, TRUE)
        assert {r.name_map[k]: v for k, v in a.globals.items()} == b.globals, (i, r.source)
        assert (a.stdout, a.error) == (b.stdout, b.error), i
        n_while += any(isinstance(s, While) for s in parsed.body)
        defs = [s for s in parsed.body if isinstance(s, FuncDef)]
        n_def += bool(defs)
        n_default += any(s.defaults for s in defs)
        ok += 1
    assert failures < 0.05 * n
    assert ok >= 2850
    assert n_while > 50 and n_def > 300 and n_default > 50  # the grid really covers while / defs / defaults


def test_abstract_ids_render_and_run_like_the_original():
    for i in range(150):
        try:
            prog = generate(_params(i), 40_000 + i)
        except GenerationFailed:
            continue
        r = render(dataclasses.replace(prog, comments=()), i, naming="descriptive")
        parsed = parse_program(r.source)
        assert all(v.id[0] in "vf" for v in parsed.variables)
        rr = render(parsed, 0)  # names it like a generated program; must not crash
        again = parse_program(rr.source, keep_names=True)
        assert render_source(again) == rr.source
        a, b = run(prog, TRUE), run(parsed, TRUE)
        assert sorted(map(repr, a.globals.values())) == sorted(map(repr, b.globals.values()))


def test_statement_ids_are_1_to_n_in_source_order_and_metadata_is_inferred():
    src = ("total = 3\nitems = [1, 2]\nword = 'ab'\nconf = {'a': 1}\n\ndef bump(n, k=2):\n"
           "    m = n + k\n    return m\n\nfor i in range(3):\n    total += bump(i)\n")
    p = parse_program(src, keep_names=True)
    from cartographer.codeworld.model import iter_stmts

    assert [s.sid for s in iter_stmts(p.body)] == list(range(1, 10))
    assert {v.id: v.kind for v in p.variables} == {"total": "int", "items": "list", "word": "str",
                                                   "conf": "dict", "bump": "func"}
    assert all(v.role == "live" for v in p.variables)
    assert set(p.locals_pool) == {"n", "k", "m", "i"}
    assert p.query == Query("final_value", "total") and p.entry is None


def test_entry_query_is_the_returned_name():
    p = parse_program("def solve(a, b):\n    out = a + b\n    return out\n", entry="solve", keep_names=True)
    assert p.entry == "solve" and p.query == Query("final_value", "out")
    p2 = parse_program("def solve(a, b):\n    out = a + b\n    return out\n", entry="solve")
    assert p2.entry == "f0" and call_entry(p2, (2, 3)).value == 5


def test_harmless_model_spellings_are_accepted():
    src = ("def solve(xs: list, n: int = -1) -> int:\n    \"\"\"doc\"\"\"\n    t: int = 0\n"
           "    for x in xs:\n        if x > n:\n            t += x\n        else:\n            pass\n"
           "    return -t\n")
    p = parse_program(src, entry="solve")
    assert call_entry(p, ([1, 5, -3],)).value == -6


# --- acceptance 1: the 25-snippet rejection list --------------------------------------

REJECT = [
    ("import os", "import"),
    ("from os import path", "import"),
    ("class A:\n    pass", "class"),
    ("f = lambda x: x", "lambda"),
    ("ys = [x for x in range(3)]", "comprehension"),
    ("try:\n    x = 1\nexcept Exception:\n    x = 2", "try"),
    ("s = f'{1}'", "fstring"),
    ("x = 0\nwhile True:\n    x += 1", "while_true_unbounded"),
    ("def f(n):\n    return f(n - 1)", "recursion"),
    ("x = 0\ndef f():\n    global x\n    x = 1", "global_nonlocal"),
    ("x = [1]\ny = x.sort()", "attribute"),
    ("x = 1.5", "float"),
    ("x = 2 ** 10", "power"),
    ("x = None\ny = x is None", "is_operator"),
    ("x = 1\ny = 0 < x < 2", "chained_comparison"),
    ("a, b = 1, 2", "tuple_unpacking"),
    ("x = 1\ndel x", "delete"),
    ("x = 1\nassert x", "assert"),
    ("def f(*args):\n    return 1", "starargs"),
    ("def f(a):\n    def g(b):\n        return b\n    return g(a)", "nested_def_depth"),
    ("x = 1\nprint(x, x)", "print_multiple_args"),
    ("x = input()", "builtin_call"),
    ("f = open('/tmp/x')", "builtin_call"),
    ("y = (x := 3)", "walrus"),
    ("x = 1\nmatch x:\n    case 1:\n        y = 2", "match"),
    ("async def f():\n    return 1", "async"),
]


@pytest.mark.parametrize("src,construct", REJECT, ids=[c + str(i) for i, (_, c) in enumerate(REJECT)])
def test_rejection_list_names_the_construct(src, construct):
    assert construct in CONSTRUCTS
    with pytest.raises(SubsetViolation) as ei:
        parse_program(src)
    assert ei.value.construct == construct, (src, ei.value)
    assert ei.value.lineno >= 1


@pytest.mark.parametrize("src,construct", [
    ("def f(a):\n    return g(a)\n\ndef g(a):\n    return f(a)", "recursion"),
    ("x = [1]\nwith x:\n    y = 1", "with"),
    ("raise ValueError", "raise"),
    ("def f():\n    yield 1", "yield"),
    ("x = [1, 2]\ny = x[::2]", "slice_step"),
    ("def f(a):\n    return a\ny = f(a=1)", "keyword_argument"),
    ("x = 4\nx //= 2", "augassign_op"),
    ("x = 7 / 2", "float"),
    ("x = eval('1')", "builtin_call"),
    ("x = getattr(1, 'real')", "builtin_call"),
    ("x = range(3)", "builtin_call"),
    ("x = [1]\ny = 1 in x", "unknown"),
    ("for i in range(3):\n    break", "unknown"),
    ("while 1 == 1:\n    y = 2", "while_true_unbounded"),
    ("y = __builtins__", "builtin_call"),
])
def test_more_constructs_are_rejected_by_name(src, construct):
    with pytest.raises(SubsetViolation) as ei:
        parse_program(src)
    assert ei.value.construct == construct, ei.value


def test_syntax_error_is_a_parse_error_not_a_subset_violation():
    with pytest.raises(CodeParseError):
        parse_program("def solve(:\n")
    with pytest.raises(CodeParseError):
        parse_program("x = " + "(" * 5000 + "1" + ")" * 5000)


# --- acceptance 5: submissions outside the subset are never executed --------------------


def test_dangerous_snippets_are_rejected_fast_with_no_side_effect(tmp_path):
    sentinel = tmp_path / "pwned"
    cases = [
        (f'import os\nos.system("touch {sentinel}")', "import"),
        (f'os = __import__("os")\nos.system("touch {sentinel}")', "builtin_call"),
        (f'f = open("{sentinel}", "w")', "builtin_call"),
        ('f = open("/etc/passwd")', "builtin_call"),
        ("x = 0\nwhile True:\n    x += 1", "while_true_unbounded"),
        (f'def solve():\n    __import__("os").system("touch {sentinel}")\n    return 1', "builtin_call"),
    ]
    for src, construct in cases:
        t0 = time.perf_counter()
        with pytest.raises(SubsetViolation) as ei:
            parse_program(src, entry="solve")
        assert time.perf_counter() - t0 < 0.1, src
        assert ei.value.construct == construct, (src, ei.value)
    assert not os.path.exists(sentinel)
    assert list(tmp_path.iterdir()) == []


def test_a_billion_iteration_while_in_the_subset_stops_at_the_step_cap():
    p = parse_program("def solve():\n    x = 0\n    while x < 1000000000:\n        x += 1\n    return x\n",
                      entry="solve")
    t0 = time.perf_counter()
    r = call_entry(p, ())
    assert time.perf_counter() - t0 < 2 and r.error is not None and r.error.startswith("step_cap")


# --- harness ----------------------------------------------------------------------------


def _fn(params, body, name="solve") -> Program:
    return Program(body=(FuncDef(1, name, tuple(params), (), tuple(body)),), query=Query("final_value", None),
                   params=CodeDimParams(), seed=0, entry=name)


def test_harness_calls_a_hand_built_function():
    p = _fn(("a", "b"), [Return(2, BinOp("+", Name("a"), Name("b")))])
    r = call_entry(p, (2, 3))
    assert r == HarnessResult(5, None, r.steps, []) and r.steps > 0
    assert [x.value for x in outputs_on(p, [(1, 1), (-4, 2)])] == [2, -2]


def test_a_mutating_function_does_not_leak_between_calls_or_into_the_input():
    p = parse_program("def solve(xs):\n    xs.append(9)\n    return len(xs)\n", entry="solve")
    arg = [1, 2]
    rs = outputs_on(p, [(arg,), (arg,), (arg,)])
    assert [r.value for r in rs] == [3, 3, 3] and arg == [1, 2]
    p2 = parse_program("def solve(xs):\n    xs.append(9)\n    return xs\n", entry="solve")
    out = call_entry(p2, (arg,)).value
    out.append(0)
    assert arg == [1, 2]


def test_runtime_errors_are_reported_not_raised():
    p = _fn(("xs",), [Return(2, MethodCall("xs", "pop", ()))])
    r = call_entry(p, ([],))
    assert r.value is None and r.error.startswith("index_error")
    assert call_entry(p, ([4],)).value == 4
    assert call_entry(p, (1, 2)).error.startswith("arity")
    missing = dataclasses.replace(p, entry="nope")
    assert call_entry(missing, ()).error.startswith("not_callable")
    setup_crash = parse_program("x = 1 // 0\n\ndef solve():\n    return 1\n", entry="solve")
    assert call_entry(setup_crash, ()).error.startswith("operand_error")


def test_step_cap_on_a_while_that_never_exits():
    loop = While(2, Compare("<", Name("a"), Const(10)), (ExprStmt(3, Const(None)),))
    p = _fn(("a",), [loop, Return(4, Name("a"))])
    r = call_entry(p, (0,), step_cap=200)
    assert r.error.startswith("step_cap") and r.steps > 200
    assert call_entry(p, (11,), step_cap=200).value == 11


def test_setup_runs_up_to_the_last_def_and_skips_demo_calls():
    src = ("K = 10\n\ndef helper(v):\n    return v + K\n\ndef solve(n):\n    return helper(n)\n\n"
           "print(solve(1))\nx = [][0]\n")
    p = parse_program(src, entry="solve")
    r = call_entry(p, (5,))
    assert (r.value, r.error, r.stdout) == (15, None, [])


def test_equal_outputs_is_type_strict_and_normalises_tuples():
    def ok(v):
        return HarnessResult(v, None, 1, [])

    assert not equal_outputs(ok(True), ok(1))
    assert not equal_outputs(ok(1), ok("1"))
    assert equal_outputs(ok((1, 2)), ok([1, 2]))
    assert equal_outputs(ok([(1, True)]), ok([[1, True]]))
    assert not equal_outputs(ok([1, True]), ok([1, 1]))
    assert not equal_outputs(ok({"a": 1}), ok({"a": True}))
    assert equal_outputs(ok(None), ok(None))
    err_a = HarnessResult(None, "index_error: x", 1, [])
    err_b = HarnessResult(None, "index_error: y", 5, [])
    err_c = HarnessResult(None, "step_cap: z", 5, [])
    assert equal_outputs(err_a, err_b) and not equal_outputs(err_a, err_c)
    assert not equal_outputs(err_a, ok(None))
    assert values_equal((1, (2, 3)), [1, [2, 3]]) and not values_equal(0, False)
