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
    ("x = [1]\ny = {1}", "unknown"),  # (was `1 in x`: membership is in the subset now)
    ("for i in range(3):\n    pass\nbreak", "unknown"),  # (was a break INSIDE the loop: accepted now)
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


# --- model-written constructs: range(<expr>), in / not in, break / continue ----------------
#
# A small deterministic generator (test-side; NOT codeworld.generate, which never emits these
# constructs) writes `solve(xs, s, d, n)` programs. Each is parsed and run by OUR harness on 5
# inputs and, in this test only, also exec'd by CPython (programs we wrote ourselves, never
# model text). The two must agree on every call: same value, or both raise (same error kind).

import copy as _copy
import random as _random


class _ProgGen:
    """`solve(xs, s, d, n)`: xs list[int], s str, d dict[str, int], n int >= 0."""

    def __init__(self, seed: int, inclusive: bool = False) -> None:
        self.r = _random.Random(seed)
        self.inclusive = inclusive  # emit the CPython translation of the inclusive_range dialect
        self.lines: list[str] = []
        self.k = 0
        self.used: set[str] = set()

    def fresh(self, base: str) -> str:
        self.k += 1
        return f"{base}{self.k}"

    def emit(self, depth: int, text: str) -> None:
        self.lines.append("    " * depth + text)

    def range_header(self, var: str) -> tuple[str, bool]:
        """(`range(...)` text, True iff `xs[var]` is a safe index)."""
        r = self.r
        one = r.random() < 0.45
        if one:
            hi, safe = r.choice([("len(xs)", True), ("n", False), ("len(s)", False), ("n + 1", False),
                                 ("len(xs) - 1", True), ("len(d)", False)])
            if hi.startswith("len(xs)"):
                self.used.add("len(xs)")
            if self.inclusive:
                hi = f"{hi} + 1"  # (safe stays: both sides then index one past the end alike)
            return f"range({hi})", safe
        lo, hi, safe = r.choice([("1", "n", False), ("1", "n", False), ("1", "len(xs)", True), ("n", "len(xs) + 2", False),
                                 ("0 - n", "n", False), ("2", "len(s)", False), ("len(xs) - n", "len(xs)", False)])
        if (lo, hi) == ("1", "n"):
            self.used.add("range(1, n)")
        if self.inclusive:
            hi = f"{hi} + 1"
        return f"range({lo}, {hi})", safe

    def cond(self, kind: str, v: str, safe: bool) -> str:
        r = self.r
        if kind == "int":
            opts = [f"{v} in xs", f"{v} not in xs", f"{v} % 3 == 0", f"{v} > n", f"{v} in [1, 4, 7]",
                    f"{v} + n in xs", f"{v} not in [0, 2]", f"xs[{v}] == n"]  # last: may raise, like Python
            if safe:
                opts += [f"xs[{v}] in xs[0:{v}]", f"xs[{v}] not in xs[0:{v}]", f"xs[{v}] > {v}"]
            c = r.choice(opts)
        else:
            c = r.choice([f"{v} in d", f"{v} not in d", f"{v} in 'aeiou'", f"{v} not in s", f"{v} == 'c'",
                          f"{v} + 'a' in s", f"{v} in ['a', 'b']"])
        if " in " in c:
            self.used.add("in_dict" if c.endswith(" d") else "in")
        return c

    def leaf(self, depth: int, kind: str, v: str, safe: bool) -> None:
        r = self.r
        if kind == "int":
            val = r.choice([v, f"{v} * 2", f"{v} - n"] + ([f"xs[{v}]"] if safe else []))
        else:
            val = r.choice([v, f"{v}.upper()"])
        choice = r.randrange(5)
        if choice == 0:
            self.emit(depth, f"out.append({val})")
        elif choice == 1 and kind == "int":
            self.emit(depth, f"acc += {val}")
        elif choice == 1:
            self.emit(depth, f"if {v} in d:")
            self.emit(depth + 1, f"acc += d[{v}]")
            self.used.add("in_dict")
        elif choice == 2:
            self.emit(depth, f"if {self.cond(kind, v, safe)}:")
            self.emit(depth + 1, "acc += 1")
            self.emit(depth, "else:")
            self.emit(depth + 1, "acc -= 1")
        else:
            self.emit(depth, "acc += 1")

    def loop_body(self, depth: int, kind: str, v: str, safe: bool, loop: str, nest: int) -> None:
        r = self.r
        n_stmts = r.randint(1, 3)
        for _ in range(n_stmts):
            x = r.random()
            if x < 0.25:
                self.emit(depth, f"if {self.cond(kind, v, safe)}:")
                self.emit(depth + 1, "break")
                self.used.add(f"break_{loop}")
            elif x < 0.47:
                self.emit(depth, f"if {self.cond(kind, v, safe)}:")
                self.emit(depth + 1, "continue")
                self.used.add(f"continue_{loop}")
            elif x < 0.54 and nest > 0:
                self.loop(depth, nest - 1)
            else:
                self.leaf(depth, kind, v, safe)

    def loop(self, depth: int, nest: int) -> None:
        r = self.r
        form = r.choice((0, 0, 1, 2, 3, 4))
        if form == 0:
            v = self.fresh("i")
            header, safe = self.range_header(v)
            self.emit(depth, f"for {v} in {header}:")
            self.used.add("range")
            self.loop_body(depth + 1, "int", v, safe, "for", nest)
        elif form == 1:
            v = self.fresh("x")
            self.emit(depth, f"for {v} in xs:")
            self.loop_body(depth + 1, "int", v, False, "for", nest)
        elif form == 2:
            v = self.fresh("ch")
            self.emit(depth, f"for {v} in s:")
            self.loop_body(depth + 1, "str", v, False, "for", nest)
        elif form == 3:
            v = self.fresh("k")
            bound = r.choice(["n", "len(xs)", "len(s) + 1"])
            self.emit(depth, f"{v} = 0")
            self.emit(depth, f"while {v} < {bound}:")
            self.emit(depth + 1, f"{v} += 1")  # increment first, so `continue` cannot skip it
            self.loop_body(depth + 1, "int", v, False, "while", nest)
        else:
            v, w = self.fresh("ys"), self.fresh("e")
            self.emit(depth, f"{v} = xs[:]")
            self.emit(depth, f"while {v}:")
            self.emit(depth + 1, f"{w} = {v}.pop()")
            self.loop_body(depth + 1, "int", w, False, "while", nest)

    def program(self) -> str:
        r = self.r
        self.emit(0, "def solve(xs, s, d, n):")
        self.emit(1, "acc = 0")
        self.emit(1, "out = []")
        for _ in range(r.randint(1, 3)):
            self.loop(1, 1)
        self.emit(1, r.choice(["return acc", "return out", "return [acc, out]", "return [acc, len(out)]"]))
        return "\n".join(self.lines) + "\n"


