"""Bounded interpreter for the codeworld subset -- the province's simulator.

With `Semantics()` (== `TRUE`) it IS Python on the subset; `tests/` checks
that differentially against CPython on thousands of rendered programs. Every
place where a shallow reader's prior might differ from Python is a field of
`Semantics`, so a foil is just this same interpreter with one field flipped
(`foils.py`). Nothing here ever executes text.
"""

from __future__ import annotations

import copy
import operator
from dataclasses import dataclass

from .model import (
    Assign,
    AugAssign,
    BinOp,
    BoolOp,
    Break,
    Call,
    Compare,
    Const,
    Continue,
    DictLit,
    ExprStmt,
    For,
    FuncDef,
    If,
    IfExp,
    Index,
    Len,
    ListLit,
    MethodCall,
    Name,
    Not,
    OpaqueLit,
    Print,
    Program,
    RangeIter,
    Return,
    SetItem,
    Slice,
    Trace,
    While,
    iter_stmts,
)

STEP_CAP = 5_000
# A foil can read a program in a way that makes values explode (a dead
# `items += items` inside a loop under `all_branches`). Past these sizes the
# reading is reported as a crash rather than allowed to eat memory.
MAX_SEQ = 10_000
MAX_INT_BITS = 4_096


def _check_size(v):
    if isinstance(v, (list, str, dict)) and len(v) > MAX_SEQ:
        raise InterpError("value too large")
    if isinstance(v, int) and not isinstance(v, bool) and v.bit_length() > MAX_INT_BITS:
        raise InterpError("value too large")
    return v

