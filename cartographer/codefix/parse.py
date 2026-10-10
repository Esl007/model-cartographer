"""Source text -> our `Program` AST, or `SubsetViolation` (Province V, docs/province-v-spec.md §0.3, §3.1).

Model-written code is NEVER executed as Python. It is read with the standard
`ast` module (`ast.parse` only builds a syntax tree; nothing is compiled to
bytecode or run) and converted into `codeworld.model` nodes, which only our
bounded interpreter executes. The accepted subset is exactly what
`codeworld.render` can produce, plus a few harmless spellings a model writes
naturally (documented below). Anything else raises `SubsetViolation` naming
the construct, before any execution.

Accepted (beyond the renderer's own output):
  * parameter / return annotations and annotated assignments `x: int = 0`
    (annotations are dropped, never evaluated);
  * `pass` (-> an expression statement `None`), bare `return` (-> `return None`);
  * unary minus on a non-literal, `-x` (-> `0 - x`, identical on ints);
  * `else: if ...` (re-rendered canonically as `elif`).

Model-written constructs the generator never emits (accepted so a correct
answer is not swallowed as `outside_subset`):
  * `for x in range(a)` / `range(a, b)` whose bounds are any subset
    expressions (`range(len(xs))`, `range(1, n)`); an int-literal bound stays
    an int in `RangeIter`. `range` with a step argument (`unknown`,
    `range_step`) and `range` anywhere but a `for` header (`builtin_call`)
    are still rejected;
  * membership `x in xs` / `x not in xs` (one operator; list / str / dict
    key), still subject to the `chained_comparison` rule;
  * `break` / `continue` inside a `for` / `while` body (also nested inside an
    `if` there); anywhere else -> `unknown` (`break_outside_loop` /
    `continue_outside_loop`).
  * the deterministic builtins `sum(xs)`, `min(..)`, `max(..)` (one
    iterable or 2+ positional args) and `abs(x)` (`model.Builtin`, evaluated
    with CPython's own semantics; empty `min` / `max` is a runtime
    `value_error`). No keywords (`key=`, `default=`, sum's `start`); every
    other builtin call (`sorted`, `range` outside a `for` header, ...) is
    still `builtin_call`.

Unbounded `while` (`while_true_unbounded`, a conservative syntactic check;
the interpreter's step cap stays the safety net for everything it lets
through): rejected when the test reads no variable / call at all (`while
True`, `while 1`, `while 1 == 1`), or when the test reads no name that the
loop body (re)binds -- an assignment, augmented assignment or `for` target
anywhere inside the body. It is NOT rejected (it cannot be told unbounded
syntactically) when the test itself contains a call or a mutating method
(`append` / `pop` / `extend`), or the body contains any call of a program
function or any in-place mutation (`xs.pop()`, `xs[i] = v`), since aliasing
or a callee may change what the test reads.

Statement ids are assigned 1..n in source order (pre-order). With
`keep_names=True` the source identifiers ARE the ids, so a program can be
re-rendered with its own names (`render_source`); otherwise identifiers get
abstract ids in first-occurrence order (`v*` top-level data, `f*` defs, `p*`
parameters and function locals, `i*` range-loop variables, `x*` other loop
variables), which `codeworld.render.render` names like a generated program.
"""

from __future__ import annotations

import ast
import builtins
import sys
from dataclasses import dataclass

from cartographer.codeworld.model import (
    BUILTIN_FUNCS,
    METHODS,
    Assign,
    AugAssign,
    BinOp,
    BoolOp,
    Break,
    Call,
    CodeDimParams,
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
    Builtin,
    Len,
    ListLit,
    MethodCall,
    Name,
    Not,
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

# Stable construct names (the `outside_subset:<construct>` signature suffix).
CONSTRUCTS = (
    "import", "class", "lambda", "comprehension", "try", "fstring", "while_true_unbounded", "recursion",
    "global_nonlocal", "attribute", "float", "power", "is_operator", "chained_comparison", "tuple_unpacking",
    "delete", "assert", "starargs", "nested_def_depth", "print_multiple_args", "builtin_call", "walrus",
    "match", "async", "with", "raise", "yield", "slice_step", "keyword_argument", "augassign_op", "unknown",
    # repository mode only (Province VI, `parse_module`; docs/province-vi-spec.md §4.5)
    "foreign_import", "import_alias", "import_star", "relative_import", "module_statement",
)

MAX_SOURCE_CHARS = 50_000
_BUILTIN_NAMES = frozenset(dir(builtins))
_OWN_BUILTINS = frozenset({"len", "print", "range"})  # builtins with their own syntax in the subset
# deterministic builtins evaluated with CPython semantics (model.Builtin); every other builtin is rejected
_ARITY = {"sum": (1, 1), "min": (1, None), "max": (1, None), "abs": (1, 1)}
_BINOPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//", ast.Mod: "%"}
_AUGOPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*"}
_CMPOPS = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
           ast.In: "in", ast.NotIn: "not in"}