def _inputs(seed: int) -> list[tuple]:
    r = _random.Random(seed * 7919 + 1)
    out = []
    for _ in range(5):
        xs = [r.randint(-3, 9) for _ in range(r.randint(0, 6))]
        s = "".join(r.choice("abcdeu") for _ in range(r.randint(0, 6)))
        d = {k: r.randint(-2, 5) for k in "abcde" if r.random() < 0.5}
        out.append((xs, s, d, r.randint(0, 7)))
    return out


_PY_KINDS = {TypeError: "type_error", IndexError: "index_error", KeyError: "key_error",
             ZeroDivisionError: "operand_error", ValueError: "value_error"}


def _cpython_call(src: str, args: tuple):
    """CPython reference: exec a program THIS TEST generated (never model text), call solve."""
    ns: dict = {}
    exec(compile(src, "<generated>", "exec"), ns)  # noqa: S102 - test-only, our own generated source
    try:
        return ns["solve"](*_copy.deepcopy(args)), None
    except Exception as e:  # noqa: BLE001
        return None, _PY_KINDS.get(type(e), type(e).__name__)


def _agree(src: str, ref_src: str, args: tuple, params=None) -> None:
    p = parse_program(src, entry="solve", params=params)
    ours = call_entry(p, args)
    val, kind = _cpython_call(ref_src, args)
    if kind is None:
        assert ours.error is None and values_equal(ours.value, val), (src, args, ours, val)
    else:
        assert ours.error is not None and ours.error.split(":", 1)[0] == kind, (src, args, ours, kind)


