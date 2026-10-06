"""Province V tasks (docs/province-v-spec.md §4): repair (V-a / V-b) and
write-from-spec (V-c) over function-form generated programs. Deterministic
in (params, seed); ground truth is our interpreter, never executed text."""

from __future__ import annotations

import copy
from dataclasses import dataclass

from ..codeworld.generate import GenerationFailed, _Rng, _stable_int, generate, to_function_form
from ..codeworld.interpret import STEP_CAP, TRUE, InterpError, Interpreter, StepCapExceeded
from ..codeworld.model import (
    CodeDimParams, For, FuncDef, If, ListLit, Program, Slice, While, iter_stmts,
)
from ..codeworld.render import _block, build_names
from . import bugs as B

MAX_DRAWS = 24


# -- execution (our interpreter; fresh state per call) ----------------------------

def execute(program: Program, args, semantics=None, step_cap: int = STEP_CAP):
    """(value, error) of `entry(*args)`: top-level defs run in a fresh
    interpreter, then the entry is called on deep copies of `args`.
    `error` is None or "<kind>: <message>" (kind = step_cap | runtime)."""
    it = Interpreter(program.semantics if semantics is None else semantics, step_cap)
    try:
        for s in program.body:
            it.exec_top(s)
        v = it.call_function(program.entry or "solve", copy.deepcopy(list(args)))
        return copy.deepcopy(v), None
    except StepCapExceeded as e:
        return None, f"step_cap: {e}"
    except InterpError as e:
        return None, f"runtime: {e}"
    except (RecursionError, MemoryError) as e:  # pragma: no cover
        return None, f"runtime: {type(e).__name__}"


def norm(v):
    """tuple -> list, recursively; a (type, value) form so True != 1."""
    if isinstance(v, (list, tuple)):
        return ("list", tuple(norm(x) for x in v))
    if isinstance(v, dict):
        return ("dict", tuple(sorted((k, norm(x)) for k, x in v.items())))
    return (type(v).__name__, v)


def outputs(program: Program, inputs, semantics=None) -> list:
    """Normalised outcome per input: norm(value), or ("error", kind)."""
    out = []
    for args in inputs:
        v, err = execute(program, args, semantics)
        out.append(("error", err.split(":")[0]) if err else norm(v))
    return out


# -- params ----------------------------------------------------------------------

@dataclass(frozen=True)
class CodeFixParams:
    n_statements: int = 12
    bug_kind: str | None = None  # one of bugs.BUG_KINDS, or None = drawn by bug_mix
    bug_mix: float = 0.5  # share of semantic (vs syntactic) bugs when bug_kind is None
    n_bugs: int = 1
    bug_distance: int | None = None  # None: any site (the drawn kind is kept); 1..8: steer toward it
    hidden_inputs: int = 0  # 0 = V-a
    edge_cases: int = 0  # 0..3 of (empty list, zero, negative, single element)
    misleading_comment: bool = False
    spec_examples: int = 3  # V-c
    identifier_confusability: float = 0.0
    n_params: int = 2

    def __post_init__(self) -> None:
        if self.bug_kind is not None and self.bug_kind not in B.BUG_KINDS:
            raise ValueError(f"bug_kind must be one of {B.BUG_KINDS}")
        checks = [(8 <= self.n_statements <= 25, "n_statements in 8..25"), (0 <= self.bug_mix <= 1, "bug_mix in 0..1"),
                  (1 <= self.n_bugs <= 3, "n_bugs in 1..3"), (self.bug_distance is None or 1 <= self.bug_distance <= 8, "bug_distance in 1..8"),
                  (0 <= self.hidden_inputs <= 8, "hidden_inputs in 0..8"), (0 <= self.edge_cases <= 3, "edge_cases in 0..3"),
                  (1 <= self.spec_examples <= 3, "spec_examples in 1..3"),
                  (0 <= self.identifier_confusability <= 1, "identifier_confusability in 0..1"),
                  (1 <= self.n_params <= 3, "n_params in 1..3")]
        for ok, msg in checks:
            if not ok:
                raise ValueError(msg)


def code_params_for(params: CodeFixParams, kinds=()) -> CodeDimParams:
    """The generator dials: every mechanism a bug kind needs a site for."""
    return CodeDimParams(n_statements=params.n_statements, n_variables=4,
                         identifier_confusability=params.identifier_confusability,
                         chain_depth=2, stack_depth=3, loop_branch=True, index_math=1,
                         while_on="frozen_predicate" in kinds,
                         mutable_default_on="default_shared" in kinds)


def reference_program(params: CodeFixParams, seed: int, kinds=()) -> Program:
    prog = generate(code_params_for(params, kinds), seed)
    ref = to_function_form(prog, seed, params.n_params)
    if "default_shared" in kinds:
        ref = B.freshify_defaults(ref)
    if "shadow" in kinds:
        ref = B.helperize(ref, seed)
    return ref


