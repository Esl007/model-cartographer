"""Planted agents (docs/province-vi-spec.md §7): scripted policies over
`RepoEnv` that fail exactly on one mechanism each. They validate the
verifier's signatures and the campaign statistics before any spend.

Every agent is `agent(env) -> None` and acts only through `env.step`, so it
sees the same tool surface a model does. Some peek at the task's latent
(the "oracle" parts: knowing which function is buggy once it is read), which
is the point -- each isolates one mechanism.
"""

from __future__ import annotations

import difflib
import re
from .compose import RepoParams, compose
from .env import RepoEnv
from .verify import verify_repo


# -- edit helpers --------------------------------------------------------------------------------

def _spans(before: str, after: str) -> list[tuple[str, str]]:
    """(old, new) candidates covering the first to the last differing line
    between two versions of a file (a bug may touch two nearby lines), from
    the bare span outward, one more context line on each side per step."""
    a, b = before.split("\n"), after.split("\n")
    ops = [op for op in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes() if op[0] != "equal"]
    if not ops:
        return []
    i1, j1 = ops[0][1], ops[0][3]
    i2, j2 = ops[-1][2], ops[-1][4]
    out = []
    for ctx in range(len(a)):
        lo, hi = max(0, i1 - ctx), min(len(a), max(i2, i1 + 1) + ctx)
        old = "\n".join(a[lo:hi])
        new = "\n".join(a[lo:i1] + b[j1:j2] + a[i2:hi])
        out.append((old, new))
        if lo == 0 and hi == len(a):
            break
    return out


def _hunk(before: str, after: str) -> tuple[str, str] | None:
    """The narrowest span (see `_spans`) whose `old` occurs exactly once in `before`."""
    return next(((o, n) for o, n in _spans(before, after) if before.count(o) == 1), None)


def revert_bug(env: RepoEnv) -> None:
    task = env.task
    path = f"{task.bug_module}.py"
    h = _hunk(task.files[path], task.reference_files[path])
    if h:
        env.step("edit_file", {"path": path, "old": h[0], "new": h[1]})


def _qualified_name(task, fid: str) -> str:
    return f"{task.module_names[task.module_of[fid]]}.{task.func_names[fid]}"


# -- agents ------------------------------------------------------------------------------------------

def perfect(env: RepoEnv) -> None:
    """run_tests -> walk the path (graph tool, or search for each callee's
    definition) -> read the bug's file -> revert -> run_tests."""
    task = env.task
    env.step("run_tests", {})
    for fid in task.path[:-1]:
        if task.params.graph_tools:
            env.step("get_code_neighbors", {"symbol": _qualified_name(task, fid)})
        else:
            nxt = task.path[task.path.index(fid) + 1]
            env.step("search", {"pattern": f"def {task.func_names[nxt]}("})
    env.step("read_file", {"path": f"{task.bug_module}.py"})
    revert_bug(env)
    env.step("run_tests", {})


def explorer_blind(env: RepoEnv) -> None:
    """list_files, then read every module in sorted order until the bug's
    file has been read; then the perfect fix."""
    task = env.task
    env.step("list_files", {})
    for p in sorted(env.files):
        if p.startswith("tests/") or env.calls_left <= 2:
            continue
        env.step("read_file", {"path": p})
        if p == f"{task.bug_module}.py":
            break
    revert_bug(env)
    env.step("run_tests", {})


def explorer_graph(env: RepoEnv) -> None:
    """get_code_neighbors down the path (graph tools on), then read the bug's
    file and fix it."""
    task = env.task
    for fid in task.path[:-1]:
        env.step("get_code_neighbors", {"symbol": _qualified_name(task, fid)})
    env.step("read_file", {"path": f"{task.bug_module}.py"})
    revert_bug(env)
    env.step("run_tests", {})


def _solve_header(src: str) -> re.Match | None:
    return re.search(r"^def solve\((.*)\):\n", src, re.M)


