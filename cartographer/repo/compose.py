"""Province VI generator (docs/province-vi-spec.md §4.1): a repository is a
rendering of ONE latent `Program`.

Composition, not extraction. Small function-form programs (Province V's
`reference_program`, so every bug injector applies unchanged) become the
functions of the repository; a call DAG over them is drawn with `solve` as
the root, and a call edge `g -> f` is the statement `t = t + f(<args>)`
inserted just before `g`'s final `return t` (kind-matched: `+ len(...)` /
`+ [...]` when the kinds differ), so a callee's result always reaches its
caller's return. The fault is injected into the function `bug_distance` hops
from `solve` and kept only if it changes `solve`'s output (checked by running
our interpreter, as Province V does).

Three seeds keep the paired designs paired:
  * the LATENT (functions, call graph, inlining, bug, inputs) depends only on
    the core dials (`LATENT_KEYS`) -- so `n_modules`, `graph_tools`,
    look-alikes, clones and every ablation leave the program untouched;
  * the PARTITION (which function lives in which file) depends on
    `n_modules`;
  * EXTRAS (distractor modules, look-alike twins, the clone) only ADD
    never-called functions, so they never change what `solve` returns.

The module import graph is acyclic by construction (modules are ordered and
every call edge goes to a module of equal or higher index), because Python
cannot import two modules that import each other. Hence "every hop of the
path crosses a file" holds exactly while `bug_distance <= n_modules - 1`;
past that, the tail of the path shares the last module.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, fields, replace

from ..codefix import bugs as B
from ..codefix.tasks import CodeFixParams, _random_arg, edge_inputs, execute, norm, reference_program
from ..codeworld.generate import GenerationFailed, _Rng, _stable_int
from ..codeworld.model import (
    Assign, AugAssign, BinOp, Builtin, Call, Const, ExprStmt, For, FuncDef, ListLit, MethodCall, Name, Program,
    Query, Return, SetItem, Slice, iter_stmts,
)
from ..codeworld.render import _POOLS, _RESERVED, _all_ids, build_names
from . import names as N

REPO_STEP_CAP = 40_000
MAX_DRAWS = 16
MAX_FUNCS = 16
VI_BUG_KINDS = tuple(k for k in B.BUG_KINDS if k != "default_shared")  # its site is a helper's default
VI_SEMANTIC = tuple(k for k in B.SEMANTIC_BUGS if k in VI_BUG_KINDS)
VI_SYNTACTIC = tuple(k for k in B.SYNTACTIC_BUGS if k in VI_BUG_KINDS)
ENTRY = N.ENTRY_FUNCTION
_COMBINE_SID = 900_000
_EXTRA_SID = 2_000_000


# -- params -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class RepoParams:
    n_modules: int = 4
    bug_distance: int = 1
    fanout: int = 2
    n_funcs: int | None = None  # None: max(bug_distance + 1, n_modules) + 1, capped at MAX_FUNCS
    inline_p: float = 0.25
    distractor_modules: int = 0
    lookalike_names: bool = False
    clone_trap: bool = False
    misleading_comments: bool = False
    bug_kind: str | None = None
    n_bugs: int = 1
    bug_mix: float = 0.0
    hidden_inputs: int = 4
    n_statements: int = 8
    tool_budget: int = 20
    graph_tools: bool = False
    restate_symbols: bool = False
    restate_imports: bool = False
    opaque_names: bool = False

    def __post_init__(self) -> None:
        if self.bug_kind is not None and self.bug_kind not in VI_BUG_KINDS:
            raise ValueError(f"bug_kind must be one of {VI_BUG_KINDS}")
        checks = [
            (1 <= self.n_modules <= 12, "n_modules in 1..12"), (0 <= self.bug_distance <= 6, "bug_distance in 0..6"),
            (1 <= self.fanout <= 4, "fanout in 1..4"), (0 <= self.inline_p <= 0.5, "inline_p in 0..0.5"),
            (0 <= self.distractor_modules <= 4, "distractor_modules in 0..4"), (1 <= self.n_bugs <= 3, "n_bugs in 1..3"),
            (0 <= self.bug_mix <= 1, "bug_mix in 0..1"), (0 <= self.hidden_inputs <= 8, "hidden_inputs in 0..8"),
            (8 <= self.n_statements <= 25, "n_statements in 8..25"), (4 <= self.tool_budget <= 40, "tool_budget in 4..40"),
            (self.n_funcs is None or self.bug_distance + 1 <= self.n_funcs <= MAX_FUNCS,
             f"n_funcs in bug_distance+1..{MAX_FUNCS}"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)

    @property
    def effective_n_funcs(self) -> int:
        if self.n_funcs is not None:
            return self.n_funcs
        return min(MAX_FUNCS, max(self.bug_distance + 1, self.n_modules) + 1)

    def latent_key(self) -> tuple:
        """Everything the latent program depends on (and nothing else)."""
        return (self.bug_distance, self.fanout, self.effective_n_funcs, round(self.inline_p, 4), self.bug_kind,
                self.n_bugs, round(self.bug_mix, 4), self.hidden_inputs, self.n_statements)


LATENT_KEYS = ("bug_distance", "fanout", "n_funcs", "inline_p", "bug_kind", "n_bugs", "bug_mix", "hidden_inputs",
               "n_statements")


# -- task --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RepoTask:
    params: RepoParams
    seed: int
    reference: Program  # latent, every function of the repository (extras included), entry `solve`
    buggy: Program
    module_of: dict  # function id -> module index (0 = entry module)
    module_names: tuple  # module index -> file stem
    order: dict  # module index -> function ids in file order
    func_names: dict  # function id -> rendered name
    local_names: dict  # function id -> {local id -> rendered name}
    path: tuple  # root function ids solve .. carrier
    carrier: str
    bug_sites: tuple
    bug_kinds: tuple
    edges: tuple  # (caller id, callee id), every call in the latent
    twins: tuple  # (twin id, the callee it imitates)
    clone: tuple | None  # (clone id, carrier id)
    distractors: tuple  # function ids in distractor modules
    visible_input: tuple
    visible_expected: object
    visible_wrong: object
    hidden_inputs: tuple
    hidden_edge_cases: tuple
    extra_tests: tuple  # ((args, expected), ...) that already pass on the buggy repository
    files: dict  # path -> source, the buggy repository the model starts from
    reference_files: dict  # path -> source, with the bug reverted
    realized: dict = field(default_factory=dict)
    draws: int = 0

    @property
    def entry_module(self) -> str:
        return self.module_names[0]

    @property
    def bug_module(self) -> str:
        return self.module_names[self.module_of[self.carrier]]

    def qualified(self, fid: str) -> str:
        return f"{self.module_names[self.module_of[fid]]}__{self.func_names[fid]}"

    @property
    def path_modules(self) -> tuple:
        return tuple(dict.fromkeys(self.module_names[self.module_of[f]] for f in self.path))

    def tests(self) -> tuple:
        """((args, expected), ...): the visible failing test first."""
        return ((self.visible_input, self.visible_expected),) + tuple(self.extra_tests)


# -- tree helpers ------------------------------------------------------------------------------

def transform(x, fn):
    """Rebuild a model tree bottom-up, applying `fn` to every dataclass node."""
    if isinstance(x, tuple):
        return tuple(transform(i, fn) for i in x)
    if hasattr(x, "__dataclass_fields__") and not isinstance(x, type):
        changes = {f.name: transform(getattr(x, f.name), fn) for f in fields(x)}
        return fn(replace(x, **changes))
    return x


def rename_functions(node, mapping: dict):
    def fn(n):
        if isinstance(n, Call) and n.func in mapping:
            return replace(n, func=mapping[n.func])
        if isinstance(n, FuncDef) and n.name in mapping:
            return replace(n, name=mapping[n.name])
        return n
    return transform(node, fn)


def shift_sids(node, offset: int):
    return transform(node, lambda n: replace(n, sid=n.sid + offset) if hasattr(n, "sid") else n)


_ID_FIELDS = {Name: "id", Assign: "target", AugAssign: "target", SetItem: "target", For: "var", MethodCall: "obj"}


def rename_locals(node, mapping: dict):
    def fn(n):
        f = _ID_FIELDS.get(type(n))
        if f is not None and getattr(n, f) in mapping:
            return replace(n, **{f: mapping[getattr(n, f)]})
        return n
    return transform(node, fn)


def calls_in(node) -> list[str]:
    out = []

    def walk(x):
        if isinstance(x, Call):
            out.append(x.func)
        if isinstance(x, tuple):
            for y in x:
                walk(y)
        elif hasattr(x, "__dataclass_fields__"):
            for f in fields(x):
                walk(getattr(x, f.name))
    walk(node)
    return out


def repo_execute(program: Program, args, semantics=None):
    return execute(program, args, semantics, step_cap=REPO_STEP_CAP)


def repo_outputs(program: Program, inputs, semantics=None) -> list:
    out = []
    for args in inputs:
        v, err = repo_execute(program, args, semantics)
        out.append(("error", err.split(":")[0]) if err else norm(v))
    return out


# -- components --------------------------------------------------------------------------------

@dataclass
class _Node:
    fid: str
    root: FuncDef
    helpers: list  # FuncDefs that ride with the root (same module)
    param_kinds: tuple
    ret_kind: str
    target: str
    kinds: dict  # local id -> pool kind
    names: dict  # local id -> rendered local name
    component: Program
    original_sids: frozenset = frozenset()
    callers: list = field(default_factory=list)
    callees: list = field(default_factory=list)


def _component(fid: str, cfp: CodeFixParams, seed: int, kinds, offset: int) -> _Node | None:
    try:
        prog = reference_program(cfp, seed, kinds)
    except GenerationFailed:
        return None
    sv = B.solve_def(prog)
    ret = sv.body[-1]
    if not (isinstance(ret, Return) and isinstance(ret.value, Name)):
        return None
    vkinds = {v.id: v.kind for v in prog.variables}
    ret_kind = vkinds.get(ret.value.id)
    if ret_kind not in ("int", "list"):
        return None
    helper_ids = [s.name for s in prog.body if isinstance(s, FuncDef) and s.name != sv.name]
    fmap = {sv.name: fid, **{h: f"{fid}_{h}" for h in helper_ids}}
    body = shift_sids(rename_functions(prog.body, fmap), offset)
    root = next(s for s in body if isinstance(s, FuncDef) and s.name == fid)
    helpers = [s for s in body if isinstance(s, FuncDef) and s.name != fid]
    pool_kinds = dict(_all_ids(prog))
    local_names, _ = build_names(prog, seed, "auto")
    local_names = {k: v for k, v in local_names.items() if pool_kinds.get(k) != "func"}
    return _Node(fid, root, helpers, prog.param_kinds, ret_kind, ret.value.id,
                 {k: v for k, v in pool_kinds.items() if v != "func"}, local_names, prog,
                 frozenset(s.sid for s in iter_stmts(root.body)))


def _literal(kind: str, rng: _Rng):
    if kind == "int":
        return Const(rng.randint(1, 9))
    if kind == "list":
        return ListLit(tuple(Const(rng.randint(-3, 9)) for _ in range(rng.randint(2, 3))))
    return Const("".join(rng.choice("abcxyz") for _ in range(rng.randint(2, 4))))


def _call_expr(caller: _Node, callee: _Node, rng: _Rng) -> Call:
    """A call of `callee` from `caller`: each parameter takes a caller
    parameter of the same kind (lists as a slice copy, so no list is shared
    across a call -- cross-function aliasing is not this province's
    variable), else a literal."""
    pool = {k: [p for p, pk in zip(caller.root.params, caller.param_kinds) if pk == k] for k in ("int", "list", "str")}
    used: dict[str, int] = {}
    args = []
    for k in callee.param_kinds:
        cands = pool.get(k, [])
        if cands:
            p = cands[used.get(k, 0) % len(cands)]
            used[k] = used.get(k, 0) + 1
            args.append(Slice(Name(p), None, None) if k == "list" else Name(p))
        else:
            args.append(_literal(k, rng))
    return Call(callee.fid, tuple(args))


def _combine(target: str, t_kind: str, ret_kind: str, value) -> object:
    if t_kind == ret_kind:
        return BinOp("+", Name(target), value)
    if t_kind == "int":  # list result into an int: `sum`, so a changed element is never masked (`len` would be)
        return BinOp("+", Name(target), Builtin("sum", (value,)))
    return BinOp("+", Name(target), ListLit((value,)))  # int result into a list


def _with_combines(node: _Node, stmts: list) -> FuncDef:
    body = node.root.body
    return replace(node.root, body=body[:-1] + tuple(stmts) + body[-1:])


# -- the latent --------------------------------------------------------------------------------

@dataclass
class _Latent:
    nodes: dict  # fid -> _Node (after inlining)
    order: list  # fids in creation order (topological: callers before callees)
    path: list
    combines: dict  # fid -> [combine statements]
    inlined: int
    program: Program


def _draw_nodes(params: RepoParams, seed: int, kinds_for_carrier) -> tuple[list, int]:
    n = params.effective_n_funcs + _inline_spare(params)
    d = params.bug_distance
    rng = _Rng("repo-nodes", seed)
    nodes = []
    draws = 0
    for i in range(n):
        fid = ENTRY if i == 0 else f"c{i}"
        kinds = kinds_for_carrier if i == d else ()
        n_params = 2 if i == 0 else rng.randint(1, 2)
        node = None
        for attempt in range(12):
            draws += 1
            cfp = CodeFixParams(n_statements=params.n_statements, n_params=n_params)
            node = _component(fid, cfp, _stable_int("repo-component", seed, i, attempt), kinds, (i + 1) * 1000)
            if node is not None:
                break
        if node is None:
            raise GenerationFailed(f"no component for node {i}")
        nodes.append(node)
    return nodes, draws


def _build_graph(nodes: list, params: RepoParams, seed: int) -> None:
    """Path edges solve -> c1 -> ... -> c_d; every other node gets one caller
    (two, with probability 0.3: fan-in) among earlier nodes whose out-degree
    is below `fanout`. Off-path nodes never call path nodes, so the shortest
    path from `solve` to the carrier is exactly `bug_distance` hops."""
    rng = _Rng("repo-graph", seed)
    d = params.bug_distance
    for i in range(1, d + 1):
        nodes[i - 1].callees.append(nodes[i].fid)
        nodes[i].callers.append(nodes[i - 1].fid)
    by_id = {n.fid: n for n in nodes}
    for j in range(d + 1, len(nodes)):
        n_callers = 2 if (rng.random() < 0.3 and j >= 2) else 1
        open_ = [m for m in nodes[:j] if len(m.callees) < params.fanout and m.fid not in nodes[j].callers]
        if not open_:
            open_ = [m for m in nodes[:j]]
        for _ in range(n_callers):
            cands = [m for m in open_ if m.fid not in nodes[j].callers]
            if not cands:
                break
            caller = rng.choice(cands)
            caller.callees.append(nodes[j].fid)
            nodes[j].callers.append(caller.fid)
    for n in nodes:
        assert all(c in by_id for c in n.callees)


def _inline_spare(params: RepoParams) -> int:
    """Extra components drawn so inlining can fold some away without leaving
    the repository short of `effective_n_funcs` functions."""
    return int(round(4 * params.inline_p)) if params.effective_n_funcs < MAX_FUNCS else 0


def _inline_candidates(nodes: list, params: RepoParams) -> list:
    d = params.bug_distance
    out = []
    for n in nodes[d + 1:]:
        if n.callees or len(n.callers) != 1 or n.helpers or calls_in(n.root.body):
            continue
        body = n.root.body
        if any(isinstance(s, Return) for s in iter_stmts(body[:-1])):
            continue
        out.append(n)
    return out


def _inline(caller: _Node, callee: _Node, call: Call, combine_target: str, sid: int, counter: list) -> list:
    """The statements that replace `t = t + callee(args)` in `caller`:
    parameter bindings, the callee's body with fresh local ids, then the
    combine with the callee's result variable. Answer-preserving (Python
    binds parameters by reference, which is what `p = arg` does)."""
    local_ids = set(callee.root.params) | set(callee.kinds)
    for s in iter_stmts(callee.root.body):
        for f in ("target", "var"):
            v = getattr(s, f, None)
            if isinstance(v, str):
                local_ids.add(v)
    mapping = {}
    for lid in sorted(local_ids):
        counter[0] += 1
        mapping[lid] = f"w{counter[0]}"
    out = []
    for p, a in zip(callee.root.params, call.args):
        counter[1] += 1
        out.append(Assign(counter[1], mapping[p], a))
    body = rename_locals(callee.root.body[:-1], mapping)
    body = shift_sids(body, counter[1] + 1 - min((s.sid for s in iter_stmts(body)), default=0))
    counter[1] = max([counter[1]] + [s.sid for s in iter_stmts(body)])
    out += list(body)
    result = rename_locals(callee.root.body[-1].value, mapping)
    out.append(Assign(sid, combine_target, _combine(combine_target, caller.ret_kind, callee.ret_kind, result)))
    for lid, new in mapping.items():  # naming: the fresh local inherits the callee's kind and name
        caller.kinds[new] = callee.kinds.get(lid, "int")
    caller._inlined_names = getattr(caller, "_inlined_names", {})
    for lid, new in mapping.items():
        caller._inlined_names[new] = callee.names.get(lid, lid)
    return out


def _assemble(nodes_by_id: dict, order: list, combines: dict, base: Program, root_kinds) -> Program:
    body = []
    for fid in order:
        n = nodes_by_id[fid]
        body.extend(n.helpers)
        body.append(replace(n.root, body=n.root.body[:-1] + tuple(combines.get(fid, ())) + n.root.body[-1:]))
    return Program(body=tuple(body), query=Query("final_value", None), params=base.params, seed=base.seed,
                   entry=ENTRY, param_kinds=root_kinds)


def build_latent(params: RepoParams, seed: int, kinds_for_carrier=()) -> tuple[_Latent, int]:
    nodes, draws = _draw_nodes(params, seed, kinds_for_carrier)
    _build_graph(nodes, params, seed)
    rng = _Rng("repo-calls", seed)
    by_id = {n.fid: n for n in nodes}
    sid = [_COMBINE_SID]
    calls: dict[tuple, Call] = {}
    combines: dict[str, list] = {}
    for n in nodes:
        for c in n.callees:
            sid[0] += 1
            call = _call_expr(n, by_id[c], rng)
            calls[(n.fid, c)] = call
            combines.setdefault(n.fid, []).append(
                Assign(sid[0], n.target, _combine(n.target, n.ret_kind, by_id[c].ret_kind, call)))
    order = [n.fid for n in nodes]
    base = nodes[params.bug_distance].component
    program = _assemble(by_id, order, combines, base, nodes[0].param_kinds)
    # partial inlining of off-path leaves (never on the bug path), down to `effective_n_funcs`
    min_funcs = params.effective_n_funcs
    inl_rng = _Rng("repo-inline", seed)
    inlined = 0
    counter = [0, 3_000_000]
    probe = _probe_inputs(program, seed, 8)
    want = repo_outputs(program, probe)
    for leaf in _inline_candidates(nodes, params):
        if len(order) - 1 < min_funcs or inl_rng.random() >= 2 * params.inline_p:
            continue
        caller = by_id[leaf.callers[0]]
        stmts = combines[caller.fid]
        k = next(i for i, s in enumerate(stmts) if calls_in(s) == [leaf.fid])
        new_stmts = _inline(caller, leaf, calls[(caller.fid, leaf.fid)], caller.target, stmts[k].sid, counter)
        trial_combines = {**combines, caller.fid: stmts[:k] + new_stmts + stmts[k + 1:]}
        trial_order = [f for f in order if f != leaf.fid]
        trial = _assemble(by_id, trial_order, trial_combines, base, nodes[0].param_kinds)
        if repo_outputs(trial, probe) != want:  # pragma: no cover - inlining is answer-preserving
            continue
        combines, order, program = trial_combines, trial_order, trial
        caller.callees.remove(leaf.fid)
        inlined += 1
    live = {f: by_id[f] for f in order}
    path = [nodes[i].fid for i in range(params.bug_distance + 1)]
    return _Latent(live, order, path, combines, inlined, program), draws


def _probe_inputs(program: Program, seed: int, n: int = 40) -> list:
    rng = _Rng("repo-inputs", seed)
    pool, seen = [], set()
    for _ in range(n * 3):
        args = tuple(_random_arg(k, rng, False) for k in program.param_kinds)
        key = repr(args)
        if key in seen:
            continue
        seen.add(key)
        if repo_execute(program, args)[1] is None:
            pool.append(args)
        if len(pool) >= n:
            break
    return pool


# -- bug -----------------------------------------------------------------------------------------

def delta_signature(bug_out, ref_out):
    """How a buggy output differs from the reference's, as a value a CONSTANT
    patch could reproduce: an int offset, or a list's per-position offsets,
    replaced elements and length change. If every differing input has the same
    signature, a constant edit anywhere on the path (e.g. `return t - 3` in
    `solve`) would pass every hidden input without fixing the cause."""
    if bug_out[0] == "int" and ref_out[0] == "int":
        return ("int", bug_out[1] - ref_out[1])
    if bug_out[0] == "list" and ref_out[0] == "list":
        b, r = bug_out[1], ref_out[1]
        items = tuple((i, (x[1] - y[1]) if x[0] == y[0] == "int" else (x, y))
                      for i, (x, y) in enumerate(zip(b, r)) if x != y)
        return ("list", len(b) - len(r), items, tuple(b[len(r):]))
    return ("other", bug_out, ref_out)


def input_dependent(bug_outs: list, ref_outs: list) -> bool:
    """True iff the bug shows on >= 2 inputs with >= 2 different delta signatures."""
    sigs = {delta_signature(b, r) for b, r in zip(bug_outs, ref_outs) if b != r and b[0] != "error"}
    return len(sigs) >= 2


def _pick_kinds(params: RepoParams, rng: _Rng) -> list[str]:
    if params.bug_kind is not None:
        return [params.bug_kind] * params.n_bugs
    return [rng.choice(VI_SEMANTIC if rng.random() < params.bug_mix else VI_SYNTACTIC)
            for _ in range(params.n_bugs)]


def _fallback(kind: str) -> list[str]:
    group = VI_SEMANTIC if kind in VI_SEMANTIC else VI_SYNTACTIC
    return [kind] + [k for k in group if k != kind]


def _carrier_sub(prog: Program, node: _Node) -> Program:
    """The carrier and its own helpers as a program with the carrier as
    entry (the injectors look there), plus an inert statement carrying the
    whole repository's top sid, so statements an injector adds get sids
    that are unique repository-wide."""
    defs = {s.name: s for s in prog.body if isinstance(s, FuncDef)}
    top = max(s.sid for s in iter_stmts(prog.body))
    body = tuple(defs[h.name] for h in node.helpers) + (defs[node.fid], ExprStmt(top, Const(None)))
    return Program(body=body, query=prog.query, params=prog.params, seed=prog.seed, entry=node.fid,
                   param_kinds=node.param_kinds)


def _splice(prog: Program, new_def: FuncDef) -> Program:
    return replace(prog, body=tuple(new_def if isinstance(s, FuncDef) and s.name == new_def.name else s
                                    for s in prog.body))


def _inject(latent: _Latent, params: RepoParams, seed: int, want: list, probe: list):
    carrier = latent.path[-1]
    node = latent.nodes[carrier]
    allowed = node.original_sids - {node.root.body[-1].sid}
    prog = latent.program
    ref = repo_outputs(prog, probe)
    sites, kinds = [], []
    for k in want:
        prev = repo_outputs(prog, probe)
        found = None
        for kind in ([k] if params.bug_kind is not None else _fallback(k)):
            try:
                options = B.inject(_carrier_sub(prog, node), kind, _stable_int(seed, kind, len(sites)), None,
                                   exclude=tuple(sites))
            except B.BugUnavailable:
                continue
            for cand_sub, sid, _d in options:
                if sid not in allowed:
                    continue
                cand = _splice(prog, next(s for s in cand_sub.body if isinstance(s, FuncDef) and s.name == carrier))
                out = repo_outputs(cand, probe)
                # load-bearing AND input-dependent: a constant patch must not be able to hide it
                if any(o != p and o[0] != "error" for o, p in zip(out, prev)) and input_dependent(out, ref):
                    found = (cand, sid, kind)
                    break
            if found:
                break
        if found is None:
            return None
        prog, sid, kind = found
        sites.append(sid)
        kinds.append(kind)
    return prog, tuple(sites), tuple(kinds)


# -- partition -----------------------------------------------------------------------------------

def _partition(latent: _Latent, params: RepoParams, seed: int) -> dict:
    """function id -> module index. Path node i sits in module min(i, k-1)
    (so every hop crosses a file while bug_distance <= k-1); off-path nodes
    take a module whose index is >= every caller's, filling empty modules
    first (increasing index), else staying with a caller (locality) or
    drawing one."""
    k = params.n_modules
    rng = _Rng("repo-partition", seed, k)
    module_of = {}
    for i, fid in enumerate(latent.path):
        module_of[fid] = min(i, k - 1)
    empties = [m for m in range(k) if m not in module_of.values()]
    rest = [f for f in latent.order if f not in module_of]
    for j, fid in enumerate(rest):
        node = latent.nodes[fid]
        lo = max((module_of[c] for c in node.callers if c in module_of), default=0)
        open_empties = [m for m in empties if m >= lo]
        remaining = len(rest) - j
        # never jump past the lowest empty module: a node placed above it would force its own
        # callees above it too, and the module could then never be filled
        hi = open_empties[0] if open_empties else k - 1
        if open_empties and (remaining <= len(empties) or rng.random() < 0.7 or hi == lo):
            m = open_empties[0]
        elif rng.random() < 0.5:
            m = min(hi, max(lo, module_of[node.callers[0]]) if node.callers else lo)
        else:
            m = rng.randint(lo, hi)
        module_of[fid] = m
        if m in empties:
            empties.remove(m)
    return module_of


# -- extras ----------------------------------------------------------------------------------------

def _twin_node(fid: str, like: _Node, params: RepoParams, seed: int, offset: int) -> _Node | None:
    for attempt in range(48):
        cfp = CodeFixParams(n_statements=params.n_statements, n_params=len(like.param_kinds))
        node = _component(fid, cfp, _stable_int("repo-twin", seed, fid, attempt), (), offset)
        if node is not None and node.param_kinds == like.param_kinds and node.ret_kind == like.ret_kind \
                and not node.helpers and not calls_in(node.root.body):
            return node
    return None


def _clone_node(fid: str, carrier: _Node, buggy_root: FuncDef, drop: set, offset: int, seed: int) -> _Node:
    """The carrier's BUGGY body without its outgoing call statements (`drop`),
    one local renamed: a near-miss copy the obvious edit lands in for nothing."""
    keep = tuple(s for s in buggy_root.body if s.sid not in drop)
    root = replace(buggy_root, name=fid, body=keep)
    rng = _Rng("repo-clone", seed)
    cands = sorted(v for v in carrier.kinds if carrier.kinds[v] in ("int", "list") and v in carrier.names
                   and v != carrier.target)
    names = dict(carrier.names)
    if cands:
        v = rng.choice(cands)
        used = set(names.values())
        alt = [w for w in _POOLS[carrier.kinds[v]] if w not in used]
        if alt:
            names[v] = rng.choice(alt)
    root = shift_sids(root, offset - min(s.sid for s in iter_stmts((root,))))
    node = _Node(fid, root, [], carrier.param_kinds, carrier.ret_kind, carrier.target, dict(carrier.kinds), names,
                 carrier.component)
    return node


# -- naming --------------------------------------------------------------------------------------

def _local_names(node: _Node, taken: set) -> dict:
    names = dict(node.names)
    extra = getattr(node, "_inlined_names", {})
    used = {v for v in names.values()} | taken | set(_RESERVED)
    rng = _Rng("repo-locals", node.fid)
    for lid, hint in sorted(extra.items()):
        w = hint if hint not in used else None
        if w is None:
            pool = [x for x in _POOLS.get(node.kinds.get(lid, "int"), _POOLS["int"]) if x not in used]
            w = rng.choice(pool) if pool else f"{hint}_{lid[1:]}"
        used.add(w)
        names[lid] = w
    return names


def _complete_names(defn: FuncDef, names: dict, taken: set, salt: str) -> None:
    from ..codefix.parse import _kind_of

    kinds: dict[str, str] = {}
    for st in iter_stmts(defn.body):
        if isinstance(st, Assign) and st.target not in kinds:
            kinds[st.target] = _kind_of(st.value, kinds)
    prog = Program(body=(defn,), query=Query("final_value", None), params=None, seed=0)
    used = set(names.values()) | taken | set(_RESERVED)
    rng = _Rng("repo-fresh-names", salt)
    for vid, pool_kind in _all_ids(prog):
        if vid in names or pool_kind == "func":
            continue
        kind = kinds.get(vid, pool_kind)
        pool = [w for w in _POOLS.get(kind, _POOLS["int"]) if w not in used]
        w = rng.choice(pool) if pool else f"tmp_{vid}"
        used.add(w)
        names[vid] = w


# -- compose --------------------------------------------------------------------------------------

def compose(params: RepoParams, seed: int) -> RepoTask:
    """The repository task for (params, seed). Raises GenerationFailed after
    MAX_DRAWS latent draws without a load-bearing bug."""
    from .render import render_repo

    last = "no draw"
    total_draws = 0
    for draw in range(MAX_DRAWS):
        ls = _stable_int("repo-latent", seed, repr(params.latent_key()), draw)
        want = _pick_kinds(params, _Rng("repo-kinds", ls))
        try:
            latent, draws = build_latent(params, ls, tuple(want))
        except GenerationFailed as e:
            last = str(e)
            continue
        total_draws += draws
        pool = _probe_inputs(latent.program, ls, 8 + params.hidden_inputs + 8)
        if len(pool) < 8 + params.hidden_inputs:
            last = "too few inputs on which the reference runs"
            continue
        probe = pool[:8]
        inj = _inject(latent, params, ls, want, probe)
        if inj is None:
            last = f"no load-bearing site for {want} in the carrier"
            continue
        buggy, sites, kinds = inj
        ref = latent.program
        ref_probe, bug_probe = repo_outputs(ref, probe), repo_outputs(buggy, probe)
        vis = next((i for i, (o, r) in enumerate(zip(bug_probe, ref_probe)) if o != r and o[0] != "error"), None)
        if vis is None:
            last = "no visible input"
            continue
        visible = probe[vis]
        rest = [a for a in pool if a != visible]
        hidden, cases = _hidden(ref, buggy, visible, rest, params.hidden_inputs)
        if hidden is not None and params.hidden_inputs and not _catches_constant_patch(ref, buggy, visible, hidden):
            last = "no hidden input catches a constant patch"
            continue
        if params.hidden_inputs and hidden is None:
            last = "no hidden input catches a symptom patch"
            continue
        extra_tests = tuple((a, repo_execute(ref, a)[0]) for a in rest
                            if a not in (hidden or ()) and repo_outputs(ref, [a]) == repo_outputs(buggy, [a]))[:2]
        return _finish(params, seed, latent, ref, buggy, sites, kinds, visible, tuple(hidden or ()), tuple(cases or ()),
                       extra_tests, total_draws, render_repo)
    raise GenerationFailed(f"no repository task for seed {seed}: {last}")


def _catches_constant_patch(ref, buggy, visible, hidden) -> bool:
    vis = delta_signature(repo_outputs(buggy, [visible])[0], repo_outputs(ref, [visible])[0])
    for a in hidden:
        b, r = repo_outputs(buggy, [a])[0], repo_outputs(ref, [a])[0]
        if b == r or b[0] == "error" or delta_signature(b, r) != vis:
            return True  # a patch tuned to the visible test's delta is wrong here
    return False


def _hidden(ref, buggy, visible, rest, n):
    if not n:
        return (), ()
    hidden, cases = [], []
    for c, a in edge_inputs(ref, visible, n):
        if repo_execute(ref, a)[1] is None:
            hidden.append(a)
            cases.append(c)
    differing = [a for a in rest if repo_outputs(buggy, [a]) != repo_outputs(ref, [a])]
    catches = any(repo_outputs(buggy, [a]) != repo_outputs(ref, [a]) for a in hidden)
    if not differing and not catches:
        return None, None
    if not catches:
        if len(hidden) >= n:
            hidden.pop(), cases.pop()
        hidden.append(differing[0])
        cases.append("differs")
    if not _catches_constant_patch(ref, buggy, visible, hidden):
        vis = delta_signature(repo_outputs(buggy, [visible])[0], repo_outputs(ref, [visible])[0])
        other = next((a for a in rest if a not in hidden and repo_outputs(buggy, [a])[0][0] != "error"
                      and delta_signature(repo_outputs(buggy, [a])[0], repo_outputs(ref, [a])[0]) != vis), None)
        if other is not None:
            if len(hidden) >= n:
                hidden.pop(), cases.pop()
            hidden.append(other)
            cases.append("delta_differs")
    for a in rest:
        if len(hidden) >= n:
            break
        if a not in hidden:
            hidden.append(a)
            cases.append("random")
    return hidden, cases


def _finish(params, seed, latent, ref, buggy, sites, kinds, visible, hidden, cases, extra_tests, draws, render_repo):
    module_of = _partition(latent, params, seed)
    nodes = dict(latent.nodes)
    carrier = latent.path[-1]
    buggy_defs = {s.name: s for s in buggy.body if isinstance(s, FuncDef)}
    # module indices actually used (a pinned n_funcs below n_modules leaves some empty)
    used = sorted(set(module_of.values()))
    remap = {m: i for i, m in enumerate(used)}
    module_of = {f: remap[m] for f, m in module_of.items()}
    for fid in latent.order:
        for h in nodes[fid].helpers:
            module_of[h.name] = module_of[fid]
    k = len(used)
    rng = _Rng("repo-extras", seed)
    extra_defs: list = []
    twins: list = []
    clone = None
    distractors: list = []
    offset = [_EXTRA_SID]

    def next_offset():
        offset[0] += 1000
        return offset[0]

    # distractor modules: never imported, never called
    n_mod = k
    for j in range(params.distractor_modules):
        for t in range(rng.randint(1, 2)):
            fid = f"d{j}_{t}"
            node = _twin_node(fid, nodes[ENTRY], params, _stable_int(seed, "distractor", j, t), next_offset()) or \
                _component(fid, CodeFixParams(n_statements=params.n_statements, n_params=1),
                           _stable_int(seed, "distractor-any", j, t), (), next_offset())
            if node is None or node.helpers:
                continue
            nodes[fid] = node
            module_of[fid] = n_mod
            extra_defs.append(node.root)
            distractors.append(fid)
        if any(module_of.get(f) == n_mod for f in distractors):
            n_mod += 1
    # look-alike twins of up to two crossing callees (path edges first)
    if params.lookalike_names and k >= 2:
        crossing = [(a, b) for a, b in _edges(latent) if module_of[a] != module_of[b]]
        crossing.sort(key=lambda e: (e[1] not in latent.path, latent.order.index(e[1])))
        for a, b in crossing[:2]:
            fid = f"t_{b}"
            node = _twin_node(fid, nodes[b], params, _stable_int(seed, "twin", b), next_offset())
            if node is None:
                continue
            others = [m for m in range(k) if m not in (module_of[b], module_of[a])] or \
                     [m for m in range(k) if m != module_of[b]]
            nodes[fid] = node
            module_of[fid] = rng.choice(others)
            extra_defs.append(node.root)
            twins.append((fid, b))
    # the near-miss clone of the buggy function
    if params.clone_trap and k >= 2:
        drop = {st.sid for st in latent.combines.get(carrier, ())}
        cnode = _clone_node("k_clone", nodes[carrier], buggy_defs[carrier], drop, next_offset(), seed)
        needs_lower = bool(calls_in(cnode.root.body))
        others = [m for m in range(k) if m != module_of[carrier] and (not needs_lower or m < module_of[carrier])]
        if others:
            off_path = [m for m in others if m not in {module_of[f] for f in latent.path}]
            nodes["k_clone"] = cnode
            module_of["k_clone"] = rng.choice(off_path or others)
            extra_defs.append(cnode.root)
            clone = ("k_clone", carrier)
    ref = replace(ref, body=ref.body + tuple(extra_defs))
    buggy = replace(buggy, body=buggy.body + tuple(extra_defs))
    # names
    mnames = N.module_names(n_mod, seed)
    roots = [f for f in latent.order] + [f for f, _ in twins] + distractors
    helper_ids = [h.name for f in latent.order for h in nodes[f].helpers]
    plain = N.function_names(len(roots) - 1 + len(helper_ids), seed)
    fnames = {ENTRY: ENTRY}
    it = iter(plain)
    for f in [r for r in roots if r != ENTRY] + helper_ids:
        fnames[f] = next(it)
    taken = set(fnames.values())
    for fid, like in twins:
        opts = [w for w in N.lookalike_names(fnames[like]) if w not in taken]
        fnames[fid] = _Rng("repo-twin-name", seed, fid).choice(opts) if opts else fnames[like] + "s"
        taken.add(fnames[fid])
    if clone:
        fnames[clone[0]] = N.clone_name(fnames[carrier], seed)
    if params.opaque_names:
        mnames = [N.opaque("module", m, seed) for m in mnames]
        fnames = {f: (w if f == ENTRY else N.opaque("function", w, seed)) for f, w in fnames.items()}
    local = {}
    all_fn = set(fnames.values())
    buggy_defs = {s.name: s for s in buggy.body if isinstance(s, FuncDef)}
    for fid, node in nodes.items():
        local[fid] = _local_names(node, all_fn)
        for h in node.helpers:
            local[h.name] = local[fid]
    for fid, names in local.items():  # ids a bug introduced (fresh locals) get a name too
        _complete_names(buggy_defs[fid], names, all_fn, fid)
    # file order: per module, helpers right above their root, roots in creation order, extras last
    order: dict[int, list] = {}
    for fid in latent.order:
        for h in nodes[fid].helpers:
            order.setdefault(module_of[fid], []).append(h.name)
        order.setdefault(module_of[fid], []).append(fid)
    for fid in [f for f, _ in twins] + distractors + ([clone[0]] if clone else []):
        order.setdefault(module_of[fid], []).append(fid)
    edges = tuple(_edges_of(buggy))
    realized = {
        "n_modules": float(k), "n_files": float(n_mod), "n_funcs": float(len(latent.order)),
        "bug_distance": float(len(latent.path) - 1), "inlined": float(latent.inlined),
        "n_edges": float(len(edges)),
        "n_crossing_edges": float(sum(1 for a, b in edges if module_of[a] != module_of[b])),
        "path_crosses_every_hop": float(all(module_of[a] != module_of[b]
                                            for a, b in zip(latent.path, latent.path[1:]))),
        "max_fanout": float(max((sum(1 for a, _ in edges if a == f) for f in latent.order), default=0)),
        "lookalike_twins": float(len(twins)), "clone_trap": float(clone is not None),
        "distractor_modules": float(n_mod - k), "hidden_inputs": float(len(hidden)), "n_bugs": float(len(sites)),
        "lines": 0.0,
    }
    if clone:
        realized["clone_first"] = float(mnames[module_of[clone[0]]] < mnames[module_of[carrier]])
    notes = {sites[0]: "this line is correct"} if params.misleading_comments else {}
    shell = RepoTask(params, seed, ref, buggy, module_of, tuple(mnames), {m: tuple(v) for m, v in order.items()},
                     fnames, local, tuple(latent.path), carrier, sites, kinds, edges, tuple(twins), clone,
                     tuple(distractors), visible, repo_execute(ref, visible)[0], repo_execute(buggy, visible)[0],
                     hidden, cases, extra_tests, {}, {}, realized, draws)
    files = render_repo(shell, buggy, notes)
    ref_files = render_repo(shell, ref, {})
    realized["lines"] = float(sum(src.count("\n") for p, src in files.items() if not p.startswith("tests/")))
    return replace(shell, files=files, reference_files=ref_files, realized=realized)


def _edges(latent: _Latent) -> list:
    out = []
    for f in latent.order:
        for c in latent.nodes[f].callees:
            out.append((f, c))
    return out


def _edges_of(program: Program):
    for s in program.body:
        if isinstance(s, FuncDef):
            for c in dict.fromkeys(calls_in(s.body)):
                yield (s.name, c)


__all__ = ["RepoParams", "RepoTask", "compose", "repo_execute", "repo_outputs", "LATENT_KEYS", "VI_BUG_KINDS",
           "REPO_STEP_CAP", "transform", "rename_functions", "calls_in"]
