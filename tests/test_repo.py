"""Province VI acceptance tests (docs/province-vi-spec.md §3).

Sizes: the default run uses reduced counts; REPO_FULL=1 runs the spec's
full counts (2,000 round trips, 300 exported repositories under CPython).
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from cartographer.codefix.parse import SubsetViolation, is_foreign_module, parse_module, parse_program
from cartographer.codefix.tasks import norm
from cartographer.codeworld.model import FuncDef, iter_stmts
from cartographer.exec.client import ToolChatResult
from cartographer.interactive.textcalls import parse_text_tool_calls
from cartographer.repo import names as N
from cartographer.repo.compose import RepoParams, calls_in, compose, repo_execute, repo_outputs
from cartographer.repo.env import RepoEnv
from cartographer.repo.evaluator import (
    RepoFixEvaluator, default_repo_budget_ray, default_repo_clone_ray, default_repo_control_ray,
    default_repo_lookalike_ray, default_repo_navigate_ray, mcnemar_exact, params_from_config, run_paired_graph,
    task_seed, wilcoxon_signed_rank_less,
)
from cartographer.repo.export import cpython_outputs, export_repo
from cartographer.repo.flatten import flat_outputs, flatten, render_parsed
from cartographer.repo.verify import verify_repo
from cartographer.repo.zoo import AGENTS, ZooEvaluator, _hunk, bounded_navigator, run_agent
from cartographer.schedule.escalate import find_breaking_point
from cartographer.schedule.gate import ResampleGatedEvaluator
from cartographer.store.schema import Verdict
from cartographer.store.store import ResultsStore

FULL = os.environ.get("REPO_FULL") == "1"


def _grid(n: int):
    """A spread of parameter settings over every dial, `n` tasks."""
    shapes = [(1, 0), (1, 2), (2, 1), (3, 2), (4, 1), (4, 3), (6, 2), (6, 5), (8, 3), (12, 6)]
    for i in range(n):
        k, d = shapes[i % len(shapes)]
        yield RepoParams(n_modules=k, bug_distance=d, bug_mix=(i % 3) / 2, fanout=1 + i % 3,
                         lookalike_names=i % 4 == 1, clone_trap=i % 5 == 2, distractor_modules=i % 3,
                         inline_p=0.25 if i % 2 else 0.0, opaque_names=i % 7 == 3), 7000 + i


# -- §3.1 flatten round trip ----------------------------------------------------------------------------

def test_flatten_executes_like_the_latent_and_re_renders_byte_identically():
    n = 2000 if FULL else 150
    for p, seed in _grid(n):
        t = compose(p, seed)
        ins = ([t.visible_input] + list(t.hidden_inputs) + [a for a, _ in t.extra_tests])[:8]
        for files, prog in ((t.files, t.buggy), (t.reference_files, t.reference)):
            flat = flatten(files, t.entry_module, prog.param_kinds)
            assert flat.ok and not flat.import_error and not flat.unresolved, (p, seed)
            assert flat_outputs(flat, ins) == repo_outputs(prog, ins), (p, seed)
            for stem, pm in flat.modules.items():
                assert render_parsed(pm) == files[f"{stem}.py"], (p, seed, stem)


def test_one_module_is_a_province_v_program():
    for seed in range(20):
        t = compose(RepoParams(n_modules=1, bug_distance=2), seed)
        mods = [p for p in t.files if not p.startswith("tests/")]
        assert len(mods) == 1
        prog = parse_program(t.files[mods[0]], entry="solve")  # Province V's own parser accepts it
        ins = [t.visible_input] + list(t.hidden_inputs)
        assert repo_outputs(prog, ins) == repo_outputs(t.buggy, ins)


def test_generation_is_deterministic():
    for p, seed in list(_grid(12)):
        a, b = compose(p, seed), compose(p, seed)
        assert a.files == b.files and a.reference_files == b.reference_files and a.bug_sites == b.bug_sites


# -- §3.2 the graph is exact ------------------------------------------------------------------------------

def _shortest(edges, src, dst):
    frontier, seen, d = {src}, {src}, 0
    while frontier:
        if dst in frontier:
            return d
        nxt = {b for a, b in edges if a in frontier} - seen
        seen |= nxt
        frontier, d = nxt, d + 1
    return None


def test_code_neighbors_graph_equals_the_latent_and_distance_is_exact():
    for p, seed in _grid(60):
        t = compose(p, seed)
        env = RepoEnv(t)
        latent_edges = {(f"{t.module_names[t.module_of[a]]}.{t.func_names[a]}",
                         f"{t.module_names[t.module_of[b]]}.{t.func_names[b]}") for a, b in t.edges}
        assert env.index.edges(env.files) == latent_edges, (p, seed)
        assert _shortest(set(t.edges), "solve", t.carrier) == p.bug_distance == len(t.path) - 1


def test_module_import_graph_is_acyclic_and_path_crosses_files():
    for p, seed in _grid(60):
        t = compose(p, seed)
        flat = flatten(t.files, t.entry_module, t.buggy.param_kinds)
        assert flat.import_error is None
        if p.bug_distance <= p.n_modules - 1:
            assert t.realized["path_crosses_every_hop"] == 1.0, (p, seed)


# -- §3.3 the bug is load-bearing and single-sited --------------------------------------------------------

def test_bug_is_load_bearing_and_confined_to_the_carrier():
    for p, seed in _grid(60):
        t = compose(replace(p, n_bugs=1), seed)
        ref_v = repo_execute(t.reference, t.visible_input)[0]
        bug_v = repo_execute(t.buggy, t.visible_input)[0]
        assert norm(ref_v) != norm(bug_v) and norm(ref_v) == norm(t.visible_expected)
        for a, e in t.extra_tests:
            assert norm(repo_execute(t.buggy, a)[0]) == norm(e)
        if p.bug_distance >= 1 and p.n_modules >= 2:
            assert t.bug_module != t.entry_module
        ref_defs = {s.name: s for s in t.reference.body if isinstance(s, FuncDef)}
        bug_defs = {s.name: s for s in t.buggy.body if isinstance(s, FuncDef)}
        changed = [f for f in ref_defs if ref_defs[f] != bug_defs[f]]
        assert changed == [t.carrier]
        assert len(t.bug_sites) == 1 and t.bug_sites[0] in {s.sid for s in iter_stmts(ref_defs[t.carrier].body)}


def test_visible_test_fails_on_buggy_and_passes_on_reference():
    for p, seed in _grid(20):
        t = compose(p, seed)
        assert "1 failed" in RepoEnv(t).run_tests_text()
        ref_env = RepoEnv(t)
        ref_env.files = dict(t.reference_files)
        assert "0 failed" in ref_env.run_tests_text()


# -- §3.4 oracles ----------------------------------------------------------------------------------------

def test_oracles():
    n = 0
    vfhb = collections.Counter()
    for p, seed in _grid(40):
        k = max(p.n_modules, 3)
        t = compose(replace(p, hidden_inputs=4, clone_trap=True, n_modules=k,
                            bug_distance=max(1, min(p.bug_distance, k - 1))), seed)
        n += 1
        assert run_agent(AGENTS["perfect"], t)[0].verdict == Verdict.PASS
        if t.clone is not None:
            assert run_agent(AGENTS["clone_editor"], t)[0].signature == "clone_edited"
        v = run_agent(AGENTS["symptom_editor"], t)[0]
        vfhb[v.signature] += 1
        assert run_agent(AGENTS["fabricator_plausible"], t)[0].signature == "fabricated_symbol:plausible"
    assert vfhb["visible_fixed_hidden_broken"] >= 0.9 * n


# -- §3.5 zoo -------------------------------------------------------------------------------------------

ZOO_EXPECT = {
    "perfect": ("pass", {}), "explorer_blind": ("pass", {}), "explorer_graph": ("pass", {"graph_tools": True}),
    "symptom_editor": ("visible_fixed_hidden_broken", {"hidden_inputs": 4}),
    "symptom_file_editor": ("wrong_site_file", {}),
    "fabricator_plausible": ("fabricated_symbol:plausible", {}),
    "fabricator_lookalike": ("fabricated_symbol:lookalike", {}),
    "fabricator_wrong_module": ("fabricated_symbol:wrong_module", {}),
    "twin_caller": ("lookalike_called", {"lookalike_names": True}),
    "clone_editor": ("clone_edited", {"clone_trap": True}),
    "copier": ("no_change", {}), "do_nothing": ("no_change", {}),
}


@pytest.mark.parametrize("agent", sorted(ZOO_EXPECT))
def test_zoo_agent_fails_exactly_on_its_mechanism(agent):
    want, kw = ZOO_EXPECT[agent]
    for seed in range(12):
        t = compose(RepoParams(n_modules=6, bug_distance=2, **kw), seed)
        v, s = run_agent(AGENTS[agent], t)
        if v.signature == "skipped":  # the task has no twin / clone for this agent (realized dims say so)
            assert agent in ("twin_caller", "clone_editor")
            continue
        assert v.signature == want, (agent, seed, v)
        if agent == "copier":
            assert s["edit_nomatch"] == 2


def test_budget_exhausted_without_edit_is_error_only_under_tight_budgets():
    t = compose(RepoParams(n_modules=4, bug_distance=1, tool_budget=6), 3)
    assert verify_repo(t, t.files, "budget_exhausted").verdict == Verdict.ERROR
    t2 = compose(RepoParams(n_modules=4, bug_distance=1, tool_budget=20), 3)
    assert verify_repo(t2, t2.files, "budget_exhausted").signature == "no_change"
    assert verify_repo(t2, t2.files, "api_error").verdict == Verdict.ERROR


def test_find_breaking_point_attributes_the_bounded_navigator_to_bug_distance():
    ev = ZooEvaluator(bounded_navigator(2))
    bp = find_breaking_point(ev, default_repo_navigate_ray(), k_trials=3, budget=120, parallelism=1)
    assert bp.found
    assert bp.config_at_break["bug_distance"] >= 3
    assert bp.hypothesis is not None and "bug_distance" in bp.hypothesis.factors


def test_paired_graph_value_detects_the_explorer_reduction():
    ev = ZooEvaluator(AGENTS["explorer"])
    res = run_paired_graph(ev, levels=(12,), n_seeds=12, bug_distance=3, parallelism=1)
    assert res.pairs == 12 and res.pass_off == res.pass_on == 12
    assert res.h4_supported and res.ratio <= 0.7, res.note


def test_pairs_see_identical_repositories():
    p = RepoParams(n_modules=6, bug_distance=2)
    for ts in range(5):
        a = compose(p, task_seed(p, ts))
        on = params_from_config(p, {"graph_tools": 1.0, "restate_symbols": 1.0, "tool_budget": 8})
        b = compose(on, task_seed(on, ts))
        assert a.files == b.files
        tw = params_from_config(p, {"lookalike_names": 1.0, "clone_trap": 1.0, "distractor_modules": 2})
        c = compose(tw, task_seed(tw, ts))
        # extras only ADD never-called functions: the program, bug and inputs are unchanged
        assert c.buggy.body[:len(a.buggy.body)] == a.buggy.body and c.bug_sites == a.bug_sites
        assert c.visible_input == a.visible_input and c.hidden_inputs == a.hidden_inputs


def test_control_ray_repartitions_the_same_program():
    base = RepoParams(n_funcs=8, bug_distance=1)
    for ts in range(4):
        progs = []
        for k in (1, 4, 8):
            p = replace(base, n_modules=k)
            t = compose(p, task_seed(p, ts))
            progs.append((t.buggy.body, t.visible_input))
            assert t.realized["n_modules"] == min(k, 8)
        assert progs[0] == progs[1] == progs[2]


# -- §3.6 nothing outside the subset runs ------------------------------------------------------------------

BAD = [
    "import os\ndef solve(a):\n    os.system('echo x')\n    return a\n",
    "def solve(a):\n    return __import__('os').getcwd()\n",
    "def solve(a):\n    return open('x').read()\n",
    "def solve(a):\n    return eval('1')\n",
    "for i in range(10 ** 9):\n    pass\ndef solve(a):\n    return a\n",
    "from pricing import *\ndef solve(a):\n    return a\n",
]


@pytest.mark.parametrize("src", BAD)
def test_unsafe_files_are_rejected_at_parse_time(src):
    with pytest.raises(SubsetViolation):
        parse_module(src, repo_modules={"pricing", "report"})


def test_module_imports_resolve():
    files = {"pricing.py": "def compute_total(x):\n    return x + 1\n",
             "report.py": "import pricing\n\n\ndef solve(a):\n    return pricing.compute_total(a) * 2\n"}
    flat = flatten(files, "report", ("int",))
    assert flat.ok and not flat.unresolved and flat_outputs(flat, [(3,)]) == [("int", 8)]
    files["report.py"] = "from pricing import compute_totals\n\n\ndef solve(a):\n    return compute_totals(a)\n"
    flat = flatten(files, "report", ("int",))
    assert flat.unresolved and flat.import_error.startswith("ImportError")
    assert flat_outputs(flat, [(3,)]) == [("error", "import_error")]


def test_typing_imports_are_dropped_not_rejected():
    pm = parse_module("from typing import List\n\n\ndef f(xs: List[int]) -> int:\n    return len(xs)\n",
                      repo_modules={"m"})
    assert not pm.from_imports and len(pm.body) == 1


def test_module_names_never_shadow_real_modules():
    for w in N.MODULE_WORDS:
        assert not is_foreign_module(w), w
    for w in N.MODULE_WORDS:
        assert w not in {x for pool in ("int", "list", "str", "p", "x", "i") for x in __import__(
            "cartographer.codeworld.render", fromlist=["_POOLS"])._POOLS[pool]}


def test_lookalike_names_are_close():
    for v in N.VERBS:
        for n in N.NOUNS:
            for w in N.lookalike_names(f"{v}_{n}"):
                assert 1 <= N.edit_distance(w, f"{v}_{n}") <= 2


# -- environment ------------------------------------------------------------------------------------------

def test_env_tools():
    t = compose(RepoParams(n_modules=4, bug_distance=1, graph_tools=True, restate_symbols=True), 11)
    env = RepoEnv(t)
    assert env.step("list_files", {})["output"].split("\n") == sorted(t.files)
    r = env.step("read_file", {"path": f"{t.entry_module}.py"})
    assert r["output"].startswith("     1\t") and "defined_symbols" in r
    assert env.step("edit_file", {"path": f"tests/test_{t.entry_module}.py", "old": "a", "new": "b"})["status"] == "error"
    assert env.step("edit_file", {"path": f"{t.entry_module}.py", "old": "no such text", "new": "x"})["status"] == "error"
    assert env.step("edit_file", {"path": f"{t.entry_module}.py", "old": "    ", "new": "x"})["error"].startswith("`old` occurs")
    assert "def solve(" in env.step("search", {"pattern": "def solve("})["output"]
    nb = env.step("get_code_neighbors", {"symbol": "solve"})["output"]
    assert nb.startswith(f"{t.entry_module}.solve") and "calls:" in nb
    assert env.step("nonexistent_tool", {})["status"] == "error"
    s = env.summary()
    assert s["edit_nomatch"] == 1 and s["edit_nonunique"] == 1 and s["test_edit_attempts"] == 1
    assert s["unknown_tool"] == 1 and s["n_calls"] == 8
    assert "get_code_neighbors" not in [x["function"]["name"] for x in RepoEnv(replace(t, params=replace(
        t.params, graph_tools=False))).tools_schema()]


def test_run_tests_reports_collection_errors():
    t = compose(RepoParams(n_modules=3, bug_distance=1), 2)
    env = RepoEnv(t)
    env.files[f"{t.entry_module}.py"] = "import os\n" + env.files[f"{t.entry_module}.py"]
    out = env.run_tests_text()
    assert out.startswith("ERROR collecting") and "foreign_import" in out
    assert verify_repo(t, env.files).signature == "outside_subset:foreign_import"


# -- text protocol --------------------------------------------------------------------------------------

def test_multiline_text_calls_parse_and_single_line_calls_are_unchanged():
    po = {"edit_file": ["path", "old", "new"], "provision": ["service_id"]}
    txt = '```tool_code\nedit_file(path="a.py", old="""    x = 1\n    y = 2""", new="""    x = 3""")\n```'
    assert parse_text_tool_calls(txt, po) == [("edit_file", {"path": "a.py", "old": "    x = 1\n    y = 2",
                                                             "new": "    x = 3"})]
    assert parse_text_tool_calls("```tool_code\nprovision('svc_0')\n```", po) == [("provision", {"service_id": "svc_0"})]
    # not valid Python (a look-alike id with '-'): the line regex still reads it, as before
    assert parse_text_tool_calls("```tool_code\nset_dep-th(x='1')\n```", po) == [("set_dep-th", {"x": "1"})]


# -- evaluator, end to end with a scripted client --------------------------------------------------------

class _Client:
    def __init__(self, plan, text=False):
        self.plan, self.text, self.i = plan, text, 0

    def chat_turn(self, model, messages, tools=None, max_tokens=0, temperature=0.0, stop=None):
        self.stops = stop
        if self.i >= len(self.plan):
            return ToolChatResult(message={"content": "done"}, tool_calls=(), text="done", tokens_in=10,
                                  tokens_out=2, latency_ms=1.0, finish_reason="stop")
        name, args = self.plan[self.i]
        self.i += 1
        if self.text:
            t = "```tool_code\n" + f"{name}(" + ", ".join(f"{k}={v!r}" for k, v in args.items()) + ")\n```"
            return ToolChatResult(message={"content": t}, tool_calls=(), text=t, tokens_in=10, tokens_out=5,
                                  latency_ms=1.0, finish_reason="stop")
        tc = {"id": f"c{self.i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        return ToolChatResult(message={"content": "", "tool_calls": [tc]}, tool_calls=((tc["id"], name, json.dumps(args)),),
                              text="", tokens_in=10, tokens_out=5, latency_ms=1.0, finish_reason="tool_calls")


@pytest.mark.parametrize("text", [False, True])
def test_evaluator_end_to_end(tmp_path, text):
    cfg = {"n_modules": 4.0, "bug_distance": 2.0}
    p = params_from_config(RepoParams(), cfg)
    t = compose(p, task_seed(p, 5))
    path = f"{t.bug_module}.py"
    old, new = _hunk(t.files[path], t.reference_files[path])
    plan = [("read_file", {"path": path}), ("edit_file", {"path": path, "old": old, "new": new}), ("run_tests", {})]
    store = ResultsStore(str(tmp_path / "s.sqlite"))
    ev = RepoFixEvaluator(_Client(plan, text), "m", store, "camp-repo-navigate-x-001", 1.0,
                          protocol="text_fallback" if text else "native")
    assert ev(cfg, 5)
    rec = store.by_run("camp-repo-navigate-x-001")[0]
    assert rec.verdict == Verdict.PASS and "kind=repo_fix" in rec.verifier_detail
    assert "calls_to_first_cause_read=1" in rec.verifier_detail and rec.realized_dims["n_modules"] == 4.0
    assert json.loads(rec.response_text)["diff"].count("\n-") >= 1


def test_gate_resamples_a_repo_fail(tmp_path):
    store = ResultsStore(str(tmp_path / "s.sqlite"))
    # the client runs the tests once, then stops on every later turn: a stable no_change FAIL
    ev = RepoFixEvaluator(_Client([("run_tests", {})]), "m", store, "camp-repo-navigate-x-002", 1.0)
    gated = ResampleGatedEvaluator(ev, n=2)
    assert gated({"n_modules": 4.0, "bug_distance": 1.0}, 1) is False
    assert len(store.by_run("camp-repo-navigate-x-002/verify")) == 2


# -- statistics ------------------------------------------------------------------------------------------

def test_mcnemar_and_wilcoxon():
    assert mcnemar_exact(0, 0) == 1.0
    assert abs(mcnemar_exact(0, 6) - 2 / 64) < 1e-12
    assert wilcoxon_signed_rank_less([-1, -2, -3, -4, -5]) == 1 / 32
    assert wilcoxon_signed_rank_less([1, 2, 3]) == 1.0
    assert 0.0 < wilcoxon_signed_rank_less([-1] * 30 + [2] * 3) < 0.01


def test_rays_are_valid_configs():
    for ray in (default_repo_navigate_ray(), default_repo_lookalike_ray(), default_repo_clone_ray(),
                default_repo_budget_ray(), default_repo_control_ray()):
        for tt in (0.0, 0.5, 1.0):
            cfg = {d.name: d.value_at(tt) for d in ray.dims}
            p = params_from_config(RepoParams(), cfg)
            compose(p, task_seed(p, 0))


# -- §3.7 wiring -----------------------------------------------------------------------------------------

def test_registry_and_frontier_list_province_vi(tmp_path):
    from cartographer.campaigns.registry import REGISTRY, catalog_markdown
    from cartographer.report import frontier

    ids = [k for k in REGISTRY if k.startswith("VI.")]
    assert set(ids) == {"VI.navigate", "VI.lookalike", "VI.clone", "VI.budget", "VI.control", "VI.graph_value"}
    assert all(i in catalog_markdown() for i in ids)
    store = ResultsStore(str(tmp_path / "empty.sqlite"))
    keys = [s.key for s in frontier.SPECS]
    for k in ("repo_navigate", "repo_lookalike", "repo_clone", "repo_budget", "repo_control", "repo_graph_value"):
        assert k in keys
    del store


# -- §3.8 export differential under CPython -------------------------------------------------------------------

def test_exported_repositories_agree_with_cpython_and_pytest(tmp_path):
    n = 300 if FULL else 24
    jobs, want = [], {}
    tasks = []
    for i, (p, seed) in enumerate(_grid(n)):
        t = compose(p, seed)
        tasks.append(t)
        for which, prog in (("buggy", t.buggy), ("reference", t.reference)):
            d = export_repo(t, tmp_path / f"r{i}_{which}", which)
            ins = [t.visible_input] + list(t.hidden_inputs)
            jobs.append((str(d), t.entry_module, ins))
            want[str(d)] = [norm(repo_execute(prog, a)[0]) for a in ins]
    got = cpython_outputs(jobs)
    for d, res in got.items():
        assert [norm(v) if s == "ok" else ("error",) for s, v in res] == want[d], d
    for i in range(0, n, max(1, n // 6)):
        for which, fails in (("reference", False), ("buggy", True)):
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                str(tmp_path / f"r{i}_{which}")], capture_output=True, text=True)
            assert (r.returncode != 0) == fails, (i, which, r.stdout[-400:])


def test_episode_records_carry_the_protocol_version(tmp_path):
    from cartographer.interactive.episode import PROTOCOL_VERSION, protocol_version_of
    from cartographer.store.schema import ExperimentRecord, RenderMode

    cfg = {"n_modules": 3.0, "bug_distance": 1.0}
    store = ResultsStore(str(tmp_path / "s.sqlite"))
    RepoFixEvaluator(_Client([("run_tests", {})]), "m", store, "camp-repo-x-003", 1.0)(cfg, 2)
    rec = store.by_run("camp-repo-x-003")[0]
    assert protocol_version_of(rec) == PROTOCOL_VERSION == 2
    assert "result_rendering=text" in rec.verifier_detail
    old = ExperimentRecord(run_id="r", model_name="m", latent_params={}, realized_dims={},
                           render_mode=RenderMode.TEMPLATE, task_seed=0, trial_seed=0, verdict=Verdict.PASS,
                           verifier_detail="kind=interactive;campaign=1")
    assert protocol_version_of(old) == 1


def test_context_window_clamps_turns_and_ends_the_episode(tmp_path):
    from cartographer.interactive.episode import run_episode

    seen = []

    class Talky(_Client):
        def chat_turn(self, model, messages, tools=None, max_tokens=0, temperature=0.0, stop=None):
            seen.append(max_tokens)
            return super().chat_turn(model, messages, tools, max_tokens, temperature, stop)

    t = compose(RepoParams(n_modules=6, bug_distance=2, tool_budget=40), 4)
    plan = [("read_file", {"path": p}) for p in sorted(t.files)] * 6
    env = RepoEnv(t)
    res = run_episode(Talky(plan), "m", env, max_tokens=16000, context_window=8000)
    assert res.outcome == "context_exhausted" and env.calls_left > 0
    assert seen[0] < 8000 and all(b <= a for a, b in zip(seen, seen[1:]))
    assert verify_repo(t, env.files, res.outcome).signature == "no_change"



def test_a_constant_patch_never_passes():
    """The generator keeps only bugs whose effect varies with the input, and the
    hidden inputs include one whose delta differs from the visible test's."""
    from cartographer.repo.compose import input_dependent

    n = 0
    for p, seed in _grid(40):
        t = compose(replace(p, hidden_inputs=4), seed)
        v, _ = run_agent(AGENTS["constant_patcher"], t)
        if v.signature == "skipped":
            continue
        n += 1
        assert v.verdict == Verdict.FAIL, (p, seed, v)
        ins = [t.visible_input] + list(t.hidden_inputs)
        assert input_dependent(repo_outputs(t.buggy, ins), repo_outputs(t.reference, ins))
    assert n >= 20