# -- inputs ------------------------------------------------------------------------

EDGE_CASES = ("empty_list", "zero", "negative", "single_element")
_CHARS = "abcxyz"


def _random_arg(kind: str, rng: _Rng, allow_empty: bool):
    if kind == "int":
        return rng.randint(-9, 20)
    if kind == "list":
        return [rng.randint(-9, 20) for _ in range(rng.randint(1 if not allow_empty else 0, 6))]
    return "".join(rng.choice(_CHARS) for _ in range(rng.randint(1 if not allow_empty else 0, 8)))


def edge_inputs(program: Program, base: tuple, n: int) -> list[tuple[str, tuple]]:
    """(case, args) for the first `n` edge cases the parameter kinds allow."""
    kinds = program.param_kinds
    out = []
    for case in EDGE_CASES:
        if len(out) >= n:
            break
        want = {"empty_list": ("list", []), "zero": ("int", 0), "negative": ("int", -3),
                "single_element": ("list", [5])}[case]
        for i, k in enumerate(kinds):
            if k == want[0]:
                args = list(base)
                args[i] = copy.deepcopy(want[1])
                out.append((case, tuple(args)))
                break
    return out


def input_pool(program: Program, base: tuple, seed: int, n: int = 40) -> list[tuple]:
    """`base` first, then random inputs (non-empty lists/strs; empties come
    only from edge cases) on which the reference runs without error."""
    rng = _Rng("codefix-inputs", seed)
    pool, seen = [], set()
    cands = [base] + [tuple(_random_arg(k, rng, False) for k in program.param_kinds) for _ in range(n * 3)]
    for args in cands:
        key = repr(args)
        if key in seen:
            continue
        seen.add(key)
        if execute(program, args)[1] is None:
            pool.append(args)
        if len(pool) >= n:
            break
    return pool


# -- rendering -----------------------------------------------------------------------

def render_function(program: Program, rendering_seed: int = 0, notes: dict | None = None) -> tuple[str, dict]:
    """(source, name map) of a function-form program; the entry keeps its
    name `solve`."""
    names, _ = build_names(program, rendering_seed, "auto")
    entry = program.entry or "solve"
    clash = [k for k, v in names.items() if v == entry and k != entry]
    for k in clash:
        names[k] = f"{names[k]}_v"
    names[entry] = entry
    lines: list[str] = []
    for s in program.body:
        if lines and lines[-1] != "":
            lines.append("")
        lines += _block((s,), names, 0, notes or {})
    return "\n".join(lines) + "\n", names


# -- tasks --------------------------------------------------------------------------

@dataclass(frozen=True)
class RepairTask:
    params: CodeFixParams
    seed: int
    reference: Program
    buggy: Program
    bug_sites: tuple
    bug_kinds: tuple
    bug_distances: tuple
    visible_input: tuple
    visible_expected: object
    visible_wrong: object
    hidden_inputs: tuple
    hidden_edge_cases: tuple
    buggy_source: str
    reference_source: str
    names: dict
    draws: int  # programs drawn before this one was available


@dataclass(frozen=True)
class WriteTask:
    params: CodeFixParams
    seed: int
    reference: Program
    spec_text: str
    examples: tuple  # ((args, output), ...)
    hidden_inputs: tuple
    hidden_edge_cases: tuple
    reference_source: str
    draws: int
    names: dict = None  # latent id -> rendered name


def _pick_kinds(params: CodeFixParams, rng: _Rng) -> list[str]:
    if params.bug_kind is not None:
        return [params.bug_kind] * params.n_bugs
    out = []
    for _ in range(params.n_bugs):
        group = B.SEMANTIC_BUGS if rng.random() < params.bug_mix else B.SYNTACTIC_BUGS
        out.append(rng.choice(group))
    return out


def _fallback_order(kind: str) -> list[str]:
    group = B.SEMANTIC_BUGS if kind in B.SEMANTIC_BUGS else B.SYNTACTIC_BUGS
    return [kind] + [k for k in group if k != kind and k != "default_shared"]


def _original_args(seed: int, params: CodeFixParams, kinds) -> tuple[Program, tuple]:
    """(function-form reference, the generated program's own initial values for its parameters)."""
    from ..codeworld.model import Assign

    prog = generate(code_params_for(params, kinds), seed)
    ref = to_function_form(prog, seed, params.n_params)
    inits: dict = {}
    for st in prog.body:
        if isinstance(st, Assign):
            inits.setdefault(st.target, st.value)
    it = Interpreter(TRUE)
    args = []
    for p in B.solve_def(ref).params:
        args.append(it._eval(inits[p], it._gframe))
    if "default_shared" in kinds:
        ref = B.freshify_defaults(ref)
    if "shadow" in kinds:
        ref = B.helperize(ref, seed)
    return ref, tuple(args)


