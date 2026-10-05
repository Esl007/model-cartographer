"""Program generator for the code-tracing province.

`generate(params, seed)` grows a `Program` statement by statement against a
LIVE interpreter state: every candidate statement is executed on a copy of
the current state first and kept only if it runs and keeps values bounded,
so the generator always knows the runtime values it is building on (that is
how the negative-modulo trap guarantees a negative operand, and how list
indexing never goes out of range).

Structure of a program:
  * initial assignments for the live variables (and their look-alike twins);
  * live statements (updates, folds into the query target, if/for blocks,
    small pure helper functions), interleaved with
  * distractor statements, which only ever write "dead" variables the
    target never reads, and
  * one block per enabled trap (`_TRAP_BUILDERS`), each folding its effect
    into the target so the trap is load-bearing.

Invariants enforced by `validate` (a program failing any is re-drawn):
  * every ENABLED trap's foil disagrees with the truth (answer relevance);
  * every DISABLED trap's foil agrees with the truth -- so a toggle that is
    off really means "no instance of this trap that matters"; non-trap code
    is written to make this hold by construction (no list-to-name
    assignment, no range loops in live code, helpers with no free
    variables / no mutable defaults / locals disjoint from globals, and
    `%`/`//` only on non-negative operands);
  * the target's look-alike twin, if any, ends with a different value;
  * deleting every distractor changes no reading's answer.

A program with a declared DIALECT (`CodeDimParams.dialect`) is grown and
checked under the dialect's semantics (truth = `program.semantics`). Each
declared rule gets its own site from the matching trap builder
(`DIALECT_TRAPS`), and `validate` additionally requires that the plain
Python reading (`python_prior`) differs from the truth AND that undoing any
single declared rule changes the answer -- every rule is load-bearing. The
disabled-trap invariant is checked with the foils relative to the dialect
(`foils_for`). An empty dialect leaves generation byte-identical.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace

from .foils import ALL_TRAP_FOILS, PYTHON_PRIOR, ROUND2_FOILS, dial_foils, foils_for
from .interpret import DIALECT_SEMANTICS, TRUE, Closure, InterpError, Interpreter, answer, answer_from_trace, run, \
    semantics_for
from .model import (
    DIALECT_TRAPS,
    TRAP_KEYS,
    Assign,
    AugAssign,
    BinOp,
    Call,
    CodeDimParams,
    Compare,
    Const,
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
    OpaqueLit,
    Print,
    Program,
    Query,
    RangeIter,
    Return,
    SetItem,
    Slice,
    VarInfo,
    While,
    iter_stmts,
)

MAX_GEN_STEPS = 2_000
MAX_INT = 99_999
MAX_LIST = 24
MAX_STR = 24
_ATTEMPTS = 32
_WORDS = ("ab", "cat", "node", "red", "mint", "sol", "kit", "oak")


class GenerationFailed(Exception):
    """No valid program after the retry budget. The evaluator records this as
    ERROR (`generation_failed=1`), never as a model failure."""


def _stable_int(*parts: object) -> int:
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    return int(hashlib.sha256(payload).hexdigest()[:15], 16)


class _Rng:
    """Counter-based deterministic RNG over sha256 (no `random` module)."""

    def __init__(self, *seed_parts: object) -> None:
        self._base = "|".join(str(p) for p in seed_parts)
        self._n = 0

    def _next(self) -> int:
        self._n += 1
        return _stable_int(self._base, self._n)

    def random(self) -> float:
        return self._next() / float(16**15)

    def randint(self, lo: int, hi: int) -> int:
        return lo + self._next() % (hi - lo + 1)

    def choice(self, seq):
        seq = list(seq)
        return seq[self._next() % len(seq)]

    def chance(self, p: float) -> bool:
        return self.random() < p


def _bounded(v) -> bool:
    if isinstance(v, bool) or v is None:
        return True
    if isinstance(v, int):
        return abs(v) <= MAX_INT
    if isinstance(v, str):
        return len(v) <= MAX_STR
    if isinstance(v, list):
        return len(v) <= MAX_LIST and all(_bounded(x) for x in v)
    if isinstance(v, dict):
        return len(v) <= MAX_LIST and all(_bounded(x) for x in v.values())
    return True


class _Builder:
    def __init__(self, params: CodeDimParams, seed: int, query_kind: str) -> None:
        self.p = params
        self.seed = seed
        self.rng = _Rng("codeworld", seed)
        self.query_kind = query_kind
        self.body: list = []
        self.n_nodes = 0
        self._sid = 0
        self.vars: dict[str, VarInfo] = {}
        self.interp = Interpreter(semantics_for(params.dialect))
        self.distractor_ids: set[int] = set()
        self.trap_sites: list[tuple[str, int]] = []
        self.locals_used: list[str] = []
        self._counters = {"v": 0, "f": 0, "p": 0, "x": 0, "i": 0}
        self.helpers: list[tuple[str, int, int]] = []  # (fid, n_required, n_total)
        self.unfolded: set[str] = set()
        self.target: str = ""
        self.chain_var: str | None = None  # chain_depth: the int carrying the dependency chain
        self.path_sids: list[int] = []  # chain_depth: sids of the chain updates, in order

    # -- ids ---------------------------------------------------------------

    def new_id(self, prefix: str) -> str:
        n = self._counters[prefix]
        self._counters[prefix] = n + 1
        vid = f"{prefix}{n}"
        if prefix in ("p", "x", "i"):
            self.locals_used.append(vid)
        return vid

    def sid(self) -> int:
        self._sid += 1
        return self._sid

    def add_var(self, kind: str, role: str, twin_of: str | None = None) -> str:
        vid = self.new_id("f" if kind == "func" else "v")
        self.vars[vid] = VarInfo(vid, kind, role, twin_of)
        return vid

    # -- runtime view ---------------------------------------------------------

    def val(self, vid: str):
        return self.interp.globals.get(vid)

    def live(self, kind: str | None = None, exclude=()) -> list[str]:
        return [
            v.id for v in self.vars.values()
            if v.role in ("live", "twin", "target") and v.id not in exclude
            and (kind is None or v.kind == kind)
        ]

    def try_emit(self, stmts: list, distractor: bool = False, trap: str | None = None,
                 trap_index: int = 0) -> bool:
        """Execute `stmts` on a copy of the state; keep them iff they run and
        everything stays bounded."""
        trial = copy.deepcopy(self.interp)
        try:
            for s in stmts:
                trial.exec_top(s)
        except InterpError:
            return False
        if trial.steps > MAX_GEN_STEPS:
            return False
        if not all(_bounded(v) for v in trial.globals.values() if not isinstance(v, Closure)):
            return False
        self.interp = trial
        self.body.extend(stmts)
        self.n_nodes += sum(1 for _ in iter_stmts(stmts))
        if distractor:
            self.distractor_ids.update(s.sid for s in stmts)
        if trap is not None:
            self.trap_sites.append((trap, stmts[trap_index].sid))
        return True

    # -- literals and terms ---------------------------------------------------

    def int_lit(self, lo: int = 1, hi: int = 9) -> Const:
        return Const(self.rng.randint(lo, hi))

    def literal(self, kind: str):
        if kind == "int":
            return self.int_lit()
        if kind == "list":
            return ListLit(tuple(self.int_lit(0, 9) for _ in range(self.rng.randint(2, 4))))
        if kind == "str":
            return Const(self.rng.choice(_WORDS))
        raise ValueError(kind)

    def term(self, vid: str):
        """An int-valued expression reading `vid`."""
        kind = self.vars[vid].kind
        v = self.val(vid)
        if kind == "int":
            return Name(vid) if self.rng.chance(0.7) else BinOp("*", Name(vid), Const(2))
        if kind == "list":
            if v and self.rng.chance(0.5):
                return Index(Name(vid), Const(self.rng.choice((0, -1))))
            return Len(Name(vid))
        return Len(Name(vid))

    def fold(self, vid: str, into: str | None = None):
        into = into or self.target
        t = self.term(vid)
        if self.vars[into].kind == "list":
            return ExprStmt(self.sid(), MethodCall(into, "append", (t,)))
        return AugAssign(self.sid(), into, "+", t)

    def fold_expr(self, expr, into: str | None = None):
        into = into or self.target
        if self.vars[into].kind == "list":
            return ExprStmt(self.sid(), MethodCall(into, "append", (expr,)))
        return AugAssign(self.sid(), into, "+", expr)

    # -- live (non-trap) statements -------------------------------------------

    def int_update(self, a: str, loop_var: str | None = None):
        assert self.vars.get(a) is None or self.vars[a].kind == "int" or a == loop_var, a
        ints = [v for v in self.live("int")] or [a]
        b = self.rng.choice(ints)
        lists = [v for v in self.live("list") if isinstance(self.val(v), list)]
        strs = self.live("str")
        options = ["add", "aug", "mul", "ifexp"]
        if lists:
            options += ["len", "index", "lenmod"]
        if strs:
            options.append("strlen")
        if self.helpers:
            options += ["call", "call"]
        if loop_var is not None:
            options += ["loopvar", "loopvar", "loopvar"]
        k = self.rng.choice(options)
        if k == "add":
            return Assign(self.sid(), a, BinOp(self.rng.choice("+-"), Name(b), self.int_lit()))
        if k == "aug":
            rhs = Name(b) if self.rng.chance(0.5) else self.int_lit()
            return AugAssign(self.sid(), a, self.rng.choice("+-"), rhs)
        if k == "mul":
            return Assign(self.sid(), a, BinOp("*", Name(b), Const(self.rng.randint(2, 3))))
        if k == "ifexp":
            c = self.rng.choice(ints)
            return Assign(self.sid(), a, IfExp(Compare(self.rng.choice((">", "<")), Name(b), Name(c)),
                                               Name(b), BinOp("+", Name(c), self.int_lit())))
        if k == "len":
            return Assign(self.sid(), a, Len(Name(self.rng.choice(lists))))
        if k == "index":
            return Assign(self.sid(), a, Index(Name(self.rng.choice(lists)), Const(self.rng.choice((0, -1)))))
        if k == "lenmod":
            src = self.rng.choice(lists)
            return Assign(self.sid(), a, BinOp(self.rng.choice(("%", "//")), Len(Name(src)),
                                               Const(self.rng.randint(2, 4))))
        if k == "strlen":
            return Assign(self.sid(), a, Len(Name(self.rng.choice(strs))))
        if k == "call":
            fid, req, tot = self.rng.choice(self.helpers)
            n_args = self.rng.randint(req, tot)
            return Assign(self.sid(), a, Call(fid, tuple(Name(self.rng.choice(ints)) for _ in range(n_args))))
        # loopvar
        return AugAssign(self.sid(), a, self.rng.choice("+-"), Name(loop_var))

    def list_update(self, L: str):
        ints = self.live("int")
        cur = self.val(L) or []
        options = ["append", "rebind", "extend"]
        if cur:
            options += ["setitem"]
        if len(cur) >= 2 and ints:
            options += ["pop"]
        other_lists = self.live("list")
        options.append("slice")
        k = self.rng.choice(options)
        item = Name(self.rng.choice(ints)) if ints and self.rng.chance(0.5) else self.int_lit(0, 9)
        if k == "append":
            return ExprStmt(self.sid(), MethodCall(L, "append", (item,)))
        if k == "rebind":
            return Assign(self.sid(), L, BinOp("+", Name(L), ListLit((item,))))
        if k == "extend":
            return ExprStmt(self.sid(), MethodCall(L, "extend", (ListLit((item, self.int_lit(0, 9))),)))
        if k == "setitem":
            return SetItem(self.sid(), L, Const(self.rng.choice((0, -1))), item)
        if k == "pop":
            a = self.rng.choice([v for v in ints if v != self.target] or ints)
            return Assign(self.sid(), a, MethodCall(L, "pop", ()))
        src = self.rng.choice(other_lists)
        return Assign(self.sid(), L, Slice(Name(src), Const(1), None))

    def str_update(self, s: str):
        k = self.rng.choice(("concat", "upper", "replace", "slice"))
        if k == "concat":
            return Assign(self.sid(), s, BinOp("+", Name(s), Const(self.rng.choice(_WORDS))))
        if k == "upper":
            return Assign(self.sid(), s, MethodCall(s, "upper", ()))
        if k == "replace":
            return Assign(self.sid(), s, MethodCall(s, "replace", (Const("a"), Const("o"))))
        return Assign(self.sid(), s, Slice(Name(s), Const(1), None))

    def simple_update(self, loop_var: str | None = None, ints_only: bool = False):
        """One non-compound statement updating some live variable."""
        if self.unfolded and self.rng.chance(0.3) and not ints_only:
            return self.fold(self.rng.choice(sorted(self.unfolded)))
        candidates = [v for v in self.live(exclude=(self.target,))
                      if not ints_only or self.vars[v].kind == "int"]
        if not candidates:
            if not ints_only:
                return self.fold(self.rng.choice(self.live(exclude=(self.target,)) or [self.target]))
            if self.vars[self.target].kind == "int":
                return self.int_update(self.target, loop_var)
            return AugAssign(self.sid(), self.target, "+", ListLit((Name(loop_var) if loop_var else self.int_lit(),)))
        a = self.rng.choice(candidates)
        kind = self.vars[a].kind
        if kind == "int":
            return self.int_update(a, loop_var)
        if kind == "list":
            return self.list_update(a)
        return self.str_update(a)

    def compound(self, depth: int, loop_var: str | None = None, ints_only: bool = False):
        """An if/for block (depth = nesting level of the block itself, 1-based)."""
        lists = [v for v in self.live("list") if isinstance(self.val(v), list) and self.val(v)]
        make_for = lists and not ints_only and self.rng.chance(0.45)

        def inner(n: int, lv, only_ints: bool) -> tuple:
            out = []
            for _ in range(n):
                if depth < self.p.max_nesting and self.rng.chance(0.25):
                    out.append(self.compound(depth + 1, lv, only_ints))
                else:
                    out.append(self.simple_update(lv, only_ints))
            return tuple(out)

        if make_for:
            L = self.rng.choice(lists)
            xv = self.new_id("x")
            return For(self.sid(), xv, Name(L), inner(self.rng.randint(1, 2), xv, True))
        ints = self.live("int")
        rhs = Name(self.rng.choice(ints)) if ints and self.rng.chance(0.5) else self.int_lit()
        if loop_var is not None and self.rng.chance(0.5):
            lhs = Name(loop_var)
        elif ints:
            lhs = Name(self.rng.choice(ints))
        else:
            lhs = Len(Name(self.rng.choice(self.live("list") + self.live("str"))))
        test = Compare(self.rng.choice((">", "<", ">=", "==")), lhs, rhs)
        body = inner(self.rng.randint(1, 2), loop_var, ints_only)
        orelse = inner(self.rng.randint(1, 2), loop_var, ints_only) if self.rng.chance(0.5) else ()
        return If(self.sid(), test, body, orelse)

    def helper_def(self):
        fid = self.add_var("func", "helper")
        p0 = self.new_id("p")
        if self.rng.chance(0.5):
            p1 = self.new_id("p")
            params, defaults = (p0, p1), (self.int_lit(1, 5),)
            body = (Return(self.sid(), BinOp(self.rng.choice("+-"), BinOp("*", Name(p0), Const(2)), Name(p1))),)
            arity = (1, 2)
        else:
            loc = self.new_id("p")
            params, defaults = (p0,), ()
            body = (Assign(self.sid(), loc, BinOp("+", Name(p0), self.int_lit())),
                    Return(self.sid(), BinOp("*", Name(loc), Const(2))))
            arity = (1, 1)
        stmt = FuncDef(self.sid(), fid, params, defaults, body)
        return stmt, (fid, arity[0], arity[1])

    def live_statement(self) -> bool:
        remaining = self.p.n_statements - self.n_nodes
        if self.unfolded and remaining <= len(self.unfolded):
            return self.try_emit([self.fold(self.rng.choice(sorted(self.unfolded)))]) and \
                self._mark_folded_last()
        r = self.rng.random()
        if len(self.helpers) < 2 and remaining >= 4 and r < 0.08:
            stmt, h = self.helper_def()
            if self.try_emit([stmt]):
                self.helpers.append(h)
                return True
            return False
        if self.p.max_nesting >= 1 and remaining >= 3 and r < 0.35:
            return self.try_emit([self.compound(1)]) and self._mark_folded_last()
        return self.try_emit([self.simple_update()]) and self._mark_folded_last()

    def _mark_folded_last(self) -> bool:
        last = self.body[-1]
        for s in iter_stmts([last]):
            tgt = getattr(s, "target", None)
            if tgt == self.target or (isinstance(s, ExprStmt) and isinstance(s.value, MethodCall)
                                      and s.value.obj == self.target):
                self.unfolded -= _names_in(s)
        return True

    # -- distractors ---------------------------------------------------------

    def distractor(self) -> bool:
        dead = [v for v in self.vars.values() if v.role == "dead"]
        ints = self.live("int") or [self.target]
        if not dead or self.rng.chance(0.25):
            d = self.add_var("int", "dead")
            src = self.rng.choice(ints)
            init_src = Name(src) if self.vars[src].kind == "int" else Const(self.rng.randint(1, 9))
            return self.try_emit([Assign(self.sid(), d, BinOp("+", init_src, self.int_lit()))], distractor=True)
        d = self.rng.choice(dead).id
        k = self.rng.choice(("aug", "range", "mod", "readlive"))
        if k == "aug":
            stmt = AugAssign(self.sid(), d, self.rng.choice("+-"), self.int_lit())
        elif k == "range":
            iv = self.new_id("i")
            stmt = For(self.sid(), iv, RangeIter(None, self.rng.randint(2, 4)),
                       (AugAssign(self.sid(), d, "+", Name(iv)),))
        elif k == "mod":
            stmt = Assign(self.sid(), d, BinOp(self.rng.choice(("%", "//")), Name(d), Const(self.rng.randint(2, 5))))
        else:
            src = self.rng.choice(ints)
            rhs = Name(src) if self.vars[src].kind == "int" else self.int_lit()
            stmt = Assign(self.sid(), d, BinOp("-", rhs, Name(d)))
        return self.try_emit([stmt], distractor=True)

    # -- traps ---------------------------------------------------------------

    def trap_aliasing(self) -> list:
        if self.vars[self.target].kind == "list" and self.rng.chance(0.6):
            alias = self.add_var("list", "trap")
            site = Assign(self.sid(), alias, Name(self.target))
            mut = self._mutate_through(alias)
            return [site, mut], 0
        src = self.add_var("list", "trap")
        alias = self.add_var("list", "trap")
        init = Assign(self.sid(), src, self.literal("list"))
        site = Assign(self.sid(), alias, Name(src))
        mut = self._mutate_through(alias)
        if isinstance(mut, SetItem):
            read = Index(Name(src), mut.index)
        else:
            read = Len(Name(src)) if self.rng.chance(0.5) else Index(Name(src), Const(-1))
        return [init, site, mut, self.fold_expr(read)], 1

    def _mutate_through(self, alias: str):
        k = self.rng.choice(("append", "setitem", "iadd"))
        v = self.int_lit(10, 19)  # never equal to an initial element (those are 0..9)
        if k == "append":
            return ExprStmt(self.sid(), MethodCall(alias, "append", (v,)))
        if k == "setitem":
            return SetItem(self.sid(), alias, Const(self.rng.choice((0, -1))), v)
        return AugAssign(self.sid(), alias, "+", ListLit((v,)))

    def trap_late_binding(self) -> list:
        r = self.add_var("int", "trap")
        f = self.add_var("func", "trap")
        p = self.new_id("p")
        k1 = self.rng.randint(2, 5)
        op = self.rng.choice("+*")
        site = FuncDef(self.sid(), f, (p,), (), (Return(self.sid(), BinOp(op, Name(p), Name(r))),))
        rebind = AugAssign(self.sid(), r, "+", Const(self.rng.randint(2, 6))) if self.rng.chance(0.5) \
            else Assign(self.sid(), r, Const(k1 + self.rng.randint(2, 6)))
        call = Call(f, (self.int_lit(2, 7),))
        return [Assign(self.sid(), r, Const(k1)), site, rebind, self.fold_expr(call)], 1

    def trap_shadowing(self) -> list:
        twin_t = {v.id for v in self.vars.values() if v.twin_of == self.target}
        gs = [g for g in self.live("int") if g != self.target and g not in twin_t]
        pre = []
        if gs:
            g = self.rng.choice(gs)
        else:
            g = self.add_var("int", "trap")
            pre = [Assign(self.sid(), g, self.int_lit())]
        f = self.add_var("func", "trap")
        y = self.add_var("int", "trap")
        p = self.new_id("p")
        site = FuncDef(self.sid(), f, (p,), (), (
            Assign(self.sid(), g, BinOp("+", BinOp("*", Name(p), Const(2)), Const(1))),
            Return(self.sid(), BinOp("+", Name(g), Const(1))),
        ))
        arg = self.rng.randint(3, 9)
        if isinstance(self.val(g), int) and 2 * arg + 1 == self.val(g):
            arg += 1  # otherwise the leaked assignment would not change g
        call = Assign(self.sid(), y, Call(f, (Const(arg),)))
        return pre + [site, call, self.fold_expr(BinOp("+", Name(g), Name(y)))], len(pre)

    def trap_inclusive_range(self) -> list:
        iv = self.new_id("i")
        hi = self.rng.randint(3, 6)
        lo = None if self.rng.chance(0.6) else self.rng.randint(1, hi - 2)
        if self.vars[self.target].kind == "list":
            body = (ExprStmt(self.sid(), MethodCall(self.target, "append", (Name(iv),))),)
            return [For(self.sid(), iv, RangeIter(lo, hi), body)], 0
        if self.rng.chance(0.5):
            m = self.rng.randint(1, 3)
            body = (AugAssign(self.sid(), self.target, "+", BinOp("*", Name(iv), Const(m)) if m > 1 else Name(iv)),)
            return [For(self.sid(), iv, RangeIter(lo, hi), body)], 0
        acc = self.add_var("int", "trap")
        loop = For(self.sid(), iv, RangeIter(lo, hi), (AugAssign(self.sid(), acc, "+", Name(iv)),))
        return [Assign(self.sid(), acc, Const(0)), loop, self.fold_expr(Name(acc))], 1

    def trap_negative_modulo(self) -> list:
        twin_t = {v.id for v in self.vars.values() if v.twin_of == self.target}
        srcs = [g for g in self.live("int") if g not in twin_t and isinstance(self.val(g), int)
                and not isinstance(self.val(g), bool)]
        d = self.add_var("int", "trap")
        b = self.add_var("int", "trap")
        m = self.rng.randint(3, 7)
        if srcs:
            src = self.rng.choice(srcs)
            base = self.val(src)
            off = base + self.rng.randint(2, 9)
            if (base - off) % m == 0:
                off += 1
            first = Assign(self.sid(), d, BinOp("-", Name(src), Const(off)))
        else:
            a = self.rng.randint(1, 9)
            off = a + self.rng.randint(2, 9)
            if (a - off) % m == 0:
                off += 1
            first = Assign(self.sid(), d, BinOp("-", Const(a), Const(off)))
        site = Assign(self.sid(), b, BinOp(self.rng.choice(("%", "//")), Name(d), Const(m)))
        return [first, site, self.fold_expr(Name(b))], 1

    def trap_mutable_default(self) -> list:
        f = self.add_var("func", "trap")
        p, q = self.new_id("p"), self.new_id("p")
        ret = Len(Name(q)) if self.rng.chance(0.6) else BinOp("+", Len(Name(q)), Name(p))
        site = FuncDef(self.sid(), f, (p, q), (ListLit(()),), (
            ExprStmt(self.sid(), MethodCall(q, "append", (Name(p),))),
            Return(self.sid(), ret),
        ))
        a1, a2 = self.add_var("int", "trap"), self.add_var("int", "trap")
        c1 = Assign(self.sid(), a1, Call(f, (self.int_lit(1, 9),)))
        c2 = Assign(self.sid(), a2, Call(f, (self.int_lit(1, 9),)))
        fold = self.fold_expr(Name(a2) if self.rng.chance(0.5) else BinOp("+", Name(a1), Name(a2)))
        return [site, c1, c2, fold], 0

    def trap_opaque_literal(self) -> list:
        """An opaque literal inside a naming scheme it breaks, carried through
        an assignment and a concatenation into the target. Copying gives
        `tkt-3f9a1c/ok`; completing the scheme gives `ticket_audit_3/ok`."""
        words = []
        while len(words) < 3:
            w = self.rng.choice(("build", "deploy", "stage", "audit", "sync", "cache", "release"))
            if w not in words:
                words.append(w)
        stmts = []
        for i, w in enumerate(words[:-1], 1):
            stmts.append(Assign(self.sid(), self.add_var("str", "trap"), Const(f"ticket_{w}_{i}")))
        opaque = self.add_var("str", "trap")
        hexv = format(self.rng.randint(0, 0xFFFFFF), "06x")
        site = Assign(self.sid(), opaque, OpaqueLit(f"tkt-{hexv}", f"ticket_{words[-1]}_{len(words)}"))
        carry, tagged = self.add_var("str", "trap"), self.add_var("str", "trap")
        stmts += [
            site,
            Assign(self.sid(), carry, Name(opaque)),
            Assign(self.sid(), tagged, BinOp("+", Name(carry), Const(self.rng.choice(("/ok", ":v2", "#done"))))),
            self.fold_expr(Name(tagged)),
        ]
        return stmts, len(words) - 1

    # -- round-2 mechanisms (docs/spec-code-hunt.md WP-G) ------------------------

    def _live_ints(self) -> list[str]:
        return [g for g in self.live("int") if g != self.target and isinstance(self.val(g), int)
                and not isinstance(self.val(g), bool)]

    def chain_step(self) -> tuple:
        """One dependent update of the chain variable `c` (created, from a
        live value, by the first step). Every update reads `c`, so after d
        steps the answer depends on a chain of d updates; the chain is folded
        into the target once, after the last step. `%`/`//` only ever see a
        non-negative `c` (so `truncating_modulo` stays off)."""
        init = []
        c = self.chain_var
        if c is None or c not in self.interp.globals:
            c = self.add_var("int", "trap")
            self.chain_var = c
            gs = self._live_ints()
            if gs and self.rng.chance(0.6):
                g = self.rng.choice(gs)
                init = [Assign(self.sid(), c, BinOp("+", Name(g), self.int_lit()))]
                cur = self.val(g) + init[0].value.right.value
            else:
                init = [Assign(self.sid(), c, self.int_lit(2, 9))]
                cur = init[0].value.value
        else:
            cur = self.val(c)
        gs = [g for g in self._live_ints() if self.val(g) != 0]
        options = ["addk", "addk"]
        if gs:
            options += ["addv", "mix"]
        if abs(cur) < 3000:
            options.append("lin")
        if cur >= 2:
            options.append("half")
        k = self.rng.choice(options)
        if k == "addk":
            upd = AugAssign(self.sid(), c, self.rng.choice("+-"), self.int_lit(1, 9))
        elif k == "addv":
            upd = AugAssign(self.sid(), c, "+", Name(self.rng.choice(gs)))
        elif k == "mix":
            upd = Assign(self.sid(), c, BinOp("+", Name(c), BinOp("*", Name(self.rng.choice(gs)), Const(2))))
        elif k == "lin":
            upd = Assign(self.sid(), c, BinOp("-", BinOp("*", Name(c), Const(2)), self.int_lit(1, 9)))
        else:
            upd = Assign(self.sid(), c, BinOp("//", Name(c), Const(2)))
        return init + [upd], len(init)

    def trap_stack(self) -> tuple:
        """`stack_depth` = k list-semantics steps stacked on ONE list `a`
        whose final state is folded into the target: (1) `b = a; b += [x]`
        (in-place: a sees x), (2) `b = b + [y]` (rebinds b: a does not see
        y), (3) `c = a[:]; c.append(z)` (a copy: a does not see z), (4)
        `def f(p): p.append(w)` called on a (the callee mutates a). Each step
        has its own foil (foils.STACK_FOILS) and changes `len(a)`."""
        k = self.p.stack_depth
        a = self.add_var("list", "trap")
        stmts = [Assign(self.sid(), a, ListLit(tuple(self.int_lit(0, 9) for _ in range(self.rng.randint(2, 3)))))]
        b = self.add_var("list", "trap")
        stmts += [Assign(self.sid(), b, Name(a)), AugAssign(self.sid(), b, "+", ListLit((self.int_lit(10, 19),)))]
        if k >= 2:
            stmts.append(Assign(self.sid(), b, BinOp("+", Name(b), ListLit((self.int_lit(20, 29),)))))
        if k >= 3:
            c = self.add_var("list", "trap")
            stmts += [Assign(self.sid(), c, Slice(Name(a), None, None)),
                      ExprStmt(self.sid(), MethodCall(c, "append", (self.int_lit(30, 39),)))]
        if k >= 4:
            f = self.add_var("func", "trap")
            p = self.new_id("p")
            stmts += [FuncDef(self.sid(), f, (p,), (), (
                ExprStmt(self.sid(), MethodCall(p, "append", (self.int_lit(40, 49),))),
                Return(self.sid(), Len(Name(p))),
            )), ExprStmt(self.sid(), Call(f, (Name(a),)))]
        stmts.append(self.fold_expr(BinOp("+", Len(Name(a)), Index(Name(a), Const(-1)))))
        return stmts, 1

    def trap_loop_branch(self) -> tuple:
        """A data-dependent branch inside a loop over a list: some elements
        take the if, some the else (threshold from a live value when one
        splits the list, else a constant), so freezing the test after the
        first pass (`predicate_frozen`) or running the body once
        (`loop_once`) gives a different total."""
        L = self.add_var("list", "trap")
        vals = []
        while len(vals) < self.rng.randint(4, 5):
            v = self.rng.randint(0, 9)
            if v not in vals:
                vals.append(v)
        lo, hi = min(vals), max(vals)
        splits = [g for g in self._live_ints() if lo <= self.val(g) < hi]
        thr = Name(self.rng.choice(splits)) if splits and self.rng.chance(0.6) else Const(self.rng.randint(lo, hi - 1))
        acc = self.add_var("int", "trap")
        x = self.new_id("x")
        loop = For(self.sid(), x, Name(L), (If(self.sid(), Compare(">", Name(x), thr),
                                               (AugAssign(self.sid(), acc, "+", Name(x)),),
                                               (AugAssign(self.sid(), acc, "-", self.int_lit(1, 3)),)),))
        stmts = [Assign(self.sid(), L, ListLit(tuple(Const(v) for v in vals))), Assign(self.sid(), acc, Const(0)),
                 loop, self.fold_expr(Name(acc))]
        return stmts, 2

    def trap_while(self) -> tuple:
        """`while t < limit: t += step; n += 1` with `t` starting from a live
        value: the exit depends on computed state. 3..8 iterations under
        truth (<= 40 is enforced by running it). A frozen test never turns
        false (the reading hits the step cap: FOIL_ERROR); `loop_once`
        counts one pass."""
        t, n = self.add_var("int", "trap"), self.add_var("int", "trap")
        gs = [g for g in self._live_ints() if abs(self.val(g)) <= 500]
        if gs and self.rng.chance(0.7):
            g = self.rng.choice(gs)
            start, init = self.val(g), Assign(self.sid(), t, Name(g))
        else:
            start = self.rng.randint(0, 9)
            init = Assign(self.sid(), t, Const(start))
        step = self.rng.randint(2, 5)
        m = self.rng.randint(3, 8)
        limit = start + step * (m - 1) + self.rng.randint(1, step)
        loop = While(self.sid(), Compare("<", Name(t), Const(limit)),
                     (AugAssign(self.sid(), t, "+", Const(step)), AugAssign(self.sid(), n, "+", Const(1))))
        fold = self.fold_expr(Name(n) if self.rng.chance(0.5) else BinOp("+", Name(n), Name(t)))
        return [init, Assign(self.sid(), n, Const(0)), loop, fold], 2

    def trap_sign_chain(self) -> tuple:
        """`sign_chain` = k operations from `//`, `*`, `%` chained on a value
        that is negative at each of them (a live value minus an offset), each
        divisor chosen so it does not divide the value -- so floor and
        truncating division disagree at every `//`/`%` step."""
        k = self.p.sign_chain
        d = self.add_var("int", "trap")
        gs = [g for g in self._live_ints() if abs(self.val(g)) <= 500]
        if gs and self.rng.chance(0.6):
            g = self.rng.choice(gs)
            off = self.val(g) + self.rng.randint(5, 30)
            first = Assign(self.sid(), d, BinOp("-", Name(g), Const(off)))
            cur = self.val(g) - off
        else:
            a = self.rng.randint(1, 9)
            off = a + self.rng.randint(5, 30)
            first = Assign(self.sid(), d, BinOp("-", Const(a), Const(off)))
            cur = a - off
        ops = {1: [self.rng.choice(("//", "%"))], 2: ["//", "%"], 3: ["//", "*", "%"]}[k]
        stmts = [first]
        for op in ops:
            if op == "*":
                m = self.rng.randint(2, 3)
                stmts.append(Assign(self.sid(), d, BinOp("-", BinOp("*", Name(d), Const(m)), Const(1))))
                cur = cur * m - 1
                continue
            m = self.rng.randint(3, 7)
            while cur % m == 0:
                m += 1
            stmts.append(Assign(self.sid(), d, BinOp(op, Name(d), Const(m))))
            cur = cur // m if op == "//" else cur % m
        stmts.append(self.fold_expr(Name(d)))
        return stmts, 1

    def trap_index_math(self) -> tuple:
        """`index_math` = k slices of one list with computed or negative end
        bounds at off-by-one boundaries (`xs[1:len(xs) - 1]`, `xs[:-2]`,
        `xs[i:i + 2]` with `i = len(xs) // 2`), each folded into the target
        as `len(w) + w[-1]`; reading the end bound as inclusive changes both."""
        k = self.p.index_math
        L = self.add_var("list", "trap")
        vals = []
        while len(vals) < self.rng.randint(5, 6):
            v = self.rng.randint(0, 9)
            if v not in vals:
                vals.append(v)
        stmts = [Assign(self.sid(), L, ListLit(tuple(Const(v) for v in vals)))]
        forms = ["inner", "negend", "window"][:k]
        for form in forms:
            w = self.add_var("list", "trap")
            if form == "inner":
                sl = Slice(Name(L), Const(1), BinOp("-", Len(Name(L)), Const(1)))
            elif form == "negend":
                sl = Slice(Name(L), None, Const(-2))
            else:
                i = self.add_var("int", "trap")
                stmts.append(Assign(self.sid(), i, BinOp("//", Len(Name(L)), Const(2))))
                sl = Slice(Name(L), Name(i), BinOp("+", Name(i), Const(2)))
            stmts.append(Assign(self.sid(), w, sl))
            stmts.append(self.fold_expr(BinOp("+", Len(Name(w)), Index(Name(w), Const(-1)))))
        return stmts, 1

    # -- driver ----------------------------------------------------------------

    def build(self) -> Program:
        p = self.p
        tkind = "list" if self.rng.chance(0.3) else "int"
        if p.opaque_literal_on:
            tkind = "list"  # the carried string must reach the answer (rng draw kept: off-programs unchanged)
        self.target = self.add_var(tkind, "target")
        others = []
        for _ in range(p.n_variables - 1):
            r = self.rng.random()
            others.append(self.add_var("int" if r < 0.55 else ("list" if r < 0.85 else "str"), "live"))
        n_twins = int(p.identifier_confusability * p.n_variables + 0.5)
        bases = [self.target] + others
        for base in bases[:n_twins]:
            self.add_var(self.vars[base].kind, "twin", twin_of=base)
        inits = []
        for v in list(self.vars.values()):
            if v.role in ("target", "live", "twin"):
                lit = self.literal(v.kind)
                if v.role == "twin":  # differ from the base's initial value
                    while lit == self._init_of(inits, v.twin_of):
                        lit = self.literal(v.kind)
                inits.append(Assign(self.sid(), v.id, lit))
        if not self.try_emit(inits):
            raise GenerationFailed("initial assignments failed")
        twin_t = {v.id for v in self.vars.values() if v.twin_of == self.target}
        self.unfolded = {v for v in self.live() if v != self.target and v not in twin_t}

        traps = list(p.traps_on()) + (["opaque_literal_on"] if p.opaque_literal_on else [])
        traps += [f"dialect:{r}" for r in p.dialect]  # one load-bearing site per declared rule
        traps += [k for k in ("stack_depth", "loop_branch", "while_on", "sign_chain", "index_math") if getattr(p, k)]
        if p.chain_depth:  # chain steps interleaved evenly with every other block
            blocks, traps = traps, []
            n_all = len(blocks) + p.chain_depth
            for i in range(n_all):
                if blocks and (i * len(blocks)) // n_all != ((i + 1) * len(blocks)) // n_all:
                    traps.append(blocks.pop(0))
                else:
                    traps.append("chain_depth")
            traps += blocks
        budget = max(p.n_statements, self.n_nodes + 1)
        # trap blocks land at evenly spread fractions of the live budget
        start = self.n_nodes
        due = [start + int((budget - start) * (i + 1) / (len(traps) + 1)) for i in range(len(traps))]
        builders = {
            "aliasing_on": self.trap_aliasing,
            "late_binding_on": self.trap_late_binding,
            "shadowing_on": self.trap_shadowing,
            "inclusive_range_trap_on": self.trap_inclusive_range,
            "negative_modulo_on": self.trap_negative_modulo,
            "mutable_default_on": self.trap_mutable_default,
            "opaque_literal_on": self.trap_opaque_literal,
            "chain_depth": self.chain_step,
            "stack_depth": self.trap_stack,
            "loop_branch": self.trap_loop_branch,
            "while_on": self.trap_while,
            "sign_chain": self.trap_sign_chain,
            "index_math": self.trap_index_math,
        }
        builders.update({f"dialect:{r}": builders[DIALECT_TRAPS[r]] for r in p.dialect})
        stalls = 0
        while self.n_nodes < budget or traps:
            if traps and self.n_nodes >= due[0]:
                trap = traps.pop(0)
                due.pop(0)
                for _ in range(8):
                    stmts, site = builders[trap]()
                    if self.try_emit(stmts, trap=trap, trap_index=site):
                        if trap == "chain_depth":
                            self.path_sids.append(stmts[site].sid)
                        break
                else:
                    raise GenerationFailed(f"could not place {trap}")
                continue
            if self.n_nodes >= budget and traps:
                due[0] = self.n_nodes
                continue
            ok = self.distractor() if self.rng.chance(p.distractor_fraction) else self.live_statement()
            stalls = 0 if ok else stalls + 1
            if stalls > 40:
                raise GenerationFailed("generator stalled")
        if self.chain_var is not None:  # the chain reaches the answer once, after its last step
            if not any(self.try_emit([self.fold(self.chain_var)]) for _ in range(4)):
                raise GenerationFailed("could not fold the chain")
        # every live variable feeds the answer at least once
        for v in sorted(self.unfolded):
            for _ in range(4):
                if self.try_emit([self.fold(v)]):
                    break
        # the target's twin is a second accumulator: give it a couple of updates
        for tw in sorted(twin_t):
            for _ in range(2):
                for _ in range(4):
                    src = self.rng.choice(self.live(exclude=(self.target, tw)) or [self.target])
                    if self.try_emit([self.fold(src, into=tw)]):
                        break
        if self.query_kind == "stdout":
            self.try_emit([Print(self.sid(), Name(self.target))])
        prog = Program(
            body=tuple(self.body),
            query=Query(self.query_kind, self.target if self.query_kind == "final_value" else None),
            params=p,
            seed=self.seed,
            variables=tuple(self.vars.values()),
            distractor_ids=frozenset(self.distractor_ids),
            trap_sites=tuple(self.trap_sites),
            locals_pool=tuple(self.locals_used),
            path_sids=tuple(self.path_sids),
        )
        k = p.misleading_comments or p.neutral_comments
        if k:
            prog = replace(prog, comments=_plan_comments(prog, k, neutral=bool(p.neutral_comments)))
        return prog

    @staticmethod
    def _init_of(inits, vid):
        for s in inits:
            if s.target == vid:
                return s.value
        return None


def _names_in(node) -> set[str]:
    """Every variable id read anywhere inside a statement or expression."""
    out: set[str] = set()

    def walk(x):
        if isinstance(x, Name):
            out.add(x.id)
        elif isinstance(x, MethodCall):
            out.add(x.obj)
            for a in x.args:
                walk(a)
        elif isinstance(x, tuple):
            for y in x:
                walk(y)
        elif hasattr(x, "__dataclass_fields__"):
            for f in x.__dataclass_fields__:
                walk(getattr(x, f))

    walk(node)
    return out


def _snapshots(program: Program, sem) -> list:
    """Data globals after each top-level statement under `sem` (stops at a crash)."""
    it = Interpreter(sem)
    out = []
    try:
        for s in program.body:
            it.exec_top(s)
            out.append(copy.deepcopy(it.data_globals()))
    except InterpError:
        pass
    return out


def _plan_comments(program: Program, k: int, neutral: bool) -> tuple:
    """Comments for k top-level, non-distractor lines that write a variable
    which some foil reading holds at a DIFFERENT value right after that
    line: the comment asserts the foil's value there (a plausible wrong
    value). Lines that update the query target come first (latest first),
    then other writes (latest first). Neutral control: the same lines
    (neutral=True; the renderer writes a neutral comment of equal length)."""
    t = program.query.target
    if t is None:
        raise GenerationFailed("comments need a final_value query")
    truth = _snapshots(program, program.semantics)
    foil_snaps = {name: _snapshots(program, sem) for name, sem in foils_for(program).items()}
    sites = []
    for j in range(len(program.body) - 1, -1, -1):
        s = program.body[j]
        if s.sid in program.distractor_ids:
            continue
        if isinstance(s, (Assign, AugAssign)):
            w = s.target
        elif isinstance(s, ExprStmt) and isinstance(s.value, MethodCall):
            w = s.value.obj
        else:
            continue
        sites.append((w != t, j, s.sid, w))
    sites.sort(key=lambda x: (x[0], -x[1]))
    out = []
    for _, j, sid, w in sites:
        for name, snaps in foil_snaps.items():
            if j < len(snaps) and w in snaps[j] and snaps[j][w] != truth[j].get(w):
                out.append((j, (sid, w, snaps[j][w], name, neutral)))
                break
        if len(out) == k:
            return tuple(c for _, c in sorted(out))
    raise GenerationFailed("no line with a plausible wrong value for a comment")


def without_distractors(program: Program) -> Program:
    return replace(program, body=tuple(s for s in program.body if s.sid not in program.distractor_ids),
                   distractor_ids=frozenset())


def validate(program: Program) -> str | None:
    """None if the program satisfies every generation invariant (module
    docstring), else the reason it does not."""
    truth_sem = program.semantics
    tr = run(program, truth_sem)
    if tr.error is not None:
        return f"runtime error: {tr.error}"
    if tr.steps > MAX_GEN_STEPS:
        return "too many steps"
    truth = answer_from_trace(program, tr)
    readings = foils_for(program)
    foils = {name: answer(program, sem) for name, sem in readings.items()}
    if program.params.dialect:
        if foils[PYTHON_PRIOR] == truth:
            return "the plain Python reading agrees with the dialect"
        for rule in program.params.dialect:
            field_name, _ = DIALECT_SEMANTICS[rule]
            undone = replace(truth_sem, **{field_name: getattr(TRUE, field_name)})
            if answer(program, undone) == truth:
                return f"dialect rule {rule} is not answer-relevant"
    required, permitted = dial_foils(program.params)
    for trap, foil in ALL_TRAP_FOILS.items():
        if foil not in readings:  # the dialect declares this semantic; checked per rule above
            continue
        on = bool(getattr(program.params, trap))
        if on and foils[foil] == truth:
            return f"trap {trap} is not answer-relevant"
        if not on and foils[foil] != truth and foil not in permitted:
            return f"disabled trap {trap} still changes the answer"
    if program.params.round2_on():
        for name in sorted(required):
            if name in foils and foils[name] == truth:
                return f"mechanism foil {name} does not diverge"
        for name in ROUND2_FOILS:
            if name in foils and name not in permitted and foils[name] != truth:
                return f"round-2 foil {name} diverges with its mechanism off"
    twin = program.twin_of_target()
    if twin is not None and program.query.kind == "final_value":
        if tr.globals.get(twin) == truth:
            return "target's twin ends with the same value"
    if program.distractor_ids:
        stripped = without_distractors(program)
        if answer(stripped, truth_sem) != truth:
            return "a distractor changes the answer"
        for name, sem in readings.items():
            if answer(stripped, sem) != foils[name]:
                return f"a distractor changes the {name} reading"
    return None


def generate(params: CodeDimParams, seed: int, query_kind: str = "final_value") -> Program:
    """Deterministic in (params, seed, query_kind). Raises `GenerationFailed`
    if no valid program is found within the retry budget."""
    last = "no attempt"
    for attempt in range(_ATTEMPTS):
        s = seed if attempt == 0 else _stable_int(seed, "retry", attempt)
        try:
            prog = _Builder(params, s, query_kind).build()
        except GenerationFailed as e:
            last = str(e)
            continue
        reason = validate(prog)
        if reason is None:
            return replace(prog, seed=seed)
        last = reason
    raise GenerationFailed(f"no valid program for seed {seed}: {last}")


__all__ = ["GenerationFailed", "generate", "validate", "without_distractors", "TRAP_KEYS"]