_MUTATING_METHODS = frozenset({"append", "pop", "extend"})


class SubsetViolation(Exception):
    """The source uses a construct outside the subset. `construct` is one of
    `CONSTRUCTS`; `detail` refines `unknown` (e.g. `range_step`,
    `break_outside_loop`) and is informational only."""

    def __init__(self, construct: str, lineno: int, detail: str = "") -> None:
        self.construct = construct
        self.lineno = lineno
        self.detail = detail
        super().__init__(f"outside subset: {construct}" + (f" ({detail})" if detail else "") + f" at line {lineno}")


class CodeParseError(Exception):
    """Not Python at all (syntax error), too long, or nested too deeply to read."""

    def __init__(self, message: str, lineno: int = 0) -> None:
        self.lineno = lineno
        super().__init__(message)


def _line(node) -> int:
    return int(getattr(node, "lineno", 0) or 0)


def _bad(construct: str, node, detail: str = "") -> SubsetViolation:
    return SubsetViolation(construct, _line(node), detail)


# Statement / expression node classes that map straight to a construct name.
_STMT_CONSTRUCT = {
    ast.Import: "import", ast.ImportFrom: "import", ast.ClassDef: "class", ast.Try: "try",
    ast.Global: "global_nonlocal", ast.Nonlocal: "global_nonlocal", ast.Delete: "delete", ast.Assert: "assert",
    ast.With: "with", ast.AsyncWith: "async", ast.AsyncFor: "async", ast.AsyncFunctionDef: "async",
    ast.Raise: "raise", ast.Match: "match",
}
if hasattr(ast, "TryStar"):
    _STMT_CONSTRUCT[ast.TryStar] = "try"
_EXPR_CONSTRUCT = {
    ast.Lambda: "lambda", ast.ListComp: "comprehension", ast.SetComp: "comprehension",
    ast.DictComp: "comprehension", ast.GeneratorExp: "comprehension", ast.JoinedStr: "fstring",
    ast.FormattedValue: "fstring", ast.NamedExpr: "walrus", ast.Await: "async", ast.Yield: "yield",
    ast.YieldFrom: "yield", ast.Starred: "starargs",
}


@dataclass
class _Scope:
    func: str | None = None  # identifier of the enclosing def (None at module level)
    in_block: bool = False  # inside an if/for/while (a def there is not module-level)
    in_loop: bool = False  # inside a for/while body (break / continue allowed)


