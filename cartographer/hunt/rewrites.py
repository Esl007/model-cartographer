"""Meaning-preserving rewrites of a codeworld `Program` (spec-code-hunt WP-H 1).

A model that passes a program and fails one of these rewrites has a
CONSISTENCY failure: the answer is the same by construction (and asserted),
only the surface form moved (Spiess et al. 2604.16320; Orvalho &
Kwiatkowska 2505.10443).

  * `swap_operands` -- `a + b` -> `b + a` / `a * b` -> `b * a` on ints,
    `a == b` -> `b == a` (and `!=`);
  * `negate_if` -- `if c: A else: B` -> `if not c: B else: A` (only with an else);
  * `unroll_first_iteration` -- `for i in range(lo, hi): body` (>= 2
    iterations) -> `i = lo; body; for i in range(lo + 1, hi): body`;
  * `insert_dead_read` -- a new, never-read variable assigned from the query
    target mid-program;
  * `rename_variable` -- the same program under a different rendering seed
    (names are a rendering property), returned as `(program, rendering_seed)`.

Every rewrite takes `(program, seed)`; `seed` picks the site when there are
several. Each checks, on the result: no runtime error under the program's
own semantics, the same answer (value AND type), and the same set of
load-bearing trap foils (a trap that changed the answer still does, a
disabled one still does not). Anything else raises `RewriteUnavailable`.

The AST walk is generic over dataclass fields, so node types this module
does not know (a future `While`, new expression nodes) are carried through
untouched rather than rejected.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import fields, is_dataclass, replace
from typing import Callable

from ..codeworld.foils import ALL_TRAP_FOILS, foils_for
from ..codeworld.interpret import answer_from_trace, run
from ..codeworld.model import (
    Assign,
    BinOp,
    Compare,
    Const,
    For,
    If,
    Len,
    Name,
    Not,
    Program,
    RangeIter,
    iter_stmts,
)
from ..codeworld.mutate import MutationUnavailable
from ..codeworld.render import render

MAX_STEPS = 2_000  # generate.MAX_GEN_STEPS


class RewriteUnavailable(MutationUnavailable):
    """This rewrite has no site on this program, or could not hold its invariant."""


def _pick(seed: int, tag: str, n: int, salt: object = "") -> list[int]:
    """A deterministic permutation of range(n) keyed on (seed, tag, salt)."""
    def key(i: int) -> str:
        return hashlib.sha256(f"{tag}|{seed}|{salt}|{i}".encode()).hexdigest()
    return sorted(range(n), key=key)


# ---------------------------------------------------------------------------
# generic AST walking
# ---------------------------------------------------------------------------


def _children(x):
    if isinstance(x, tuple):
        return list(x)
    if is_dataclass(x) and not isinstance(x, type):
        return [getattr(x, f.name) for f in fields(x)]
    return []


def _preorder(x):
    yield x
    for c in _children(x):
        yield from _preorder(c)


def _transform(x, visit):
    """Rebuild `x` bottom-up after giving `visit` a chance to replace any node
    (pre-order). `visit` returns None to keep descending."""
    r = visit(x)
    if r is not None:
        return r
    if isinstance(x, tuple):
        new = tuple(_transform(y, visit) for y in x)
        return new if any(a is not b for a, b in zip(new, x)) else x
    if is_dataclass(x) and not isinstance(x, type):
        changes = {}
        for f in fields(x):
            v = getattr(x, f.name)
            nv = _transform(v, visit)
            if nv is not v:
                changes[f.name] = nv
        return replace(x, **changes) if changes else x
    return x


def _replace_nth(body: tuple, pred: Callable, n: int, make: Callable) -> tuple:
    """Replace the n-th node (pre-order) matching `pred` with `make(node)`."""
    count = [0]

    def visit(x):
        if pred(x):
            hit = count[0] == n
            count[0] += 1
            if hit:
                return make(x)
        return None

    return _transform(body, visit)


def _all_sids(body) -> list[int]:
    out = []
    for x in _preorder(body):
        if is_dataclass(x) and not isinstance(x, type) and any(f.name == "sid" for f in fields(x)):
            out.append(x.sid)
    return out


def _resid(node, start: int):
    """A copy of `node` with every statement's sid renumbered from `start`."""
    nxt = [start]

    def visit(x):
        if is_dataclass(x) and not isinstance(x, type) and any(f.name == "sid" for f in fields(x)):
            changes = {}
            for f in fields(x):
                if f.name == "sid":
                    continue
                v = getattr(x, f.name)
                nv = _transform(v, visit)
                if nv is not v:
                    changes[f.name] = nv
            sid = nxt[0]
            nxt[0] += 1
            return replace(x, sid=sid, **changes)
        return None

    return _transform(node, visit), nxt[0]


