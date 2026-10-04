"""Deterministic verifier for code-tracing answers.

Truth comes from the interpreter. On a wrong answer the verifier names the
FAILURE SIGNATURE -- which shallow reading the model used:

  * `confused_identifier:<name>` -- the answer is the final value of the
    target's look-alike sibling (checked first; the binding analogue of the
    tool province's `unknown_tool`);
  * `foil:<name>[,<name>...]` -- the answer equals one or more foil readings
    (`value_semantics`, `early_binding`, ...; `python_prior` first when the
    program declares a dialect and the answer is plain Python's);
  * `wrong_value` -- neither;
  * `malformed_answer` / `empty_answer` -- no parseable literal.

The detail code is the part before the first ':' (what `frontier_summary`
keys causes on).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from ..store.schema import Verdict
from .foils import foils_for
from .interpret import FOIL_ERROR, answer_from_trace, run
from .model import Program
from .render import RenderedProgram

_ANSWER_RE = re.compile(r"\bANSWER\s*\**\s*:\s*\**\s*(.+?)\s*\**\s*$", re.IGNORECASE | re.MULTILINE)
_FENCE_RE = re.compile(r"```(?:python|py)?\s*(.*?)\s*```", re.DOTALL)


def _normalise(v):
    if isinstance(v, tuple):
        return [_normalise(x) for x in v]
    if isinstance(v, list):
        return [_normalise(x) for x in v]
    if isinstance(v, dict):
        return {k: _normalise(x) for k, x in v.items()}
    return v


def _literal(text: str):
    t, prev = text.strip(), None
    while t != prev:  # peel backticks, bold markers and a trailing full stop in any order
        prev = t
        t = t.strip().strip("`*").strip()
        if t.endswith("."):
            t = t[:-1]
    try:
        return True, ast.literal_eval(t)
    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
        return False, None


def parse_answer(text: str | None):
    """(ok, value, normalised_tuple). The LAST `ANSWER:` line wins; otherwise
    the last fenced block; otherwise the last non-empty line."""
    if not text or not text.strip():
        return False, None, False
    candidates: list[str] = []
    m = _ANSWER_RE.findall(text)
    if m:
        candidates.append(m[-1])
    else:
        fences = _FENCE_RE.findall(text)
        if fences:
            candidates.append(fences[-1])
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        if lines:
            candidates.append(lines[-1])
    for c in candidates:
        ok, v = _literal(c)
        if ok:
            was_tuple = isinstance(v, tuple)
            return True, _normalise(v), was_tuple
    return False, None, False


@dataclass(frozen=True)
class TraceVerification:
    verdict: Verdict
    detail: str
    parsed: object = None
    truth: object = None
    foil_matches: tuple = ()
    normalised_tuple: bool = False
    extra: dict = field(default_factory=dict)


def verify_answer(text: str | None, program: Program, rendered: RenderedProgram) -> TraceVerification:
    tr = run(program, program.semantics)
    truth = answer_from_trace(program, tr)
    if not text or not text.strip():
        return TraceVerification(Verdict.FAIL, "empty_answer", truth=truth)
    ok, parsed, was_tuple = parse_answer(text)
    if not ok:
        return TraceVerification(Verdict.FAIL, "malformed_answer", truth=truth)
    if parsed == truth and type(parsed) is type(truth):
        return TraceVerification(Verdict.PASS, "correct", parsed=parsed, truth=truth, normalised_tuple=was_tuple)
    foil_matches = tuple(
        name for name, sem in foils_for(program).items()
        if (a := answer_from_trace(program, run(program, sem))) is not FOIL_ERROR and a == parsed
        and type(a) is type(parsed)
    )
    parts: list[str] = []
    target = program.query.target
    twin = rendered.lookalike_of(program, target) if target else None
    if twin is not None and twin in tr.globals and tr.globals[twin] == parsed:
        parts.append(f"confused_identifier:{rendered.name_map[twin]}")
    if foil_matches:
        parts.append("foil:" + ",".join(foil_matches))
    detail = "+".join(parts) if parts else "wrong_value"
    return TraceVerification(Verdict.FAIL, detail, parsed=parsed, truth=truth, foil_matches=foil_matches,
                             normalised_tuple=was_tuple)
