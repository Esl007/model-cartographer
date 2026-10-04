"""Province IV counterfactual-dialect trap (Wu et al. 2023, "Reasoning or
Reciting?"): a program DECLARES deviations from Python in a header before the
code; truth is our interpreter under the declared semantics, and the plain
Python reading is the `python_prior` foil. All offline, no LLM anywhere."""

from __future__ import annotations

import ast
import copy
import hashlib
from dataclasses import replace

import pytest

from cartographer.codeworld.foils import FOILS, PYTHON_PRIOR, TRAP_FOILS, foil_answers, foils_for
from cartographer.codeworld.generate import GenerationFailed, generate, validate
from cartographer.codeworld.interpret import DIALECT_SEMANTICS, TRUE, Semantics, answer, run, semantics_for
from cartographer.codeworld.model import DIALECT_RULES, DIALECT_TRAPS, TRAP_KEYS, CodeDimParams
from cartographer.codeworld.render import DIALECT_HEADER, render
from cartographer.codeworld.verify import verify_answer
from cartographer.exec.evaluator import (
    _apply_code_config,
    _code_latent,
    _default_code_base,
    build_code_task,
    default_code_dialect_ray,
)
from cartographer.schedule.escalate import find_breaking_point
from cartographer.store.schema import Verdict
from tests.test_codeworld import _cpython, grid
from tests.test_codeworld_zoo import _evaluator, seen, store  # noqa: F401  (pytest fixtures)

# sha256 over 240 generated programs, computed BEFORE the dialect existed
# (prompt + body + trap sites + distractor ids + variables).
_BASELINE_DIGEST = "06d711fb59edccfaaa1f978129612c002841b6ed59f766674cd0a948f2ad6081"


def _subset(i: int) -> tuple:
    """A deterministic non-empty dialect (all 15 subsets cycle)."""
    mask = 1 + i % 15
    return tuple(r for j, r in enumerate(DIALECT_RULES) if (mask >> j) & 1)


def dialect_grid(i: int) -> CodeDimParams:
    """`grid(i)` with a non-empty dialect; trap toggles that double-book a
    declared rule are switched off."""
    d = _subset(i)
    p = grid(i)
    return replace(p, **{DIALECT_TRAPS[r]: False for r in d}, dialect=d)


def python_reader(prog, rendered):  # ignores the header
    return answer(prog, Semantics())


def dialect_reader(prog, rendered):  # follows the header
    return answer(prog)


# --- params, semantics, foils ---------------------------------------------------------


def test_params_validate_and_canonicalise_the_dialect():
    p = CodeDimParams(dialect=("fresh_defaults", "inclusive_range"))
    assert p.dialect == ("inclusive_range", "fresh_defaults")  # fixed DIALECT_RULES order
    with pytest.raises(ValueError):
        CodeDimParams(dialect=("one_indexed",))
    with pytest.raises(ValueError):
        CodeDimParams(dialect=("inclusive_range", "inclusive_range"))
    for rule, trap in DIALECT_TRAPS.items():
        with pytest.raises(ValueError):
            CodeDimParams(dialect=(rule,), **{trap: True})


def test_semantics_for_and_program_semantics():
    assert semantics_for(()) is TRUE
    full = semantics_for(DIALECT_RULES)
    assert full == Semantics(range_upper="inclusive", modulo="truncate", list_assignment="copy",
                             default_args="fresh")
    for rule, (field, value) in DIALECT_SEMANTICS.items():
        assert getattr(semantics_for((rule,)), field) == value
    prog = generate(CodeDimParams(n_statements=8, dialect=("truncating_modulo",)), 3)
    assert prog.semantics == Semantics(modulo="truncate")
    assert answer(prog) == answer(prog, prog.semantics) != answer(prog, TRUE)


def test_foils_are_relative_to_the_dialect_truth():
    plain = generate(CodeDimParams(n_statements=8), 1)
    assert foils_for(plain) is FOILS  # plain Python: unchanged, no python_prior
    prog = generate(CodeDimParams(n_statements=8, dialect=("inclusive_range", "copy_on_assign")), 1)
    readings = foils_for(prog)
    assert next(iter(readings)) == PYTHON_PRIOR and readings[PYTHON_PRIOR] == TRUE
    assert "inclusive_range" not in readings and "value_semantics" not in readings  # declared fields
    assert readings["early_binding"] == Semantics(range_upper="inclusive", list_assignment="copy",
                                                  closure_binding="early")
    assert set(foil_answers(prog)) == set(readings)