# ---------------------------------------------------------------------------
# invariant check
# ---------------------------------------------------------------------------


def _same(a, b) -> bool:
    return a == b and type(a) is type(b)


def _trap_pattern(program: Program, truth) -> dict:
    readings = foils_for(program)
    out = {}
    for trap, foil in ALL_TRAP_FOILS.items():
        if foil in readings:
            out[trap] = not _same(answer_from_trace(program, run(program, readings[foil])), truth)
    return out


def check_preserved(old: Program, new: Program, name: str) -> Program:
    """Raise `RewriteUnavailable` unless `new` means what `old` means."""
    tr_old = run(old)
    tr_new = run(new)
    if tr_new.error is not None:
        raise RewriteUnavailable(f"{name}: runtime error {tr_new.error}")
    if tr_new.steps > max(MAX_STEPS, tr_old.steps):
        raise RewriteUnavailable(f"{name}: too many steps")
    t_old, t_new = answer_from_trace(old, tr_old), answer_from_trace(new, tr_new)
    if not _same(t_old, t_new):
        raise RewriteUnavailable(f"{name} changed the answer")
    if _trap_pattern(old, t_old) != _trap_pattern(new, t_new):
        raise RewriteUnavailable(f"{name} changed which traps are load-bearing")
    try:
        render(new, new.seed)
    except (KeyError, TypeError) as e:
        raise RewriteUnavailable(f"{name}: does not render ({e})") from e
    return new


# ---------------------------------------------------------------------------
# the rewrites
# ---------------------------------------------------------------------------


def _int_like(e, program: Program) -> bool:
    if isinstance(e, Const):
        return isinstance(e.value, int) and not isinstance(e.value, bool)
    if isinstance(e, Len):
        return True
    if isinstance(e, Name):
        v = program.var(e.id)
        return v is not None and v.kind == "int"
    if isinstance(e, BinOp) and e.op in ("-", "//", "%", "*", "+"):
        if e.op in ("//", "%", "-"):
            return _int_like(e.left, program) or _int_like(e.right, program)
        return _int_like(e.left, program) and _int_like(e.right, program)
    return False


def _swappable(program: Program):
    def pred(x) -> bool:
        if isinstance(x, BinOp) and x.op in ("+", "*"):
            return x.left != x.right and (_int_like(x.left, program) or _int_like(x.right, program))
        if isinstance(x, Compare) and x.op in ("==", "!="):
            return x.left != x.right
        return False
    return pred


def swap_operands(program: Program, seed: int = 0) -> Program:
    pred = _swappable(program)
    n = sum(1 for x in _preorder(program.body) if pred(x))
    if not n:
        raise RewriteUnavailable("no commutative int operation or ==/!= to swap")
    for k in _pick(seed, "swap_operands", n, program.seed):
        body = _replace_nth(program.body, pred, k, lambda x: replace(x, left=x.right, right=x.left))
        try:
            return check_preserved(program, replace(program, body=body), "swap_operands")
        except RewriteUnavailable:
            continue
    raise RewriteUnavailable("no swap preserved the meaning")


def _negatable(x) -> bool:
    return isinstance(x, If) and bool(x.orelse) and bool(x.body)


def negate_if(program: Program, seed: int = 0) -> Program:
    n = sum(1 for x in _preorder(program.body) if _negatable(x))
    if not n:
        raise RewriteUnavailable("no if/else to negate")

    def flip(x: If) -> If:
        test = x.test.operand if isinstance(x.test, Not) else Not(x.test)
        return replace(x, test=test, body=x.orelse, orelse=x.body)

    for k in _pick(seed, "negate_if", n, program.seed):
        body = _replace_nth(program.body, _negatable, k, flip)
        try:
            return check_preserved(program, replace(program, body=body), "negate_if")
        except RewriteUnavailable:
            continue
    raise RewriteUnavailable("no negation preserved the meaning")


def _unrollable(x) -> bool:
    if not (isinstance(x, For) and isinstance(x.iter, RangeIter) and x.body):
        return False
    lo = 0 if x.iter.lo is None else x.iter.lo
    return x.iter.hi - lo >= 2


def _splice_nth(body: tuple, pred: Callable, n: int, make: Callable) -> tuple:
    """Like `_replace_nth`, but `make` returns a LIST of statements spliced
    into the enclosing block in place of the matched statement."""
    count = [0]

    def visit(x):
        if isinstance(x, tuple) and any(pred(y) for y in x):
            out, changed = [], False
            for y in x:
                if pred(y):
                    hit = count[0] == n
                    count[0] += 1
                    if hit:
                        out.extend(make(y))
                        changed = True
                        continue
                out.append(_transform(y, visit))
            return tuple(out) if changed or any(a is not b for a, b in zip(out, x)) else x
        return None

    return _transform(body, visit)


