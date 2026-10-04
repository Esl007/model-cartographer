"""Render a `Program` to Python source and a question prompt.

Rendering never changes a value: variables are abstract ids in the AST and
only get names here. `naming="auto"` gives each look-alike twin pair a
confusable spelling (`total` / `totl`, `items` / `item`); `naming=
"descriptive"` gives every variable an unrelated descriptive name -- that is
the `rename_descriptive` ablation. `annotate` adds a comment on a trap's
line, which states the semantics without changing them.
"""

from __future__ import annotations

import keyword
from dataclasses import dataclass, field

from .generate import _Rng
from .model import (
    Assign,
    AugAssign,
    BinOp,
    BoolOp,
    Call,
    Compare,
    Const,
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
    iter_stmts,
)

_POOLS = {
    "int": ("total", "count", "score", "level", "offset", "limit", "step", "value", "size", "delta", "base",
            "extra", "rate", "bonus", "width", "height", "depth", "amount", "weight", "margin", "shift",
            "factor", "cost", "budget", "peak", "tally", "reserve", "quota", "credit", "spare"),
    "list": ("items", "values", "queue", "stack", "scores", "nums", "parts", "batch", "cells", "slots",
             "marks", "codes", "bins", "rows"),
    "str": ("label", "word", "tag", "text", "prefix", "title", "mode", "token"),
    "dict": ("config", "table", "stats", "counts"),
    "func": ("scale", "bump", "tally_up", "collect", "adjust", "combine", "shift_by", "grow", "apply_rate",
             "measure", "pack", "boost"),
    "p": ("v", "n", "amt", "arg", "val", "num", "k", "m", "q", "w", "bag", "acc"),
    "x": ("x", "elem", "entry", "cur", "e"),
    "i": ("i", "j", "idx", "r"),
}
_RESERVED = set(keyword.kwlist) | {"range", "len", "print", "list", "dict", "str", "int", "sum", "min", "max"}


def _lookalikes(word: str) -> list[str]:
    """Confusable spellings of `word`: a dropped vowel, a digit for a letter,
    a trailing underscore or `s`, a swapped pair. Never equal to `word`."""
    out: list[str] = []
    vowels = "aeiou"
    for i in range(1, len(word) - 1):
        if word[i] in vowels:
            out.append(word[:i] + word[i + 1:])
            break
    for a, b in (("l", "1"), ("o", "0"), ("i", "1"), ("e", "3")):
        j = word.find(a, 1)
        if j > 0:
            out.append(word[:j] + b + word[j + 1:])
            break
    out.append(word + "_")
    out.append(word[:-1] if word.endswith("s") else word + "s")
    if len(word) >= 4:
        out.append(word[:1] + word[2] + word[1] + word[3:])
    return [w for w in out if w != word and w.isidentifier() and w not in _RESERVED]


@dataclass(frozen=True)
class RenderedProgram:
    source: str
    prompt: str
    name_map: dict = field(default_factory=dict)  # id -> rendered name
    naming: str = "descriptive"  # "descriptive" | "lookalike"
    target_name: str | None = None
    query_kind: str = "final_value"

    def lookalike_of(self, program: Program, vid: str) -> str | None:
        """The twin id paired with `vid`, if this rendering made them look alike."""
        if self.naming != "lookalike":
            return None
        for a, b in program.twin_pairs():
            if a == vid:
                return b
            if b == vid:
                return a
        return None


def _all_ids(program: Program) -> list[tuple[str, str]]:
    """(id, pool-kind) for every name the source will contain, in a stable order."""
    seen: dict[str, str] = {}
    for v in program.variables:
        seen[v.id] = v.kind
    for vid in program.locals_pool:
        seen.setdefault(vid, vid[0])
    from .generate import _names_in

    _prefix_kind = {"v": "int", "f": "func", "p": "p", "x": "x", "i": "i"}
    for s in iter_stmts(program.body):  # anything the registry missed (hand-built programs)
        ids = set(_names_in(s))
        if isinstance(s, For):
            ids.add(s.var)
        elif isinstance(s, FuncDef):
            ids.update(s.params)
            ids.add(s.name)
        tgt = getattr(s, "target", None)
        if isinstance(tgt, str):
            ids.add(tgt)
        for vid in sorted(ids):
            seen.setdefault(vid, _prefix_kind.get(vid[0], "int"))
    for s in iter_stmts(program.body):
        for e in _calls_in(s):
            seen.setdefault(e, "func")
    return list(seen.items())


def _calls_in(node) -> set[str]:
    out: set[str] = set()

    def walk(x):
        if isinstance(x, Call):
            out.add(x.func)
        if isinstance(x, tuple):
            for y in x:
                walk(y)
        elif hasattr(x, "__dataclass_fields__"):
            for f in x.__dataclass_fields__:
                walk(getattr(x, f))

    walk(node)
    return out