# --- generation: byte-identical when off, load-bearing when on --------------------------


def test_empty_dialect_generation_is_byte_identical_to_before():
    h = hashlib.sha256()
    for i in range(240):
        p = grid(i)
        if i % 3 == 0:
            p = replace(p, opaque_literal_on=True)
        try:
            prog = generate(p, 31_000 + i, query_kind="stdout" if i % 11 == 0 else "final_value")
        except GenerationFailed:
            h.update(b"FAIL")
            continue
        r = render(prog, i)
        h.update(r.prompt.encode())
        h.update(repr(prog.body).encode())
        h.update(repr(prog.trap_sites).encode())
        h.update(repr(sorted(prog.distractor_ids)).encode())
        h.update(repr(prog.variables).encode())
    assert h.hexdigest() == _BASELINE_DIGEST


def test_every_declared_rule_is_load_bearing_and_disabled_traps_stay_absent():
    fails = 0
    for i in range(300):
        p = dialect_grid(i)
        try:
            prog = generate(p, 12_000 + i)
        except GenerationFailed:
            fails += 1
            continue
        assert validate(prog) is None
        truth = answer(prog)
        assert answer(prog, TRUE) != truth, i  # the Python prior is always wrong
        for rule in p.dialect:  # undoing any ONE rule changes the answer
            field, _ = DIALECT_SEMANTICS[rule]
            assert answer(prog, replace(prog.semantics, **{field: getattr(TRUE, field)})) != truth, (i, rule)
        assert {t for t, _ in prog.trap_sites if t.startswith("dialect:")} == {f"dialect:{r}" for r in p.dialect}
        readings = foil_answers(prog)
        for trap, foil in TRAP_FOILS.items():
            if foil in readings:  # relative foils: enabled matter, disabled do not
                assert (readings[foil] != truth) == bool(getattr(p, trap)), (i, trap)
    assert fails / 300 < 0.03


# --- rendering ---------------------------------------------------------------------------


def test_header_precedes_the_code_one_numbered_line_per_rule():
    plain = render(generate(CodeDimParams(n_statements=8), 5), 5)
    assert "dialect" not in plain.prompt and plain.prompt.startswith("Here is a short Python program.\n\n```python")
    for k in range(1, 5):
        prog = generate(CodeDimParams(n_statements=8, dialect=DIALECT_RULES[:k]), 5)
        r = render(prog, 5)
        head, code = r.prompt.split("```python", 1)
        assert "differences from standard Python" in head
        lines = [ln for ln in head.splitlines() if ln[:1] == "("]
        assert lines == [f"({i}) {DIALECT_HEADER[rule]}" for i, rule in enumerate(DIALECT_RULES[:k], 1)]
        assert "dialect" not in code and r.prompt.rstrip().endswith("`ANSWER: <python literal>`.")
        # nothing else changes: the same program without the header renders the same code
        assert code == render(replace(prog, params=replace(prog.params, dialect=())), 5).prompt.split(
            "```python", 1)[1]


# --- the verifier: header-ignoring vs header-following readers ----------------------------


def test_header_ignoring_reader_fails_iff_dialect_and_names_python_prior():
    for i in range(160):
        p = dialect_grid(i) if i % 2 else grid(i)
        prog = generate(p, 15_000 + i, query_kind="stdout" if i % 9 == 0 else "final_value")
        r = render(prog, i)
        tv = verify_answer(f"ANSWER: {python_reader(prog, r)!r}", prog, r)
        if p.dialect:
            assert tv.verdict == Verdict.FAIL and "foil:python_prior" in tv.detail, (i, tv.detail)
            assert tv.foil_matches[0] == PYTHON_PRIOR
        else:
            assert tv.verdict == Verdict.PASS
        good = verify_answer(f"ANSWER: {dialect_reader(prog, r)!r}", prog, r)
        assert good.verdict == Verdict.PASS and good.truth == answer(prog)


