"""Province V bug injectors (docs/province-v-spec.md §4.2): answer-CHANGING
edits of a function-form `Program`, each at one named statement (the site).

`inject(program, kind, seed, distance)` -> (buggy Program, site sid, kind) or
`BugUnavailable`. Load-bearingness (the edit changes the output on some
input) is checked by the caller (`tasks.make_repair_task`), which owns the
inputs. Deterministic; no `random`.
"""

from __future__ import annotations

from dataclasses import fields, replace

from ..codeworld.generate import _Rng
from ..codeworld.model import (
    Assign, AugAssign, BinOp, Call, Compare, Const, ExprStmt, For, FuncDef, If, Index, ListLit, MethodCall, Name,
    Program, RangeIter, Return, Slice, While, iter_stmts,
)

BUG_KINDS = ("off_by_one", "alias_leak", "rebind_vs_mutate", "wrong_op", "stale_update", "shadow",
             "default_shared", "frozen_predicate", "wrong_index")
SEMANTIC_BUGS = ("alias_leak", "rebind_vs_mutate", "stale_update", "shadow", "default_shared", "frozen_predicate")
SYNTACTIC_BUGS = ("off_by_one", "wrong_op", "wrong_index")
# The reading-province foil each bug is the write-side sibling of.
BUG_FOILS = {"off_by_one": "inclusive_range", "alias_leak": "value_semantics",
             "rebind_vs_mutate": "augassign_rebinds", "wrong_op": "truncating_modulo",
             "stale_update": "stale_read", "shadow": "flat_scope", "default_shared": "fresh_default",
             "frozen_predicate": "predicate_frozen", "wrong_index": "index_math"}


class BugUnavailable(Exception):
    """No site in this program admits this bug kind."""


# -- tree helpers ---------------------------------------------------------------

def solve_def(program: Program) -> FuncDef:
    entry = program.entry or "solve"
    for s in program.body:
        if isinstance(s, FuncDef) and s.name == entry:
            return s
    raise BugUnavailable("program has no entry function")


def max_sid(program: Program) -> int:
    return max(s.sid for s in iter_stmts(program.body))


def replace_stmt(body: tuple, sid: int, new: tuple) -> tuple:
    """`body` with the statement `sid` (at any depth) replaced by the
    statements `new` (possibly none). Raises BugUnavailable if that empties
    a block."""
    out = []
    for s in body:
        if s.sid == sid:
            out.extend(new)
            continue
        if isinstance(s, If):
            s = replace(s, body=replace_stmt(s.body, sid, new), orelse=replace_stmt(s.orelse, sid, new))
            if not s.body:
                raise BugUnavailable("edit empties a block")
        elif isinstance(s, (For, While, FuncDef)):
            s = replace(s, body=replace_stmt(s.body, sid, new))
            if not s.body:
                raise BugUnavailable("edit empties a block")
        out.append(s)
    return tuple(out)


def with_stmt(program: Program, sid: int, new: tuple) -> Program:
    return replace(program, body=replace_stmt(program.body, sid, new))


def _expr_variants(e, edit) -> list:
    """Every copy of expression `e` with exactly one sub-expression replaced
    by one of `edit(node)`'s alternatives."""
    out = [alt for alt in edit(e)]
    if hasattr(e, "__dataclass_fields__"):
        for f in fields(e):
            v = getattr(e, f.name)
            if isinstance(v, tuple):
                for i, item in enumerate(v):
                    for alt in _expr_variants(item, edit):
                        out.append(replace(e, **{f.name: v[:i] + (alt,) + v[i + 1:]}))
            elif hasattr(v, "__dataclass_fields__"):
                for alt in _expr_variants(v, edit):
                    out.append(replace(e, **{f.name: alt}))
    return out


def _header_variants(s, edit) -> list:
    """Edits of the expressions a statement itself holds (not its body)."""
    if isinstance(s, (Assign, AugAssign, ExprStmt, Return)):
        return [replace(s, value=v) for v in _expr_variants(s.value, edit)]
    if isinstance(s, (If, While)):
        return [replace(s, test=v) for v in _expr_variants(s.test, edit)]
    if isinstance(s, For) and not isinstance(s.iter, RangeIter):
        return [replace(s, iter=v) for v in _expr_variants(s.iter, edit)]
    return []