class _Converter:
    def __init__(self, keep_names: bool) -> None:
        self.keep_names = keep_names
        self.ids: dict[str, str] = {}
        self._counters = {"v": 0, "f": 0, "p": 0, "i": 0, "x": 0}
        self._sid = 0
        self.def_names: set[str] = set()
        self.bound: set[str] = set()
        self.calls: dict[str, list[tuple[str, int]]] = {}  # def identifier -> [(callee identifier, line)]

    # -- ids -------------------------------------------------------------------

    def sid(self) -> int:
        self._sid += 1
        return self._sid

    def vid(self, name: str, prefix: str, node) -> str:
        if name.startswith("__") and name.endswith("__"):
            raise _bad("builtin_call", node, name)
        if name in self.ids:
            return self.ids[name]
        if self.keep_names:
            vid = name
        else:
            n = self._counters[prefix]
            self._counters[prefix] = n + 1
            vid = f"{prefix}{n}"
        self.ids[name] = vid
        return vid

    def read(self, name: str, node, scope: _Scope) -> str:
        if name in _BUILTIN_NAMES and name not in self.bound:
            raise _bad("builtin_call", node, name)
        return self.vid(name, "f" if name in self.def_names else ("v" if scope.func is None else "p"), node)

    def bind(self, name: str, node, scope: _Scope, prefix: str | None = None) -> str:
        if name in _OWN_BUILTINS:
            raise _bad("unknown", node, f"shadows_builtin:{name}")
        if prefix is None:
            prefix = "v" if scope.func is None else "p"
        return self.vid(name, prefix, node)

    # -- module --------------------------------------------------------------

    def module(self, tree: ast.Module) -> tuple:
        for node in ast.walk(tree):  # pre-pass: which identifiers the program binds itself
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.bound.add(node.id)
            elif isinstance(node, ast.arg):
                self.bound.add(node.arg)
            elif isinstance(node, ast.FunctionDef):
                self.bound.add(node.name)
                self.def_names.add(node.name)
        body = self.block(tree.body, _Scope())
        self._check_cycles()
        return body

    def _check_cycles(self) -> None:
        state: dict[str, int] = {}

        def visit(f: str) -> None:
            state[f] = 1
            for g, line in self.calls.get(f, ()):
                if state.get(g) == 1:
                    raise SubsetViolation("recursion", line, f"{f}->{g}")
                if g in self.calls and g not in state:
                    visit(g)
            state[f] = 2

        for f in sorted(self.calls):
            if f not in state:
                visit(f)

    def block(self, stmts, scope: _Scope) -> tuple:
        return tuple(self.stmt(s, scope) for s in stmts)

    # -- statements ------------------------------------------------------------

    def stmt(self, s, scope: _Scope):
        construct = _STMT_CONSTRUCT.get(type(s))
        if construct is not None:
            raise _bad(construct, s)
        if isinstance(s, ast.FunctionDef):
            return self.funcdef(s, scope)
        if isinstance(s, ast.Assign):
            if len(s.targets) != 1:
                raise _bad("unknown", s, "multiple_targets")
            return self.assign(s.targets[0], s.value, s, scope)
        if isinstance(s, ast.AnnAssign):
            if s.value is None:
                raise _bad("unknown", s, "bare_annotation")
            return self.assign(s.target, s.value, s, scope)
        if isinstance(s, ast.AugAssign):
            t = s.target
            if isinstance(t, ast.Attribute):
                raise _bad("attribute", s)
            if not isinstance(t, ast.Name):
                raise _bad("unknown", s, "augassign_target")
            op = _AUGOPS.get(type(s.op))
            if op is None:
                raise _bad("augassign_op", s, type(s.op).__name__)
            sid = self.sid()
            value = self.expr(s.value, scope)
            return AugAssign(sid, self.bind(t.id, t, scope), op, value)
        if isinstance(s, ast.If):
            sid = self.sid()
            test = self.expr(s.test, scope)
            inner = _Scope(scope.func, True, scope.in_loop)
            return If(sid, test, self.block(s.body, inner), self.block(s.orelse, inner))
        if isinstance(s, ast.For):
            return self.for_(s, scope)
        if isinstance(s, ast.While):
            if s.orelse:
                raise _bad("unknown", s, "while_else")
            constant_test = not any(isinstance(n, (ast.Name, ast.Call, ast.Attribute)) for n in ast.walk(s.test))
            # `while True: ... break` is a common correct idiom: accept a constant test
            # when the body contains a break (the step cap stays the safety net).
            has_break = any(isinstance(n, ast.Break) for n in ast.walk(s))
            if constant_test and not has_break:
                raise _bad("while_true_unbounded", s, "constant test")
            sid = self.sid()
            test = self.expr(s.test, scope)
            body = self.block(s.body, _Scope(scope.func, True, True))
            unchanged = None if (constant_test or has_break) else _while_test_unchanged(test, body)
            if unchanged is not None:
                back = {v: k for k, v in self.ids.items()}
                names = ", ".join(sorted(back.get(v, v) for v in unchanged))
                raise _bad("while_true_unbounded", s, f"test reads {names}, never reassigned in the loop body")
            return While(sid, test, body)
        if isinstance(s, ast.Return):
            if scope.func is None:
                raise _bad("unknown", s, "return_outside_function")
            sid = self.sid()
            return Return(sid, Const(None) if s.value is None else self.expr(s.value, scope))
        if isinstance(s, ast.Expr):
            v = s.value
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "print" \
                    and "print" not in self.bound:
                if v.keywords:
                    raise _bad("keyword_argument", v)
                if any(isinstance(a, ast.Starred) for a in v.args):
                    raise _bad("starargs", v)
                if len(v.args) != 1:
                    raise _bad("print_multiple_args", v, f"{len(v.args)} args")
                sid = self.sid()
                return Print(sid, self.expr(v.args[0], scope))
            sid = self.sid()
            return ExprStmt(sid, self.expr(v, scope))
        if isinstance(s, ast.Pass):
            return ExprStmt(self.sid(), Const(None))
        if isinstance(s, (ast.Break, ast.Continue)):
            kind = type(s).__name__.lower()
            if not scope.in_loop:
                raise _bad("unknown", s, f"{kind}_outside_loop")
            return (Break if kind == "break" else Continue)(self.sid())
        raise _bad("unknown", s, type(s).__name__)

    def assign(self, t, value, s, scope: _Scope):
        if isinstance(t, (ast.Tuple, ast.List)):
            raise _bad("tuple_unpacking", s)
        if isinstance(t, ast.Starred):
            raise _bad("starargs", s)
        if isinstance(t, ast.Attribute):
            raise _bad("attribute", s)
        if isinstance(t, ast.Subscript):
            if isinstance(t.slice, ast.Slice):
                raise _bad("slice_step" if t.slice.step is not None else "unknown", s, "slice_assignment")
            if not isinstance(t.value, ast.Name):
                raise _bad("unknown", s, "nested_setitem")
            sid = self.sid()
            target = self.read(t.value.id, t.value, scope)
            index = self.expr(t.slice, scope)
            return SetItem(sid, target, index, self.expr(value, scope))
        if not isinstance(t, ast.Name):
            raise _bad("unknown", s, type(t).__name__)
        sid = self.sid()
        v = self.expr(value, scope)
        return Assign(sid, self.bind(t.id, t, scope), v)

    def for_(self, s: ast.For, scope: _Scope):
        if s.orelse:
            raise _bad("unknown", s, "for_else")
        if isinstance(s.target, (ast.Tuple, ast.List)):
            raise _bad("tuple_unpacking", s)
        if not isinstance(s.target, ast.Name):
            raise _bad("unknown", s, "for_target")
        it = s.iter
        sid = self.sid()
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range" \
                and "range" not in self.bound:
            if it.keywords:
                raise _bad("keyword_argument", it)
            if any(isinstance(a, ast.Starred) for a in it.args):
                raise _bad("starargs", it)
            if not 1 <= len(it.args) <= 2:
                raise _bad("unknown", it, "range_step" if len(it.args) == 3 else "range_arity")
            bounds = [b if (b := _int_literal(a)) is not None else self.expr(a, scope) for a in it.args]
            iterator = RangeIter(None, bounds[0]) if len(bounds) == 1 else RangeIter(bounds[0], bounds[1])
            var = self.bind(s.target.id, s.target, scope, "i" if s.target.id not in self.ids else None)
        elif isinstance(it, ast.Name):
            iterator = Name(self.read(it.id, it, scope))
            var = self.bind(s.target.id, s.target, scope, "x" if s.target.id not in self.ids else None)
        else:
            self.expr(it, scope)  # name the construct if it is a known one (comprehension, call, ...)
            raise _bad("unknown", it, "for_iter")
        return For(sid, var, iterator, self.block(s.body, _Scope(scope.func, True, True)))

    def funcdef(self, s: ast.FunctionDef, scope: _Scope):
        if scope.func is not None or scope.in_block:
            raise _bad("nested_def_depth", s, "def inside a def" if scope.func else "def inside a block")
        if s.decorator_list:
            raise _bad("unknown", s, "decorator")
        if getattr(s, "type_params", None):
            raise _bad("unknown", s, "type_params")
        a = s.args
        if a.vararg is not None or a.kwarg is not None:
            raise _bad("starargs", s)
        if a.kwonlyargs:
            raise _bad("keyword_argument", s, "keyword-only parameter")
        if a.posonlyargs:
            raise _bad("unknown", s, "positional_only_marker")
        sid = self.sid()
        name = self.vid(s.name, "f", s)
        if s.name in _OWN_BUILTINS:
            raise _bad("unknown", s, f"shadows_builtin:{s.name}")
        inner = _Scope(s.name, False)
        defaults = tuple(self.default(d, inner) for d in a.defaults)
        params = tuple(self.bind(p.arg, p, inner, "p") for p in a.args)
        if len(set(params)) != len(params):
            raise _bad("unknown", s, "duplicate_parameter")
        self.calls.setdefault(s.name, [])
        body = self.block(s.body, inner)
        return FuncDef(sid, name, params, defaults, body)

    def default(self, d, scope: _Scope):
        if _int_literal(d) is not None:
            return Const(_int_literal(d))
        if isinstance(d, ast.Constant) or isinstance(d, (ast.List, ast.Dict)):
            e = self.expr(d, _Scope())
            if _is_literal(e):
                return e
        self.expr(d, _Scope(scope.func, scope.in_block))  # a named construct beats `nonliteral_default`
        raise _bad("unknown", d, "nonliteral_default")

    # -- expressions -------------------------------------------------------------

    def expr(self, e, scope: _Scope):
        construct = _EXPR_CONSTRUCT.get(type(e))
        if construct is not None:
            raise _bad(construct, e)
        if isinstance(e, ast.Constant):
            v = e.value
            if isinstance(v, (float, complex)):
                raise _bad("float", e)
            if v is None or isinstance(v, (bool, int, str)):
                return Const(v)
            raise _bad("unknown", e, type(v).__name__)
        if isinstance(e, ast.Name):
            return Name(self.read(e.id, e, scope))
        if isinstance(e, ast.List):
            return ListLit(tuple(self.expr(x, scope) for x in e.elts))
        if isinstance(e, ast.Dict):
            keys = []
            for k in e.keys:
                if k is None:
                    raise _bad("starargs", e, "dict unpacking")
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    if isinstance(k, ast.Constant) and isinstance(k.value, float):
                        raise _bad("float", k)
                    raise _bad("unknown", k, "non_str_dict_key")
                keys.append(k.value)
            return DictLit(tuple(keys), tuple(self.expr(v, scope) for v in e.values))
        if isinstance(e, ast.BinOp):
            if isinstance(e.op, ast.Pow):
                raise _bad("power", e)
            if isinstance(e.op, ast.Div):
                raise _bad("float", e, "true division")
            op = _BINOPS.get(type(e.op))
            if op is None:
                raise _bad("unknown", e, type(e.op).__name__)
            return BinOp(op, self.expr(e.left, scope), self.expr(e.right, scope))
        if isinstance(e, ast.UnaryOp):
            if isinstance(e.op, ast.Not):
                return Not(self.expr(e.operand, scope))
            if isinstance(e.op, ast.USub):
                lit = _int_literal(e)
                if lit is not None:
                    return Const(lit)
                return BinOp("-", Const(0), self.expr(e.operand, scope))
            raise _bad("unknown", e, type(e.op).__name__)
        if isinstance(e, ast.BoolOp):
            op = "and" if isinstance(e.op, ast.And) else "or"
            vals = [self.expr(v, scope) for v in e.values]
            out = vals[0]
            for v in vals[1:]:
                out = BoolOp(op, out, v)
            return out
        if isinstance(e, ast.Compare):
            if len(e.ops) != 1:
                raise _bad("chained_comparison", e)
            if isinstance(e.ops[0], (ast.Is, ast.IsNot)):
                raise _bad("is_operator", e)
            op = _CMPOPS.get(type(e.ops[0]))
            if op is None:
                raise _bad("unknown", e, type(e.ops[0]).__name__)
            return Compare(op, self.expr(e.left, scope), self.expr(e.comparators[0], scope))
        if isinstance(e, ast.IfExp):
            return IfExp(self.expr(e.test, scope), self.expr(e.body, scope), self.expr(e.orelse, scope))
        if isinstance(e, ast.Subscript):
            target = self.expr(e.value, scope)
            sl = e.slice
            if isinstance(sl, ast.Slice):
                if sl.step is not None:
                    raise _bad("slice_step", e)
                lo = None if sl.lower is None else self.expr(sl.lower, scope)
                hi = None if sl.upper is None else self.expr(sl.upper, scope)
                return Slice(target, lo, hi)
            if isinstance(sl, ast.Tuple):
                raise _bad("unknown", e, "tuple_index")
            return Index(target, self.expr(sl, scope))
        if isinstance(e, ast.Call):
            return self.call(e, scope)
        if isinstance(e, ast.Attribute):
            raise _bad("attribute", e, e.attr)
        if isinstance(e, ast.Tuple):
            raise _bad("unknown", e, "tuple")
        raise _bad("unknown", e, type(e).__name__)

    def call(self, e: ast.Call, scope: _Scope):
        f = e.func
        if isinstance(f, ast.Attribute):
            if not isinstance(f.value, ast.Name):
                self.expr(f.value, scope)  # `__import__("os").system(..)` is named by its inner call
            if not isinstance(f.value, ast.Name) or f.attr not in METHODS:
                raise _bad("attribute", e, f.attr)
            obj = self.read(f.value.id, f.value, scope)
            self._call_args(e)
            return MethodCall(obj, f.attr, tuple(self.expr(a, scope) for a in e.args))
        if not isinstance(f, ast.Name):
            raise _bad("unknown", e, "call_of_expression")
        name = f.id
        if name == "len" and name not in self.bound:
            self._call_args(e)
            if len(e.args) != 1:
                raise _bad("unknown", e, "len_arity")
            return Len(self.expr(e.args[0], scope))
        if name in BUILTIN_FUNCS and name not in self.bound and name not in self.def_names:
            self._call_args(e)
            lo, hi = _ARITY[name]
            if len(e.args) < lo or (hi is not None and len(e.args) > hi):
                raise _bad("unknown", e, f"{name}_arity")
            return Builtin(name, tuple(self.expr(a, scope) for a in e.args))
        if name not in self.def_names:  # input, open, __import__, eval, exec, getattr, range(...), sum, ...
            raise _bad("builtin_call", e, name)
        self._call_args(e)
        if scope.func is not None:
            if name == scope.func:
                raise _bad("recursion", e, name)
            self.calls.setdefault(scope.func, []).append((name, _line(e)))
        return Call(self.read(name, f, scope), tuple(self.expr(a, scope) for a in e.args))

    @staticmethod
    def _call_args(e: ast.Call) -> None:
        if e.keywords:
            if any(k.arg is None for k in e.keywords):
                raise _bad("starargs", e, "**kwargs")
            raise _bad("keyword_argument", e)
        if any(isinstance(a, ast.Starred) for a in e.args):
            raise _bad("starargs", e)