def test_existing_foils_still_name_their_misreading_inside_a_dialect():
    for i in range(40):
        prog = generate(CodeDimParams(n_statements=10, dialect=("inclusive_range", "copy_on_assign"),
                                      late_binding_on=True), 600 + i)
        r = render(prog, i)
        wrong = answer(prog, foils_for(prog)["early_binding"])
        tv = verify_answer(f"ANSWER: {wrong!r}", prog, r)
        assert tv.verdict == Verdict.FAIL and "early_binding" in tv.foil_matches


# --- ground truth checked against CPython ------------------------------------------------


def test_cpython_differential_still_holds_for_the_empty_dialect():
    mismatches = []
    for i in range(400):
        try:
            prog = generate(grid(i), 1000 + i, query_kind="stdout" if i % 10 == 0 else "final_value")
        except GenerationFailed:
            continue
        r = render(prog, i)
        env, out = _cpython(r.source)
        tr = run(prog)
        if {r.name_map[k]: v for k, v in tr.globals.items()} != env or out != tr.stdout:
            mismatches.append(i)
    assert mismatches == []


class _Translate(ast.NodeTransformer):
    """Mechanically rewrite rendered source so CPython runs it under a dialect."""

    def __init__(self, dialect) -> None:
        self.d = set(dialect)

    def visit_Call(self, node):
        self.generic_visit(node)
        if "inclusive_range" in self.d and isinstance(node.func, ast.Name) and node.func.id == "range":
            node.args[-1] = ast.BinOp(node.args[-1], ast.Add(), ast.Constant(1))  # range(a, b) -> range(a, b + 1)
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if "truncating_modulo" in self.d and isinstance(node.op, (ast.FloorDiv, ast.Mod)):
            fn = "_tdiv" if isinstance(node.op, ast.FloorDiv) else "_tmod"
            return ast.Call(ast.Name(fn, ast.Load()), [node.left, node.right], [])
        return node

    def visit_Assign(self, node):
        self.generic_visit(node)
        if "copy_on_assign" in self.d and isinstance(node.value, ast.Name):
            node.value = ast.Call(ast.Name("_copy", ast.Load()), [node.value], [])
        return node

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        defaults = node.args.defaults
        if "fresh_defaults" in self.d and defaults:
            params = node.args.args[len(node.args.args) - len(defaults):]
            pre = [ast.If(ast.Compare(ast.Name(a.arg, ast.Load()), [ast.Is()], [ast.Name("_NODEF", ast.Load())]),
                          [ast.Assign([ast.Name(a.arg, ast.Store())], d)], [])
                   for a, d in zip(params, defaults)]
            node.args.defaults = [ast.Name("_NODEF", ast.Load()) for _ in defaults]
            node.body = pre + node.body
        return node


def _cpython_dialect(source: str, dialect):
    tree = ast.fix_missing_locations(_Translate(dialect).visit(ast.parse(source)))
    out: list[str] = []
    env = {
        "__builtins__": {"range": range, "len": len, "int": int,
                         "print": lambda *a: out.append(" ".join(str(x) for x in a))},
        "_tdiv": lambda a, b: int(a / b),
        "_tmod": lambda a, b: a - b * int(a / b),
        "_copy": lambda v: copy.deepcopy(v) if isinstance(v, (list, dict)) else v,
        "_NODEF": object(),
    }
    exec(compile(tree, "<translated>", "exec"), env)  # tests only: never on the live path
    return {k: v for k, v in env.items() if not k.startswith("_") and not callable(v)}, out


