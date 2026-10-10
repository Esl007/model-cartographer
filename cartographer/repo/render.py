"""Rendering a repository task to files (docs/province-vi-spec.md §4.2).

One file per module: sorted `from <module> import <name>` lines (one name
per line), then the module's functions separated by two blank lines, laid
out by `codeworld.render`'s own statement renderer. The test file holds the
visible failing assert first, then up to two asserts that already pass.
"""

from __future__ import annotations

from ..codeworld.model import FuncDef
from ..codeworld.render import _stmt_lines
from .compose import calls_in


def module_source(import_lines: list[str], defs: list, names: dict, notes: dict | None = None) -> str:
    """The canonical layout of one repository file."""
    lines: list[str] = list(import_lines)
    for i, d in enumerate(defs):
        if lines:
            lines += ["", ""]
        lines += _stmt_lines(d, names, 0, notes or {})
    return "\n".join(lines) + "\n"


def test_path(task) -> str:
    return f"tests/test_{task.entry_module}.py"


def _call(args) -> str:
    return "solve(" + ", ".join(repr(a) for a in args) + ")"


def render_tests(task) -> str:
    lines = [f"from {task.entry_module} import solve"]
    for i, (args, expected) in enumerate(task.tests(), 1):
        lines += ["", "", f"def test_solve_{i}():", f"    assert {_call(args)} == {expected!r}"]
    return "\n".join(lines) + "\n"


def render_repo(task, program, notes: dict) -> dict[str, str]:
    """path -> source for `program` (the buggy or the reference latent)
    under `task`'s partition and names."""
    defs = {s.name: s for s in program.body if isinstance(s, FuncDef)}
    files: dict[str, str] = {}
    for m in sorted(task.order):
        fids = task.order[m]
        imports = sorted({(task.module_names[task.module_of[c]], task.func_names[c])
                          for fid in fids for c in calls_in(defs[fid].body) if task.module_of[c] != m})
        # each def renders with its own locals; function names are repository-wide
        chunks = [module_source([], [defs[fid]], {**task.local_names[fid], **task.func_names}, notes).rstrip("\n")
                  for fid in fids]
        head = "\n".join(f"from {a} import {b}" for a, b in imports)
        files[f"{task.module_names[m]}.py"] = (head + "\n\n\n" if head else "") + "\n\n\n".join(chunks) + "\n"
    files[test_path(task)] = render_tests(task)
    return files