def symptom_editor(env: RepoEnv) -> None:
    """Special-case the visible input at the top of `solve`."""
    task = env.task
    path = f"{task.entry_module}.py"
    src = env.files[path]
    m = _solve_header(src)
    params = [p.strip() for p in m.group(1).split(",")]
    cond = " and ".join(f"{p} == {a!r}" for p, a in zip(params, task.visible_input))
    patch = f"{m.group(0)}    if {cond}:\n        return {task.visible_expected!r}\n"
    env.step("read_file", {"path": path})
    env.step("edit_file", {"path": path, "old": m.group(0), "new": patch})
    env.step("run_tests", {})


def symptom_file_editor(env: RepoEnv) -> None:
    """Edit a plausible line in the entry module (the file the failing test
    names): flip the first `+` of `solve`'s body to `-`."""
    task = env.task
    path = f"{task.entry_module}.py"
    src = env.files[path]
    m = _solve_header(src)
    body = src[m.end():]
    line = next((ln for ln in body.split("\n") if " + " in ln), None)
    env.step("read_file", {"path": path})
    if line is not None and src.count(line) == 1:
        env.step("edit_file", {"path": path, "old": line, "new": line.replace(" + ", " - ", 1)})
    env.step("run_tests", {})


def bounded_navigator(hops: int):
    """Follows imports by reading files up to `hops` modules along the path;
    fixes the bug if it got there, else edits the symptom file."""
    def agent(env: RepoEnv) -> None:
        task = env.task
        mods = list(task.path_modules)
        for m in mods[:hops + 1]:
            env.step("read_file", {"path": f"{m}.py"})
        if task.bug_module in mods[:hops + 1]:
            revert_bug(env)
            env.step("run_tests", {})
        else:
            symptom_file_editor(env)
    agent.__name__ = f"bounded_navigator_{hops}"
    return agent


def fabricator(mode: str = "plausible"):
    """Adds a call to a symbol that does not exist (plausible: a made-up
    name; lookalike: a real callee's name one edit away; wrong_module: a real
    function imported from a module that does not define it)."""
    def agent(env: RepoEnv) -> None:
        task = env.task
        path = f"{task.entry_module}.py"
        src = env.files[path]
        real = task.func_names[task.path[1]] if len(task.path) > 1 else task.func_names[task.path[0]]
        if mode == "plausible":
            mod, name = "utils", "fetch_grand_total"
        elif mode == "lookalike":
            mod, name = task.module_names[task.module_of[task.path[1]]], real[:-1]
        else:  # wrong_module
            mod = next(m for m in task.module_names if m != task.module_names[task.module_of[task.path[1]]])
            name = real
        ret = re.findall(r"^    return (\w+)$", src, re.M)[-1]
        new = f"from {mod} import {name}\n" + src.replace(f"    return {ret}\n", f"    return {ret} + {name}(1)\n", 1)
        env.step("write_file", {"path": path, "content": new})
        env.step("run_tests", {})
    agent.__name__ = f"fabricator_{mode}"
    return agent


def twin_caller(env: RepoEnv) -> None:
    """Imports a look-alike twin from its real module and calls it in place of
    the function it imitates (needs `lookalike_names`)."""
    task = env.task
    if not task.twins:
        return
    twin, like = task.twins[0]
    caller = next(a for a, b in task.edges if b == like)
    path = f"{task.module_names[task.module_of[caller]]}.py"
    src = env.files[path]
    tname, lname = task.func_names[twin], task.func_names[like]
    tmod = task.module_names[task.module_of[twin]]
    new = src.replace(f"from {task.module_names[task.module_of[like]]} import {lname}\n",
                      f"from {tmod} import {tname}\n", 1).replace(f"{lname}(", f"{tname}(")
    env.step("write_file", {"path": path, "content": new})
    env.step("run_tests", {})


def clone_editor(env: RepoEnv) -> None:
    """Reverts the bug in the near-miss clone instead of the real function."""
    task = env.task
    if task.clone is None:
        return
    clone, carrier = task.clone
    path = f"{task.module_names[task.module_of[clone]]}.py"
    src = env.files[path]
    bug_path = f"{task.bug_module}.py"
    renames = [(n, task.local_names[clone][v]) for v, n in task.local_names[carrier].items()
               if task.local_names[clone].get(v, n) != n]  # the clone renamed one local
    for old, new in _spans(task.files[bug_path], task.reference_files[bug_path]):
        for a, b in renames:
            old, new = re.sub(rf"\b{a}\b", b, old), re.sub(rf"\b{a}\b", b, new)
        if src.count(old) == 1:
            env.step("edit_file", {"path": path, "old": old, "new": new})
            break
    env.step("run_tests", {})