def build_names(program: Program, rendering_seed: int, naming: str) -> tuple[dict, str]:
    rng = _Rng("codeworld-render", program.seed, rendering_seed)
    used: set[str] = set(_RESERVED)
    names: dict[str, str] = {}
    pools = {k: list(v) for k, v in _POOLS.items()}
    lookalike = naming == "auto" and any(v.twin_of for v in program.variables)
    twins = {v.id: v.twin_of for v in program.variables if v.twin_of}

    def fresh(kind: str) -> str:
        pool = [w for w in pools.get(kind, ()) if w not in used]
        if pool:
            w = rng.choice(pool)
        else:
            base = rng.choice(_POOLS.get(kind, ("var",)))
            k = 2
            while f"{base}_{k}" in used:
                k += 1
            w = f"{base}_{k}"
        used.add(w)
        return w

    ids = _all_ids(program)
    for vid, kind in ids:  # bases first, so a twin can derive from its base's name
        if vid not in twins:
            names[vid] = fresh(kind)
    for vid, kind in ids:
        if vid in twins:
            if lookalike:
                options = [w for w in _lookalikes(names[twins[vid]]) if w not in used]
                if options:
                    w = rng.choice(options)
                    used.add(w)
                    names[vid] = w
                    continue
            names[vid] = fresh(kind)
    return names, ("lookalike" if lookalike else "descriptive")


# -- expressions ---------------------------------------------------------------

_BIN_PREC = {"+": 6, "-": 6, "*": 7, "//": 7, "%": 7}


def _expr(e, n: dict) -> tuple[str, int]:
    if isinstance(e, Const):
        if isinstance(e.value, int) and not isinstance(e.value, bool) and e.value < 0:
            return repr(e.value), 8
        return repr(e.value), 10
    if isinstance(e, OpaqueLit):
        return repr(e.value), 10
    if isinstance(e, Name):
        return n[e.id], 10
    if isinstance(e, ListLit):
        return "[" + ", ".join(_wrap(x, n, 1) for x in e.items) + "]", 10
    if isinstance(e, DictLit):
        return "{" + ", ".join(f"{k!r}: {_wrap(v, n, 1)}" for k, v in zip(e.keys, e.values)) + "}", 10
    if isinstance(e, BinOp):
        p = _BIN_PREC[e.op]
        return f"{_wrap(e.left, n, p)} {e.op} {_wrap(e.right, n, p + 1)}", p
    if isinstance(e, Compare):
        return f"{_wrap(e.left, n, 6)} {e.op} {_wrap(e.right, n, 6)}", 5
    if isinstance(e, BoolOp):
        p = 3 if e.op == "and" else 2
        return f"{_wrap(e.left, n, p)} {e.op} {_wrap(e.right, n, p + 1)}", p
    if isinstance(e, Not):
        return f"not {_wrap(e.operand, n, 4)}", 4
    if isinstance(e, Index):
        return f"{_wrap(e.target, n, 9)}[{_wrap(e.index, n, 1)}]", 9
    if isinstance(e, Slice):
        lo = "" if e.lo is None else _wrap(e.lo, n, 1)
        hi = "" if e.hi is None else _wrap(e.hi, n, 1)
        return f"{_wrap(e.target, n, 9)}[{lo}:{hi}]", 9
    if isinstance(e, Len):
        return f"len({_wrap(e.arg, n, 1)})", 10
    if isinstance(e, IfExp):
        return f"{_wrap(e.body, n, 2)} if {_wrap(e.test, n, 2)} else {_wrap(e.orelse, n, 1)}", 1
    if isinstance(e, Call):
        return f"{n[e.func]}(" + ", ".join(_wrap(a, n, 1) for a in e.args) + ")", 9
    if isinstance(e, MethodCall):
        return f"{n[e.obj]}.{e.method}(" + ", ".join(_wrap(a, n, 1) for a in e.args) + ")", 9
    raise TypeError(type(e).__name__)


def _wrap(e, n: dict, min_prec: int) -> str:
    text, prec = _expr(e, n)
    return f"({text})" if prec < min_prec else text


def render_expr(e, names: dict) -> str:
    return _wrap(e, names, 0)


# -- statements ------------------------------------------------------------------


def _free_names(fd: FuncDef) -> list[str]:
    from .generate import _names_in

    local = set(fd.params) | {s.target for s in iter_stmts(fd.body) if isinstance(s, (Assign, AugAssign))}
    return sorted(_names_in(fd.body) - local)