def unroll_first_iteration(program: Program, seed: int = 0) -> Program:
    n = sum(1 for x in _preorder(program.body) if _unrollable(x))
    if not n:
        raise RewriteUnavailable("no for-range loop with >= 2 iterations")
    top_sids = {s.sid for s in program.body}
    for k in _pick(seed, "unroll", n, program.seed):
        nxt = max(_all_sids(program.body), default=0) + 1
        new_top: list[int] = []
        matched: list[int] = []

        def make(x: For):
            lo = 0 if x.iter.lo is None else x.iter.lo
            first = Assign(nxt, x.var, Const(lo))
            copied, _ = _resid(x.body, nxt + 1)
            rest = replace(x, iter=RangeIter(lo + 1, x.iter.hi))
            matched.append(x.sid)
            if x.sid in top_sids:
                new_top.extend([first.sid, *(s.sid for s in copied)])
            return [first, *copied, rest]

        body = _splice_nth(program.body, _unrollable, k, make)
        new = replace(program, body=body)
        if matched and matched[0] in program.distractor_ids:  # an unrolled distractor loop stays a distractor
            new = replace(new, distractor_ids=frozenset(program.distractor_ids | set(new_top)))
        try:
            return check_preserved(program, new, "unroll_first_iteration")
        except RewriteUnavailable:
            continue
    raise RewriteUnavailable("no unrolling preserved the meaning")


def _fresh_var_id(program: Program) -> str:
    used = set(v.id for v in program.variables) | set(program.locals_pool)
    for x in _preorder(program.body):
        if isinstance(x, Name):
            used.add(x.id)
        elif is_dataclass(x) and not isinstance(x, type):
            for f in fields(x):
                v = getattr(x, f.name)
                if isinstance(v, str) and re.fullmatch(r"[a-z]\d+", v):
                    used.add(v)
    nums = [int(m.group(1)) for u in used if (m := re.fullmatch(r"v(\d+)", u))]
    return f"v{max(nums, default=-1) + 1}"


def insert_dead_read(program: Program, seed: int = 0) -> Program:
    target = program.query.target
    if program.query.kind != "final_value" or target is None:
        raise RewriteUnavailable("dead read needs a final_value query target")
    tr = run(program)
    # positions after which the target exists, strictly inside the program
    track = tr.target_track
    sites = [i for i in range(1, len(program.body)) if i - 1 < len(track) and track[i - 1] is not None]
    if not sites:
        raise RewriteUnavailable("no mid-program site where the target is defined")
    vid = _fresh_var_id(program)
    sid = max(_all_sids(program.body), default=0) + 1
    for k in _pick(seed, "dead_read", len(sites), program.seed):
        pos = sites[k]
        stmt = Assign(sid, vid, Name(target))
        body = program.body[:pos] + (stmt,) + program.body[pos:]
        # The new id is deliberately NOT registered in `variables`: the renderer
        # then names it last (after every registered variable and local), so
        # the rest of the program keeps its spellings and the only textual
        # change is the inserted line (a twin's look-alike spelling may still move).
        new = replace(program, body=body, distractor_ids=frozenset(program.distractor_ids | {sid}))
        try:
            return check_preserved(program, new, "insert_dead_read")
        except RewriteUnavailable:
            continue
    raise RewriteUnavailable("no dead read preserved the meaning")


def rename_variable(program: Program, seed: int = 0) -> tuple[Program, int]:
    """Names live in the rendering: return the same program with a rendering
    seed whose source differs from the default rendering (`program.seed`)."""
    base = render(program, program.seed).source
    for attempt in range(16):
        rs = int(hashlib.sha256(f"rename|{program.seed}|{seed}|{attempt}".encode()).hexdigest()[:12], 16)
        if render(program, rs).source != base:
            return program, rs
    raise RewriteUnavailable("no rendering seed changes the names")


REWRITES: dict[str, Callable[[Program, int], Program | tuple[Program, int]]] = {
    "swap_operands": swap_operands,
    "negate_if": negate_if,
    "unroll_first_iteration": unroll_first_iteration,
    "insert_dead_read": insert_dead_read,
    "rename_variable": rename_variable,
}


def apply_rewrite_program(program: Program, name: str, seed: int = 0) -> tuple[Program, int]:
    """(rewritten program, rendering seed). The rendering seed is
    `program.seed` (the default rendering) unless the rewrite is a rename."""
    out = REWRITES[name](program, seed)
    if isinstance(out, tuple):
        return out
    return out, program.seed


__all__ = ["REWRITES", "RewriteUnavailable", "apply_rewrite_program", "check_preserved", "insert_dead_read",
           "negate_if", "rename_variable", "swap_operands", "unroll_first_iteration"]
