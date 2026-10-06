"""Latent representation for the CODE-TRACING province (Province IV).

A `Program` is our own small AST over a deliberately narrow Python subset
(see docs/province-iv-spec.md §5.1). It is the source of truth: the source
text a model reads is a *rendering* of it (`render.py`), and the answer is
computed by our own bounded interpreter (`interpret.py`), never by executing
text. Variables are abstract ids (`v0`, `v1`, ...; functions `f0`, ...) so the
same program can be rendered with descriptive or look-alike names without
changing a single value -- that is what makes `rename_descriptive` an
answer-preserving ablation.

Every statement carries a `sid` unique within its program, so distractor
labels, trap sites and per-statement traces can point at statements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Const:
    value: object  # int | bool | str | None


@dataclass(frozen=True)
class ListLit:
    items: tuple


@dataclass(frozen=True)
class DictLit:
    keys: tuple  # tuple[str, ...]
    values: tuple


@dataclass(frozen=True)
class Name:
    id: str


@dataclass(frozen=True)
class BinOp:
    op: str  # + - * // %
    left: object
    right: object


@dataclass(frozen=True)
class Compare:
    """`left op right`. `op` is one of COMPARE_OPS: the six ordering /
    equality operators, plus membership `in` / `not in` (list / str element
    or substring, dict KEY membership). The generator never emits `in` /
    `not in`; they exist for parsed model-written code (Province V)."""

    op: str  # == != < <= > >= | in | not in
    left: object
    right: object


@dataclass(frozen=True)
class BoolOp:
    op: str  # and | or
    left: object
    right: object


@dataclass(frozen=True)
class Not:
    operand: object


@dataclass(frozen=True)
class Index:
    target: object
    index: object


@dataclass(frozen=True)
class Slice:
    target: object
    lo: object | None
    hi: object | None


@dataclass(frozen=True)
class Len:
    arg: object


@dataclass(frozen=True)
class IfExp:
    test: object
    body: object
    orelse: object


@dataclass(frozen=True)
class Call:
    func: str  # function id
    args: tuple


@dataclass(frozen=True)
class OpaqueLit:
    """An opaque string literal (`"tkt-3f9a1c"`) sitting inside a naming
    scheme it breaks. `value` is what Python evaluates; `pattern` is what a
    reader who COMPLETES the scheme instead of copying the literal would
    write (`"ticket_audit_3"`). Only the `pattern_completion` foil reads it."""

    value: str
    pattern: str


@dataclass(frozen=True)
class MethodCall:
    obj: str  # variable id
    method: str  # append | pop | extend | get | upper | replace
    args: tuple


Expr = Union[Const, ListLit, DictLit, Name, BinOp, Compare, BoolOp, Not, Index, Slice, Len, IfExp, Call,
             MethodCall, OpaqueLit]

METHODS = {"append", "pop", "extend", "get", "upper", "replace"}
COMPARE_OPS = ("==", "!=", "<", "<=", ">", ">=", "in", "not in")

# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Assign:
    sid: int
    target: str
    value: object


@dataclass(frozen=True)
class SetItem:
    sid: int
    target: str
    index: object
    value: object


@dataclass(frozen=True)
class AugAssign:
    sid: int
    target: str
    op: str  # + - *
    value: object


@dataclass(frozen=True)
class If:
    sid: int
    test: object
    body: tuple
    orelse: tuple = ()


@dataclass(frozen=True)
class RangeIter:
    """`range(hi)` / `range(lo, hi)`. The generator only emits int bounds; a
    bound may also be an expression (`range(len(xs))`, `range(1, n)`) in
    parsed model-written code, evaluated when the loop starts (must be an
    int at run time)."""

    lo: object | None  # int | Expr | None
    hi: object  # int | Expr


@dataclass(frozen=True)
class For:
    sid: int
    var: str
    iter: object  # RangeIter | Name
    body: tuple


@dataclass(frozen=True)
class FuncDef:
    sid: int
    name: str
    params: tuple  # tuple[str, ...]
    defaults: tuple  # trailing defaults, aligned to the LAST len(defaults) params
    body: tuple


@dataclass(frozen=True)
class Return:
    sid: int
    value: object


@dataclass(frozen=True)
class ExprStmt:
    sid: int
    value: object


@dataclass(frozen=True)
class Print:
    sid: int
    value: object


@dataclass(frozen=True)
class While:
    """`while test: body`. The generator only emits loops whose exit depends
    on computed state and that terminate in <= 40 iterations under truth."""

    sid: int
    test: object
    body: tuple


@dataclass(frozen=True)
class Break:
    """`break` (innermost enclosing for / while). Never generated; parsed
    model-written code only."""

    sid: int


@dataclass(frozen=True)
class Continue:
    """`continue` (innermost enclosing for / while). Never generated; parsed
    model-written code only."""

    sid: int


Stmt = Union[Assign, SetItem, AugAssign, If, For, While, FuncDef, Return, ExprStmt, Print, Break, Continue]

# ---------------------------------------------------------------------------
# Program
# ---------------------------------------------------------------------------

TRAP_KEYS = (
    "aliasing_on",
    "late_binding_on",
    "shadowing_on",
    "inclusive_range_trap_on",
    "negative_modulo_on",
    "mutable_default_on",
)
# Traps outside the six-trap family, so the code_traps ray and its frontier row
# keep their meaning. `opaque_literal_on` is the code-tracing replication of the
# interactive ticket-fabrication finding (docs/spec-ticket-fabrication.md, Part D).
EXTRA_TRAP_KEYS = ("opaque_literal_on",)

# Counterfactual dialects (Wu et al. 2023, "Reasoning or Reciting?"): a program
# may DECLARE deviations from Python, stated in a header before the code. Truth
# is the interpreter under the declared semantics; the Python reading becomes
# the `python_prior` foil. Fixed order: `dialect_rules=k` turns on the first k.
DIALECT_RULES = ("inclusive_range", "truncating_modulo", "copy_on_assign", "fresh_defaults")
# The trap builder whose site makes each rule load-bearing (and the toggle that
# would double-book the same semantic, so the two are mutually exclusive).
DIALECT_TRAPS = {
    "inclusive_range": "inclusive_range_trap_on",
    "truncating_modulo": "negative_modulo_on",
    "copy_on_assign": "aliasing_on",
    "fresh_defaults": "mutable_default_on",
}


# Round-2 dials (int or bool), in a fixed order.
ROUND2_DIALS = ("chain_depth", "stack_depth", "loop_branch", "while_on", "sign_chain", "index_math",
                "misleading_comments", "neutral_comments")
# stack_depth = k stacks the first k of these steps on one list (alias +
# augmented assignment, concatenation rebind, slice copy, mutating callee).
STACK_STEPS = ("augassign", "concat", "slice", "param")


@dataclass(frozen=True)
class CodeDimParams:
    n_statements: int = 6
    max_nesting: int = 1
    n_variables: int = 3
    distractor_fraction: float = 0.0
    identifier_confusability: float = 0.0
    aliasing_on: bool = False
    late_binding_on: bool = False
    shadowing_on: bool = False
    inclusive_range_trap_on: bool = False
    negative_modulo_on: bool = False
    mutable_default_on: bool = False
    opaque_literal_on: bool = False
    dialect: tuple = ()  # declared deviations from Python, a subset of DIALECT_RULES
    # Round-2 mechanisms (docs/spec-code-hunt.md WP-G). All at 0/False => the
    # generator draws exactly the programs it drew before they existed.
    chain_depth: int = 0  # dependent updates on the path to the queried value (foil stale_read)
    stack_depth: int = 0  # list semantics stacked on ONE list that reaches the answer (STACK_STEPS)
    loop_branch: bool = False  # data-dependent branch inside a loop over a list (foil predicate_frozen)
    while_on: bool = False  # a `while` whose exit depends on computed state (foil predicate_frozen)
    sign_chain: int = 0  # chained // % * on a value that is negative there (foil truncating_modulo)
    index_math: int = 0  # slices with computed / negative end bounds (foil slice_end_inclusive)
    misleading_comments: int = 0  # comments on target-update lines asserting a foil's value (signature comment_value)
    neutral_comments: int = 0  # control: the same lines get a neutral comment of equal length

    def __post_init__(self) -> None:
        dialect = tuple(self.dialect)
        unknown = [r for r in dialect if r not in DIALECT_RULES]
        if unknown or len(set(dialect)) != len(dialect):
            raise ValueError(f"dialect must be distinct rules from {DIALECT_RULES}, got {dialect}")
        for r in dialect:
            if getattr(self, DIALECT_TRAPS[r]):
                raise ValueError(f"dialect rule {r!r} conflicts with trap toggle {DIALECT_TRAPS[r]!r}")
        object.__setattr__(self, "dialect", tuple(r for r in DIALECT_RULES if r in dialect))
        if not 2 <= self.n_statements <= 80:
            raise ValueError("n_statements must be in 2..80")
        if not 0 <= self.max_nesting <= 3:
            raise ValueError("max_nesting must be in 0..3")
        if not 2 <= self.n_variables <= 12:
            raise ValueError("n_variables must be in 2..12")
        if not 0.0 <= self.distractor_fraction <= 0.6:
            raise ValueError("distractor_fraction must be in 0..0.6")
        if not 0.0 <= self.identifier_confusability <= 1.0:
            raise ValueError("identifier_confusability must be in 0..1")
        if not 0 <= self.chain_depth <= 24:
            raise ValueError("chain_depth must be in 0..24")
        if not 0 <= self.stack_depth <= len(STACK_STEPS):
            raise ValueError(f"stack_depth must be in 0..{len(STACK_STEPS)}")
        if not all(0 <= getattr(self, k) <= 3 for k in ("sign_chain", "index_math", "misleading_comments",
                                                           "neutral_comments")):
            raise ValueError("sign_chain, index_math, misleading_comments, neutral_comments must be in 0..3")
        if self.misleading_comments and self.neutral_comments:
            raise ValueError("misleading_comments and its neutral_comments control are exclusive")
        if self.sign_chain and "truncating_modulo" in self.dialect:
            raise ValueError("sign_chain needs Python floor division; it conflicts with the truncating_modulo dialect")
        if self.stack_depth and "copy_on_assign" in self.dialect:
            raise ValueError("stack_depth needs Python aliasing; it conflicts with the copy_on_assign dialect")

    def round2_on(self) -> bool:
        """True iff any round-2 mechanism dial is non-zero (then the round-2
        foils join the program's readings; see foils.foils_for)."""
        return any(getattr(self, k) for k in ROUND2_DIALS)

    def traps_on(self) -> tuple[str, ...]:
        return tuple(k for k in TRAP_KEYS if getattr(self, k))


@dataclass(frozen=True)
class VarInfo:
    """One top-level name. `kind` is the static type (int/list/str/dict/func).
    `role`: target | live | twin | dead | trap | loop. `twin_of` names the
    variable this one is a look-alike sibling of (rendering pairs them)."""

    id: str
    kind: str
    role: str
    twin_of: str | None = None


@dataclass(frozen=True)
class Query:
    kind: str  # "final_value" | "stdout"
    target: str | None


@dataclass(frozen=True)
class Program:
    body: tuple
    query: Query
    params: CodeDimParams
    seed: int
    variables: tuple = ()  # tuple[VarInfo, ...]
    distractor_ids: frozenset = frozenset()  # sids of top-level distractor statements
    trap_sites: tuple = ()  # tuple[(trap_key, sid), ...] -- the line that carries the trap
    locals_pool: tuple = ()  # function-local / loop names (ids) used, for rendering
    path_sids: tuple = ()  # sids of the chain_depth updates on the target's dependency path, in order
    # (sid, var id, asserted value, foil name, neutral) per commented line; neutral comments assert nothing
    comments: tuple = ()
    # Province V (function form, docs/province-v-spec.md §4.1): the top-level def the harness calls
    # (None = a plain top-level program) and the kinds (int/list/str) of its positional parameters.
    entry: str | None = None
    param_kinds: tuple = ()  # tuple[str, ...]

    @property
    def semantics(self):
        """The semantics this program DECLARES (Python unless it has a dialect):
        ground truth for every answer."""
        from .interpret import semantics_for

        return semantics_for(self.params.dialect)

    def var(self, vid: str) -> VarInfo | None:
        for v in self.variables:
            if v.id == vid:
                return v
        return None

    def twin_of_target(self) -> str | None:
        t = self.query.target
        for v in self.variables:
            if v.twin_of == t and t is not None:
                return v.id
        return None

    def twin_pairs(self) -> list[tuple[str, str]]:
        return [(v.twin_of, v.id) for v in self.variables if v.twin_of is not None]


def iter_stmts(body):
    """Every statement node, depth-first, including nested ones."""
    for s in body:
        yield s
        if isinstance(s, If):
            yield from iter_stmts(s.body)
            yield from iter_stmts(s.orelse)
        elif isinstance(s, (For, While, FuncDef)):
            yield from iter_stmts(s.body)


def nesting_depth(body, depth: int = 0) -> int:
    best = depth
    for s in body:
        if isinstance(s, If):
            best = max(best, nesting_depth(s.body, depth + 1), nesting_depth(s.orelse, depth + 1))
        elif isinstance(s, (For, While)):
            best = max(best, nesting_depth(s.body, depth + 1))
    return best


@dataclass
class Trace:
    """What one interpreter run produced."""

    globals: dict
    stdout: list
    steps: int
    target_track: list = field(default_factory=list)  # target value after each top-level stmt
    alias_pairs: set = field(default_factory=set)
    error: str | None = None
