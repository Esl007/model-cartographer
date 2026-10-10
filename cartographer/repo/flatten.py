"""Files -> one `Program` (docs/province-vi-spec.md §4.5).

Every repository file is parsed by `codefix.parse.parse_module` (never run
as Python). Each def becomes `<module>__<name>`, every call is rewritten to
the qualified callee it resolves to, and the result is one `Program` whose
entry is `<entry module>__solve` -- so our interpreter, which knows nothing
about modules, runs the whole repository. Two modules may define the same
bare name.

Import semantics follow Python where they decide an outcome: importing the
entry module imports, transitively, every repository module it names; a
from-import of a name that module does not define, a module that does not
exist, or an import cycle among those modules makes the import fail, and
then every test fails (`import_error`). A call to a name defined and
imported nowhere fails only when it runs ("not a function", Python's
NameError). Both are recorded in `unresolved`, which the verifier scores as
`fabricated_symbol`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..codefix.parse import CodeParseError, ParsedModule, SubsetViolation, parse_module
from ..codeworld.model import FuncDef, Program, Query, iter_stmts
from .compose import rename_functions, repo_outputs, shift_sids
from .render import module_source


@dataclass(frozen=True)
class Unresolved:
    module: str  # file stem where the reference occurs
    symbol: str  # the name referenced
    source: str | None  # the module it was imported from / qualified with; None for a bare call
    how: str  # "import" | "module" | "attribute" | "call"
    lineno: int


@dataclass
class FlatRepo:
    program: Program | None = None
    modules: dict = field(default_factory=dict)  # stem -> ParsedModule
    defs: dict = field(default_factory=dict)  # stem -> def names in file order
    unresolved: list = field(default_factory=list)
    imports: dict = field(default_factory=dict)  # stem -> repository modules it imports
    import_error: str | None = None
    error: tuple | None = None  # ("outside_subset", construct, path, lineno, detail) | ("parse_error", msg, path, lineno)

    @property
    def ok(self) -> bool:
        return self.error is None and self.program is not None

    def qualified_defs(self) -> dict:
        return {s.name: s for s in self.program.body if isinstance(s, FuncDef)} if self.program else {}


def module_files(files: dict) -> dict:
    """stem -> source for the repository's modules (top-level `*.py`; tests excluded)."""
    return {p[:-3]: src for p, src in files.items() if p.endswith(".py") and "/" not in p}


def parse_files(files: dict) -> tuple[dict, tuple | None]:
    mods = module_files(files)
    parsed: dict[str, ParsedModule] = {}
    for stem in sorted(mods):
        try:
            parsed[stem] = parse_module(mods[stem], repo_modules=set(mods))
        except SubsetViolation as e:
            return parsed, ("outside_subset", e.construct, f"{stem}.py", e.lineno, e.detail)
        except CodeParseError as e:
            return parsed, ("parse_error", str(e), f"{stem}.py", e.lineno)
    return parsed, None


