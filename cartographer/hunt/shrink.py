"""Delta debugging (ddmin, Zeller & Hildebrandt 2002) over a program's
TOP-LEVEL statements: the smallest program that still fails.

A candidate (an order-preserving subset of the top-level statements, with
`params`, `variables` and `seed` unchanged so names render identically) is
ADMISSIBLE iff, under the program's own semantics, it runs without error,
the query target is still defined, and its true answer differs from at
least one foil reading that does not crash -- otherwise there is nothing a
reader could get wrong. Shrunk programs deliberately break generator
invariants (dead traps, unfolded variables), so `generate.validate` is NOT
applied; this module's own admissibility check is the contract.

Only admissible candidates are shown to `still_fails`; every such call
counts against `max_checks`. Calls are memoised on the statement subset,
so the result is deterministic given `still_fails`' answers.
`still_fails` may raise `ShrinkAborted` (budget gone): the best program so
far is returned with `aborted=True`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable

from ..codeworld.foils import foils_for
from ..codeworld.interpret import FOIL_ERROR, answer_from_trace, run
from ..codeworld.model import Program
from ..codeworld.render import render


class ShrinkAborted(Exception):
    """Raised by a `still_fails` callback that can no longer run (budget)."""


@dataclass
class ShrinkResult:
    program: Program
    removed_sids: tuple = ()
    checks: int = 0
    necessary_sids: tuple = ()
    aborted: bool = False
    exhausted: bool = False  # max_checks reached before ddmin finished
    undetermined_sids: tuple = field(default_factory=tuple)  # necessity not tested (budget)


def subprogram(program: Program, keep_sids) -> Program:
    keep = set(keep_sids)
    body = tuple(s for s in program.body if s.sid in keep)
    present = {sid for s in body for sid in _sids(s)}
    return replace(
        program, body=body,
        distractor_ids=frozenset(d for d in program.distractor_ids if d in present),
        trap_sites=tuple((t, sid) for t, sid in program.trap_sites if sid in present),
    )


def _sids(stmt):
    from .rewrites import _all_sids

    return _all_sids((stmt,))


def admissible(program: Program, rendering_seed: int | None = None) -> bool:
    tr = run(program)
    if tr.error is not None:
        return False
    if program.query.kind == "final_value":
        if program.query.target not in tr.globals:
            return False
    elif not tr.stdout:
        return False
    truth = answer_from_trace(program, tr)
    differs = False
    for sem in foils_for(program).values():
        a = answer_from_trace(program, run(program, sem))
        if a is not FOIL_ERROR and not (a == truth and type(a) is type(truth)):
            differs = True
            break
    if not differs:
        return False
    try:
        render(program, program.seed if rendering_seed is None else rendering_seed)
    except (KeyError, TypeError):
        return False
    return True


def shrink(program: Program, still_fails: Callable[[Program], bool], max_checks: int = 40,
           rendering_seed: int | None = None) -> ShrinkResult:
    all_sids = [s.sid for s in program.body]
    memo: dict[tuple, bool] = {}
    checks = [0]

    class _Exhausted(Exception):
        pass

    def test(sids: list[int]) -> bool:
        key = tuple(sids)
        if key in memo:
            return memo[key]
        cand = subprogram(program, sids)
        if not sids or not admissible(cand, rendering_seed):
            memo[key] = False
            return False
        if checks[0] >= max_checks:
            raise _Exhausted
        checks[0] += 1
        out = bool(still_fails(cand))
        memo[key] = out
        return out

    items = list(all_sids)
    aborted = exhausted = False
    n = 2
    try:
        while len(items) >= 2:
            size = len(items)
            chunk = -(-size // n)
            chunks = [items[i:i + chunk] for i in range(0, size, chunk)]
            reduced = False
            for c in chunks:
                if len(c) < size and test(c):
                    items, n, reduced = c, 2, True
                    break
            if not reduced:
                for i in range(len(chunks)):
                    comp = [s for j, ch in enumerate(chunks) if j != i for s in ch]
                    if comp and len(comp) < size and test(comp):
                        items, n, reduced = comp, max(n - 1, 2), True
                        break
            if not reduced:
                if n >= size:
                    break
                n = min(size, 2 * n)
    except _Exhausted:
        exhausted = True
    except ShrinkAborted:
        aborted = True

    necessary: list[int] = []
    undetermined: list[int] = []
    if not (aborted or exhausted):
        for sid in items:
            rest = [s for s in items if s != sid]
            try:
                if not test(rest):  # inadmissible or no longer fails
                    necessary.append(sid)
            except _Exhausted:
                undetermined.append(sid)
            except ShrinkAborted:
                aborted = True
                undetermined.extend(s for s in items if s not in necessary and s not in undetermined)
                break
    else:
        undetermined = list(items)
    keep = set(items)
    return ShrinkResult(
        program=subprogram(program, items),
        removed_sids=tuple(s for s in all_sids if s not in keep),
        checks=checks[0],
        necessary_sids=tuple(necessary),
        aborted=aborted,
        exhausted=exhausted,
        undetermined_sids=tuple(undetermined),
    )


__all__ = ["ShrinkAborted", "ShrinkResult", "admissible", "shrink", "subprogram"]
