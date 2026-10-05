"""Province IV round 2 (docs/spec-code-hunt.md, WP-G): dependency-chain depth,
stacked list semantics on one list, data-dependent branches in loops and
computed-exit `while` loops, adversarial program selection, and the rays that
turn them up at fixed size. All offline, no LLM anywhere."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from cartographer.codeworld import (
    FOILS,
    CodeDimParams,
    answer,
    foil_divergence,
    foils_for,
    generate,
    measure,
    render,
    run,
)
from cartographer.codeworld.foils import ROUND2_FOILS, STACK_FOILS, dial_foils, foil_answers
from cartographer.codeworld.generate import GenerationFailed
from cartographer.codeworld.interpret import FOIL_ERROR
from cartographer.codeworld.measure import critical_path_depth
from cartographer.codeworld.model import (
    ROUND2_DIALS,
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
    Return,
    Slice,
    While,
    Assign,
    iter_stmts,
)
from cartographer.exec.evaluator import (
    _apply_code_config,
    _code_latent,
    build_code_task,
    default_code_depth_ray,
    default_code_gotcha_ray,
    default_code_stack_ray,
)
from cartographer.schedule.escalate import find_breaking_point
from cartographer.store.schema import Verdict
from tests.test_codeworld import _cpython, grid
from tests.test_codeworld_dialect import dialect_grid
from tests.test_codeworld_zoo import ZooClient, seen, store  # noqa: F401  (pytest fixtures)
from cartographer.exec.evaluator import CodeTraceEvaluator

# sha256 over 240 (params, seed) pairs -- plain, opaque-literal and dialect
# programs -- computed BEFORE any round-2 code existed (prompt + body + trap
# sites + distractor ids + variables).
_BASELINE_DIGEST = "2acc29d9748b4f8a7f20fe3fadc625113f2c99e1f9f2fa53d34ce472c4f0a834"


def _digest() -> str:
    h = hashlib.sha256()
    for i in range(240):
        p = dialect_grid(i) if i % 4 == 3 else grid(i)
        if i % 5 == 0:
            p = replace(p, opaque_literal_on=True)
        try:
            prog = generate(p, 52_000 + i, query_kind="stdout" if i % 9 == 0 else "final_value")
        except GenerationFailed:
            h.update(b"FAIL")
            continue
        r = render(prog, i)
        h.update(r.prompt.encode())
        h.update(repr(prog.body).encode())
        h.update(repr(prog.trap_sites).encode())
        h.update(repr(sorted(prog.distractor_ids)).encode())
        h.update(repr(prog.variables).encode())
    return h.hexdigest()


def r2_grid(i: int, comments: bool = True) -> CodeDimParams:
    """A deterministic walk over the round-2 dial grid (size, nesting,
    distractors and look-alikes varied too)."""
    h = lambda k, n: (i * 2654435761 + k * 97) % n  # noqa: E731
    return CodeDimParams(
        n_statements=[8, 15, 22, 30][h(1, 4)], max_nesting=h(2, 3), n_variables=[2, 3, 5][h(3, 3)],
        distractor_fraction=[0, 0.2, 0.4][h(4, 3)], identifier_confusability=[0, 0.3, 0.6][h(5, 3)],
        chain_depth=[0, 0, 3, 8, 15][h(6, 5)], stack_depth=h(7, 5), loop_branch=bool(h(8, 2)),
        while_on=bool(h(9, 2)), negative_modulo_on=h(10, 7) == 0, sign_chain=h(11, 4), index_math=h(12, 4),
        misleading_comments=h(13, 4) if comments else 0,
    )


def _sized(i: int, **dials) -> CodeDimParams:
    return CodeDimParams(n_statements=[12, 18, 22][i % 3], max_nesting=i % 3, n_variables=[2, 3, 4][i % 3],
                         distractor_fraction=[0, 0.2][i % 2], **dials)


def _prog(body, target="v0") -> Program:
    return Program(body=tuple(body), query=Query("final_value", target), params=CodeDimParams(), seed=0)


# --- byte identity with every dial off ---------------------------------------------------


def test_default_off_programs_are_byte_identical_to_before_round2():
    assert _digest() == _BASELINE_DIGEST


def test_programs_with_dials_off_keep_exactly_the_old_foils_and_measures():
    prog = generate(grid(3), 11)
    assert not prog.params.round2_on()
    assert foils_for(prog) is FOILS
    m = measure(prog)
    assert not ({"critical_path_depth", "chain_depth", "stack_depth"} & set(m))
    assert prog.path_sids == ()


def test_params_validate_round2_dials():
    for bad in ({"chain_depth": -1}, {"chain_depth": 25}, {"stack_depth": 5}):
        with pytest.raises(ValueError):
            CodeDimParams(**bad)
    with pytest.raises(ValueError):
        CodeDimParams(stack_depth=1, dialect=("copy_on_assign",))
    assert CodeDimParams(while_on=True).round2_on() and not CodeDimParams().round2_on()


# --- semantics: each new belief on a hand-built program ---------------------------------


def test_each_new_semantics_field_flips_exactly_its_belief():
    a, b = "v0", "v1"
    aug = _prog([Assign(1, a, ListLit((Const(1),))), Assign(2, b, Name(a)),
                 AugAssign(3, b, "+", ListLit((Const(2),)))])
    assert answer(aug) == [1, 2] and answer(aug, ROUND2_FOILS["augassign_rebinds"]) == [1]
    cat = _prog([Assign(1, a, ListLit((Const(1),))), Assign(2, b, Name(a)),
                 Assign(3, b, BinOp("+", Name(b), ListLit((Const(2),))))])
    assert answer(cat) == [1] and answer(cat, ROUND2_FOILS["concat_mutates"]) == [1, 2]
    sl = _prog([Assign(1, a, ListLit((Const(1),))), Assign(2, b, Slice(Name(a), None, None)),
                ExprStmt(3, MethodCall(b, "append", (Const(5),)))])
    assert answer(sl) == [1] and answer(sl, ROUND2_FOILS["slice_aliases"]) == [1, 5]
    par = _prog([FuncDef(1, "f0", ("p0",), (), (ExprStmt(2, MethodCall("p0", "append", (Const(7),))),
                                                  Return(3, Len(Name("p0"))))),
                 Assign(4, a, ListLit(())), ExprStmt(5, Call("f0", (Name(a),)))])
    assert answer(par) == [7] and answer(par, ROUND2_FOILS["param_copies"]) == []
    br = _prog([Assign(1, a, Const(0)), For(2, "x0", ListLit((Const(1), Const(5), Const(6))),
                                            (If(3, Compare(">", Name("x0"), Const(4)),
                                                (AugAssign(4, a, "+", Name("x0")),),
                                                (AugAssign(5, a, "-", Const(1)),)),))])
    assert answer(br) == 10 and answer(br, ROUND2_FOILS["predicate_frozen"]) == -3
    wh = _prog([Assign(1, a, Const(0)), Assign(2, b, Const(0)),
                While(3, Compare("<", Name(a), Const(10)), (AugAssign(4, a, "+", Const(3)),
                                                            AugAssign(5, b, "+", Const(1))))], target=b)
    assert answer(wh) == 4
    assert answer(wh, FOILS["loop_once"]) == 1
    assert answer(wh, ROUND2_FOILS["predicate_frozen"]) is FOIL_ERROR  # never exits: step cap
    chain = replace(_prog([Assign(1, a, Const(2)), AugAssign(2, a, "+", Const(3)),
                           AugAssign(3, a, "+", Const(4))]), path_sids=(2, 3))
    assert answer(chain) == 9 and answer(chain, ROUND2_FOILS["stale_read"]) == 5
    assert answer(_prog(chain.body), ROUND2_FOILS["stale_read"]) == 9  # no declared path: no effect


# --- acceptance: the interpreter IS Python on the new grid (incl. while) ---------------


def test_interpreter_matches_cpython_on_3000_round2_programs():
    n, mismatches, failures, n_while = 3000, [], 0, 0
    for i in range(n):
        try:
            prog = generate(r2_grid(i, comments=i % 10 != 0), 3_000 + i,
                            query_kind="stdout" if i % 10 == 0 else "final_value")
        except GenerationFailed:
            failures += 1
            continue
        n_while += any(isinstance(s, While) for s in iter_stmts(prog.body))
        r = render(prog, i)
        env, out = _cpython(r.source)
        tr = run(prog)
        if {r.name_map[k]: v for k, v in tr.globals.items()} != env or out != tr.stdout:
            mismatches.append(i)
    assert mismatches == []
    assert failures / n < 0.03
    assert n_while > 1000


def test_generation_failure_rate_on_the_round2_grid_is_below_3_percent():
    fails = 0
    for i in range(400):
        try:
            generate(r2_grid(i * 7 + 3), 70_000 + i)
        except GenerationFailed:
            fails += 1
    assert fails / 400 < 0.03


# --- acceptance: every enabled mechanism is load-bearing; disabled ones are absent ----


def _check_dials(prog):
    truth = answer(prog)
    foils = foil_answers(prog)
    required, permitted = dial_foils(prog.params)
    for name in required:
        assert foils[name] != truth, (prog.seed, name)
    for name in ROUND2_FOILS:
        if name not in permitted:
            assert foils[name] == truth, (prog.seed, name)


@pytest.mark.parametrize("dial,values", [
    ("stack_depth", (1, 2, 3, 4)), ("chain_depth", (3, 5, 10, 20)),
    ("loop_branch", (True,)), ("while_on", (True,)), ("sign_chain", (1, 2, 3)), ("index_math", (1, 2, 3)),
])
def test_each_dial_foil_diverges_when_on_and_no_off_foil_does(dial, values):
    for i in range(160):
        v = values[i % len(values)]
        prog = generate(_sized(i, **{dial: v}), 40_000 + i)
        _check_dials(prog)
        truth = answer(prog)
        if dial == "stack_depth":  # exactly the first k stacked foils diverge
            for j, name in enumerate(STACK_FOILS):
                assert (foil_answers(prog)[name] != truth) == (j < v), (i, name)


def test_while_loops_terminate_within_40_iterations_and_render_as_python():
    for i in range(60):
        prog = generate(_sized(i, while_on=True), 600 + i)
        loops = [s for s in iter_stmts(prog.body) if isinstance(s, While)]
        assert len(loops) == 1
        counter = loops[0].body[1].target
        assert 2 <= run(prog).globals[counter] <= 40
        assert "while " in render(prog, i).source


# --- chain depth: realized critical path tracks the dial at constant size --------------


def test_critical_path_depth_tracks_chain_depth_monotonically_at_constant_size():
    means, sizes = [], []
    for d in (0, 5, 10, 15, 20):
        progs = [generate(CodeDimParams(n_statements=22, n_variables=3, chain_depth=d), 100 + i) for i in range(30)]
        means.append(sum(critical_path_depth(p) for p in progs) / 30)
        sizes.append(sum(measure(p)["n_statements"] for p in progs) / 30)
        if d:
            assert all(len(p.path_sids) == d for p in progs)
            assert all(measure(p)["critical_path_depth"] >= d for p in progs)
    assert means == sorted(means) and means[-1] - means[0] >= 15
    assert max(sizes) <= 30 and min(sizes) >= 20  # size held roughly constant


# --- zoo: one planted fake reader per new foil, through the real evaluator --------------

# (foil, configs where its mechanism is ON, configs where it is OFF but other round-2 dials are on)
_ZOO = {
    "augassign_rebinds": ([{"stack_depth": k} for k in (1, 2, 3, 4)],
                          [{"chain_depth": 4}, {"loop_branch": 1}, {"while_on": 1}]),
    "concat_mutates": ([{"stack_depth": k} for k in (2, 3, 4)], [{"stack_depth": 1}, {"chain_depth": 4}]),
    "slice_aliases": ([{"stack_depth": k} for k in (3, 4)], [{"stack_depth": 1}, {"stack_depth": 2}]),
    "param_copies": ([{"stack_depth": 4}], [{"stack_depth": k} for k in (1, 2, 3)]),
    "predicate_frozen": ([{"loop_branch": 1}, {"while_on": 1}, {"loop_branch": 1, "while_on": 1}],
                         [{"stack_depth": 2}, {"chain_depth": 6}]),
    "stale_read": ([{"chain_depth": d} for d in (3, 8, 15)], [{"stack_depth": 3}, {"while_on": 1}]),
    "slice_end_inclusive": ([{"index_math": k} for k in (1, 2, 3)],
                            [{"sign_chain": 2}, {"stack_depth": 3}, {"misleading_comments": 2, "sign_chain": 1}]),
}


@pytest.mark.parametrize("foil", sorted(_ZOO))
def test_each_new_planted_misreader_fails_exactly_on_its_mechanism(foil, seen, store):  # noqa: F811
    sem = ROUND2_FOILS[foil]
    ev = CodeTraceEvaluator(ZooClient(lambda p, r: answer(p, sem), seen), "zoo-model", store, "zoo",
                            cost_cap_usd=1e9)
    on_cfgs, off_cfgs = _ZOO[foil]
    on = [ev({"n_statements": 10 + i % 14, "max_nesting": i % 2, **on_cfgs[i % len(on_cfgs)]}, i) for i in range(24)]
    off = [ev({"n_statements": 10 + i % 14, "max_nesting": i % 2, **off_cfgs[i % len(off_cfgs)]}, i)
           for i in range(24)]
    assert not any(on), f"{foil} passed with its mechanism on"
    assert all(off), f"{foil} failed with its mechanism off"
    fails = [r for r in store.by_run("zoo") if r.verdict == Verdict.FAIL]
    assert len(fails) == 24
    for r in fails:
        code = r.verifier_detail.split(";detail=", 1)[1].split(";", 1)[0]
        if foil == "predicate_frozen" and code == "malformed_answer":
            continue  # a frozen `while` test never exits: that reading is a crash, not a value
        assert code.startswith("foil:") and foil in code.split(":", 1)[1].split(","), code


def test_stack_ray_breaks_the_augassign_reader_and_attributes_stack_depth(seen, store):  # noqa: F811
    sem = ROUND2_FOILS["augassign_rebinds"]
    ev = CodeTraceEvaluator(ZooClient(lambda p, r: answer(p, sem), seen), "zoo-model", store, "zoo",
                            cost_cap_usd=1e9)
    bp = find_breaking_point(ev, default_code_stack_ray(), budget=300, k_trials=7)
    assert bp.found
    assert bp.hypothesis is not None
    assert bp.hypothesis.necessary_factors == ("stack_depth",)
    assert bp.config_at_break["stack_depth"] >= 1


def test_perfect_reader_never_breaks_on_the_round2_rays(seen, store):  # noqa: F811
    for ray in (default_code_depth_ray(), default_code_stack_ray()):
        ev = CodeTraceEvaluator(ZooClient(lambda p, r: answer(p), seen), "zoo-model", store, "zoo",
                                cost_cap_usd=1e9)
        bp = find_breaking_point(ev, ray, budget=60, k_trials=3)
        assert not bp.found


# --- adversarial selection ---------------------------------------------------------------


def test_adversarial_selection_raises_mean_divergence_and_is_deterministic():
    rand, adv = [], []
    for i in range(30):
        cfg = {"n_statements": 10 + i % 12, "stack_depth": i % 5, "loop_branch": i % 2, "chain_depth": i % 3 * 4,
               "aliasing_on": int(i % 4 == 0)}
        _, p0, _, s0 = build_code_task(cfg, i)
        _, p1, r1, s1 = build_code_task(cfg, i, adversarial=True, adversarial_candidates=12)
        _, p2, r2, _ = build_code_task(cfg, i, adversarial=True, adversarial_candidates=12)
        assert p1 == p2 and r1.prompt == r2.prompt and s0 == s1
        rand.append(foil_divergence(p0).divergence_score)
        adv.append(foil_divergence(p1).divergence_score)
    assert all(a >= 0 for a in adv)
    assert sum(adv) / 30 > sum(rand) / 30


def test_adversarial_flag_is_recorded_in_the_verifier_detail(seen, store):  # noqa: F811
    ev = CodeTraceEvaluator(ZooClient(lambda p, r: answer(p), seen), "zoo-model", store, "zoo",
                            cost_cap_usd=1e9, adversarial=True, adversarial_candidates=5)
    assert ev({"n_statements": 12, "stack_depth": 2}, 0)
    (r,) = store.by_run("zoo")
    assert ";adversarial=1" in r.verifier_detail and ";stack_depth=2" in r.verifier_detail
    plain = CodeTraceEvaluator(ZooClient(lambda p, r: answer(p), seen), "zoo-model", store, "zoo2",
                               cost_cap_usd=1e9)
    assert plain({"n_statements": 12}, 0)
    (r2,) = store.by_run("zoo2")
    assert "adversarial" not in r2.verifier_detail and "stack_depth" not in r2.verifier_detail


# --- config keys, latent, rays, registry, cause words ----------------------------------------


def test_config_keys_latent_and_realized_dims():
    p = _apply_code_config(CodeDimParams(), {"chain_depth": 30, "stack_depth": 2.6, "loop_branch": 0.7,
                                             "while_on": 0.2})
    assert (p.chain_depth, p.stack_depth, p.loop_branch, p.while_on) == (24, 3, True, False)
    lat = _code_latent(p)
    assert all(k in lat for k in ROUND2_DIALS) and lat["stack_depth"] == 3.0
    prog = generate(CodeDimParams(n_statements=22, stack_depth=3, loop_branch=True, while_on=True, chain_depth=4), 5)
    m = measure(prog)
    assert m["stack_depth"] == 3 and m["loop_branch"] == 1 and m["while_on"] == 1 and m["chain_depth"] == 4


def test_round2_rays_pin_size_and_turn_up_their_dials():
    depth, stack, gotcha = default_code_depth_ray(), default_code_stack_ray(), default_code_gotcha_ray()
    assert gotcha.config_at(0.5) == {"n_statements": 22, "sign_chain": 2, "index_math": 2, "misleading_comments": 2}
    assert [depth.config_at(t) for t in (0, 0.5, 1)] == [
        {"n_statements": 22, "chain_depth": 5}, {"n_statements": 22, "chain_depth": 12},
        {"n_statements": 22, "chain_depth": 20}]
    assert stack.config_at(0) == {"n_statements": 22, "stack_depth": 0, "loop_branch": 0, "while_on": 0}
    assert stack.config_at(1) == {"n_statements": 22, "stack_depth": 4, "loop_branch": 1, "while_on": 1}
    for t in (0, 0.5, 1):  # every point on both rays generates
        for ray in (depth, stack, gotcha):
            build_code_task(ray.config_at(t), 1)


def test_registry_has_the_round2_campaigns():
    from cartographer.campaigns.registry import REGISTRY

    for cid, tmpl, ray in (("IV.chain_depth", "camp-code-depth-{label}-{n:03d}", default_code_depth_ray),
                           ("IV.stack", "camp-code-stack-{label}-{n:03d}", default_code_stack_ray),
                           ("IV.gotcha", "camp-code-gotcha-{label}-{n:03d}", default_code_gotcha_ray)):
        spec = REGISTRY[cid]
        assert spec.province == "IV" and spec.run_id_template == tmpl and spec.ray_factory is ray
        assert spec.question.endswith("?") and spec.evidence_kind == "axis"


def test_frontier_cause_words_for_every_new_foil():
    from cartographer.report.frontier import CAUSE_WORDS

    for name in ROUND2_FOILS:
        words = CAUSE_WORDS[f"foil_{name}"]
        assert words and "_" not in words.replace("`", "").split()[0]
    assert CAUSE_WORDS["comment_value"]


# --- misleading comments and their neutral control -----------------------------------------


def test_misleading_comments_assert_a_foil_value_and_the_neutral_control_matches_them():
    from cartographer.codeworld.interpret import Interpreter

    for i in range(40):
        k = 1 + i % 3
        mis = generate(_sized(i, misleading_comments=k, index_math=1), 900 + i)
        neu = generate(_sized(i, neutral_comments=k, index_math=1), 900 + i)
        assert mis.body == neu.body and len(mis.comments) == k
        assert [c[0] for c in mis.comments] == [c[0] for c in neu.comments]
        lines_m, lines_n = render(mis, i).source.splitlines(), render(neu, i).source.splitlines()
        assert [len(x) for x in lines_m] == [len(x) for x in lines_n]  # equal-length control
        assert sum(" is now " in x for x in lines_m) == k and not any(" is now " in x for x in lines_n)
        idx = {s.sid: j for j, s in enumerate(mis.body)}
        for sid, vid, value, foil, neutral in mis.comments:  # the asserted value is wrong at that line
            it = Interpreter()
            for s in mis.body[:idx[sid] + 1]:
                it.exec_top(s)
            assert it.globals[vid] != value and not neutral
            assert foil in foils_for(mis)
        assert answer(mis) == answer(neu)


def test_a_reader_that_trusts_comments_gets_the_comment_value_signature(seen, store):  # noqa: F811
    def trusting(prog, rendered):  # answers the value the last comment on the target asserts, if any
        on_target = [c for c in prog.comments if c[1] == prog.query.target]
        return on_target[-1][2] if on_target else answer(prog)

    ev = CodeTraceEvaluator(ZooClient(trusting, seen), "zoo-model", store, "zoo", cost_cap_usd=1e9)
    results = [ev({"n_statements": 12 + i % 8, "misleading_comments": 3, "sign_chain": 1}, i) for i in range(20)]
    fails = [r for r in store.by_run("zoo") if r.verdict == Verdict.FAIL]
    assert len(fails) == results.count(False) >= 5
    assert all("comment_value" in r.verifier_detail for r in fails)