def flatten(files: dict, entry_module: str, param_kinds: tuple) -> FlatRepo:
    parsed, err = parse_files(files)
    if err is not None:
        return FlatRepo(modules=parsed, error=err)
    defs = {stem: [s.name for s in pm.body] for stem, pm in parsed.items()}
    unresolved: list[Unresolved] = []
    imports: dict[str, set] = {stem: set() for stem in parsed}
    body: list = []
    offset = 0
    for stem in sorted(parsed):
        pm = parsed[stem]
        local = set(defs[stem])
        mapping = {d: f"{stem}__{d}" for d in local}
        for lname, src, name, line in pm.from_imports:
            if src in parsed:
                imports[stem].add(src)
            if lname in local:
                continue
            if src in parsed and name in defs[src]:
                mapping[lname] = f"{src}__{name}"
            else:
                unresolved.append(Unresolved(stem, name, src, "import", line))
                mapping[lname] = f"?{stem}:{name}"
        for src, line in pm.module_imports:
            if src in parsed:
                imports[stem].add(src)
            else:
                unresolved.append(Unresolved(stem, src, src, "module", line))
        for src, name, line in pm.attr_calls:
            key = f"{src}.{name}"
            if src in parsed and name in defs[src]:
                mapping[key] = f"{src}__{name}"
            else:
                if src in parsed:
                    unresolved.append(Unresolved(stem, name, src, "attribute", line))
                mapping[key] = f"?{stem}:{key}"
        for name, line in pm.unresolved:
            unresolved.append(Unresolved(stem, name, None, "call", line))
            mapping["?" + name] = f"?{stem}:{name}"
        mod_body = shift_sids(rename_functions(pm.body, mapping), offset)
        offset = max([offset] + [s.sid for s in iter_stmts(mod_body)])
        body.extend(mod_body)
    entry = f"{entry_module}__solve"
    flat = FlatRepo(modules=parsed, defs=defs, unresolved=unresolved, imports=imports)
    flat.import_error = _import_error(entry_module, parsed, imports, unresolved)
    if entry_module not in parsed or "solve" not in defs[entry_module]:
        flat.error = ("missing_entry", f"{entry_module}.solve", f"{entry_module}.py", 0)
        return flat
    flat.program = Program(body=tuple(body), query=Query("final_value", None), params=_default_params(),
                           seed=0, entry=entry, param_kinds=tuple(param_kinds))
    return flat


def _default_params():
    from ..codeworld.model import CodeDimParams

    return CodeDimParams()


def _import_error(entry: str, parsed: dict, imports: dict, unresolved: list) -> str | None:
    if entry not in parsed:
        return None
    closure, stack = [], [entry]
    while stack:
        m = stack.pop()
        if m in closure:
            continue
        closure.append(m)
        stack.extend(sorted(imports.get(m, ())))
    for u in unresolved:
        if u.module in closure and u.how == "import":
            if u.source not in parsed:
                return f"ModuleNotFoundError: No module named '{u.source}' (imported in {u.module}.py)"
            return f"ImportError: cannot import name '{u.symbol}' from '{u.source}' ({u.source}.py)"
        if u.module in closure and u.how == "module":
            return f"ModuleNotFoundError: No module named '{u.source}' (imported in {u.module}.py)"
    state: dict[str, int] = {}

    def cyc(m: str) -> str | None:
        state[m] = 1
        for n in sorted(imports.get(m, ())):
            if state.get(n) == 1:
                return f"ImportError: circular import between {m}.py and {n}.py"
            if n not in state:
                r = cyc(n)
                if r:
                    return r
        state[m] = 2
        return None

    return cyc(entry)


def flat_outputs(flat: FlatRepo, inputs) -> list:
    """Per input: norm(value), ("error", kind); every input errors when the
    repository cannot be imported or does not parse."""
    if not flat.ok:
        return [("error", flat.error[0] if flat.error else "missing_entry")] * len(inputs)
    if flat.import_error:
        return [("error", "import_error")] * len(inputs)
    return repo_outputs(flat.program, inputs)


def render_parsed(pm: ParsedModule) -> str:
    """A parsed file laid out canonically again (comments are not kept)."""
    lines = [f"from {m} import {n}" for _l, m, n, _line in pm.from_imports]
    lines += [f"import {m}" for m, _line in pm.module_imports]
    names: dict = {}
    from ..codeworld.render import _all_ids

    for d in pm.body:
        prog = Program(body=(d,), query=Query("final_value", None), params=_default_params(), seed=0)
        for vid, _k in _all_ids(prog):
            names[vid] = vid[1:] if vid.startswith("?") else vid
    head = "\n".join(lines)
    chunks = [module_source([], [d], names).rstrip("\n") for d in pm.body]
    return (head + "\n\n\n" if head else "") + "\n\n\n".join(chunks) + "\n"