def test_range_expr_membership_break_continue_agree_with_cpython_on_320_programs():
    used: dict[str, int] = {}
    n_calls = n_err = 0
    for seed in range(320):
        g = _ProgGen(seed)
        src = g.program()
        parsed = parse_program(src, keep_names=True, entry="solve")
        assert render_source(parsed) == src, src  # renders back byte for byte
        for args in _inputs(seed):
            _agree(src, src, args)
            n_calls += 1
            n_err += _cpython_call(src, args)[1] is not None
        for k in g.used:
            used[k] = used.get(k, 0) + 1
    assert n_calls == 1600 and n_err < 0.2 * n_calls, n_err
    for k in ("range", "len(xs)", "range(1, n)", "in", "in_dict", "break_for", "continue_for", "break_while",
              "continue_while"):
        assert used.get(k, 0) >= 15, (k, used)


def test_inclusive_range_dialect_on_expression_bounds_matches_translated_cpython():
    dialect = CodeDimParams(dialect=("inclusive_range",))
    n_range = 0
    for seed in range(120):
        src = _ProgGen(1000 + seed).program()
        translated = _ProgGen(1000 + seed, inclusive=True).program()  # same draws, `range(.. hi + 1)`
        if "range(" not in src:
            continue
        n_range += 1
        assert translated != src
        for args in _inputs(1000 + seed):
            _agree(src, translated, args, params=dialect)
    assert n_range >= 40
    p = parse_program("def solve(n):\n    t = 0\n    for i in range(n):\n        t += i\n    return t\n",
                      entry="solve", params=dialect)
    assert call_entry(p, (4,)).value == 0 + 1 + 2 + 3 + 4
    assert call_entry(parse_program("def solve(n):\n    t = 0\n    for i in range(n):\n        t += i\n"
                                    "    return t\n", entry="solve"), (4,)).value == 6


def test_membership_semantics_and_errors():
    p = parse_program("def solve(a, b):\n    return [a in b, a not in b]\n", entry="solve")
    for a, b in [(1, [1, 2]), (3, [1, 2]), ("b", "abc"), ("bc", "abc"), ("z", "abc"), ("k", {"k": 0}),
                 ("v", {"k": "v"}), ([1], [[1], 2]), ("", "")]:
        assert call_entry(p, (a, b)).value == [a in b, a not in b], (a, b)
    assert call_entry(p, (1, "abc")).error.startswith("type_error")
    assert call_entry(p, (1, 5)).error.startswith("type_error")
    assert call_entry(p, ([1], {"a": 1})).error.startswith("type_error")
    with pytest.raises(SubsetViolation) as ei:
        parse_program("x = [1]\ny = 1 in x in x")
    assert ei.value.construct == "chained_comparison"


def test_range_bounds_must_be_ints_at_run_time():
    p = parse_program("def solve(a):\n    t = 0\n    for i in range(a):\n        t += 1\n    return t\n",
                      entry="solve")
    assert call_entry(p, (3,)).value == 3 and call_entry(p, (-2,)).value == 0
    assert call_entry(p, ("3",)).error.startswith("type_error")
    assert call_entry(p, ([1],)).error.startswith("type_error")
    big = parse_program("def solve():\n    t = 0\n    for i in range(10000000):\n        t += 1\n    return t\n",
                        entry="solve")
    assert call_entry(big, ()).error.startswith("step_cap")


def test_break_and_continue_bind_to_the_innermost_loop():
    src = ("def solve(xs):\n    out = []\n    for a in xs:\n        for b in xs:\n            if b > a:\n"
           "                break\n            if b == a:\n                continue\n            out.append(b)\n"
           "        if a == 3:\n            break\n    return out\n")
    p = parse_program(src, entry="solve")
    for xs in ([1, 2, 3, 4], [3, 1, 2], [], [5, 5, 0, 3, 1]):
        assert values_equal(call_entry(p, (xs,)).value, _cpython_call(src, (xs,))[0]), xs


