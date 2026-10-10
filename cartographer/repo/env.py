"""The repository environment (docs/province-vi-spec.md §4.3): tools that
mirror a coding harness's own -- list_files, read_file, search, edit_file,
write_file, run_tests and (when `graph_tools`) get_code_neighbors -- over an
in-memory copy of the repository.

Duck-typed to `interactive.episode.run_episode` (`opening_messages`,
`tools_schema`, `step`, `call_budget`, `calls_left`, `goal_reached`,
`summary`, `render_result`). Nothing the model writes is executed as Python:
`run_tests` parses the files into our subset and runs our interpreter;
`get_code_neighbors` and `restate_symbols` read the files with `ast`
(syntax only).
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass

from ..codefix.tasks import norm
from .compose import RepoTask, repo_execute
from .flatten import flatten
from .render import test_path

TOOL_NAMES = ("list_files", "read_file", "search", "edit_file", "write_file", "run_tests", "get_code_neighbors")
MAX_SEARCH_HITS = 40
_MODULE_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.py$")
_TRANSCRIPT_CHARS = 4000

SUBSET_NOTE = (
    "The code must stay within the Python subset this repository's checker supports: top-level `def`s and "
    "imports only (no module-level statements); ints, strings, lists, dicts; if / for / while; the builtins "
    "len, range, print, sum, min, max, abs; imports only between the repository's own modules, as "
    "`from <module> import <name>` or `import <module>` with `<module>.<name>(...)` (no `as`, no `*`, no "
    "relative imports). Not supported: other imports, classes, lambda, comprehensions, try/except, with, "
    "floats or `/`, `**`, f-strings, tuple unpacking, keyword arguments in calls, recursion."
)


@dataclass
class RepoStep:
    n: int
    tool: str
    args: dict
    ok: bool
    kind: str | None  # error kind, or None
    output: str


# -- a syntax-only index of the current files (for get_code_neighbors / restate_symbols) ------------

@dataclass(frozen=True)
class _Def:
    module: str
    name: str
    line: int
    calls: tuple  # ((qualified "m.f" or "?name", call line), ...)


def _index_module(stem: str, src: str, stems: set) -> list[_Def] | None:
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return None
    local = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    imported: dict[str, str] = {}
    aliases: set[str] = set()
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                imported[a.asname or a.name] = f"{n.module}.{a.name}"
        elif isinstance(n, ast.Import):
            for a in n.names:
                aliases.add(a.asname or a.name)
    out = []
    for n in tree.body:
        if not isinstance(n, ast.FunctionDef):
            continue
        calls = []
        for c in ast.walk(n):
            if not isinstance(c, ast.Call):
                continue
            f = c.func
            if isinstance(f, ast.Name):
                if f.id in local:
                    calls.append((f"{stem}.{f.id}", c.lineno))
                elif f.id in imported:
                    calls.append((imported[f.id], c.lineno))
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in aliases:
                calls.append((f"{f.value.id}.{f.attr}", c.lineno))
        out.append(_Def(stem, n.name, n.lineno, tuple(sorted(calls, key=lambda x: (x[1], x[0])))))
    return out


class RepoIndex:
    """Defs and resolved call edges of the current files. A file that does not
    parse keeps its last good entry, so one bad edit does not blind the tool."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, list]] = {}

    def defs(self, files: dict) -> list[_Def]:
        stems = {p[:-3] for p in files if p.endswith(".py") and "/" not in p}
        out: list[_Def] = []
        for p in sorted(files):
            if not (p.endswith(".py") and "/" not in p):
                continue
            src = files[p]
            hit = self._cache.get(p)
            if hit is None or hit[0] != src:
                got = _index_module(p[:-3], src, stems)
                if got is not None:
                    self._cache[p] = (src, got)
                    hit = self._cache[p]
            if hit is not None:
                out.extend(hit[1])
        return out

    def edges(self, files: dict) -> set[tuple[str, str]]:
        defs = self.defs(files)
        names = {f"{d.module}.{d.name}" for d in defs}
        return {(f"{d.module}.{d.name}", c) for d in defs for c, _ in d.calls if c in names}


# -- the environment ----------------------------------------------------------------------------------

