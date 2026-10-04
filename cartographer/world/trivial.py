"""Trivialized false-belief worlds: the query agent sees the final move too.

Motivation (Malek et al. 2025, arXiv 2507.07313; SimpleToM, arXiv
2410.13648): when the classic false-belief story is TRIVIALIZED -- the agent
actually watches the final move, so the right answer is simply the object's
true location -- frontier models still answer as if the trap were there
(GPT-4o 75 -> 20 %, Gemini 2.5 Pro 94 -> 63 %, o3 88 -> 74 %). They match
the "Sally-Anne" template instead of reading who saw what.

At `witness_cutoff == 0.0` every generated belief task is the classic story:
the query agent witnesses every causal-chain Move on the subject EXCEPT the
final one, so the correct answer is the pre-final-move location (the
"stale" location) and differs from the true location. `make_full_witness`
adds the agent to the witness set of every Move on the subject, final move
included; nothing else changes (same events, same order, same other
witnesses, same narration order, same query), so `simulate.answer` on the
result is the TRUE location and the stale location becomes the trap. That
trap is the mirror image of the `final_true_state` foil (see `foils.py`'s
`stale_belief`).

Applicability is strict and never silent: a world qualifies only if its
original witnessing is the classic shape (the agent missed exactly the
final subject Move and nothing else that matters), so the trap answer in the
trivialized story is exactly what the original story's answer was. Worlds
that don't qualify (non-belief queries, `expect_unknown`, a truncating
`witness_cutoff`, a later Tell that would override what the agent saw,
subject distractor moves the agent missed, ...) are reported unavailable:
`full_witness_applicable` returns False and `make_full_witness` raises
`FullWitnessUnavailable` rather than returning an un-trivialized world.

Pure functions over the latent `World`: no randomness, no model calls.
"""

from __future__ import annotations

from dataclasses import replace

from .model import Move, World
from .simulate import answer as sim_answer
from .simulate import simulate


class FullWitnessUnavailable(ValueError):
    """Raised by `make_full_witness` when trivialization would not turn the
    world into a valid full-witness task (see `full_witness_applicable`)."""


def _belief_agent(world: World) -> str | None:
    query = world.query
    if query.kind != "belief" or query.agent is None or query.expect_unknown:
        return None
    return query.agent


def _subject_move_indices(world: World) -> list[int]:
    """Indices of every `Move` on the query subject, in event-list order
    (the order `simulate` applies them in -- chronological)."""
    subject = world.query.subject
    return [
        i for i, ev in enumerate(world.events) if isinstance(ev, Move) and ev.obj == subject
    ]


def _with_agent_witnessing(world: World, agent: str, indices: list[int], add: bool) -> World:
    events = list(world.events)
    for i in indices:
        ev = events[i]
        witnesses = ev.witnesses | {agent} if add else ev.witnesses - {agent}
        events[i] = replace(ev, witnesses=frozenset(witnesses))
    return replace(world, events=events)


def _full_witness_unchecked(world: World) -> World:
    agent = _belief_agent(world)
    assert agent is not None
    return _with_agent_witnessing(world, agent, _subject_move_indices(world), add=True)


def is_full_witness_world(world: World) -> bool:
    """True iff `world` is a belief world whose query agent witnesses EVERY
    Move on the subject (what `make_full_witness` produces). Generated
    (un-trivialized) belief worlds never have this shape: the final chain
    move's actor is always drawn excluding the query agent."""
    agent = _belief_agent(world)
    if agent is None:
        return False
    moves = _subject_move_indices(world)
    return bool(moves) and all(agent in world.events[i].witnesses for i in moves)


def stale_location(world: World) -> str | None:
    """Location id the query agent would believe had they NOT witnessed the
    final Move on the subject, everything else as given -- i.e. the belief
    under the original (un-trivialized) witnessing, which on a classic
    false-belief world is the pre-final-move location.

    Invariant under `make_full_witness` on applicable worlds: on the
    original world it equals `simulate.answer(world)`; on the trivialized
    world it equals the same location (the trap). None for non-belief /
    expect_unknown queries, worlds with no Move on the subject, or when the
    agent would hold no belief at all.
    """
    agent = _belief_agent(world)
    if agent is None:
        return None
    moves = _subject_move_indices(world)
    if not moves:
        return None
    blinded = _with_agent_witnessing(world, agent, [moves[-1]], add=False)
    return simulate(blinded).beliefs.get((agent, world.query.subject))


def _true_location(world: World) -> str | None:
    return simulate(world).final_locations.get(world.query.subject)


def full_witness_applicable(world: World) -> bool:
    """Whether trivializing this (un-trivialized) world yields a valid
    full-witness task whose trap is the original story's answer.

    All of the following must hold:
      - belief query with a named agent and `expect_unknown=False`;
      - at least one Move on the subject;
      - the stale location exists and differs from the true location (the
        trivialization actually changes the answer);
      - the original world's answer IS the stale location (classic
        false-belief shape: the agent missed the final move);
      - after trivialization the answer is the TRUE location (no later Tell
        or other event overrides what the agent saw);
      - the trivialized world's stale location is unchanged (the agent
        missed nothing BUT the final move, so the trap in the trivialized
        story is exactly the original answer).
    Call this on the un-trivialized world; an already-trivialized world is
    not applicable (its answer is no longer the stale location).
    """
    if _belief_agent(world) is None:
        return False
    if not _subject_move_indices(world):
        return False
    true_loc = _true_location(world)
    stale = stale_location(world)
    if stale is None or true_loc is None or stale == true_loc:
        return False
    if sim_answer(world).value != stale:
        return False
    trivialized = _full_witness_unchecked(world)
    if sim_answer(trivialized).value != true_loc:
        return False
    if stale_location(trivialized) != stale:
        return False
    return True


def make_full_witness(world: World, seed: int) -> World:
    """Return `world` with the belief-query agent added as a witness of
    EVERY Move on the subject (the whole causal chain, final move
    included). Everything else is unchanged: same events in the same
    order, same other witnesses, same narration order, relative-time cues
    and query. `simulate.answer` on the result is the true location.

    Raises `FullWitnessUnavailable` when `full_witness_applicable(world)`
    is False -- an inapplicable world is never silently passed through.
    `seed` is accepted for signature symmetry with `mutate.*`; the
    transformation is fully determined by `world`.
    """
    del seed
    if not full_witness_applicable(world):
        raise FullWitnessUnavailable(
            "full-witness trivialization unavailable for this world "
            "(needs a classic false-belief belief query whose agent missed "
            "only the final subject move)"
        )
    return _full_witness_unchecked(world)