@pytest.mark.parametrize("src,detail", [
    ("x = 0\nwhile True:\n    x += 1", "constant test"),
    ("x = 0\nwhile 1:\n    x += 1", "constant test"),  # no break: nothing can end it
    ("x = 0\nwhile 'go':\n    x += 1", "constant test"),
    ("def solve(n):\n    t = 0\n    while n > 0:\n        t += n\n    return t", "test reads n"),
    ("def solve(xs, i):\n    t = 0\n    while i < len(xs):\n        t += xs[i]\n    return t", "test reads i, xs"),
])
def test_obviously_unbounded_while_is_rejected_at_parse_time(src, detail):
    with pytest.raises(SubsetViolation) as ei:
        parse_program(src, entry="solve")
    assert ei.value.construct == "while_true_unbounded" and detail in ei.value.detail, ei.value


@pytest.mark.parametrize("src,args,want", [
    ("def solve(n):\n    t = 0\n    while n > 0:\n        t += n\n        n -= 1\n    return t", (4,), 10),
    ("def solve(xs):\n    t = 0\n    while xs:\n        t += xs.pop()\n    return t", ([1, 2, 3],), 6),
    ("def solve(xs):\n    i = 0\n    while i < len(xs):\n        if xs[i] < 0:\n            break\n"
     "        i += 1\n    return i", ([2, 5, -1, 3],), 2),
    ("def solve(xs):\n    ys = xs\n    while xs:\n        ys.pop()\n    return len(xs)", ([1, 2],), 0),
    ("def solve(n):\n    t = 0\n    while n > 0:\n        for j in range(n):\n            t += 1\n"
     "        n = n // 2\n    return t", (4,), 7),
])
def test_bounded_while_forms_are_accepted(src, args, want):
    assert call_entry(parse_program(src, entry="solve"), args).value == want


def test_break_continue_outside_a_loop_and_range_step_are_rejected():
    cases = [
        ("break", "unknown", "break_outside_loop"),
        ("x = 1\nif x:\n    continue", "unknown", "continue_outside_loop"),
        ("def solve(xs):\n    if xs:\n        break\n    return 1", "unknown", "break_outside_loop"),
        ("for i in range(1, 10, 2):\n    x = i", "unknown", "range_step"),
        ("n = 3\nfor i in range(0, n, 1):\n    x = i", "unknown", "range_step"),
        ("x = range(3)", "builtin_call", "range"),
        ("n = 3\nxs = [range(n)]", "builtin_call", "range"),
        ("for i in range(1.5):\n    x = i", "float", ""),
        ("for i in range(len(xs) ** 2):\n    x = i", "power", ""),
    ]
    for src, construct, detail in cases:
        with pytest.raises(SubsetViolation) as ei:
            parse_program(src)
        assert ei.value.construct == construct and ei.value.detail == detail, (src, ei.value)


def test_while_true_with_break_is_accepted_and_runs():
    from cartographer.codefix import call_entry, parse_program

    src = "def solve(n):\n    i = 0\n    while True:\n        i += 1\n        if i >= n:\n            break\n    return i\n"
    prog = parse_program(src, entry="solve")
    assert call_entry(prog, (5,)).value == 5
    src2 = "def solve(n):\n    i = 0\n    while True:\n        i += 1\n    return i\n"
    import pytest as _pt

    from cartographer.codefix.parse import SubsetViolation

    with _pt.raises(SubsetViolation) as e:
        parse_program(src2, entry="solve")
    assert e.value.construct == "while_true_unbounded"


# --- deterministic builtins sum / min / max / abs (spec-province-v-ambiguities §B) ----------


