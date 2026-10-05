"""Province V (docs/province-v-spec.md §3-§8): function form, bug injectors,
repair / write tasks, verifier signatures, and the planted-writer zoo run
through the REAL `CodeRepairEvaluator` / `CodeWriteEvaluator`."""

from __future__ import annotations

import re

import pytest

from cartographer.codefix import bugs as B
from cartographer.codefix import tasks as T
from cartographer.codefix import verify as V
from cartographer.codefix.tasks import CodeFixParams, execute, make_repair_task, make_write_task, norm, outputs
from cartographer.codefix.verify import render_repair_prompt, render_write_prompt, verify_submission
from cartographer.codeworld import CodeDimParams, answer, generate
from cartographer.codeworld.generate import GenerationFailed, to_function_form
from cartographer.codeworld.model import Assign
from cartographer.exec import evaluator as ev_mod
from cartographer.exec.client import ChatResult
from cartographer.exec.evaluator import (
    CodeRepairEvaluator,
    CodeWriteEvaluator,
    default_repair_kind_ray,
    default_repair_ray,
    default_write_ray,
)
from cartographer.schedule.escalate import find_breaking_point
from cartographer.store.schema import Verdict
from cartographer.store.store import ResultsStore


def _block(src: str, prose: str = "Here is the fix.") -> str:
    return f"{prose}\n\n```python\n{src}```\n"


# --- function form ----------------------------------------------------------------


def _inits(prog):
    out = {}
    for s in prog.body:
        if isinstance(s, Assign):
            out.setdefault(s.target, s.value)
    return out


@pytest.mark.parametrize("n_params", [1, 2, 3])
def test_function_form_returns_the_original_answer(n_params):
    from cartographer.codeworld.interpret import Interpreter

    checked = 0
    for seed in range(40):
        prog = generate(CodeDimParams(n_statements=14, n_variables=4, chain_depth=2, loop_branch=True), seed)
        try:
            fn = to_function_form(prog, seed, n_params)
        except GenerationFailed:
            continue
        sv = B.solve_def(fn)
        assert fn.entry == "solve" and len(sv.params) == len(fn.param_kinds) <= n_params
        assert len(set(fn.param_kinds)) == len(fn.param_kinds)  # distinct kinds
        it = Interpreter()
        args = [it._eval(_inits(prog)[p], it._gframe) for p in sv.params]
        value, err = execute(fn, args)
        assert err is None and norm(value) == norm(answer(prog))
        checked += 1
    assert checked >= 35


# --- tasks and bugs --------------------------------------------------------------------


def _patch(task) -> str:
    """The symptom patcher: `if <params> == <visible>: return <expected>`."""
    sv = B.solve_def(task.buggy)
    cond = " and ".join(f"{task.names[p]} == {a!r}" for p, a in zip(sv.params, task.visible_input))
    lines = task.buggy_source.split("\n")
    i = next(i for i, ln in enumerate(lines) if ln.startswith("def solve("))
    lines.insert(i + 1, f"    if {cond}:\n        return {task.visible_expected!r}")
    return "\n".join(lines)


def test_bugs_are_single_site_and_load_bearing_on_300_tasks():
    for seed in range(300):
        task = make_repair_task(CodeFixParams(hidden_inputs=seed % 9, bug_mix=(seed % 5) / 4), seed)
        assert len(task.bug_sites) == 1 and len(task.bug_kinds) == 1
        assert sum(1 for s in B.solve_def(task.buggy).body) >= 1
        assert norm(task.visible_wrong) != norm(task.visible_expected)
        assert outputs(task.buggy, [task.visible_input]) != outputs(task.reference, [task.visible_input])
        assert task.visible_input not in task.hidden_inputs
        assert len(task.hidden_inputs) == task.params.hidden_inputs


@pytest.mark.parametrize("kind", B.BUG_KINDS)
def test_every_bug_kind_is_available(kind):
    for seed in range(8):
        task = make_repair_task(CodeFixParams(bug_kind=kind, hidden_inputs=3), seed)
        assert task.bug_kinds == (kind,)


