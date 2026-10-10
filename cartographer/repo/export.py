"""Export a repository task as a real package (docs/province-vi-spec.md §9):
the releasable benchmark. Anyone with CPython and pytest can run it -- the
generator, the interpreter and this project are not needed to check a fix.

    <dir>/
      <module>.py ...          the repository (buggy, or reference with `which="reference"`)
      tests/test_<entry>.py    the visible tests
      conftest.py              empty; puts <dir> on sys.path under pytest
      expected_hidden.json     hidden inputs and the expected outputs (the real grading)
      README.md                seed, parameters, how to grade
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .compose import RepoTask, repo_execute


def _jsonable(v):
    if isinstance(v, tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


def export_repo(task: RepoTask, directory: str | Path, which: str = "buggy") -> Path:
    if which not in ("buggy", "reference"):
        raise ValueError("which must be 'buggy' or 'reference'")
    root = Path(directory)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    files = task.files if which == "buggy" else task.reference_files
    for path, src in files.items():
        (root / path).write_text(src)
    (root / "conftest.py").write_text("")
    hidden = [{"args": _jsonable(list(a)), "expected": _jsonable(repo_execute(task.reference, a)[0])}
              for a in task.hidden_inputs]
    (root / "expected_hidden.json").write_text(json.dumps({
        "entry": f"{task.entry_module}.solve",
        "visible": {"args": _jsonable(list(task.visible_input)), "expected": _jsonable(task.visible_expected)},
        "hidden": hidden,
    }, indent=1))
    params = {k: v for k, v in asdict(task.params).items()}
    (root / "README.md").write_text(
        f"# Province VI repository (seed {task.seed}, {which})\n\n"
        f"A test in `tests/` fails. The fix is graded by calling `{task.entry_module}.solve` on every input in "
        "`expected_hidden.json` (and the visible one) and comparing to `expected` with `==` (True != 1).\n\n"
        f"Parameters: `{json.dumps(params)}`\n")
    return root


# Run under CPython in a separate process by `cpython_outputs`: imports each exported
# repository fresh and calls `solve` on its inputs. Only ever run on repositories WE generated.
_PROBE = r'''
import importlib, json, sys
out = {}
for d, entry, inputs in json.load(sys.stdin):
    sys.path.insert(0, d)
    res = []
    try:
        solve = importlib.import_module(entry).solve
    except Exception as e:
        res = [["error", "import"]] * len(inputs)
    else:
        for a in inputs:
            try:
                res.append(["ok", solve(*a)])
            except Exception as e:
                res.append(["error", type(e).__name__])
    out[d] = res
    sys.path.remove(d)
    for m in list(sys.modules):  # the next repository reuses module names: forget this one's
        f = getattr(sys.modules[m], "__file__", None)
        if f and str(f).startswith(d + "/"):
            del sys.modules[m]
print(json.dumps(out))
'''


def cpython_outputs(jobs: list[tuple[str, str, list]], timeout: float = 300.0) -> dict:
    """{dir: [["ok", value] | ["error", ExcName], ...]} by running CPython on
    exported repositories. `jobs`: (dir, entry module, [args, ...])."""
    import subprocess
    import sys

    payload = json.dumps([[str(d), e, [_jsonable(list(a)) for a in ins]] for d, e, ins in jobs])
    r = subprocess.run([sys.executable, "-c", _PROBE], input=payload, capture_output=True, text=True,
                       timeout=timeout, check=True)
    return json.loads(r.stdout)