def _builtin_expr(r, depth: int = 0) -> str:
    """A random int/bool/list expression built from sum/min/max/abs over the
    solve(xs, ys, a, b) parameters (ints, bools, lists of ints / bools)."""
    lists = ["xs", "ys", "xs[1:]", "[a, b]", "[]", "[True, a]", "xs + ys"]
    ints = ["a", "b", "True", "False", "-3", "0", "len(xs)", "a - b"]
    if depth < 2 and r.random() < 0.4:
        ints.append(_builtin_expr(r, depth + 1))
    f = r.choice(("sum", "min", "max", "abs"))
    if f == "sum":
        return f"sum({r.choice(lists)})"
    if f == "abs":
        return f"abs({r.choice(ints)})"
    if r.random() < 0.5:
        return f"{f}({r.choice(lists)})"
    return f"{f}(" + ", ".join(r.choice(ints) for _ in range(r.randint(2, 4))) + ")"


def _builtin_program(seed: int) -> str:
    r = _random.Random(10_000 + seed)
    lines = ["def solve(xs, ys, a, b):", "    acc = 0", "    out = []"]
    for _ in range(r.randint(1, 4)):
        e = _builtin_expr(r)
        form = r.randrange(4)
        if form == 0:
            lines.append(f"    acc += {e}")
        elif form == 1:
            lines.append(f"    out.append({e})")
        elif form == 2:
            lines += ["    for x in xs:", f"        if x > {e}:", "            acc += x"]
        else:
            lines.append(f"    ys = ys + [{e}]")
    lines.append(r.choice(["    return acc", "    return out", "    return [acc, out]", f"    return {_builtin_expr(r)}"]))
    return "\n".join(lines) + "\n"


def _builtin_inputs(seed: int) -> list[tuple]:
    r = _random.Random(seed * 104729 + 3)
    out = []
    for _ in range(4):
        xs = [r.randint(-5, 9) for _ in range(r.randint(0, 5))]
        ys = [r.choice([True, False, r.randint(-4, 4)]) for _ in range(r.randint(0, 4))]
        out.append((xs, ys, r.choice([True, False, r.randint(-7, 7)]), r.randint(-7, 7)))
    return out


def test_builtins_sum_min_max_abs_agree_with_cpython_on_400_programs():
    n_calls = n_err = 0
    used: dict[str, int] = {}
    for seed in range(400):
        src = _builtin_program(seed)
        parsed = parse_program(src, keep_names=True, entry="solve")
        assert render_source(parsed) == src, src  # renders back byte for byte
        for args in _builtin_inputs(seed):
            _agree(src, src, args)  # value and type-strict equality (True != 1), or same error kind
            n_calls += 1
            n_err += _cpython_call(src, args)[1] is not None
        for f in ("sum(", "min(", "max(", "abs("):
            used[f] = used.get(f, 0) + (f in src)
    assert n_calls == 1600 and n_err < 0.5 * n_calls, n_err
    assert min(used.values()) >= 100, used


@pytest.mark.parametrize("src,want", [
    ("def solve(xs):\n    return sum([True, True, False])\n", 2),
    ("def solve(xs):\n    return abs(True)\n", 1),
    ("def solve(xs):\n    return max(True, 1)\n", True),
    ("def solve(xs):\n    return min(0, False)\n", 0),
    ("def solve(xs):\n    return min(3, 1, 2)\n", 1),
    ("def solve(xs):\n    return max(xs)\n", 4),
])
def test_builtin_edge_semantics_are_cpython(src, want):
    res = call_entry(parse_program(src, entry="solve"), ([4, -2],))
    assert res.error is None and type(res.value) is type(want) and res.value == want


def test_builtin_empty_min_max_is_a_runtime_error_and_other_builtins_stay_rejected():
    for f in ("min", "max"):
        res = call_entry(parse_program(f"def solve(xs):\n    return {f}(xs)\n", entry="solve"), ([],))
        assert res.value is None and res.error.startswith("value_error")
    for src, construct in [("x = sorted([1])", "builtin_call"), ("x = sum([1], 5)", "unknown"),
                           ("x = max([1], default=0)", "keyword_argument"), ("x = abs()", "unknown"),
                           ("x = round(1)", "builtin_call"), ("f = sum", "builtin_call")]:
        with pytest.raises(SubsetViolation) as e:
            parse_program(src)
        assert e.value.construct == construct, src