class RepoEnv:
    # Episode protocol opt-ins read by `run_episode` (Province III's env defines neither):
    # stop generating where a text-protocol model starts writing a tool result itself, and
    # execute at most one call per turn, so every call is made after reading every earlier
    # result (first smoke, 2026-10-09: Gemma 4 QAT wrote up to 20 calls in one message with
    # imagined results between them).
    # Stop strings end the turn where a SECOND call (or a self-written result) begins -- what
    # native function calling does by construction, and what ReAct harnesses do by stopping at
    # "Observation:". Diagnostic smoke 2026-10-09 (smoke-vi-gemma4qat-3): after its first
    # ```tool_code block Gemma 4 QAT wrote a leaked thinking marker (`thought`) and the next
    # call, acting out a whole imagined trajectory until the token cap. vLLM accepts <= 4 here.
    text_stop = ("```tool_output", "\nthought\n```", "```\n```tool_code", "```\n\n```tool_code")
    max_calls_per_turn = 1
    # A capped turn whose first call block was complete before the cap executes that call
    # (the cap only cut what followed it). Live run 2026-10-09 (camp-repo-*-001): 16 of 16
    # remaining cap_hits were complete calls followed by thousands of tokens of hand-tracing.
    accept_capped_complete_call = True

    def __init__(self, task: RepoTask) -> None:
        self.task = task
        self.params = task.params
        self.files: dict[str, str] = dict(task.files)
        self.initial: dict[str, str] = dict(task.files)
        self.call_budget = task.params.tool_budget
        self.steps: list[RepoStep] = []
        self.index = RepoIndex()
        self._initial_report = self.run_tests_text()

    # -- run_episode surface ----------------------------------------------------------------------
    @property
    def goal_reached(self) -> bool:
        return False  # the model decides when it is done; the verdict is on the end state

    @property
    def calls_left(self) -> int:
        return self.call_budget - len(self.steps)

    def tools_schema(self) -> list[dict]:
        def tool(name, desc, props=None, required=()):
            return {"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": props or {}, "required": list(required)}}}

        s = {"type": "string"}
        out = [
            tool("list_files", "List every file in the repository."),
            tool("read_file", "Read a file. Each line comes back as its number, a TAB, then the line exactly as "
                 "in the file (the number and the TAB are not part of the file).",
                 {"path": {**s, "description": "file path, e.g. `pricing.py`"}}, ["path"]),
            tool("search", "Find a literal, case-sensitive text in every file; returns `path:line: text` "
                 f"(at most {MAX_SEARCH_HITS} hits).", {"pattern": {**s, "description": "the text to find"}},
                 ["pattern"]),
            tool("edit_file", "Replace the one occurrence of `old` in a file with `new`. `old` must match the "
                 "file exactly (indentation included) and occur exactly once.",
                 {"path": s, "old": {**s, "description": "exact text to replace"},
                  "new": {**s, "description": "replacement text"}}, ["path", "old", "new"]),
            tool("write_file", "Replace a whole file's content, or create a new module at the repository root.",
                 {"path": s, "content": s}, ["path", "content"]),
            tool("run_tests", "Run the repository's tests and report each one."),
        ]
        if self.params.graph_tools:
            out.append(tool(
                "get_code_neighbors", "The callers and callees of a function, with file and line, from the "
                "repository's call graph.",
                {"symbol": {**s, "description": "`module.function` or a bare function name"}}, ["symbol"]))
        return out

    def opening_messages(self) -> list[dict]:
        system = (
            "You are fixing a bug in a small Python repository. A test in `tests/` fails. Find the cause and "
            "fix it with the tools. Files under `tests/` are read-only. " + SUBSET_NOTE
            + f" You may make at most {self.call_budget} tool calls. When you are done, reply with a short "
            "summary and no tool call."
        )
        user = ("The test suite currently fails:\n\n" + self._initial_report
                + "\n\nRepository files:\n" + "\n".join(sorted(self.files)))
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def step(self, tool: str, args: dict) -> dict:
        n = len(self.steps) + 1
        args = {str(k): v for k, v in (args or {}).items()}
        handlers = {
            "list_files": self._list_files, "read_file": self._read_file, "search": self._search,
            "edit_file": self._edit_file, "write_file": self._write_file, "run_tests": self._run_tests,
        }
        if self.params.graph_tools:
            handlers["get_code_neighbors"] = self._neighbors
        h = handlers.get(tool)
        if h is None:
            ok, kind, text = False, "unknown_tool", f"unknown tool `{tool}`; available: {', '.join(handlers)}"
        else:
            try:
                ok, kind, text = h(args)
            except _BadArgs as e:
                ok, kind, text = False, "bad_args", str(e)
        result = {"status": "ok", "output": text} if ok else {"status": "error", "error": text}
        if self.params.restate_symbols:
            result["defined_symbols"] = self.defined_symbols()
        self.steps.append(RepoStep(n, tool, args, ok, kind, text))
        return result

    def render_result(self, result: dict) -> str:
        text = result.get("output", "") if result.get("status") == "ok" else "ERROR: " + str(result.get("error"))
        if "defined_symbols" in result:
            text += "\n\ndefined_symbols:\n" + "\n".join(
                f"  {m}: {', '.join(v)}" for m, v in result["defined_symbols"].items())
        return text

    # -- tools ------------------------------------------------------------------------------------
    def _list_files(self, args) -> tuple:
        return True, None, "\n".join(sorted(self.files))

    def _read_file(self, args) -> tuple:
        path = _arg(args, "path")
        if path not in self.files:
            return False, "no_such_file", f"no such file: {path}"
        lines = self.files[path].split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        # `cat -n` layout: the number, then a TAB, then the line exactly. With a spaces-only
        # separator ("   3  " + line) a model cannot tell separator from indentation: live run
        # camp-repo-control-gemma4qat-002, 37 of 38 failed edits carried 6 spaces for 4.
        return True, None, "\n".join(f"{i:6d}\t{ln}" for i, ln in enumerate(lines, 1))

    def _search(self, args) -> tuple:
        pat = _arg(args, "pattern")
        if not pat:
            return False, "bad_args", "`pattern` must not be empty"
        hits = []
        for p in sorted(self.files):
            for i, ln in enumerate(self.files[p].split("\n"), 1):
                if pat in ln:
                    hits.append(f"{p}:{i}: {ln}")
        if not hits:
            return True, None, "no matches"
        more = len(hits) - MAX_SEARCH_HITS
        return True, None, "\n".join(hits[:MAX_SEARCH_HITS]) + (f"\n... {more} more matches" if more > 0 else "")

    def _writable(self, path: str) -> tuple | None:
        if path.startswith("tests/"):
            return False, "test_edit", "files under tests/ are read-only"
        return None

    def _edit_file(self, args) -> tuple:
        path, old, new = _arg(args, "path"), _arg(args, "old"), _arg(args, "new", allow_empty=True)
        bad = self._writable(path)
        if bad:
            return bad
        if path not in self.files:
            return False, "no_such_file", f"no such file: {path}"
        if not old:
            return False, "bad_args", "`old` must not be empty"
        src = self.files[path]
        count = src.count(old)
        if count == 0:  # retry with trailing whitespace stripped on every line of both
            norm_src = "\n".join(ln.rstrip() for ln in src.split("\n"))
            norm_old = "\n".join(ln.rstrip() for ln in old.split("\n"))
            if norm_old and norm_src.count(norm_old) == 1:
                src, old, count = norm_src, norm_old, 1
        if count == 0:
            return False, "edit_nomatch", (f"`old` was not found in {path}; it must match the file exactly, "
                                           "indentation included (read_file's line number and the TAB after it "
                                           "are not part of the file)")
        if count > 1:
            return False, "edit_nonunique", f"`old` occurs {count} times in {path}; include more surrounding lines"
        self.files[path] = src.replace(old, new, 1)
        return True, None, f"edited {path}"

    def _write_file(self, args) -> tuple:
        path, content = _arg(args, "path"), _arg(args, "content", allow_empty=True)
        bad = self._writable(path)
        if bad:
            return bad
        if path not in self.files and not _MODULE_PATH.match(path):
            return False, "bad_path", "new files must be modules at the repository root, e.g. `helpers.py`"
        existed = path in self.files
        self.files[path] = content if content.endswith("\n") or not content else content + "\n"
        return True, None, f"{'rewrote' if existed else 'created'} {path}"

    def _run_tests(self, args) -> tuple:
        return True, None, self.run_tests_text()

    def _neighbors(self, args) -> tuple:
        sym = _arg(args, "symbol").strip().removesuffix("()")
        defs = self.index.defs(self.files)
        where = {f"{d.module}.{d.name}": d for d in defs}
        if "." in sym:
            hits = [where[sym]] if sym in where else []
        else:
            hits = [d for d in defs if d.name == sym]
        if not hits:
            return False, "no_such_symbol", f"no function named `{sym}`"
        blocks = []
        for d in hits:
            q = f"{d.module}.{d.name}"
            callers = [(f"{c.module}.{c.name}", c.module, line) for c in defs for t, line in c.calls if t == q]
            lines = [f"{q}  ({d.module}.py:{d.line})", "  called by:"]
            lines += [f"    {n}  ({m}.py:{ln})" for n, m, ln in callers] or ["    (nobody)"]
            lines.append("  calls:")
            lines += [f"    {t}  ({d.module}.py:{ln})" for t, ln in d.calls if not t.startswith("?")] or ["    (nothing)"]
            blocks.append("\n".join(lines))
        return True, None, "\n\n".join(blocks)

    # -- shared --------------------------------------------------------------------------------------
    def defined_symbols(self) -> dict:
        out: dict[str, list] = {}
        for d in self.index.defs(self.files):
            out.setdefault(d.module, []).append(d.name)
        return out

    def run_tests_text(self) -> str:
        task = self.task
        tpath = test_path(task)
        flat = flatten(self.files, task.entry_module, task.buggy.param_kinds)
        head = f"ERROR collecting {tpath}"
        if flat.error is not None:
            kind = flat.error[0]
            if kind == "outside_subset":
                _, construct, path, line, detail = flat.error
                msg = (f"    {path}, line {line}: unsupported construct `{construct}`"
                       + (f" ({detail})" if detail else "") + " -- outside the Python subset this repository allows")
            elif kind == "parse_error":
                _, msg0, path, line = flat.error
                msg = f"    {path}, line {line}: {msg0}"
            else:
                msg = f"    ImportError: cannot import name 'solve' from '{task.entry_module}'"
            return f"{head}\n{msg}\nno tests ran"
        if flat.import_error:
            return f"{head}\n    {flat.import_error}\nno tests ran"
        lines, n_fail = [], 0
        for i, (a, expected) in enumerate(task.tests(), 1):
            call = "solve(" + ", ".join(repr(x) for x in a) + ")"
            v, err = repo_execute(flat.program, a)
            name = f"{tpath}::test_solve_{i}"
            if err is None and norm(v) == norm(expected):
                lines.append(f"{name} PASSED")
                continue
            n_fail += 1
            lines += [f"{name} FAILED", f"    assert {call} == {expected!r}"]
            if err is None and self.params.diff_emphasis:
                # EF condition D (formatting only): the same two values, aligned in a block
                lines += ["    AssertionError:", f"    | expected: {expected!r}", f"    | actual:   {v!r}"]
            elif err is None:
                lines.append(f"    AssertionError: {call} returned {v!r}")
            elif err.startswith("step_cap"):
                lines.append(f"    {call} did not finish (step limit exceeded)")
            else:
                lines.append(f"    RuntimeError: {err.split(':', 1)[1].strip()}")
        n = len(task.tests())
        lines.append(f"{n_fail} failed, {n - n_fail} passed")
        if self.params.show_data_flow and n_fail:
            lines.append("\n" + self.data_flow_text(flat))
        if self.params.restate_imports:
            src = self.files.get(f"{task.entry_module}.py", "")
            imps = [ln for ln in src.split("\n") if ln.startswith(("from ", "import "))]
            lines.append(f"\n{task.entry_module}.py imports:\n" + ("\n".join(f"  {x}" for x in imps) or "  (none)"))
        return "\n".join(lines)

    def data_flow_text(self, flat) -> str:
        """EF causal C (`show_data_flow`): how the failing test's value is produced right now, as
        recorded by OUR interpreter on the visible input -- the chain of calls from `solve` down
        the call path that reaches the bug, with each call's arguments and returned value. It
        names functions and values the model could trace itself; it never shows a fix."""
        task = self.task
        wanted = [task.qualified(f) for f in task.path]
        calls = _record_calls(flat.program, task.visible_input, set(wanted))
        out = [f"Data flow for solve{_args(task.visible_input)} in the code as it is now:"]
        depth = 0
        for q in wanted:
            if q not in calls:
                break
            a, r = calls[q]
            mod, _, fn = q.partition("__")
            out.append(f"{'  ' * (depth + 1)}{'' if depth == 0 else 'which uses '}{mod}.{fn}{_args(a)}"
                       f" -> returns {r!r}")
            depth += 1
        return "\n".join(out)

    # -- outcome -------------------------------------------------------------------------------------
    def edited_files(self) -> list[str]:
        return sorted(p for p in set(self.files) | set(self.initial)
                      if self.files.get(p) != self.initial.get(p))

    def summary(self) -> dict:
        task = self.task
        bug_file = f"{task.bug_module}.py"
        path_files = {f"{m}.py" for m in task.path_modules}
        reads = [s for s in self.steps if s.tool == "read_file" and s.ok]
        read_paths = list(dict.fromkeys(s.args.get("path") for s in reads))
        first_cause = next((s.n for s in reads if s.args.get("path") == bug_file), -1)
        edits = [s for s in self.steps if s.tool in ("edit_file", "write_file") and s.ok]
        last_edit = edits[-1].n if edits else 0
        tests_after = any(s.tool == "run_tests" and s.n > last_edit for s in self.steps) if edits else False
        kinds: dict[str, int] = {}
        for s in self.steps:
            if s.kind:
                kinds[s.kind] = kinds.get(s.kind, 0) + 1
        mods_read = [p for p in read_paths if p and not p.startswith("tests/")]
        return {
            "n_calls": len(self.steps),
            "n_reads": len(reads),
            "n_searches": sum(1 for s in self.steps if s.tool == "search"),
            "n_neighbors": sum(1 for s in self.steps if s.tool == "get_code_neighbors"),
            "n_edits": len(edits),
            "n_test_runs": sum(1 for s in self.steps if s.tool == "run_tests"),
            "edit_nomatch": kinds.get("edit_nomatch", 0),
            "edit_nonunique": kinds.get("edit_nonunique", 0),
            "whole_file_rewrite": sum(1 for s in edits if s.tool == "write_file"),
            "test_edit_attempts": kinds.get("test_edit", 0),
            "unknown_tool": kinds.get("unknown_tool", 0),
            "n_errors": sum(1 for s in self.steps if not s.ok),
            "calls_to_first_cause_read": first_cause,
            "files_read": len(mods_read),
            "files_read_on_path": sum(1 for p in mods_read if p in path_files),
            "ran_tests_after_last_edit": int(tests_after),
            "edited_files": self.edited_files(),
        }

    def transcript(self) -> str:
        def clip(v):
            return v if not isinstance(v, str) or len(v) <= _TRANSCRIPT_CHARS else v[:_TRANSCRIPT_CHARS] + "...[clipped]"

        return json.dumps([{"step": s.n, "tool": s.tool, "args": {k: clip(v) for k, v in s.args.items()},
                            "ok": s.ok, "kind": s.kind, "output": clip(s.output)} for s in self.steps])


