"""Province V verifier (docs/province-v-spec.md §6) and prompt rendering (§5).

A submission is parsed by `parse.parse_program` (never executed as Python)
and run by our interpreter on the visible and hidden inputs."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field, fields

from ..codeworld.foils import foils_for
from ..codeworld.model import FuncDef, iter_stmts
from ..store.schema import Verdict
from .parse import CodeParseError, SubsetViolation, parse_program
from .tasks import RepairTask, WriteTask, outputs

_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class FixVerification:
    verdict: Verdict
    detail: str
    parsed: bool
    diff_sids: tuple = ()
    extra: dict = field(default_factory=dict)


def last_code_block(text: str) -> str | None:
    blocks = _FENCE_RE.findall(text)
    return blocks[-1][1] if blocks else None


def _shape(x):
    """A statement / expression with sids dropped (structural identity)."""
    if isinstance(x, tuple):
        return tuple(_shape(i) for i in x)
    if hasattr(x, "__dataclass_fields__"):
        return (type(x).__name__,) + tuple(
            _shape(getattr(x, f.name)) for f in fields(x) if f.name not in ("sid", "body", "orelse"))
    return x


def _flat(program) -> list:
    return [s for s in iter_stmts(program.body)]


def diff_sids(buggy_parsed, buggy_latent, submission) -> tuple:
    """Sids (of the latent buggy program) of statements the submission
    changed, inserted next to, or removed. The parsed and latent buggy
    programs flatten to the same statement sequence."""
    a = [_shape(s) for s in _flat(buggy_parsed)]
    b = [_shape(s) for s in _flat(submission)]
    sids = [s.sid for s in _flat(buggy_latent)]
    if len(sids) != len(a):  # pragma: no cover - parser round-trip guarantees equal length
        return ()
    out = set()
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            out.update(sids[i1:i2])
        else:  # pure insertion: attribute to the statement it precedes
            out.add(sids[min(i1, len(sids) - 1)])
    return tuple(sorted(out))


def _entry_sig(program):
    for s in program.body:
        if isinstance(s, FuncDef) and s.name == program.entry:
            return len(s.params), len(s.defaults)
    return None


def _parse(text: str | None):
    """(program, FixVerification-or-None)."""
    if text is None or not text.strip():
        return None, FixVerification(Verdict.FAIL, "empty_answer", False)
    src = last_code_block(text)
    if src is None:
        return None, FixVerification(Verdict.FAIL, "no_code_block", False)
    try:
        prog = parse_program(src, entry="solve")
    except SubsetViolation as e:
        return None, FixVerification(Verdict.ERROR, f"outside_subset:{e.construct}", False,
                                     extra={"lineno": e.lineno})
    except CodeParseError:
        return None, FixVerification(Verdict.FAIL, "parse_error", False)
    if _entry_sig(prog) is None:
        return None, FixVerification(Verdict.FAIL, "signature_changed", True)
    return prog, None


def verify_submission(text: str | None, task) -> FixVerification:
    if isinstance(task, WriteTask):
        return verify_write(text, task)
    prog, early = _parse(text)
    if early is not None:
        return early
    want = len(next(s for s in task.reference.body if isinstance(s, FuncDef) and s.name == "solve").params)
    if _entry_sig(prog)[0] != want:
        return FixVerification(Verdict.FAIL, "signature_changed", True)
    inputs = [task.visible_input] + list(task.hidden_inputs)
    got = outputs(prog, inputs)
    ref = outputs(task.reference, inputs)
    bug = outputs(task.buggy, inputs)
    buggy_parsed = parse_program(task.buggy_source, entry="solve")
    diff = diff_sids(buggy_parsed, task.buggy, prog)
    extra = {"diff_lines": len(diff)}
    if got == ref:
        return FixVerification(Verdict.PASS, "pass", True, diff, extra)
    if got[0] == ref[0]:
        return FixVerification(Verdict.FAIL, "visible_fixed_hidden_broken", True, diff, extra)
    if got == bug:
        return FixVerification(Verdict.FAIL, "no_change", True, diff, extra)
    for name, sem in sorted(foils_for(task.reference).items()):
        foil = outputs(task.reference, inputs, sem)
        if foil != ref and foil == got:
            return FixVerification(Verdict.FAIL, f"introduced:{name}", True, diff, extra)
    # a crash is named as a crash first (deviation from the §6 list order: a submission that
    # crashes everywhere would otherwise read as `regressed` / `wrong_site`)
    err = next((g for g in got if g[0] == "error"), None)
    if err is not None:
        return FixVerification(Verdict.FAIL, f"runtime_error:{err[1]}", True, diff, extra)
    if diff and not set(diff) & set(task.bug_sites):
        return FixVerification(Verdict.FAIL, "wrong_site", True, diff, extra)
    if any(g != r and b == r for g, r, b in zip(got, ref, bug)):
        return FixVerification(Verdict.FAIL, "regressed", True, diff, extra)
    return FixVerification(Verdict.FAIL, "wrong_value", True, diff, extra)


def verify_write(text: str | None, task: WriteTask) -> FixVerification:
    prog, early = _parse(text)
    if early is not None:
        return early
    want = len(task.reference.param_kinds)
    if _entry_sig(prog)[0] != want:
        return FixVerification(Verdict.FAIL, "signature_changed", True)
    inputs = [a for a, _ in task.examples] + list(task.hidden_inputs)
    got = outputs(prog, inputs)
    ref = outputs(task.reference, inputs)
    if got == ref:
        return FixVerification(Verdict.PASS, "pass", True)
    n_ex = len(task.examples)
    edge_fail = [c for c, g, r in zip(task.hidden_edge_cases, got[n_ex:], ref[n_ex:]) if g != r and c != "random"]
    if got[:n_ex] == ref[:n_ex]:
        return FixVerification(Verdict.FAIL, "edge_case:" + edge_fail[0] if edge_fail else "examples_only", True)
    err = next((g for g in got if g[0] == "error"), None)
    if err is not None:
        return FixVerification(Verdict.FAIL, f"runtime_error:{err[1]}", True)
    return FixVerification(Verdict.FAIL, "wrong_value", True)


# -- prompts (§5) -----------------------------------------------------------------

def _call(args) -> str:
    return "solve(" + ", ".join(repr(a) for a in args) + ")"


def render_repair_prompt(task: RepairTask) -> str:
    return (
        "Here is a Python function and one example where it returns the wrong value.\n\n"
        f"```python\n{task.buggy_source}```\n\n"
        f"`{_call(task.visible_input)}` returns `{task.visible_wrong!r}`, but it should return "
        f"`{task.visible_expected!r}`.\n\n"
        "Find and fix the bug. The fix will also be checked on other inputs. Return the corrected function, "
        "complete (with any helper functions it uses), in one ```python block. Keep the same name and parameters."
    )


def render_trace_prompt(task: RepairTask):
    """(program, RenderedProgram) of the paired /trace query: `trace_program`
    rendered by `codeworld.render.render(program, task.seed)` exactly as the
    round-2 `CodeTraceEvaluator` renders its programs."""
    from ..codeworld.render import render
    from .tasks import trace_program

    program = trace_program(task)
    return program, render(program, task.seed)


def render_write_prompt(task: WriteTask) -> str:
    ex = "\n".join(f"- `{_call(a)}` returns `{out!r}`" for a, out in task.examples)
    return (
        f"Write `def solve(...)` so that the following holds. {task.spec_text}\n\nExamples:\n{ex}\n\n"
        "Your function will also be checked on other inputs, including edge cases. Use plain Python only "
        "(no imports). Return only the function (and any helpers) in one ```python block."
    )


__all__ = ["FixVerification", "diff_sids", "last_code_block", "render_repair_prompt", "render_trace_prompt", "render_write_prompt",
           "verify_submission", "verify_write"]
