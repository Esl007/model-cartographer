"""Realised difficulty dimensions of a generated program. Analysis reads
these, never the requested knob settings (a 6-statement request with three
traps on really produces ~20 statements)."""

from __future__ import annotations

from .interpret import answer_from_trace, run
from .model import Assign, AugAssign, ExprStmt, For, FuncDef, MethodCall, Program, SetItem, While, iter_stmts, \
    nesting_depth


def _free_reads(fd: FuncDef) -> set[str]:
    from .generate import _names_in
    from .interpret import _assigned_names

    return _names_in(fd.body) - set(fd.params) - _assigned_names(fd.body)


def _writes(s) -> set[str]:
    out: set[str] = set()
    for x in iter_stmts([s]):
        if isinstance(x, (Assign, AugAssign, SetItem)):
            out.add(x.target)
        elif isinstance(x, ExprStmt) and isinstance(x.value, MethodCall):
            out.add(x.value.obj)
        elif isinstance(x, For):
            out.add(x.var)
    return out


def critical_path_depth(program: Program) -> int:
    """Longest chain of top-level statements feeding the queried value, by
    static def-use over the top-level body: a statement that writes names W
    after reading names R gets depth 1 + max(depth of R) and every name in W
    takes that depth (a compound statement -- if / for / while -- counts as
    one step that reads and writes everything inside it; function bodies are
    not followed into). The result is the depth of the query target at the
    end (stdout queries: of the deepest printed name). Under truth every
    statement runs, so this is an upper bound on the dynamic chain."""
    from .generate import _names_in

    depth: dict[str, int] = {}
    printed = 0
    for s in program.body:
        if isinstance(s, FuncDef):
            continue
        writes = _writes(s)
        reads = _names_in(s)
        if not isinstance(s, Assign):  # read-modify-write (+=, x[i] =, .append, compound blocks)
            reads |= writes
        d = 1 + max((depth.get(r, 0) for r in reads), default=0)
        for w in writes:
            depth[w] = d
        if type(s).__name__ == "Print":
            printed = max(printed, d - 1)
    t = program.query.target
    return depth.get(t, 0) if t is not None else printed


def measure(program: Program) -> dict[str, float]:
    tr = run(program)  # the program's own (dialect) semantics
    nodes = list(iter_stmts(program.body))
    data_vars = [k for k in tr.globals if program.var(k) is not None]
    twin_ids = {v.id for v in program.variables if v.twin_of} | {v.twin_of for v in program.variables if v.twin_of}
    funcs = [s for s in nodes if isinstance(s, FuncDef)]
    distractor_nodes = sum(1 for _ in iter_stmts([s for s in program.body if s.sid in program.distractor_ids]))
    return {
        "n_statements": float(len(nodes)),
        "max_nesting": float(nesting_depth(program.body)),
        "n_variables": float(len(data_vars)),
        "trace_length": float(tr.steps),
        "distractor_fraction": distractor_nodes / len(nodes) if nodes else 0.0,
        "n_live_aliases": float(len(tr.alias_pairs)),
        "n_closures": float(sum(1 for f in funcs if _free_reads(f))),
        "traps_on": float(len(program.params.traps_on())),
        "identifier_confusability": (
            len([k for k in data_vars if k in twin_ids]) / len(data_vars) if data_vars else 0.0
        ),
        "answer_size": float(len(repr(answer_from_trace(program, tr)))),
        **_round2(program, nodes),
    }


def _round2(program: Program, nodes: list) -> dict[str, float]:
    """Realised round-2 mechanisms; empty for programs with every round-2
    dial off (so their measurements are exactly as before)."""
    if not program.params.round2_on():
        return {}
    sites = {k for k, _ in program.trap_sites}
    return {
        "chain_depth": float(len(program.path_sids)),
        "critical_path_depth": float(critical_path_depth(program)),
        "stack_depth": float(program.params.stack_depth if "stack_depth" in sites else 0),
        "loop_branch": float("loop_branch" in sites),
        "while_on": float(sum(1 for s in nodes if isinstance(s, While))),
        "sign_chain": float(program.params.sign_chain if "sign_chain" in sites else 0),
        "index_math": float(program.params.index_math if "index_math" in sites else 0),
        "misleading_comments": float(sum(1 for c in program.comments if not c[4])),
        "neutral_comments": float(sum(1 for c in program.comments if c[4])),
    }