def test_one_call_per_turn_and_stop_sequence_in_the_text_protocol():
    from cartographer.interactive.episode import run_episode

    t = compose(RepoParams(n_modules=3, bug_distance=1), 6)

    class Batch(_Client):
        def chat_turn(self, model, messages, tools=None, max_tokens=0, temperature=0.0, stop=None):
            self.stops = stop
            if self.i:
                return ToolChatResult(message={"content": "done"}, tool_calls=(), text="done", tokens_in=1,
                                      tokens_out=1, latency_ms=1.0, finish_reason="stop")
            self.i = 1
            txt = "\n".join("```tool_code\n" + c + "\n```" for c in ("list_files()", "run_tests()", "list_files()"))
            return ToolChatResult(message={"content": txt}, tool_calls=(), text=txt, tokens_in=1, tokens_out=1,
                                  latency_ms=1.0, finish_reason="stop")

    c = Batch([], text=True)
    env = RepoEnv(t)
    res = run_episode(c, "m", env, protocol="text_fallback")
    assert res.skipped_calls == 2 and len(env.steps) == 1 and env.steps[0].tool == "list_files"
    assert c.stops == list(RepoEnv.text_stop) and "```tool_output" in c.stops


def test_a_stop_string_cut_fence_is_closed_before_parsing():
    from cartographer.interactive.episode import run_episode

    t = compose(RepoParams(n_modules=3, bug_distance=1), 6)

    class Cut(_Client):
        def chat_turn(self, model, messages, tools=None, max_tokens=0, temperature=0.0, stop=None):
            self.stops = stop
            if self.i:
                return ToolChatResult(message={"content": "done"}, tool_calls=(), text="done", tokens_in=1,
                                      tokens_out=1, latency_ms=1.0, finish_reason="stop")
            self.i = 1  # as if vLLM stopped at "```\n```tool_code": the first block's closing fence is cut
            txt = "Let me look.\n```tool_code\nlist_files()\n"
            return ToolChatResult(message={"content": txt}, tool_calls=(), text=txt, tokens_in=1, tokens_out=1,
                                  latency_ms=1.0, finish_reason="stop")

    c = Cut([], text=True)
    env = RepoEnv(t)
    run_episode(c, "m", env, protocol="text_fallback")
    assert [s.tool for s in env.steps] == ["list_files"]
    assert "\nthought\n```" in c.stops and len(c.stops) <= 4