_COMPARE = {
    "==": operator.eq, "!=": operator.ne, "<": operator.lt,
    "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}


@dataclass(frozen=True)
class Semantics:
    list_assignment: str = "alias"  # "alias" (Python) | "copy"
    closure_binding: str = "late"  # "late" (Python) | "early"
    range_upper: str = "exclusive"  # "exclusive" (Python) | "inclusive"
    modulo: str = "floor"  # "floor" (Python) | "truncate"
    default_args: str = "shared"  # "shared" (Python) | "fresh"
    scoping: str = "lexical"  # "lexical" (Python) | "flat"
    conditionals: str = "evaluate"  # "evaluate" (Python) | "all_branches"
    loops: str = "iterate"  # "iterate" (Python) | "once"
    literals: str = "copy"  # "copy" (Python) | "pattern" (complete the naming scheme instead)
    # Round-2 beliefs (docs/spec-code-hunt.md WP-G):
    augassign_list: str = "mutate"  # `xs += [..]`: "mutate" (Python) | "rebind" (a new list, like xs = xs + [..])
    list_concat: str = "new"  # `xs = xs + [..]`: "new" (Python) | "mutate" (extends xs in place)
    slice_copy: str = "copy"  # `xs[:]`: "copy" (Python) | "alias" (the same list)
    param_passing: str = "share"  # list arguments: "share" (Python) | "copy" (callee gets a copy)
    loop_predicate: str = "live"  # "live" (Python) | "frozen": a while test, or an if test inside a
    #                                loop, is evaluated once per loop entry and reused
    slice_end: str = "exclusive"  # `xs[a:b]`: "exclusive" (Python) | "inclusive" (b is included)
    path_updates: str = "all"  # "all" (Python) | "skip_one": the last update on the target's
    #                            dependency path (Program.path_sids) is not applied (a stale read)


TRUE = Semantics()

# Each declared dialect rule (model.DIALECT_RULES) is one Semantics field set
# to its non-Python value -- the same values the matching foil uses.
DIALECT_SEMANTICS: dict[str, tuple[str, str]] = {
    "inclusive_range": ("range_upper", "inclusive"),
    "truncating_modulo": ("modulo", "truncate"),
    "copy_on_assign": ("list_assignment", "copy"),
    "fresh_defaults": ("default_args", "fresh"),
}


def semantics_for(dialect=()) -> Semantics:
    """The semantics a program with this declared dialect runs under
    (`TRUE` for plain Python)."""
    if not dialect:
        return TRUE
    return Semantics(**dict(DIALECT_SEMANTICS[r] for r in dialect))


class InterpError(Exception):
    """The program did something outside the subset or raised at runtime."""


class StepCapExceeded(InterpError):
    pass


class _FoilError:
    """Sentinel answer for a foil whose reading of the program crashes. Never
    equal to any real value, so a crashing foil always 'diverges'."""

    def __repr__(self) -> str:
        return "<foil-error>"

    def __eq__(self, other) -> bool:
        return other is self

    def __hash__(self) -> int:
        return 0


FOIL_ERROR = _FoilError()
_MISSING = object()


class _Return(Exception):
    def __init__(self, value):
        self.value = value


class _Break(Exception):
    """`break`: caught by the innermost enclosing for / while."""


class _Continue(Exception):
    """`continue`: caught by the innermost enclosing for / while."""


_LOOP_EXITS = (_Break, _Continue)


def _membership(op: str, a, b) -> bool:
    """`a in b` / `a not in b` on a list, str or dict (dict: key membership)."""
    if not isinstance(b, (list, str, dict)):
        raise InterpError(f"compare {op}: not a container") from TypeError(
            f"argument of type {type(b).__name__!r} is not iterable")
    try:
        r = a in b
    except TypeError as err:
        raise InterpError(f"compare {op}: {err}") from err
    return r if op == "in" else not r


@dataclass
class Closure:
    defn: FuncDef
    localnames: frozenset
    default_values: tuple
    captured: dict | None = None  # early-binding snapshot of globals


@dataclass
class _Frame:
    vars: dict
    localnames: frozenset = frozenset()
    closure: Closure | None = None
    is_global: bool = True


def _assigned_names(body) -> set[str]:
    out: set[str] = set()
    for s in iter_stmts(body):
        if isinstance(s, (Assign, AugAssign)):
            out.add(s.target)
        elif isinstance(s, For):
            out.add(s.var)
    return out


def _trunc_divmod(a: int, b: int) -> tuple[int, int]:
    q = abs(a) // abs(b)
    if (a < 0) != (b < 0):
        q = -q
    return q, a - b * q


class Interpreter:
    def __init__(self, sem: Semantics = TRUE, step_cap: int = STEP_CAP) -> None:
        self.sem = sem
        self.step_cap = step_cap
        self.globals: dict = {}
        self.stdout: list[str] = []
        self.steps = 0
        self._gframe = _Frame(self.globals)
        self._frozen: list[dict] = []  # loop_predicate="frozen": one test cache per active loop

    # -- statements ---------------------------------------------------------

    def exec_top(self, stmt) -> None:
        try:
            self._exec(stmt, self._gframe)
        except _LOOP_EXITS as e:
            raise InterpError(f"{type(e).__name__[1:].lower()} outside loop") from None

    def _tick(self) -> None:
        self.steps += 1
        if self.steps > self.step_cap:
            raise StepCapExceeded(f"step cap {self.step_cap} exceeded")

    def _exec_block(self, body, frame: _Frame) -> None:
        for s in body:
            self._exec(s, frame)

    def _store(self, frame: _Frame, name: str, value) -> None:
        frame.vars[name] = _check_size(value)

    def _exec(self, s, frame: _Frame) -> None:
        self._tick()
        if isinstance(s, Assign):
            if (
                self.sem.list_concat == "mutate" and isinstance(s.value, BinOp) and s.value.op == "+"
                and isinstance(s.value.left, Name) and s.value.left.id == s.target
                and isinstance(self._read(s.target, frame), list)
            ):
                cur = self._read(s.target, frame)
                ext = self._eval(s.value.right, frame)
                if not isinstance(ext, list):
                    raise InterpError("list + non-list")
                cur.extend(ext)
                self._store(frame, s.target, cur)
                return
            v = self._eval(s.value, frame)
            if (
                self.sem.list_assignment == "copy"
                and isinstance(s.value, Name)
                and isinstance(v, (list, dict))
            ):
                v = copy.deepcopy(v)
            self._store(frame, s.target, v)
        elif isinstance(s, AugAssign):
            cur = self._read(s.target, frame)
            v = self._eval(s.value, frame)
            if s.op == "+" and isinstance(cur, list):
                if not isinstance(v, list):
                    raise InterpError("list += non-list")
                if self.sem.augassign_list == "rebind":
                    self._store(frame, s.target, cur + v)
                else:
                    cur.extend(v)  # in place, exactly like Python
                    self._store(frame, s.target, cur)
            else:
                self._store(frame, s.target, self._binop(s.op, cur, v))
        elif isinstance(s, SetItem):
            obj = self._read(s.target, frame)
            idx = self._eval(s.index, frame)
            val = self._eval(s.value, frame)
            try:
                obj[idx] = val
            except (IndexError, KeyError, TypeError) as e:
                raise InterpError(f"setitem: {e}") from e
        elif isinstance(s, If):
            if self.sem.conditionals == "all_branches":
                self._exec_block(s.body, frame)
                self._exec_block(s.orelse, frame)
            elif self._truthy(self._test(s, frame)):
                self._exec_block(s.body, frame)
            else:
                self._exec_block(s.orelse, frame)
        elif isinstance(s, For):
            if isinstance(s.iter, RangeIter):
                lo = 0 if s.iter.lo is None else self._bound(s.iter.lo, frame)
                hi = self._bound(s.iter.hi, frame) + (1 if self.sem.range_upper == "inclusive" else 0)
                seq = range(lo, hi)
            else:
                seq = self._eval(s.iter, frame)
                if not isinstance(seq, (list, str)):
                    raise InterpError("for over non-sequence")
            self._frozen.append({})
            try:
                for i, item in enumerate(seq):
                    if self.sem.loops == "once" and i >= 1:
                        break
                    self._store(frame, s.var, item)
                    try:
                        self._exec_block(s.body, frame)
                    except _Break:
                        break
                    except _Continue:
                        continue
            finally:
                self._frozen.pop()
        elif isinstance(s, While):
            frozen = self.sem.loop_predicate == "frozen"  # test read once: a true test never turns false
            cond = self._truthy(self._eval(s.test, frame))
            i = 0
            self._frozen.append({})
            try:
                while cond:
                    if self.sem.loops == "once" and i >= 1:
                        break
                    self._tick()
                    try:
                        self._exec_block(s.body, frame)
                    except _Break:
                        break
                    except _Continue:
                        pass
                    i += 1
                    if not frozen:
                        cond = self._truthy(self._eval(s.test, frame))
            finally:
                self._frozen.pop()
        elif isinstance(s, FuncDef):
            localnames = frozenset(s.params) | frozenset(_assigned_names(s.body))
            defaults = tuple(self._eval(d, self._gframe) for d in s.defaults)
            captured = None
            if self.sem.closure_binding == "early":
                captured = {
                    k: copy.deepcopy(v) for k, v in self.globals.items() if not isinstance(v, Closure)
                }
            self._store(frame, s.name, Closure(s, localnames, defaults, captured))
        elif isinstance(s, Return):
            raise _Return(self._eval(s.value, frame))
        elif isinstance(s, ExprStmt):
            self._eval(s.value, frame)
        elif isinstance(s, Print):
            self.stdout.append(str(self._eval(s.value, frame)))
        elif isinstance(s, Break):
            raise _Break()
        elif isinstance(s, Continue):
            raise _Continue()
        else:  # pragma: no cover - generator never emits anything else
            raise InterpError(f"unknown statement {type(s).__name__}")

    # -- expressions ----------------------------------------------------------

    def _test(self, s, frame: _Frame):
        """An if's test; under loop_predicate="frozen", inside a loop it is
        evaluated on the first pass only and reused on every later pass."""
        if self.sem.loop_predicate != "frozen" or not self._frozen:
            return self._eval(s.test, frame)
        cache = self._frozen[-1]
        if s.sid not in cache:
            cache[s.sid] = self._eval(s.test, frame)
        return cache[s.sid]

    def _bound(self, b, frame: _Frame) -> int:
        """A `range` bound: an int literal as is, an expression evaluated
        (it must be an int at run time, exactly like Python's `range`)."""
        if isinstance(b, int):
            return b
        v = self._eval(b, frame)
        if not isinstance(v, int):
            raise InterpError(f"range bound is {type(v).__name__}, not int") from TypeError(
                f"{type(v).__name__!r} object cannot be interpreted as an integer")
        return v

    def _read(self, name: str, frame: _Frame):
        if not frame.is_global:
            if name in frame.localnames:
                if name not in frame.vars:
                    raise InterpError(f"local {name!r} read before assignment")
                return frame.vars[name]
            cl = frame.closure
            if cl is not None and cl.captured is not None and name in cl.captured:
                return cl.captured[name]
        v = self.globals.get(name, _MISSING)
        if v is _MISSING:
            raise InterpError(f"name {name!r} is not defined")
        return v

    @staticmethod
    def _truthy(v) -> bool:
        return bool(v)

    def _binop(self, op: str, a, b):
        try:
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op in ("//", "%"):
                if not isinstance(a, int) or not isinstance(b, int) or b == 0:
                    raise InterpError(f"bad operands for {op}")
                if self.sem.modulo == "truncate":
                    q, r = _trunc_divmod(a, b)
                else:
                    q, r = divmod(a, b)
                return q if op == "//" else r
        except TypeError as e:
            raise InterpError(f"binop {op}: {e}") from e
        raise InterpError(f"unknown op {op}")

    def _eval(self, e, frame: _Frame):
        if isinstance(e, Const):
            return e.value
        if isinstance(e, OpaqueLit):
            return e.pattern if self.sem.literals == "pattern" else e.value
        if isinstance(e, ListLit):
            return [self._eval(x, frame) for x in e.items]
        if isinstance(e, DictLit):
            return {k: self._eval(v, frame) for k, v in zip(e.keys, e.values)}
        if isinstance(e, Name):
            return self._read(e.id, frame)
        if isinstance(e, BinOp):
            return self._binop(e.op, self._eval(e.left, frame), self._eval(e.right, frame))
        if isinstance(e, Compare):
            a, b = self._eval(e.left, frame), self._eval(e.right, frame)
            if e.op in ("in", "not in"):
                return _membership(e.op, a, b)
            try:
                return _COMPARE[e.op](a, b)
            except TypeError as err:
                raise InterpError(f"compare: {err}") from err
        if isinstance(e, BoolOp):
            a = self._eval(e.left, frame)
            if e.op == "and":
                return a if not a else self._eval(e.right, frame)
            return a if a else self._eval(e.right, frame)
        if isinstance(e, Not):
            return not self._eval(e.operand, frame)
        if isinstance(e, Index):
            obj, idx = self._eval(e.target, frame), self._eval(e.index, frame)
            try:
                return obj[idx]
            except (IndexError, KeyError, TypeError) as err:
                raise InterpError(f"index: {err}") from err
        if isinstance(e, Slice):
            obj = self._eval(e.target, frame)
            lo = None if e.lo is None else self._eval(e.lo, frame)
            hi = None if e.hi is None else self._eval(e.hi, frame)
            if self.sem.slice_copy == "alias" and lo is None and hi is None and isinstance(obj, list):
                return obj
            if self.sem.slice_end == "inclusive" and isinstance(hi, int):
                hi = None if hi == -1 else hi + 1
            return obj[lo:hi]
        if isinstance(e, Len):
            v = self._eval(e.arg, frame)
            if not isinstance(v, (list, str, dict)):
                raise InterpError("len of non-sized")
            return len(v)
        if isinstance(e, IfExp):
            return self._eval(e.body if self._truthy(self._eval(e.test, frame)) else e.orelse, frame)
        if isinstance(e, Call):
            fn = self._read(e.func, frame)
            if not isinstance(fn, Closure):
                raise InterpError(f"{e.func} is not a function")
            return self._call(fn, [self._eval(a, frame) for a in e.args])
        if isinstance(e, MethodCall):
            obj = self._read(e.obj, frame)
            args = [self._eval(a, frame) for a in e.args]
            try:
                if e.method == "append" and isinstance(obj, list):
                    obj.append(*args)
                    _check_size(obj)
                    return None
                if e.method == "pop" and isinstance(obj, list):
                    return obj.pop(*args)
                if e.method == "extend" and isinstance(obj, list):
                    obj.extend(*args)
                    _check_size(obj)
                    return None
                if e.method == "get" and isinstance(obj, dict):
                    return obj.get(*args)
                if e.method == "upper" and isinstance(obj, str):
                    return obj.upper()
                if e.method == "replace" and isinstance(obj, str):
                    return obj.replace(*args)
            except (IndexError, TypeError) as err:
                raise InterpError(f"method {e.method}: {err}") from err
            raise InterpError(f"method {e.method} not allowed on {type(obj).__name__}")
        raise InterpError(f"unknown expression {type(e).__name__}")

    def _call(self, fn: Closure, argvals: list):
        fd = fn.defn
        n_params, n_def = len(fd.params), len(fd.defaults)
        if not n_params - n_def <= len(argvals) <= n_params:
            raise InterpError(f"{fd.name}: wrong number of arguments")
        bound: dict = {}
        for i, p in enumerate(fd.params):
            if i < len(argvals):
                a = argvals[i]
                bound[p] = copy.deepcopy(a) if self.sem.param_passing == "copy" and isinstance(a, list) else a
            else:
                di = i - (n_params - n_def)
                if self.sem.default_args == "fresh":
                    bound[p] = self._eval(fd.defaults[di], self._gframe)
                else:
                    bound[p] = fn.default_values[di]
        if self.sem.scoping == "flat":
            self.globals.update(bound)
            frame = self._gframe
        else:
            frame = _Frame(bound, fn.localnames, fn, is_global=False)
        try:
            self._exec_block(fd.body, frame)
        except _Return as r:
            return r.value
        except _LOOP_EXITS as e:  # a loop exit never crosses a call boundary
            raise InterpError(f"{type(e).__name__[1:].lower()} outside loop") from None
        return None

    def call_function(self, name: str, args) -> object:
        """Call the top-level def `name` with positional `args` (Province V
        harness entry point). Raises InterpError if `name` is not a function,
        on a wrong arity, on a runtime error, or past the step cap."""
        fn = self.globals.get(name, _MISSING)
        if not isinstance(fn, Closure):
            raise InterpError(f"{name} is not a function")
        try:
            return self._call(fn, list(args))
        except _Return:  # pragma: no cover - _call catches its own returns
            raise InterpError("return outside function")

    # -- snapshots ------------------------------------------------------------

    def data_globals(self) -> dict:
        return {k: v for k, v in self.globals.items() if not isinstance(v, Closure)}

    def alias_pairs(self) -> set:
        seen: dict[int, list[str]] = {}
        for k, v in self.globals.items():
            if isinstance(v, (list, dict)):
                seen.setdefault(id(v), []).append(k)
        out = set()
        for names in seen.values():
            names = sorted(names)
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    out.add((names[i], names[j]))
        return out


def run(program: Program, semantics: Semantics | None = None, step_cap: int = STEP_CAP) -> Trace:
    """Execute the whole program. Never raises on program errors: a crash is
    reported in `Trace.error` (foils crash legitimately). `semantics=None`
    means the program's own declared semantics (`program.semantics`)."""
    it = Interpreter(program.semantics if semantics is None else semantics, step_cap)
    target = program.query.target
    track: list = []
    aliases: set = set()
    error = None
    skip = None
    if it.sem.path_updates == "skip_one" and program.path_sids:
        skip = program.path_sids[-1]
    try:
        for s in program.body:
            if s.sid == skip:
                if target is not None:
                    track.append(copy.deepcopy(it.globals.get(target, None)))
                continue
            it.exec_top(s)
            if target is not None:
                track.append(copy.deepcopy(it.globals.get(target, None)))
            aliases |= it.alias_pairs()
    except _Return:
        error = "return outside function"
    except InterpError as e:
        error = str(e)
    except (RecursionError, MemoryError) as e:  # pragma: no cover
        error = type(e).__name__
    return Trace(
        globals=copy.deepcopy(it.data_globals()),
        stdout=list(it.stdout),
        steps=it.steps,
        target_track=track,
        alias_pairs=aliases,
        error=error,
    )


def answer_from_trace(program: Program, trace: Trace):
    if trace.error is not None:
        return FOIL_ERROR
    if program.query.kind == "stdout":
        return list(trace.stdout)
    return trace.globals.get(program.query.target, FOIL_ERROR)


def answer(program: Program, semantics: Semantics | None = None):
    """The answer to the program's query under `semantics` (default: the
    program's declared semantics, i.e. the truth); `FOIL_ERROR` if that
    reading of the program crashes."""
    return answer_from_trace(program, run(program, semantics))