def test_dialect_truth_matches_cpython_on_translated_programs():
    checked, mismatches, per_rule = 0, [], dict.fromkeys(DIALECT_RULES, 0)
    for i in range(360):
        p = dialect_grid(i)
        try:
            prog = generate(p, 21_000 + i, query_kind="stdout" if i % 10 == 0 else "final_value")
        except GenerationFailed:
            continue
        r = render(prog, i)
        env, out = _cpython_dialect(r.source, p.dialect)
        tr = run(prog)
        assert tr.error is None
        if {r.name_map[k]: v for k, v in tr.globals.items()} != env or out != tr.stdout:
            mismatches.append(i)
        plain_env, plain_out = _cpython(r.source)  # untranslated CPython == the python_prior reading
        prior = run(prog, TRUE)
        assert {r.name_map[k]: v for k, v in prior.globals.items()} == plain_env and plain_out == prior.stdout
        checked += 1
        for rule in p.dialect:
            per_rule[rule] += 1
    assert mismatches == []
    assert checked >= 300 and min(per_rule.values()) >= 100


# --- the instrument: evaluator, escalation, frontier ---------------------------------------


def test_config_knob_maps_to_the_first_k_rules():
    base = _default_code_base()
    for k in range(5):
        assert _apply_code_config(base, {"dialect_rules": float(k)}).dialect == DIALECT_RULES[:k]
    assert _apply_code_config(base, {"dialect_rules": 9.0}).dialect == DIALECT_RULES  # clamped
    assert _apply_code_config(base, {"dialect_rules": -1.0}).dialect == ()
    assert _code_latent(_apply_code_config(base, {"dialect_rules": 3.0}))["dialect_rules"] == 3.0
    ray = default_code_dialect_ray()
    assert {d.name for d in ray.dims} == {"n_statements", "dialect_rules"}
    assert ray.config_at(0.0) == {"n_statements": 6.0, "dialect_rules": 0.0}
    assert ray.config_at(1.0) == {"n_statements": 24.0, "dialect_rules": 4.0}
    _, prog, rendered, _ = build_code_task({"n_statements": 10, "dialect_rules": 2}, 7)
    assert prog.params.dialect == DIALECT_RULES[:2] and "(2) " in rendered.prompt


def test_header_ignoring_fake_fails_in_the_evaluator_iff_rules_are_declared(seen, store):  # noqa: F811
    ev = _evaluator(python_reader, seen, store)
    results = [(i % 5, ev({"n_statements": 6 + i % 18, "dialect_rules": i % 5}, i)) for i in range(30)]
    assert all(ok == (k == 0) for k, ok in results)
    fails = [r for r in store.by_run("zoo") if r.verdict == Verdict.FAIL]
    assert len(fails) == 24 and all("detail=foil:python_prior" in r.verifier_detail for r in fails)
    assert all(f";dialect_rules={int(r.latent_params['dialect_rules'])}" in r.verifier_detail for r in fails)
    follow = _evaluator(dialect_reader, seen, store, run_id="zoo-follow")
    assert all(follow({"n_statements": 6 + i % 18, "dialect_rules": i % 5}, i) for i in range(30))


def test_escalation_attributes_the_break_to_dialect_rules(seen, store):  # noqa: F811
    ev = _evaluator(python_reader, seen, store)
    bp = find_breaking_point(ev, default_code_dialect_ray(), budget=300, k_trials=7)
    assert bp.found
    assert bp.config_at_break["dialect_rules"] >= 1
    assert bp.hypothesis is not None
    assert bp.hypothesis.necessary_factors == ("dialect_rules",)
    fails = [r for r in store.by_run("zoo") if r.verdict == Verdict.FAIL]
    assert fails and all("detail=foil:python_prior" in r.verifier_detail for r in fails)
    assert not find_breaking_point(_evaluator(dialect_reader, seen, store, run_id="zoo-ok"),
                                   default_code_dialect_ray(), budget=120, k_trials=5).found


def test_frontier_names_the_python_prior():
    from types import SimpleNamespace

    from cartographer.report.frontier import CAUSE_WORDS, SPECS, _cause_code

    spec = next(s for s in SPECS if s.key == "code_traps")
    rec = SimpleNamespace(verifier_detail="kind=code_trace;campaign=1;query=final_value;"
                                          "detail=foil:python_prior,early_binding;n_stmts=9")
    assert _cause_code(spec, rec) == "foil_python_prior"
    assert CAUSE_WORDS["foil_python_prior"] == "follows standard Python instead of the dialect the program declares"
    assert set(TRAP_KEYS) >= set(DIALECT_TRAPS.values())
