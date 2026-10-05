"""WP-H (docs/spec-code-hunt.md): meaning-preserving rewrites, ddmin shrink and
the hunt loop, all offline with planted fake readers (the zoo pattern from
tests/test_codeworld_zoo.py: the fake client maps prompt -> (program, rendered))."""

from __future__ import annotations

import json

import pytest

from cartographer.codeworld import FOIL_ERROR, FOILS, CodeDimParams, answer, generate, render
from cartographer.codeworld.model import (
    Assign,
    AugAssign,
    ExprStmt,
    MethodCall,
    Name,
    SetItem,
)
from cartographer.codeworld.mutate import ANSWER_PRESERVING, REWRITE_NAMES, apply_rewrite
from cartographer.exec.client import ChatResult
from cartographer.hunt import hunt as hunt_mod
from cartographer.hunt.hunt import HuntResult, run_hunt
from cartographer.hunt.rewrites import REWRITES, RewriteUnavailable, apply_rewrite_program, negate_if
from cartographer.hunt.shrink import admissible, shrink
from cartographer.report.frontier import frontier_summary
from cartographer.store.schema import Verdict
from cartographer.store.store import ResultsStore

PRICED = "gemma-4-31b-it-qat-w4a16-ct"


def _programs(n: int, seed0: int = 40_000):
    for i in range(n):
        yield generate(CodeDimParams(
            n_statements=8 + i % 25, max_nesting=i % 4, n_variables=2 + i % 6,
            distractor_fraction=(i % 4) * 0.15, identifier_confusability=(i % 3) / 2,
            aliasing_on=i % 5 == 0, late_binding_on=i % 7 == 0, negative_modulo_on=i % 11 == 0,
        ), seed0 + i)


# --- fakes ---------------------------------------------------------------------------


def _wrong(prog):
    t = answer(prog)
    for name in ("value_semantics", "all_branches", "loop_once", "inclusive_range"):
        a = answer(prog, FOILS[name])
        if a is not FOIL_ERROR and a != t:
            return a
    return t + 1 if isinstance(t, int) and not isinstance(t, bool) else [t, "x"]


def perfect(prog, rendered, n):
    return answer(prog)


def value_semantics_reader(prog, rendered, n):
    return answer(prog, FOILS["value_semantics"])


def flaky(prog, rendered, n):
    return _wrong(prog) if n == 1 else answer(prog)


def negation_blind(prog, rendered, n):
    return _wrong(prog) if "if not " in rendered.source else answer(prog)


class ZooClient:
    def __init__(self, reader, seen: dict):
        self.reader, self.seen = reader, seen
        self.calls = 0
        self.per_prompt: dict = {}

    def chat(self, model, prompt, max_tokens=0, temperature=0.0, retry_on_length=True, max_tokens_retry=0):
        self.calls += 1
        self.per_prompt[prompt] = self.per_prompt.get(prompt, 0) + 1
        prog, rendered = self.seen[prompt]
        text = f"Tracing...\nANSWER: {self.reader(prog, rendered, self.per_prompt[prompt])!r}"
        return ChatResult(text=text, tokens_in=500, tokens_out=800, latency_ms=1.0, finish_reason="stop")


@pytest.fixture
def seen(monkeypatch):
    seen: dict = {}
    real = hunt_mod.code_render

    def recording(program, seed, *a, **kw):
        r = real(program, seed, *a, **kw)
        seen[r.prompt] = (program, r)
        return r

    monkeypatch.setattr(hunt_mod, "code_render", recording)
    return seen


@pytest.fixture
def store(tmp_path):
    s = ResultsStore(tmp_path / "hunt.sqlite")
    yield s
    s.close()


ALIAS_CFG = {"n_statements": 10, "aliasing_on": 1}
PLAIN_CFG = {"n_statements": 14, "max_nesting": 2}


def _hunt(reader, seen, store, tmp_path, run_id="camp-code-hunt-zoo-001", model="zoo-model", **kw):
    client = ZooClient(reader, seen)
    kw.setdefault("configs", [ALIAS_CFG])
    kw.setdefault("n_programs", 4)
    kw.setdefault("parallelism", 1)
    res = run_hunt(client, store, run_id, model, out_dir=str(tmp_path / "hunt"), **kw)
    return res, client


def _stage_records(store, run_id):
    return store.by_run(run_id) + store.by_run(run_id + "/verify") + store.by_run(run_id + "/shrink")


# --- rewrites --------------------------------------------------------------------------


def test_every_rewrite_preserves_the_answer_and_changes_the_source_on_300_programs():
    applied = {name: 0 for name in REWRITES}
    for i, prog in enumerate(_programs(300)):
        truth = answer(prog)
        base_src = render(prog, prog.seed).source
        for name in REWRITES:
            try:
                new, rs = apply_rewrite_program(prog, name, i)
            except RewriteUnavailable:
                continue
            applied[name] += 1
            out = answer(new)
            assert out == truth and type(out) is type(truth), name
            assert render(new, rs).source != base_src, name
    assert all(v >= 20 for v in applied.values()), applied