def make_repair_task(params: CodeFixParams, seed: int) -> RepairTask:
    """Draw programs from seeds derived from `seed` until bugs of the asked
    kinds can be injected load-bearingly (raises GenerationFailed after
    MAX_DRAWS)."""
    last = "no draw"
    for draw in range(MAX_DRAWS):
        s = _stable_int("codefix-repair", seed, draw)
        rng = _Rng("codefix-kinds", seed, draw)
        want = _pick_kinds(params, rng)
        try:
            ref, base = _original_args(s, params, want)
        except GenerationFailed as e:
            last = str(e)
            continue
        if execute(ref, base)[1] is not None:
            last = "reference fails on its own inputs"
            continue
        pool = input_pool(ref, base, s)
        edges = [(c, a) for c, a in edge_inputs(ref, base, params.edge_cases) if execute(ref, a)[1] is None]
        probe = pool[:8]
        ref_probe = outputs(ref, probe)
        buggy, sites, kinds, dists = ref, [], [], []
        ok = True
        for k in want:
            # first load-bearing site per kind; with bug_kind=None the drawn kind competes with the
            # rest of its group (semantic / syntactic) on closeness to the requested bug_distance
            found = []
            prev = outputs(buggy, probe)
            steer = params.bug_kind is None and params.bug_distance is not None
            want_d = params.bug_distance if params.bug_distance is not None else 0
            for rank, kind in enumerate(_fallback_order(k) if params.bug_kind is None else [k]):
                try:
                    options = B.inject(buggy, kind, _stable_int(s, kind, len(sites)), params.bug_distance,
                                       exclude=tuple(sites))
                except B.BugUnavailable:
                    continue
                for cand, sid, d in options:
                    out = outputs(cand, probe)
                    if any(o != p and o[0] != "error" for o, p in zip(out, prev)):
                        found.append((abs(d - want_d) if params.bug_distance is not None else 0, rank, kind, cand,
                                      sid, d))
                        break
                if found and (not steer or (found[-1][0] <= 2 and rank == 0)):
                    break
            if not found:
                ok = False
                last = f"no load-bearing site for {k}"
                break
            # the drawn kind is kept when it lands within 2 statements of the requested distance
            _, _, kind, buggy, sid, d = min(found, key=lambda x: (not (x[1] == 0 and x[0] <= 2), x[0], x[1]))
            sites.append(sid), kinds.append(kind), dists.append(d)
        if not ok:
            continue
        bug_probe = outputs(buggy, probe)
        vis = next((i for i, (o, r) in enumerate(zip(bug_probe, ref_probe)) if o != r and o[0] != "error"), None)
        if vis is None:
            last = "no visible input"
            continue
        visible = probe[vis]
        rest = [a for a in pool if a != visible]
        hidden: list = []
        hidden_cases: list = []
        if params.hidden_inputs:
            for c, a in edges[:params.hidden_inputs]:
                hidden.append(a)
                hidden_cases.append(c)
            differing = [a for a in rest if outputs(buggy, [a]) != outputs(ref, [a])]
            if not differing and not any(outputs(buggy, [a]) != outputs(ref, [a]) for a in hidden):
                last = "no hidden input catches a symptom patch"
                continue
            if differing and not any(outputs(buggy, [a]) != outputs(ref, [a]) for a in hidden):
                if len(hidden) >= params.hidden_inputs:
                    hidden.pop(), hidden_cases.pop()
                hidden.append(differing[0])
                hidden_cases.append("differs")
            for a in rest:
                if len(hidden) >= params.hidden_inputs:
                    break
                if a not in hidden:
                    hidden.append(a)
                    hidden_cases.append("random")
        notes = {}
        names_src, names = render_function(buggy, s)
        if params.misleading_comment:
            notes = {sites[0]: "this line is correct"}
            names_src, names = render_function(buggy, s, notes)
        ref_src, _ = render_function(ref, s)
        exp, _ = execute(ref, visible)
        wrong, _ = execute(buggy, visible)
        return RepairTask(params, seed, ref, buggy, tuple(sites), tuple(kinds), tuple(dists), visible, exp, wrong,
                          tuple(hidden), tuple(hidden_cases), names_src, ref_src, names, draw)
    raise GenerationFailed(f"no repair task for seed {seed}: {last}")


inject_bugs = make_repair_task  # the injection loop lives in make_repair_task (sites need inputs)

PAIR_TRACE_TARGET = "v_pairtrace_result"


def _lit(v):
    from ..codeworld.model import Const

    if isinstance(v, (list, tuple)):
        return ListLit(tuple(_lit(x) for x in v))
    return Const(v)