# -- per-kind candidate edits: stmt -> list[tuple of replacement stmts] -------------

_OPS = {"//": "%", "%": "//", "+": "-", "-": "+"}
_CMP = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "=="}


def _off_by_one(s, program, nxt):
    out = []
    if isinstance(s, For) and isinstance(s.iter, RangeIter):
        for d in (-1, 1):
            out.append((replace(s, iter=RangeIter(s.iter.lo, s.iter.hi + d)),))

    def edit(e):
        if isinstance(e, Slice) and e.hi is not None:
            return [replace(e, hi=Const(e.hi.value + 1)) if isinstance(e.hi, Const) and isinstance(e.hi.value, int)
                    else replace(e, hi=BinOp("-", e.hi, Const(1)))]
        return []
    return out + [(v,) for v in _header_variants(s, edit)]


def _alias_leak(s, program, nxt):
    if isinstance(s, Assign) and isinstance(s.value, Slice) and s.value.lo is None and s.value.hi is None \
            and isinstance(s.value.target, Name):
        return [(replace(s, value=s.value.target),)]
    return []


def _rebind_vs_mutate(s, program, nxt):
    if isinstance(s, AugAssign) and s.op == "+" and isinstance(s.value, ListLit):
        return [(Assign(s.sid, s.target, BinOp("+", Name(s.target), s.value)),)]
    if isinstance(s, Assign) and isinstance(s.value, BinOp) and s.value.op == "+" \
            and isinstance(s.value.left, Name) and s.value.left.id == s.target and isinstance(s.value.right, ListLit):
        return [(AugAssign(s.sid, s.target, "+", s.value.right),)]
    return []


def _wrong_op(s, program, nxt):
    def edit(e):
        if isinstance(e, BinOp) and e.op in _OPS:
            return [replace(e, op=_OPS[e.op])]
        if isinstance(e, Compare) and e.op in _CMP:
            return [replace(e, op=_CMP[e.op])]
        return []
    out = [(v,) for v in _header_variants(s, edit)]
    if isinstance(s, AugAssign) and s.op in ("+", "-") and not isinstance(s.value, ListLit):
        out.append((replace(s, op=_OPS[s.op]),))
    return out


def _stale_update(s, program, nxt):
    if isinstance(s, (Assign, AugAssign)) or (isinstance(s, ExprStmt) and isinstance(s.value, MethodCall)):
        return [()]
    return []


def _shadow(s, program, nxt):
    if isinstance(s, (Assign, AugAssign)):
        calls = [e for e in _expr_variants(s.value, lambda e: [e] if isinstance(e, Call) else [])
                 if isinstance(e, Call)]
        return [(ExprStmt(s.sid, c),) for c in calls[:1]]
    return []


def _is_fresh_guard(st, q) -> bool:
    return (isinstance(st, If) and isinstance(st.test, Compare) and st.test.op == "=="
            and st.test.left == Name(q) and st.test.right == Const(None) and not st.orelse
            and len(st.body) == 1 and isinstance(st.body[0], Assign) and st.body[0].target == q
            and st.body[0].value == ListLit(()))


def _default_shared(s, program, nxt):
    if isinstance(s, FuncDef) and s.defaults and s.defaults[-1] == Const(None) and s.body \
            and _is_fresh_guard(s.body[0], s.params[-1]):
        return [(replace(s, defaults=s.defaults[:-1] + (ListLit(()),), body=s.body[1:]),)]
    return []


