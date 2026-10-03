"""Realised difficulty dimensions of a generated program. Analysis reads
these, never the requested knob settings (a 6-statement request with three
traps on really produces ~20 statements)."""

from __future__ import annotations

from .interpret import TRUE, answer_from_trace, run
from .model import FuncDef, Program, iter_stmts, nesting_depth


def _free_reads(fd: FuncDef) -> set[str]:
    from .generate import _names_in
    from .interpret import _assigned_names

    return _names_in(fd.body) - set(fd.params) - _assigned_names(fd.body)


def measure(program: Program) -> dict[str, float]:
    tr = run(program, TRUE)
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
    }