def test_rewrites_are_registered_as_answer_preserving_in_mutate():
    assert set(REWRITES) == set(REWRITE_NAMES) and set(REWRITE_NAMES) <= ANSWER_PRESERVING
    prog = generate(CodeDimParams(n_statements=14, max_nesting=2), 5)
    new, rendered = apply_rewrite(prog, "rename_variable", 1)
    assert answer(new) == answer(prog) and rendered.source != render(prog, prog.seed).source


def test_negate_if_unavailable_without_an_else():
    for prog in _programs(200):
        from cartographer.codeworld.model import If, iter_stmts

        ifs = [s for s in iter_stmts(prog.body) if isinstance(s, If)]
        if ifs and not any(s.orelse for s in ifs):
            with pytest.raises(RewriteUnavailable):
                negate_if(prog, 0)
            return
    pytest.fail("no program with an if but no else")


def test_negate_if_swaps_arms_and_negates():
    for prog in _programs(200):
        try:
            new = negate_if(prog, 3)
        except RewriteUnavailable:
            continue
        src = render(new, new.seed).source
        assert "if not " in src and "if not " not in render(prog, prog.seed).source
        return
    pytest.fail("negate_if never applied")


def test_rewrites_tolerate_unknown_node_types():
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Mystery:  # e.g. a future While node, carried through untouched
        sid: int
        body: tuple

    prog = generate(CodeDimParams(n_statements=10), 3)
    from cartographer.hunt.rewrites import _preorder, _transform

    body = prog.body + (Mystery(999, ()),)
    assert _transform(body, lambda x: None) is body
    assert any(isinstance(x, Mystery) for x in _preorder(body))


# --- shrink ------------------------------------------------------------------------------


def _vs_fails(p):
    return answer(p, FOILS["value_semantics"]) != answer(p)


def _alias_pairs(prog):
    """(alias assignment sid, mutation sid) pairs in a program's top level."""
    out = []
    for s in prog.body:
        if isinstance(s, Assign) and isinstance(s.value, Name):
            for m in prog.body:
                mut = (isinstance(m, ExprStmt) and isinstance(m.value, MethodCall) and m.value.obj == s.target) \
                    or (isinstance(m, (SetItem, AugAssign)) and m.target == s.target)
                if mut and m.sid > s.sid:
                    out.append((s.sid, m.sid))
    return out


def test_shrink_value_semantics_reader_to_alias_and_mutation():
    n_done = 0
    for i in range(12):
        prog = generate(CodeDimParams(n_statements=16, aliasing_on=True, max_nesting=1), 900 + i)
        assert _vs_fails(prog)
        sr = shrink(prog, _vs_fails, max_checks=200)
        assert len(sr.program.body) <= 6, render(sr.program).source
        assert _vs_fails(sr.program) and admissible(sr.program)
        pairs = _alias_pairs(sr.program)
        assert pairs, render(sr.program).source
        assert any(a in sr.necessary_sids and m in sr.necessary_sids for a, m in pairs)
        rendered = render(sr.program, prog.seed)
        assert rendered.source.strip()
        n_done += 1
    assert n_done == 12


def test_shrink_respects_max_checks_and_is_deterministic():
    prog = generate(CodeDimParams(n_statements=24, aliasing_on=True), 77)
    calls = []

    def sf(p):
        calls.append(1)
        return _vs_fails(p)

    sr = shrink(prog, sf, max_checks=3)
    assert len(calls) == sr.checks <= 3
    a = shrink(prog, _vs_fails, max_checks=40)
    b = shrink(prog, _vs_fails, max_checks=40)
    assert a.program.body == b.program.body and a.necessary_sids == b.necessary_sids


def test_shrunk_programs_render_and_verify():
    from cartographer.codeworld import verify_answer

    prog = generate(CodeDimParams(n_statements=16, aliasing_on=True), 31)
    sr = shrink(prog, _vs_fails)
    r = render(sr.program, prog.seed)
    ok = verify_answer(f"ANSWER: {answer(sr.program)!r}", sr.program, r)
    bad = verify_answer(f"ANSWER: {answer(sr.program, FOILS['value_semantics'])!r}", sr.program, r)
    assert ok.verdict == Verdict.PASS and bad.verdict == Verdict.FAIL and "value_semantics" in bad.detail


# --- hunt ------------------------------------------------------------------------------------


def test_perfect_reader_yields_no_findings_and_no_verify_or_shrink_records(seen, store, tmp_path):
    res, _ = _hunt(perfect, seen, store, tmp_path, configs=[ALIAS_CFG, PLAIN_CFG], n_programs=6)
    assert res.findings == [] and res.n_base == 6 and res.n_base_fail == 0 and res.n_rewrite_fail == 0
    assert res.n_rewrite > 0
    assert store.by_run(res.run_id + "/verify") == [] and store.by_run(res.run_id + "/shrink") == []