def copier(env: RepoEnv) -> None:
    """Right intent, wrong `old`: the bug's lines copied WITH read_file's line
    numbers, twice; then gives up."""
    task = env.task
    path = f"{task.bug_module}.py"
    h = _hunk(task.files[path], task.reference_files[path])
    out = env.step("read_file", {"path": path})["output"].split("\n")
    if h:
        first = task.files[path].split("\n").index(h[0].split("\n")[0])
        old = "\n".join(out[first:first + h[0].count("\n") + 1])
        for _ in range(2):
            env.step("edit_file", {"path": path, "old": old, "new": h[1]})


def constant_patcher(env: RepoEnv) -> None:
    """Shifts `solve`'s returned value by the visible test's error (int
    outputs only): the symptom patch a constant-delta bug would let pass."""
    task = env.task
    exp, got = task.visible_expected, task.visible_wrong
    if not (isinstance(exp, int) and isinstance(got, int)) or isinstance(exp, bool):
        return
    path = f"{task.entry_module}.py"
    src = env.files[path]
    ret = re.findall(r"^    return (\w+)$", src, re.M)[-1]
    env.step("edit_file", {"path": path, "old": f"    return {ret}\n",
                           "new": f"    return {ret} + ({exp - got})\n"})
    env.step("run_tests", {})


def explorer(env: RepoEnv) -> None:
    """The paired-graph fake: graph walk when the tool exists, blind reading otherwise."""
    (explorer_graph if env.params.graph_tools else explorer_blind)(env)


def do_nothing(env: RepoEnv) -> None:
    env.step("run_tests", {})


AGENTS = {
    "perfect": perfect, "explorer_blind": explorer_blind, "explorer_graph": explorer_graph,
    "symptom_editor": symptom_editor, "symptom_file_editor": symptom_file_editor,
    "fabricator_plausible": fabricator("plausible"), "fabricator_lookalike": fabricator("lookalike"),
    "fabricator_wrong_module": fabricator("wrong_module"), "twin_caller": twin_caller,
    "clone_editor": clone_editor, "copier": copier, "do_nothing": do_nothing, "explorer": explorer,
    "constant_patcher": constant_patcher,
}


def run_agent(agent, task) -> tuple:
    """(RepoVerification, env.summary()) for one scripted episode. An agent
    that does not apply to this task (makes no call) yields signature
    `skipped`."""
    env = RepoEnv(task)
    agent(env)
    if not env.steps:
        from ..store.schema import Verdict
        from .verify import RepoVerification

        return RepoVerification(Verdict.ERROR, "skipped"), env.summary()
    outcome = "budget_exhausted" if env.calls_left <= 0 else "stopped_short"
    return verify_repo(task, env.files, outcome), env.summary()


class ZooEvaluator:
    """`(config, trial_seed) -> bool` over a planted agent (no client), so
    `find_breaking_point` and the paired campaigns can be validated offline."""

    def __init__(self, agent, base: RepoParams | None = None) -> None:
        from .evaluator import params_from_config, task_seed

        self.agent = agent
        self.base = base or RepoParams()
        self._params = params_from_config
        self._seed = task_seed
        self.error_count = 0
        self.records: list = []

    def __call__(self, config: dict, trial_seed: int, run_suffix: str = "") -> bool:
        params = self._params(self.base, config)
        task = compose(params, self._seed(params, trial_seed))
        v, s = run_agent(self.agent, task)
        self.records.append((dict(config), trial_seed, run_suffix, v, s))
        if v.verdict.name == "ERROR":
            self.error_count += 1
        return v.verdict.name == "PASS"


__all__ = ["AGENTS", "ZooEvaluator", "run_agent", "revert_bug", "bounded_navigator", "fabricator"]