def test_stacked_bugs_sit_on_different_statements():
    for seed in range(20):
        task = make_repair_task(CodeFixParams(n_bugs=3, bug_mix=0.5, hidden_inputs=4), seed)
        assert len(set(task.bug_sites)) == len(task.bug_sites) == 3


def test_hidden_inputs_include_edge_cases_when_kinds_allow():
    hits = 0
    for seed in range(40):
        task = make_repair_task(CodeFixParams(hidden_inputs=6, edge_cases=3, n_params=3), seed)
        kinds = task.reference.param_kinds
        cases = set(task.hidden_edge_cases)
        if "list" in kinds and execute(task.reference, _with(task, "list", []))[1] is None:
            assert "empty_list" in cases
            assert any(a[kinds.index("list")] == [] for a in task.hidden_inputs)
            hits += 1
        if "int" in kinds:
            assert {"zero", "negative"} & cases or execute(task.reference, _with(task, "int", 0))[1] is not None
    assert hits >= 10


def _with(task, kind, value):
    args = list(task.visible_input)
    args[task.reference.param_kinds.index(kind)] = value
    return tuple(args)


def test_oracles_reference_reverter_and_symptom_patch():
    patch_fail_b = 0
    n = 60
    for seed in range(n):
        a = make_repair_task(CodeFixParams(hidden_inputs=0), seed)
        b = make_repair_task(CodeFixParams(hidden_inputs=5, edge_cases=1), seed)
        for t in (a, b):
            assert verify_submission(_block(t.reference_source), t).verdict == Verdict.PASS  # reference / reverter
            assert verify_submission(_block(t.buggy_source), t).detail == "no_change"
        assert verify_submission(_block(_patch(a)), a).verdict == Verdict.PASS
        vb = verify_submission(_block(_patch(b)), b)
        if vb.verdict == Verdict.FAIL:
            assert vb.detail == "visible_fixed_hidden_broken"
            patch_fail_b += 1
    assert patch_fail_b >= 0.9 * n


def test_task_generation_is_deterministic():
    p = CodeFixParams(hidden_inputs=4, edge_cases=2, bug_mix=0.7)
    for seed in (1, 7, 42):
        a, b = make_repair_task(p, seed), make_repair_task(p, seed)
        assert render_repair_prompt(a) == render_repair_prompt(b) and a.hidden_inputs == b.hidden_inputs
        w1, w2 = make_write_task(p, seed), make_write_task(p, seed)
        assert render_write_prompt(w1) == render_write_prompt(w2) and w1.hidden_inputs == w2.hidden_inputs


def test_misleading_comment_marks_the_bug_line_and_still_parses():
    task = make_repair_task(CodeFixParams(misleading_comment=True, hidden_inputs=2), 3)
    assert "# this line is correct" in task.buggy_source
    assert verify_submission(_block(task.reference_source), task).verdict == Verdict.PASS


# --- verifier protocol outcomes -----------------------------------------------------


@pytest.fixture
def task():
    return make_repair_task(CodeFixParams(hidden_inputs=4), 5)


def test_protocol_signatures(task):
    assert verify_submission("", task).detail == "empty_answer"
    assert verify_submission("I would change the loop bound.", task).detail == "no_code_block"
    assert verify_submission(_block("def solve(:\n"), task).detail == "parse_error"
    renamed = task.reference_source.replace("def solve(", "def fixed(")
    assert verify_submission(_block(renamed), task).detail == "signature_changed"