def trace_program(task: RepairTask) -> Program:
    """The paired /trace query as a round-2 `codeworld` program
    (docs/spec-province-v-ambiguities.md A1.1): the BUGGY function plus one
    top-level `result = solve(<visible input>)`, queried for `result`'s final
    value. Rendered with `codeworld.render.render` -- the exact path
    `CodeTraceEvaluator` uses -- so it carries no expected output, no bug /
    repair framing and no misleading-comment note (those live only in
    `render_repair_prompt` / `RepairTask.buggy_source`)."""
    from dataclasses import replace

    from ..codeworld.model import Assign, Call, Query

    entry = task.buggy.entry or "solve"
    sid = 1 + max((getattr(s, "sid", 0) or 0 for s in iter_stmts(task.buggy.body)), default=0)
    call = Assign(sid, PAIR_TRACE_TARGET, Call(entry, tuple(_lit(a) for a in task.visible_input)))
    return replace(task.buggy, body=tuple(task.buggy.body) + (call,), query=Query("final_value", PAIR_TRACE_TARGET),
                   comments=())


# -- V-c spec text ---------------------------------------------------------------------

_KIND_WORDS = {"int": "an integer", "list": "a list of integers", "str": "a string"}


def summary_phrases(program: Program) -> list[str]:
    """Behaviour words from the reference's structure (never its code)."""
    sv = B.solve_def(program)
    phrases = []
    stmts = list(iter_stmts(sv.body))
    if any(isinstance(s, For) for s in stmts) and any(isinstance(s, If) for s in stmts):
        phrases.append("walks a list and accumulates a running total, adding or subtracting depending on "
                       "a threshold")
    if any(isinstance(s, While) for s in stmts):
        phrases.append("repeats an update until a condition stops holding")
    from ..codeworld.model import AugAssign, MethodCall, ExprStmt

    if any(isinstance(s, AugAssign) and isinstance(s.value, ListLit) for s in stmts) or any(
            isinstance(s, ExprStmt) and isinstance(s.value, MethodCall) for s in stmts):
        phrases.append("builds up a list, partly through a second name for it")
    if any(isinstance(x, Slice) for s in stmts for x in _subexprs(s)):
        phrases.append("uses part of a list")
    if any(isinstance(s, FuncDef) for s in program.body if s.name != sv.name):
        phrases.append("uses a small helper calculation")
    return phrases


def _subexprs(node):
    if hasattr(node, "__dataclass_fields__"):
        yield node
        for f in node.__dataclass_fields__:
            v = getattr(node, f)
            if isinstance(v, tuple):
                for x in v:
                    if not hasattr(x, "sid"):
                        yield from _subexprs(x)
            elif not hasattr(v, "sid"):
                yield from _subexprs(v)


def render_spec(program: Program, names: dict, examples) -> str | None:
    phrases = summary_phrases(program)
    if not phrases:
        return None
    sv = B.solve_def(program)
    args = ", ".join(f"`{names[p]}` ({_KIND_WORDS[k]})" for p, k in zip(sv.params, program.param_kinds))
    return (f"`solve` takes {args} and returns a single value. It " + "; it ".join(phrases)
            + ". The examples below fix the exact behaviour.")


def make_write_task(params: CodeFixParams, seed: int) -> WriteTask:
    last = "no draw"
    n_hidden = max(params.hidden_inputs, 4)
    for draw in range(MAX_DRAWS):
        s = _stable_int("codefix-write", seed, draw)
        try:
            ref, base = _original_args(s, params, ())
        except GenerationFailed as e:
            last = str(e)
            continue
        pool = input_pool(ref, base, s)
        if len(pool) < params.spec_examples + n_hidden:
            last = "too few inputs"
            continue
        src, names = render_function(ref, s)
        examples = tuple((a, execute(ref, a)[0]) for a in pool[:params.spec_examples])
        text = render_spec(ref, names, examples)
        if text is None:
            last = "no faithful spec for this structure"
            continue
        edges = [(c, a) for c, a in edge_inputs(ref, base, params.edge_cases) if execute(ref, a)[1] is None]
        hidden = [a for _, a in edges] + [a for a in pool[params.spec_examples:]][:n_hidden]
        cases = [c for c, _ in edges] + ["random"] * (len(hidden) - len(edges))
        return WriteTask(params, seed, ref, text, examples, tuple(hidden), tuple(cases), src, draw, names)
    raise GenerationFailed(f"no write task for seed {seed}: {last}")


__all__ = ["CodeFixParams", "EDGE_CASES", "RepairTask", "WriteTask", "code_params_for", "edge_inputs", "execute",
           "inject_bugs", "input_pool", "trace_program", "make_repair_task", "make_write_task", "norm", "outputs", "reference_program",
           "render_function", "render_spec", "summary_phrases"]