def _walk_nodes(x):
    """Every model node (expressions and statements) inside `x`, depth-first."""
    if isinstance(x, tuple):
        for y in x:
            yield from _walk_nodes(y)
    elif hasattr(x, "__dataclass_fields__"):
        yield x
        for f in x.__dataclass_fields__:
            yield from _walk_nodes(getattr(x, f))


def _while_test_unchanged(test, body) -> set | None:
    """The names a `while` test reads if NONE of them can change inside the
    loop body (the loop can then never exit through its test), else None.
    Conservative: any call, mutating method or in-place store in the body,
    or any call / mutating method in the test, means "may change"."""
    reads: set[str] = set()
    for n in _walk_nodes(test):
        if isinstance(n, Call) or isinstance(n, MethodCall) and n.method in _MUTATING_METHODS:
            return None
        if isinstance(n, Name):
            reads.add(n.id)
        elif isinstance(n, MethodCall):
            reads.add(n.obj)
    if not reads:
        return None  # a constant test is caught earlier, on the source
    assigned: set[str] = set()
    for n in _walk_nodes(body):
        if isinstance(n, (Call, SetItem)) or isinstance(n, MethodCall) and n.method in _MUTATING_METHODS:
            return None
        if isinstance(n, (Assign, AugAssign)):
            assigned.add(n.target)
        elif isinstance(n, For):
            assigned.add(n.var)
    return None if reads & assigned else reads