@pytest.mark.parametrize("snippet,construct", [
    ("import os\nos.system('echo x')\n", "import"),
    ("def solve(a):\n    return __import__('os')\n", None),
    ("def solve(a):\n    f = open('x')\n    return 1\n", None),
])
def test_outside_subset_is_never_executed(snippet, construct, task, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("executed")

    monkeypatch.setattr(V, "outputs", boom)
    fv = verify_submission(_block(snippet), task)
    assert fv.verdict == Verdict.ERROR and fv.detail.startswith("outside_subset:") and not fv.parsed
    if construct:
        assert fv.detail == f"outside_subset:{construct}"


def test_huge_while_is_bounded_by_the_step_cap(task):
    names = ", ".join(task.names[p] for p in B.solve_def(task.buggy).params)
    src = f"def solve({names}):\n    i = 0\n    while i < 1000000000:\n        i += 1\n    return i\n"
    fv = verify_submission(_block(src), task)
    assert (fv.verdict == Verdict.ERROR and fv.detail.startswith("outside_subset:")) or \
        fv.detail == "runtime_error:step_cap"


# --- the zoo through the real evaluator ------------------------------------------------


def _python_habit(t) -> str:
    return re.sub(r"(?m)^(\s*)(\w+) = \2 \+ (\[.*\])$", r"\1\2 += \3", t.reference_source)


def _wrong_site(t) -> str | None:
    """Leave the bug in place and change a different statement."""
    for kind in ("wrong_op", "off_by_one", "wrong_index"):
        try:
            options = B.inject(t.buggy, kind, 0, 1, exclude=t.bug_sites)
        except B.BugUnavailable:
            continue
        for cand, sid, _ in options:
            o = outputs(cand, [t.visible_input])
            if o != outputs(t.buggy, [t.visible_input]) and o != outputs(t.reference, [t.visible_input]) \
                    and o[0][0] != "error":
                return _render_like(t, cand)
    return None


def _render_like(t, prog) -> str:
    from cartographer.codeworld.render import _block as rblock

    lines = []
    for s in prog.body:
        if lines:
            lines.append("")
        lines += rblock((s,), t.names, 0, {})
    return "\n".join(lines) + "\n"


WRITERS = {
    "perfect": lambda t: _block(t.reference_source),
    "reverter": lambda t: _block(_render_like(t, t.reference)),
    "symptom_patcher": lambda t: _block(_patch(t)),
    "no_op": lambda t: _block(t.buggy_source),
    "python_habit": lambda t: _block(_python_habit(t)),
    "wrong_site_fixer": lambda t: _block(_wrong_site(t) or t.buggy_source),
    "escaper": lambda t: _block("import os\n" + t.reference_source),
    "blocked_prose": lambda t: "The bug is in the loop; change the bound and it will work.",
}


class ZooClient:
    def __init__(self, writer, seen: dict):
        self.writer, self.seen, self.calls = writer, seen, 0

    def chat(self, model, prompt, max_tokens=0, temperature=0.0, retry_on_length=True, max_tokens_retry=0):
        self.calls += 1
        kind, t = self.seen[prompt]
        text = f"ANSWER: {t.visible_wrong!r}" if kind == "trace" else self.writer(t)
        return ChatResult(text=text, tokens_in=400, tokens_out=600, latency_ms=1.0, finish_reason="stop")


@pytest.fixture
def seen(monkeypatch):
    seen: dict = {}
    real = T.make_repair_task

    def recording(params, seed):
        t = real(params, seed)
        seen[render_repair_prompt(t)] = ("repair", t)
        seen[ev_mod._trace_prompt(t)] = ("trace", t)
        return t

    monkeypatch.setattr(T, "make_repair_task", recording)
    real_w = T.make_write_task

    def recording_w(params, seed):
        t = real_w(params, seed)
        seen[render_write_prompt(t)] = ("write", t)
        return t

    monkeypatch.setattr(T, "make_write_task", recording_w)
    return seen


@pytest.fixture
def store(tmp_path):
    s = ResultsStore(tmp_path / "zoo.sqlite")
    yield s
    s.close()


def _repair_ev(name, seen, store, **kw):
    return CodeRepairEvaluator(ZooClient(WRITERS[name], seen), "zoo-model", store, "zoo", cost_cap_usd=1e9, **kw)


def _details(store, run="zoo"):
    return [r for r in store.by_run(run)]


@pytest.mark.parametrize("name,va,vb", [
    ("perfect", "pass", "pass"),
    ("reverter", "pass", "pass"),
    ("symptom_patcher", "pass", "visible_fixed_hidden_broken"),
    ("no_op", "no_change", "no_change"),
    ("escaper", "outside_subset:import", "outside_subset:import"),
    ("blocked_prose", "no_code_block", "no_code_block"),
])
def test_zoo_signatures(name, va, vb, seen, store):
    ev = _repair_ev(name, seen, store)
    for i in range(12):
        ev({"hidden_inputs": 0, "bug_distance": 1 + i % 8}, i)
        ev({"hidden_inputs": 5, "bug_distance": 1 + i % 8}, i)
    recs = _details(store)
    assert len(recs) == 24
    a = {re.search(r"detail=([^;]+)", r.verifier_detail).group(1) for r in recs if "hidden=0" in r.verifier_detail}
    b = {re.search(r"detail=([^;]+)", r.verifier_detail).group(1) for r in recs if "hidden=5" in r.verifier_detail}
    if name == "symptom_patcher":
        assert a == {va} and b <= {vb, "pass"} and sum(
            "visible_fixed_hidden_broken" in r.verifier_detail for r in recs) >= 11
    else:
        assert a == {va} and b == {vb}
    if name == "escaper":
        assert all(r.verdict == Verdict.ERROR for r in recs)
    traces = _details(store, "zoo/trace")
    assert len(traces) == 24 and all(r.verdict == Verdict.PASS for r in traces)
    assert all("traced_ok=1" in r.verifier_detail and r.realized_dims["traced_ok"] == 1.0 for r in recs)


def test_zoo_python_habit_introduces_a_list_misreading(seen, store):
    ev = _repair_ev("python_habit", seen, store, pair_trace=False)
    for i in range(15):
        ev({"hidden_inputs": 0, "bug_mix": 0.0}, i)  # syntactic bugs: the habit is the only list change
    recs = _details(store)
    assert len(recs) == 15 and not _details(store, "zoo/trace")
    for r in recs:
        # every generated program aliases a list and rebinds it with `x = x + [..]`
        assert r.verdict == Verdict.FAIL and "detail=introduced:concat_mutates" in r.verifier_detail


def test_zoo_wrong_site_fixer(seen, store):
    ev = _repair_ev("wrong_site_fixer", seen, store, pair_trace=False)
    for i in range(15):
        ev({"hidden_inputs": 3}, i)
    details = [re.search(r"detail=([^;]+)", r.verifier_detail).group(1) for r in _details(store)]
    assert details.count("wrong_site") >= 13, details


def test_length_cut_answer_is_error_not_fail(seen, store):
    class Cut(ZooClient):
        def chat(self, *a, **k):
            r = super().chat(*a, **k)
            return ChatResult(text=r.text, tokens_in=1, tokens_out=1, latency_ms=1.0, finish_reason="length")

    ev = CodeRepairEvaluator(Cut(WRITERS["perfect"], seen), "zoo-model", store, "zoo", cost_cap_usd=1e9)
    assert ev({"hidden_inputs": 2}, 1) is False
    assert all(r.verdict == Verdict.ERROR for r in store.by_run("zoo"))


def test_breaking_point_on_repair_ray_attributes_symptom_patch_to_hidden_inputs(seen, store):
    ev = _repair_ev("symptom_patcher", seen, store, pair_trace=False)
    bp = find_breaking_point(ev, default_repair_ray(), budget=120, k_trials=5)
    assert bp.found
    assert bp.hypothesis.necessary_factors == ("hidden_inputs",)


def test_perfect_writer_never_breaks_on_the_kind_ray(seen, store):
    ev = _repair_ev("perfect", seen, store, pair_trace=False)
    bp = find_breaking_point(ev, default_repair_kind_ray(), budget=40, k_trials=3)
    assert not bp.found


# --- V-c write ---------------------------------------------------------------------------


def _edge_ignorer(t) -> str:
    sv = B.solve_def(t.reference)
    lst = t.names[sv.params[t.reference.param_kinds.index("list")]]
    src = t.reference_source
    i = src.index("def solve(")
    j = src.index("\n", i) + 1
    return src[:j] + f"    if not {lst}:\n        return 1\n" + src[j:]


def test_write_tasks_perfect_passes_and_edge_ignorer_fails_only_with_edge_cases():
    tested = 0
    for seed in range(30):
        t0 = make_write_task(CodeFixParams(edge_cases=0, n_params=3), seed)
        t1 = make_write_task(CodeFixParams(edge_cases=1, n_params=3), seed)
        for t in (t0, t1):
            assert t.spec_text and "solve" in render_write_prompt(t)
            assert verify_submission(_block(t.reference_source), t).verdict == Verdict.PASS
        if "list" not in t0.reference.param_kinds or "empty_list" not in t1.hidden_edge_cases:
            continue
        ref_on_empty = outputs(t1.reference, [a for a, c in zip(t1.hidden_inputs, t1.hidden_edge_cases)
                                              if c == "empty_list"])
        if ref_on_empty == [norm(1)]:
            continue
        assert verify_submission(_block(_edge_ignorer(t0)), t0).verdict == Verdict.PASS
        fv = verify_submission(_block(_edge_ignorer(t1)), t1)
        assert fv.verdict == Verdict.FAIL and fv.detail == "edge_case:empty_list"
        tested += 1
    assert tested >= 10


def test_write_evaluator_records(seen, store):
    ev = CodeWriteEvaluator(ZooClient(lambda t: _block(t.reference_source), seen), "zoo-model", store, "zw",
                            cost_cap_usd=1e9)
    assert all(ev({"edge_cases": e, "spec_examples": 3 - e % 3}, e) for e in range(4))
    recs = store.by_run("zw")
    assert len(recs) == 4 and all("kind=code_write" in r.verifier_detail for r in recs)


# --- wiring ---------------------------------------------------------------------------------


def test_rays_registry_frontier_findings_wiring():
    from cartographer.campaigns.registry import REGISTRY
    from cartographer.report.frontier import CAUSE_WORDS, SPECS
    from cartographer.viewer.findings import CAPABILITY_KINDS

    ids = set(REGISTRY)
    assert {"V.repair", "V.repair_regressions", "V.write"} <= ids
    keys = {s.key for s in SPECS}
    assert {"repair", "repair_regressions", "write"} <= keys
    for sig in ("visible_fixed_hidden_broken", "no_change", "wrong_site", "regressed", "wrong_value",
                "signature_changed", "no_code_block", "parse_error", "outside_subset", "empty_answer",
                "introduced:concat_mutates", "edge_case:empty_list", "runtime_error:runtime"):
        assert sig in CAUSE_WORDS
    assert "code_repair" in CAPABILITY_KINDS and "code_write" in CAPABILITY_KINDS
    for ray in (default_repair_ray(), default_repair_kind_ray(), default_write_ray()):
        assert ray.dims


def test_bug_distance_steers_the_site_and_is_reported():
    near = 0
    for seed in range(40):
        for d in (1, 4, 8):
            t = make_repair_task(CodeFixParams(bug_distance=d, hidden_inputs=2, n_statements=18), seed)
            assert t.bug_distances[0] == B.site_distance(t.buggy, t.bug_sites[0]) or t.bug_kinds[0] in (
                "frozen_predicate", "stale_update")  # these edits add / remove a statement
            near += abs(t.bug_distances[0] - d) <= 2
    assert near >= 0.9 * 120


def test_any_distance_keeps_kind_variety():
    kinds = {make_repair_task(CodeFixParams(hidden_inputs=2, bug_mix=0.5), s).bug_kinds[0] for s in range(80)}
    assert len(kinds) >= 7