def test_value_semantics_reader_is_found_gated_shrunk_and_confirmed(seen, store, tmp_path):
    res, _ = _hunt(value_semantics_reader, seen, store, tmp_path, n_programs=3, budget_trials=1000)
    assert res.n_base == 3 and res.n_base_fail == 3 and res.n_stable == 3
    assert len(res.findings) == 3
    for f in res.findings:
        assert f.kind == "base" and f.detail.startswith("foil:") and "value_semantics" in f.detail
        assert f.n_statements_after <= 6 < f.n_statements_before
        assert f.confirm_pass_rate == 0.0 and f.confirmed
        assert len(f.necessary_lines) >= 2
        assert all(line.strip() in f.shrunk_source for line in f.necessary_lines)


def test_flaky_reader_yields_no_stable_findings(seen, store, tmp_path):
    res, _ = _hunt(flaky, seen, store, tmp_path, n_programs=3)
    assert res.n_base_fail == 3 and res.n_stable == 0 and res.n_noise == 3 and res.findings == []
    assert len(store.by_run(res.run_id + "/verify")) == 9
    assert store.by_run(res.run_id + "/shrink") == []


def test_rewrite_sensitive_reader_found_only_through_negate_if(seen, store, tmp_path):
    res, _ = _hunt(negation_blind, seen, store, tmp_path, configs=[PLAIN_CFG], n_programs=12,
                   budget_trials=2000)
    assert res.n_base_fail == 0 and res.n_base == 12
    assert res.findings, "negate_if never applied on 12 programs"
    assert all(f.kind == "rewrite:negate_if" for f in res.findings)
    assert all("if not " in f.source and "if not " in f.shrunk_source for f in res.findings)
    base = [r for r in store.by_run(res.run_id) if "stage=base" in r.verifier_detail]
    assert all(r.verdict == Verdict.PASS for r in base)


def test_trial_budget_stops_the_hunt_without_phantom_failures(seen, store, tmp_path):
    res, client = _hunt(value_semantics_reader, seen, store, tmp_path, n_programs=4, budget_trials=6)
    assert res.trials_used == 6 == client.calls == len(_stage_records(store, res.run_id))
    assert res.n_base == 4 and res.n_base_fail == 4
    # 2 trials left: the first gate cannot complete -> nothing is stable, nothing is a finding
    assert res.n_stable == 0 and res.findings == []
    assert "budget" in res.note


def test_cost_cap_stops_the_hunt_without_phantom_failures(seen, store, tmp_path):
    # 500 in / 800 out at $0.10 / $0.30 per M = $0.00029 per call
    res, client = _hunt(perfect, seen, store, tmp_path, model=PRICED, n_programs=10, cost_cap=0.001)
    assert client.calls == 4 and res.trials_used == 4 and res.n_base == 4
    assert res.n_base_fail == 0 and res.n_rewrite == 0 and res.findings == []
    assert res.spent_usd >= 0.001 and "cost cap" in res.note
    assert all(r.verdict == Verdict.PASS for r in store.by_run(res.run_id))


def test_json_written_and_round_trips(seen, store, tmp_path):
    res, _ = _hunt(value_semantics_reader, seen, store, tmp_path, n_programs=2)
    path = tmp_path / "hunt" / f"{res.run_id}.json"
    assert path.exists() and res.json_path == str(path)
    data = json.loads(path.read_text())
    back = HuntResult.from_json_dict(data)
    assert back.to_json_dict() == data
    assert back.findings[0].shrunk_source == res.findings[0].shrunk_source
    assert data["findings"][0]["kind"] == "base"


def test_records_carry_hunt_and_stage_tags(seen, store, tmp_path):
    res, _ = _hunt(value_semantics_reader, seen, store, tmp_path, configs=[ALIAS_CFG, PLAIN_CFG], n_programs=4)
    recs = _stage_records(store, res.run_id)
    assert recs and all(";hunt=1;stage=" in r.verifier_detail for r in recs)
    assert all(r.verifier_detail.startswith("kind=code_trace;campaign=1;query=") for r in recs)
    stages = {r.verifier_detail.split("stage=")[1].split(";")[0] for r in recs}
    assert {"base", "verify", "shrink"} <= stages
    assert any(s.startswith("rewrite:") for s in stages)  # the plain programs pass and get rewritten
    assert all("stage=verify" in r.verifier_detail for r in store.by_run(res.run_id + "/verify"))
    assert all("stage=shrink" in r.verifier_detail for r in store.by_run(res.run_id + "/shrink"))


def test_frontier_code_hunt_row_appears_for_a_hunt_run(seen, store, tmp_path):
    res, _ = _hunt(value_semantics_reader, seen, store, tmp_path, configs=[ALIAS_CFG, PLAIN_CFG], n_programs=8)
    row = next(r for r in frontier_summary(store, "zoo-model").rows if r.key == "code_hunt")
    primary = [r for r in store.by_run(res.run_id) if r.verdict != Verdict.ERROR]
    assert row.status != "NOT TESTED" and row.n_scored == len(primary)
    assert row.n_scored - row.n_pass == res.n_base_fail + res.n_rewrite_fail
    assert row.province == "IV · Code tracing"