def _stmt_lines(s, n: dict, indent: int, notes: dict) -> list[str]:
    pad = "    " * indent
    note = notes.get(s.sid)
    tail = f"  # {note}" if note else ""
    if isinstance(s, Assign):
        return [f"{pad}{n[s.target]} = {render_expr(s.value, n)}{tail}"]
    if isinstance(s, SetItem):
        return [f"{pad}{n[s.target]}[{render_expr(s.index, n)}] = {render_expr(s.value, n)}{tail}"]
    if isinstance(s, AugAssign):
        return [f"{pad}{n[s.target]} {s.op}= {render_expr(s.value, n)}{tail}"]
    if isinstance(s, ExprStmt):
        return [f"{pad}{render_expr(s.value, n)}{tail}"]
    if isinstance(s, Print):
        return [f"{pad}print({render_expr(s.value, n)}){tail}"]
    if isinstance(s, Return):
        return [f"{pad}return {render_expr(s.value, n)}{tail}"]
    if isinstance(s, If):
        lines = [f"{pad}if {render_expr(s.test, n)}:{tail}"]
        lines += _block(s.body, n, indent + 1, notes)
        orelse = s.orelse
        while len(orelse) == 1 and isinstance(orelse[0], If):
            inner = orelse[0]
            lines.append(f"{pad}elif {render_expr(inner.test, n)}:")
            lines += _block(inner.body, n, indent + 1, notes)
            orelse = inner.orelse
        if orelse:
            lines.append(f"{pad}else:")
            lines += _block(orelse, n, indent + 1, notes)
        return lines
    if isinstance(s, For):
        if isinstance(s.iter, RangeIter):
            it = f"range({s.iter.hi})" if s.iter.lo is None else f"range({s.iter.lo}, {s.iter.hi})"
        else:
            it = render_expr(s.iter, n)
        return [f"{pad}for {n[s.var]} in {it}:{tail}"] + _block(s.body, n, indent + 1, notes)
    if isinstance(s, FuncDef):
        n_req = len(s.params) - len(s.defaults)
        parts = [n[p] for p in s.params[:n_req]]
        parts += [f"{n[p]}={render_expr(d, n)}" for p, d in zip(s.params[n_req:], s.defaults)]
        return [f"{pad}def {n[s.name]}({', '.join(parts)}):{tail}"] + _block(s.body, n, indent + 1, notes)
    raise TypeError(type(s).__name__)


def _block(body, n: dict, indent: int, notes: dict) -> list[str]:
    out: list[str] = []
    for s in body:
        out += _stmt_lines(s, n, indent, notes)
    return out


def _notes(program: Program, n: dict, annotate) -> dict:
    notes: dict[int, str] = {}
    by_sid = {s.sid: s for s in iter_stmts(program.body)}
    for trap, sid in program.trap_sites:
        s = by_sid.get(sid)
        if trap == "aliasing_on" and "aliasing" in annotate and isinstance(s, Assign) \
                and isinstance(s.value, Name):
            notes[sid] = f"{n[s.target]} and {n[s.value.id]} now refer to the same list"
        if trap == "late_binding_on" and "closures" in annotate and isinstance(s, FuncDef):
            free = _free_names(s)
            if free:
                notes[sid] = f"{n[s.name]} looks up {n[free[0]]} when it is called, not when it is defined"
    return notes


def render(program: Program, rendering_seed: int = 0, naming: str = "auto",
           annotate: tuple = ()) -> RenderedProgram:
    """`naming`: "auto" (look-alike spellings for twin pairs, when the program
    has any) or "descriptive". `annotate`: subset of {"aliasing", "closures"}."""
    names, style = build_names(program, rendering_seed, naming)
    notes = _notes(program, names, annotate)
    lines: list[str] = []
    for i, s in enumerate(program.body):
        if isinstance(s, FuncDef) and lines and lines[-1] != "":
            lines.append("")
        lines += _stmt_lines(s, names, 0, notes)
        if isinstance(s, FuncDef) and i + 1 < len(program.body):
            lines.append("")
    source = "\n".join(lines) + "\n"
    if program.query.kind == "stdout":
        target_name = None
        question = (
            "What does this program print? Work through it carefully, then give the printed lines "
            "as a Python list of strings on its own last line, formatted as `ANSWER: [...]`."
        )
    else:
        target_name = names[program.query.target]
        question = (
            f"After this program runs, what is the value of `{target_name}`? Work through it carefully, "
            "then give the final answer on its own last line as `ANSWER: <python literal>`."
        )
    prompt = f"Here is a short Python program.\n\n```python\n{source}```\n\n{question}"
    return RenderedProgram(source=source, prompt=prompt, name_map=names, naming=style,
                           target_name=target_name, query_kind=program.query.kind)