def _frozen_predicate(s, program, nxt):
    if not isinstance(s, (For, While)):
        return []
    assigned = {x.target for x in iter_stmts(s.body) if isinstance(x, (Assign, AugAssign))}
    tests = []
    if isinstance(s, While):
        tests.append(s)
    tests += [x for x in iter_stmts(s.body) if isinstance(x, If)]
    from ..codeworld.generate import _names_in

    out = []
    if isinstance(s, For) and isinstance(s.iter, Name):
        # the classic frozen test: an if inside the loop reads the FIRST element, copied before the loop
        for t in tests:
            if s.var in _names_in(t.test):
                cp = nxt()
                loop = replace(s, body=replace_stmt(s.body, t.sid, (replace(t, test=_subst(t.test, s.var, cp)),)))
                out.append((Assign(_FROZEN_SID, cp, Index(s.iter, Const(0))), loop))
    for t in tests:
        for x in sorted(_names_in(t.test) & assigned):
            if isinstance(s, For) and x == s.var:
                continue
            cp = nxt()
            new_test = _subst(t.test, x, cp)
            loop = replace(s, test=new_test) if t is s else replace(
                s, body=replace_stmt(s.body, t.sid, (replace(t, test=new_test),)))
            out.append((Assign(_FROZEN_SID, cp, Name(x)), loop))
    return out


_FROZEN_SID = -1  # placeholder, renumbered by `inject`


def _subst(e, old: str, new: str):
    if isinstance(e, Name):
        return Name(new) if e.id == old else e
    if hasattr(e, "__dataclass_fields__"):
        kw = {}
        for f in fields(e):
            v = getattr(e, f.name)
            if isinstance(v, tuple):
                kw[f.name] = tuple(_subst(i, old, new) for i in v)
            elif hasattr(v, "__dataclass_fields__"):
                kw[f.name] = _subst(v, old, new)
        return replace(e, **kw) if kw else e
    return e


def _wrong_index(s, program, nxt):
    def edit(e):
        if isinstance(e, Index):
            if e.index == Const(0):
                return [replace(e, index=Const(-1))]
            if e.index == Const(-1):
                return [replace(e, index=Const(0))]
            if isinstance(e.index, Name):
                return [replace(e, index=BinOp("+", e.index, Const(1)))]
            if isinstance(e.index, Const) and isinstance(e.index.value, int) and e.index.value >= 0:
                return [replace(e, index=Const(e.index.value + 1))]
        return []
    return [(v,) for v in _header_variants(s, edit)]


INJECTORS = {"off_by_one": _off_by_one, "alias_leak": _alias_leak, "rebind_vs_mutate": _rebind_vs_mutate,
             "wrong_op": _wrong_op, "stale_update": _stale_update, "shadow": _shadow,
             "default_shared": _default_shared, "frozen_predicate": _frozen_predicate,
             "wrong_index": _wrong_index}


# -- sites and distance ----------------------------------------------------------

def site_distance(program: Program, sid: int) -> int | None:
    """Top-level statements of `solve` between the site's enclosing top-level
    statement and the `return` (the bug-symptom distance). Sites inside a
    helper def count from the helper's first call site."""
    body = solve_def(program).body
    for i, s in enumerate(body):
        if any(x.sid == sid for x in iter_stmts((s,))):
            return len(body) - 1 - i
    helper = next((s for s in program.body if isinstance(s, FuncDef) and any(
        x.sid == sid for x in iter_stmts((s,)))), None)
    if helper is None:
        return None
    for i, s in enumerate(body):
        if helper.name in _calls(s):
            return len(body) - 1 - i
    return None


def _calls(node) -> set:
    from ..codeworld.render import _calls_in

    return _calls_in(node)


def candidates(program: Program, kind: str) -> list[tuple[int, tuple]]:
    """(site sid, replacement statements) for every place `kind` applies,
    in program order. Never the final `return` of `solve`."""
    if kind not in INJECTORS:
        raise ValueError(f"unknown bug kind {kind!r}")
    sv = solve_def(program)
    final = sv.body[-1].sid
    counter = [0]
    base = _fresh_index(program)

    def nxt():
        counter[0] += 1
        return f"v{base + counter[0]}"

    out = []
    for s in iter_stmts(program.body):
        if s.sid == final or (isinstance(s, FuncDef) and s.name == sv.name):
            continue
        for new in INJECTORS[kind](s, program, nxt):
            out.append((s.sid, new))
    return out


def _fresh_index(program: Program) -> int:
    from ..codeworld.render import _all_ids

    n = 0
    for vid, _ in _all_ids(program):
        if vid[:1] == "v" and vid[1:].isdigit():
            n = max(n, int(vid[1:]))
    return n + 50


