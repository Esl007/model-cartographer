"""Realized dimension measurement, computed from the world itself.

Mirrors the five canonical dims from `cartographer/store/schema.py`'s
`DimVector` convention: entity_state, temporal, coreference, distractors,
context_length. Never echoes `DimParams`; always recomputed from the
generated/mutated `World`.
"""

from __future__ import annotations

from .model import Filler, Move, Tell, Transfer, World


def _causal_chain_indices(world: World) -> set[int]:
    subject = world.query.subject
    return {
        i
        for i, event in enumerate(world.events)
        if isinstance(event, (Move, Transfer)) and event.obj == subject
    }


def _kendall_tau_distance(narration_order: list[int], n: int) -> int:
    positions = [0] * n
    for pos, event_idx in enumerate(narration_order):
        positions[event_idx] = pos
    inversions = 0
    for i in range(n):
        for j in range(i + 1, n):
            if positions[i] > positions[j]:
                inversions += 1
    return inversions


def measure(world: World) -> dict[str, float]:
    chain_indices = _causal_chain_indices(world)

    entity_state = float(len(chain_indices))

    n = len(world.events)
    if n >= 2:
        max_inversions = n * (n - 1) / 2
        inversions = _kendall_tau_distance(world.narration_order, n)
        temporal = inversions / max_inversions if max_inversions > 0 else 0.0
    else:
        temporal = 0.0

    gender_by_entity = {e.id: e.gender for e in world.entities}
    participants_by_gender: dict[str, set[str]] = {"f": set(), "m": set()}
    for event in world.events:
        for participant in _participants(event):
            gender = gender_by_entity.get(participant)
            if gender in participants_by_gender:
                participants_by_gender[gender].add(participant)
    coreference = 0
    for gender_group in participants_by_gender.values():
        members = sorted(gender_group)
        count = len(members)
        coreference += count * (count - 1) // 2

    distractors = float(
        sum(
            1
            for i, event in enumerate(world.events)
            if i not in chain_indices and not isinstance(event, Filler)
        )
    )

    context_length = float(len(world.events))

    witness_gap = float(_witness_gap(world, chain_indices))

    return {
        "entity_state": entity_state,
        "temporal": float(temporal),
        "coreference": float(coreference),
        "distractors": distractors,
        "context_length": context_length,
        "witness_gap": witness_gap,
    }


def _witness_gap(world: World, chain_indices: set[int]) -> int:
    """Number of causal-chain Move events, after the query agent's LAST
    witnessed causal-chain event, that the agent did NOT witness.

    0 whenever the query isn't a belief query, the agent has no recorded
    chain-witnessing at all (falls back to a Tell-derived belief instead --
    not a "gap" in the sense this dim measures), or witness_cutoff is
    inactive (agent witnesses through the second-to-last chain event, the
    pre-cutoff default, leaving no unwitnessed chain moves afterward when
    the chain has <=1 further moves -- see below for the exact boundary).
    """
    query = world.query
    if query.kind != "belief" or query.agent is None:
        return 0
    agent = query.agent
    chain_moves_in_order = [
        i for i in sorted(chain_indices) if isinstance(world.events[i], Move)
    ]
    witnessed_chain_positions = [
        pos
        for pos, i in enumerate(chain_moves_in_order)
        if agent in world.events[i].witnesses
    ]
    if not witnessed_chain_positions:
        return 0
    last_witnessed_pos = max(witnessed_chain_positions)
    return len(chain_moves_in_order) - 1 - last_witnessed_pos


def _participants(event) -> set[str]:
    if isinstance(event, Move):
        return {event.actor}
    if isinstance(event, Transfer):
        return {event.giver, event.receiver}
    if isinstance(event, Tell):
        return {event.speaker, event.listener}
    if isinstance(event, Filler):
        return {event.actor}
    return set()