class _BadArgs(Exception):
    pass


def _args(a) -> str:
    return "(" + ", ".join(repr(x) for x in a) + ")"


def _record_calls(program, args, wanted: set) -> dict:
    """{qualified function id: (args, return value)} of the FIRST call of each wanted function when
    `program`'s entry runs on `args`, recorded by our interpreter (never Python)."""
    import copy

    from ..codeworld.interpret import TRUE, InterpError, Interpreter

    seen: dict = {}

    class _Rec(Interpreter):
        def _call(self, fn, argvals):
            snapshot = copy.deepcopy(list(argvals))
            out = super()._call(fn, argvals)
            name = fn.defn.name
            if name in wanted and name not in seen:
                seen[name] = (tuple(snapshot), copy.deepcopy(out))
            return out

    it = _Rec(TRUE, 40_000)
    try:
        for st in program.body:
            it.exec_top(st)
        it.call_function(program.entry, copy.deepcopy(list(args)))
    except (InterpError, RecursionError):
        pass
    return seen


def _arg(args: dict, name: str, allow_empty: bool = False) -> str:
    if name not in args:
        raise _BadArgs(f"missing argument `{name}`")
    v = args[name]
    if not isinstance(v, str):
        raise _BadArgs(f"`{name}` must be a string")
    return v