def apply_edit(program: Program, sid: int, new: tuple) -> Program:
    top = max_sid(program)
    fixed = tuple(replace(s, sid=top + 1 + i) if s.sid == _FROZEN_SID else s for i, s in enumerate(new))
    return with_stmt(program, sid, fixed)


def inject(program: Program, kind: str, seed: int, distance: int | None = 1, exclude=()) -> list[tuple[Program, int, int]]:
    """Every (buggy program, site, realised distance) for `kind`, ordered by
    |realised - requested distance| then by a seed-derived shuffle key, so
    the caller takes the first one that is load-bearing. Raises
    BugUnavailable if none."""
    rng = _Rng("codefix-bug", seed, kind)
    out = []
    for sid, new in candidates(program, kind):
        if sid in exclude:
            continue
        d = site_distance(program, sid)
        if d is None:
            continue
        try:
            out.append((abs(d - distance) if distance is not None else 0, rng.random(), apply_edit(program, sid, new),
                        sid, d))
        except BugUnavailable:
            continue
    if not out:
        raise BugUnavailable(f"no site for {kind}")
    out.sort(key=lambda x: (x[0], x[1]))
    return [(p, sid, d) for _, _, p, sid, d in out]


def freshify_defaults(program: Program) -> Program:
    """Rewrite each helper's shared list default `q=[]` into the fresh-list
    idiom `q=None` + `if q == None: q = []` (answer-changing when the helper
    is called twice). The `default_shared` bug reverses this."""
    top = max_sid(program)
    body = []
    for s in program.body:
        if isinstance(s, FuncDef) and s.defaults and s.defaults[-1] == ListLit(()):
            q = s.params[-1]
            top += 2
            guard = If(top - 1, Compare("==", Name(q), Const(None)), (Assign(top, q, ListLit(())),))
            s = replace(s, defaults=s.defaults[:-1] + (Const(None),), body=(guard,) + s.body)
        body.append(s)
    return replace(program, body=tuple(body))


def helperize(program: Program, seed: int) -> Program:
    """Move one top-level `t = <arithmetic>` of `solve` into a pure helper
    `def fN(p..): return <arithmetic>` called as `t = fN(..)` (answer-
    preserving). The `shadow` bug then drops the assignment of the call's
    result, as if the helper set `t` itself. Unchanged if no line fits."""
    from ..codeworld.generate import _names_in

    sv = solve_def(program)
    rng = _Rng("codefix-helperize", seed)
    fits = [s for s in sv.body if isinstance(s, Assign) and isinstance(s.value, BinOp)
            and not any(isinstance(e, (Call, MethodCall)) for e in _all_exprs(s.value)) and _names_in(s.value)]
    if not fits:
        return program
    st = fits[rng.randint(0, len(fits) - 1)]
    reads = sorted(_names_in(st.value))
    base = _fresh_index(program)
    params = tuple(f"p{base + i}" for i in range(len(reads)))
    expr = st.value
    for old, new in zip(reads, params):
        expr = _subst(expr, old, new)
    top = max_sid(program)
    fid = f"f{base}"
    helper = FuncDef(top + 2, fid, params, (), (Return(top + 1, expr),))
    call = replace(st, value=Call(fid, tuple(Name(r) for r in reads)))
    prog = with_stmt(program, st.sid, (call,))
    return replace(prog, body=(helper,) + prog.body)


def _all_exprs(e):
    yield e
    if hasattr(e, "__dataclass_fields__"):
        for f in fields(e):
            v = getattr(e, f.name)
            for x in (v if isinstance(v, tuple) else (v,)):
                if hasattr(x, "__dataclass_fields__"):
                    yield from _all_exprs(x)


__all__ = ["BUG_FOILS", "BUG_KINDS", "BugUnavailable", "INJECTORS", "SEMANTIC_BUGS", "SYNTACTIC_BUGS",
           "apply_edit", "candidates", "freshify_defaults", "helperize", "inject", "max_sid", "site_distance", "solve_def",
           "with_stmt"]