def test_a_capped_turn_with_a_complete_call_executes_it_and_one_without_is_cap_hit():
    from cartographer.interactive.episode import run_episode

    t = compose(RepoParams(n_modules=3, bug_distance=1), 6)

    class Rambler(_Client):
        def __init__(self, first):
            super().__init__([], text=True)
            self.first = first

        def chat_turn(self, model, messages, tools=None, max_tokens=0, temperature=0.0, stop=None):
            if self.i:
                return ToolChatResult(message={"content": "done"}, tool_calls=(), text="done", tokens_in=1,
                                      tokens_out=1, latency_ms=1.0, finish_reason="stop")
            self.i = 1
            return ToolChatResult(message={"content": self.first}, tool_calls=(), text=self.first, tokens_in=1,
                                  tokens_out=6000, latency_ms=1.0, finish_reason="length")

    env = RepoEnv(t)
    res = run_episode(Rambler("```tool_code\nrun_tests()\n```\nLet me trace: x = 1, x = 2, ..."), "m", env,
                      protocol="text_fallback", retry_max_tokens=None)
    assert res.truncated_turns == 1 and [s.tool for s in env.steps] == ["run_tests"] and res.outcome != "cap_hit"
    env2 = RepoEnv(t)
    res2 = run_episode(Rambler("Let me think about this for a long time ... ```tool_code\nread_fi"), "m", env2,
                       protocol="text_fallback", retry_max_tokens=None)
    assert res2.outcome == "cap_hit" and not env2.steps
