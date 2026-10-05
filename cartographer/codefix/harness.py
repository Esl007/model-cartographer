"""Call a function-form `Program` on input tuples under OUR interpreter (Province V, docs/province-v-spec.md §2, §4.1).

Nothing here executes text: the program is already our AST (`parse.py` or
the generator), and `codeworld.interpret.Interpreter` runs it with its step
cap. Every call gets a fresh interpreter, so calls are independent of input
order (a mutable default shared *within* one call still behaves as Python);
arguments are deep-copied in and the result deep-copied out, so a function
that mutates its argument cannot leak into the caller's input or a later call.

Setup: the program's top-level statements are executed in order up to and
including its LAST top-level `def` (helpers, constants and the entry itself
exist); statements after the last def -- typically a model's own demo call
such as `print(solve([1, 2]))` -- are not run.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from cartographer.codeworld.interpret import STEP_CAP, Closure, InterpError, Interpreter, Semantics, StepCapExceeded
from cartographer.codeworld.model import FuncDef, Program

DEFAULT_ENTRY = "solve"


@dataclass
class HarnessResult:
    value: object
    error: str | None  # "<kind>: <message>" (see `error_kind`), None on success
    steps: int
    stdout: list = field(default_factory=list)  # list[str]

    @property
    def ok(self) -> bool:
        return self.error is None


def _classify(e: BaseException) -> str:
    """A stable short kind for a runtime failure."""
    if isinstance(e, StepCapExceeded):
        return "step_cap"
    if isinstance(e, RecursionError):
        return "recursion"
    if isinstance(e, MemoryError):
        return "memory"
    cause = e.__cause__
    if isinstance(cause, IndexError):
        return "index_error"
    if isinstance(cause, KeyError):
        return "key_error"
    if isinstance(cause, TypeError):
        return "type_error"
    msg = str(e)
    if "wrong number of arguments" in msg:
        return "arity"
    if "is not a function" in msg:
        return "not_callable"
    if "is not defined" in msg or "read before assignment" in msg:
        return "name_error"
    if "bad operands" in msg:
        return "operand_error"
    if "value too large" in msg:
        return "size"
    return "runtime_error"


def error_kind(error: str | None) -> str | None:
    """The kind prefix of a `HarnessResult.error` (None when there is no error)."""
    if error is None:
        return None
    return error.split(":", 1)[0]


def _setup_prefix(program: Program) -> tuple:
    last_def = max((i for i, s in enumerate(program.body) if isinstance(s, FuncDef)), default=-1)
    return program.body[: last_def + 1]


def call_entry(program: Program, args: tuple, semantics: Semantics | None = None,
               step_cap: int = STEP_CAP) -> HarnessResult:
    """Run the program's setup (see module doc), then call `program.entry`
    (default "solve") with positional `args`. Never raises on a program
    error: it is reported in `error`. `semantics=None` = the program's
    declared semantics (the truth). The step cap covers setup and call."""
    sem = program.semantics if semantics is None else semantics
    it = Interpreter(sem, step_cap)
    entry = program.entry or DEFAULT_ENTRY
    stage = "setup"
    try:
        for s in _setup_prefix(program):
            it.exec_top(s)
        stage = "call"
        if not isinstance(it.globals.get(entry), Closure):
            return HarnessResult(None, f"not_callable: no function {entry!r}", it.steps, list(it.stdout))
        value = it.call_function(entry, copy.deepcopy(tuple(args)))
        return HarnessResult(copy.deepcopy(value), None, it.steps, list(it.stdout))
    except (InterpError, RecursionError, MemoryError) as e:
        kind = _classify(e)
        where = "" if stage == "call" else " (during setup)"
        return HarnessResult(None, f"{kind}: {e}{where}", it.steps, list(it.stdout))
    except Exception as e:  # noqa: BLE001 - an interpreter bug must not crash a campaign
        return HarnessResult(None, f"internal_error: {type(e).__name__}: {e}", it.steps, list(it.stdout))


def outputs_on(program: Program, inputs: list, semantics: Semantics | None = None,
               step_cap: int = STEP_CAP) -> list[HarnessResult]:
    """`call_entry` on each input tuple, each in a fresh interpreter."""
    return [call_entry(program, tuple(args), semantics, step_cap) for args in inputs]


def _norm(v):
    if isinstance(v, (tuple, list)):
        return [_norm(x) for x in v]
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    return v


def _strict_equal(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_strict_equal(a[k], b[k]) for k in a)
    return a == b


def values_equal(a, b) -> bool:
    """Python equality after tuple -> list normalisation, type-strict
    (`True != 1`, `1 != "1"`), recursively."""
    return _strict_equal(_norm(a), _norm(b))


def equal_outputs(a: HarnessResult, b: HarnessResult) -> bool:
    """Same returned value (`values_equal`), or both failed with the same
    error kind. stdout is not compared (the unit is the returned value)."""
    if a.error is not None or b.error is not None:
        return a.error is not None and b.error is not None and error_kind(a.error) == error_kind(b.error)
    return values_equal(a.value, b.value)