def _int_literal(node) -> int | None:
    """`3` or `-3` (a non-bool int literal), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
            and isinstance(node.operand.value, int) and not isinstance(node.operand.value, bool):
        return -node.operand.value
    return None


def _is_literal(e) -> bool:
    if isinstance(e, Const):
        return True
    if isinstance(e, ListLit):
        return all(_is_literal(x) for x in e.items)
    if isinstance(e, DictLit):
        return all(_is_literal(x) for x in e.values)
    return False


# -- metadata -----------------------------------------------------------------------


def _kind_of(e, kinds: dict) -> str:
    if isinstance(e, ListLit) or isinstance(e, Slice) and _kind_of(e.target, kinds) == "list":
        return "list"
    if isinstance(e, DictLit):
        return "dict"
    if isinstance(e, Const):
        return "str" if isinstance(e.value, str) else "int"
    if isinstance(e, Name):
        return kinds.get(e.id, "int")
    if isinstance(e, Slice):
        return _kind_of(e.target, kinds)
    if isinstance(e, MethodCall) and e.method in ("upper", "replace"):
        return "str"
    if isinstance(e, BinOp) and e.op == "+":
        k = _kind_of(e.left, kinds)
        return k if k in ("list", "str") else "int"
    return "int"


def _top_level_stmts(body):
    """Module-level statements, descending into if/for/while but not defs."""
    for s in body:
        yield s
        if isinstance(s, If):
            yield from _top_level_stmts(s.body)
            yield from _top_level_stmts(s.orelse)
        elif isinstance(s, (For, While)):
            yield from _top_level_stmts(s.body)


def _metadata(body: tuple, entry: str | None, entry_id: str | None):
    kinds: dict[str, str] = {}
    order: list[str] = []
    loop_vars: list[str] = []
    for s in _top_level_stmts(body):
        if isinstance(s, FuncDef):
            if s.name not in kinds:
                kinds[s.name] = "func"
                order.append(s.name)
        elif isinstance(s, (Assign, AugAssign)) and s.target not in kinds:
            kinds[s.target] = _kind_of(s.value, kinds) if isinstance(s, Assign) else "int"
            order.append(s.target)
        elif isinstance(s, For):
            loop_vars.append(s.var)
    variables = tuple(VarInfo(v, kinds[v], "live") for v in order)
    pool: list[str] = []
    for s in body:
        if isinstance(s, FuncDef):
            pool += list(s.params)
            pool += [t.target if not isinstance(t, For) else t.var for t in iter_stmts(s.body)
                     if isinstance(t, (Assign, AugAssign, For))]
    pool += loop_vars
    seen = set(kinds)
    locals_pool = []
    for v in pool:
        if v not in seen:
            seen.add(v)
            locals_pool.append(v)
    query = None
    if entry_id is not None:
        for s in body:
            if isinstance(s, FuncDef) and s.name == entry_id and s.body and isinstance(s.body[-1], Return) \
                    and isinstance(s.body[-1].value, Name):
                query = Query("final_value", s.body[-1].value.id)
    if query is None:
        last = [s.target for s in _top_level_stmts(body) if isinstance(s, (Assign, AugAssign))]
        query = Query("final_value", last[-1]) if last else Query("stdout", None)
    return variables, tuple(locals_pool), query


def parse_program(source: str, *, entry: str | None = None, keep_names: bool = False,
                  params: CodeDimParams | None = None, seed: int = 0) -> Program:
    """Parse `source` into a `Program` without executing anything.

    Raises `SubsetViolation(construct, lineno)` for anything outside the subset
    and `CodeParseError` for text that is not Python (or too long / too deep).
    `entry` names the def the harness will call (stored as `Program.entry`, as
    its id); `params` carries a declared dialect (default: plain Python).

    `Program.query`: a `final_value` query on the variable `entry` returns, if
    the entry def ends in `return <name>` (note: that is a LOCAL of the def;
    the harness, not `run`, is the way to evaluate a function-form program);
    otherwise a `final_value` query on the last top-level assigned name, or a
    `stdout` query when nothing is assigned at top level."""
    if not isinstance(source, str):
        raise CodeParseError("source is not text")
    if len(source) > MAX_SOURCE_CHARS:
        raise CodeParseError(f"source longer than {MAX_SOURCE_CHARS} characters")
    try:
        tree = ast.parse(source, mode="exec")  # syntax tree only: nothing is compiled or run
    except SyntaxError as e:
        raise CodeParseError(f"syntax error: {e.msg}", e.lineno or 0) from None
    except (RecursionError, MemoryError, ValueError) as e:
        raise CodeParseError(f"unreadable source: {type(e).__name__}") from None
    conv = _Converter(keep_names)
    try:
        body = conv.module(tree)
    except RecursionError:
        raise CodeParseError("source nested too deeply") from None
    entry_id = conv.ids.get(entry) if entry is not None else None
    if entry is not None and entry_id is None:
        entry_id = entry if keep_names else None
    variables, locals_pool, query = _metadata(body, entry, entry_id)
    return Program(body=body, query=query, params=params or CodeDimParams(), seed=seed, variables=variables,
                   locals_pool=locals_pool, entry=entry_id if entry_id is not None else entry)


def render_source(program: Program, names: dict | None = None) -> str:
    """The program's source exactly as `codeworld.render.render` lays it out,
    but with the given id -> name map (default: identity, i.e. a program
    parsed with `keep_names=True` comes back with its own identifiers).
    Comments are not reproduced (the parser drops them)."""
    from cartographer.codeworld.render import _all_ids, _stmt_lines

    if names is None:
        names = {vid: vid for vid, _ in _all_ids(program)}
    lines: list[str] = []
    for i, s in enumerate(program.body):
        if isinstance(s, FuncDef) and lines and lines[-1] != "":
            lines.append("")
        lines += _stmt_lines(s, names, 0, {})
        if isinstance(s, FuncDef) and i + 1 < len(program.body):
            lines.append("")
    return "\n".join(lines) + "\n"


# -- repository mode (Province VI, docs/province-vi-spec.md §4.5) -----------------------------
#
# One file of a generated repository. Module level may hold only `def`s and imports (plus
# docstrings, dropped). Imports between the repository's own modules are accepted in two forms,
# `from <m> import <n>[, <n>...]` and `import <m>` with calls `<m>.<n>(...)`; whether `<n>`
# exists in `<m>` is decided later, over the whole repository (`cartographer.repo.flatten`),
# because a model may add a helper in one file and import it from another. A call to a name the
# file neither defines nor imports is NOT a parse error: it is recorded (`unresolved`) and the
# repository verifier scores it `fabricated_symbol` -- that is the mechanism under test.
# `from typing import ...` / `from __future__ import ...` are dropped like annotations are
# (the V-B lesson: never reject a harmless spelling a correct answer uses).

_NOOP_IMPORT_MODULES = frozenset({"typing", "__future__"})
_FOREIGN_THIRD_PARTY = frozenset({"numpy", "pandas", "scipy", "sympy", "requests", "pytest", "attr", "attrs",
                                  "pydantic", "typing_extensions", "six", "yaml", "toml", "dateutil"})


def is_foreign_module(name: str) -> bool:
    """A module that is not part of any generated repository: the standard
    library or a common third-party package (importing it is outside the
    subset, `foreign_import`). Repository module names never collide with
    these (asserted in tests)."""
    top = name.split(".")[0]
    return top in sys.stdlib_module_names or top in _FOREIGN_THIRD_PARTY


@dataclass(frozen=True)
class ParsedModule:
    """One repository file in our AST. `body` holds only `FuncDef`s whose ids
    ARE the source identifiers. Calls inside them name either a def of this
    file (`f`), a from-imported name (`f`, resolved by `from_imports`), a
    module-qualified callee (`m.f`, for `import m`), or an unresolved name
    (`?f`)."""

    body: tuple
    from_imports: tuple  # ((local name, module, name, lineno), ...) in source order
    module_imports: tuple  # ((module, lineno), ...) for `import m`
    unresolved: tuple  # ((name, lineno), ...)
    attr_calls: tuple  # ((module, name, lineno), ...)


class _RepoConverter(_Converter):
    def __init__(self, repo_modules) -> None:
        super().__init__(keep_names=True)
        self.repo_modules = frozenset(repo_modules)
        self.imported: dict[str, tuple[str, str]] = {}
        self.aliases: set[str] = set()
        self.from_imports: list = []
        self.module_imports: list = []
        self.unresolved: list = []
        self.attr_calls: list = []

    def module(self, tree: ast.Module) -> tuple:
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.bound.add(node.id)
            elif isinstance(node, ast.arg):
                self.bound.add(node.arg)
            elif isinstance(node, ast.FunctionDef):
                self.bound.add(node.name)
                self.def_names.add(node.name)
        for s in tree.body:  # imports first: a call may precede the import line textually
            if isinstance(s, (ast.Import, ast.ImportFrom)):
                self.import_(s)
        body = []
        for s in tree.body:
            if isinstance(s, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
                continue  # module docstring / bare string
            if isinstance(s, ast.FunctionDef):
                body.append(self.stmt(s, _Scope()))
                continue
            construct = _STMT_CONSTRUCT.get(type(s))
            if construct is not None:
                raise _bad(construct, s)
            raise _bad("module_statement", s, type(s).__name__)
        self._check_cycles()
        return tuple(body)

    def import_(self, s) -> None:
        if isinstance(s, ast.ImportFrom):
            mod = s.module or ""
            if s.level:
                raise _bad("relative_import", s)
            if mod.split(".")[0] in _NOOP_IMPORT_MODULES:
                return
            if any(a.name == "*" for a in s.names):
                raise _bad("import_star", s, mod)
            if any(a.asname for a in s.names):
                raise _bad("import_alias", s, mod)
            if mod not in self.repo_modules and ("." in mod or is_foreign_module(mod)):
                raise _bad("foreign_import", s, mod)
            for a in s.names:
                if a.name in _OWN_BUILTINS:
                    raise _bad("unknown", s, f"shadows_builtin:{a.name}")
                self.imported[a.name] = (mod, a.name)
                self.bound.add(a.name)
                self.from_imports.append((a.name, mod, a.name, _line(s)))
            return
        for a in s.names:
            if a.asname:
                raise _bad("import_alias", s, a.name)
            if a.name.split(".")[0] in _NOOP_IMPORT_MODULES:
                continue
            if a.name not in self.repo_modules and ("." in a.name or is_foreign_module(a.name)):
                raise _bad("foreign_import", s, a.name)
            self.aliases.add(a.name)
            self.module_imports.append((a.name, _line(s)))

    def stmt(self, s, scope: _Scope):
        if isinstance(s, (ast.Import, ast.ImportFrom)):
            raise _bad("import", s, "import inside a def")
        return super().stmt(s, scope)

    def call(self, e: ast.Call, scope: _Scope):
        f = e.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in self.aliases:
            self._call_args(e)
            self.attr_calls.append((f.value.id, f.attr, _line(e)))
            return Call(f"{f.value.id}.{f.attr}", tuple(self.expr(a, scope) for a in e.args))
        if isinstance(f, ast.Name):
            name = f.id
            if name in self.imported and name not in self.def_names:
                self._call_args(e)
                return Call(name, tuple(self.expr(a, scope) for a in e.args))
            if name not in self.bound and name not in _BUILTIN_NAMES:
                self._call_args(e)
                self.unresolved.append((name, _line(e)))
                return Call("?" + name, tuple(self.expr(a, scope) for a in e.args))
        return super().call(e, scope)


def parse_module(source: str, *, repo_modules) -> ParsedModule:
    """Parse one repository file (no execution). Raises `SubsetViolation` /
    `CodeParseError` exactly like `parse_program`; see the block comment above
    for what repository mode adds."""
    if not isinstance(source, str):
        raise CodeParseError("source is not text")
    if len(source) > MAX_SOURCE_CHARS:
        raise CodeParseError(f"source longer than {MAX_SOURCE_CHARS} characters")
    try:
        tree = ast.parse(source, mode="exec")  # syntax tree only: nothing is compiled or run
    except SyntaxError as e:
        raise CodeParseError(f"syntax error: {e.msg}", e.lineno or 0) from None
    except (RecursionError, MemoryError, ValueError) as e:
        raise CodeParseError(f"unreadable source: {type(e).__name__}") from None
    conv = _RepoConverter(repo_modules)
    try:
        body = conv.module(tree)
    except RecursionError:
        raise CodeParseError("source nested too deeply") from None
    return ParsedModule(body, tuple(conv.from_imports), tuple(conv.module_imports), tuple(conv.unresolved),
                        tuple(conv.attr_calls))
