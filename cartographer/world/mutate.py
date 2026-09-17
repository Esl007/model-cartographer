"""Mutation operators over `World`s: each returns a new object, input unchanged.

`ANSWER_PRESERVING` operators never change `answer(world)`. `ANSWER_CHANGING`
operators are expected to (for the query kinds they target).
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from .model import Filler, Move, Obj, World
from .simulate import validate


def _stable_int(*parts: str, modulus: int) -> int:
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    as_int = int(digest[:15], 16)
    return as_int % modulus


def _append_events(
    world: World, new_events: list, seed: int, tag: str, objects=None
) -> World:
    events = list(world.events) + new_events
    narration_order = list(world.narration_order) + list(
        range(len(world.events), len(events))
    )
    n = len(narration_order)
    n_shuffles = _stable_int(tag, "shuffle_count", str(seed), modulus=max(n, 1))
    for k in range(n_shuffles):
        pos = _stable_int(tag, "shuffle_pos", str(seed), str(k), modulus=max(n - 1, 1))
        if n > 1:
            narration_order[pos], narration_order[pos + 1] = (
                narration_order[pos + 1],
                narration_order[pos],
            )
    extra = {"objects": objects} if objects is not None else {}
    return replace(world, events=events, narration_order=narration_order, **extra)


def add_distractors(world: World, n: int, similarity: str, seed: int) -> World:
    location_ids = [l.id for l in world.locations]
    entity_ids = [e.id for e in world.entities]
    subject = world.query.subject

    objects = list(world.objects)
    non_subject = [o.id for o in objects if o.id != subject]
    if not non_subject:
        decoy = Obj(id=f"{subject}_decoy", name="decoy", category="decoy")
        objects.append(decoy)
        non_subject = [decoy.id]

    start_time = max((e.time for e in world.events), default=-1) + 1
    new_events = []
    for i in range(n):
        idx = _stable_int("mutate_distractor_obj", similarity, str(seed), str(i), modulus=len(non_subject))
        obj_id = non_subject[idx]
        src_idx = _stable_int("mutate_distractor_src", similarity, str(seed), str(i), modulus=len(location_ids))
        src = location_ids[src_idx]
        remaining = [l for l in location_ids if l != src] or location_ids
        dst_idx = _stable_int("mutate_distractor_dst", similarity, str(seed), str(i), modulus=len(remaining))
        dst = remaining[dst_idx]
        actor_idx = _stable_int("mutate_distractor_actor", similarity, str(seed), str(i), modulus=len(entity_ids))
        actor = entity_ids[actor_idx]
        new_events.append(
            Move(
                obj=obj_id,
                src=src,
                dst=dst,
                actor=actor,
                time=start_time + i,
                witnesses=frozenset({actor}),
            )
        )
    new_world = _append_events(world, new_events, seed, "add_distractors", objects=objects)
    assert not validate(new_world)
    return new_world


def add_filler(world: World, n: int, seed: int) -> World:
    entity_ids = [e.id for e in world.entities]
    start_time = max((e.time for e in world.events), default=-1) + 1
    new_events = []
    for i in range(n):
        actor_idx = _stable_int("mutate_filler_actor", str(seed), str(i), modulus=len(entity_ids))
        actor = entity_ids[actor_idx]
        seed_val = _stable_int("mutate_filler_seed", str(seed), str(i), modulus=1_000_000)
        new_events.append(
            Filler(
                actor=actor,
                description_seed=seed_val,
                time=start_time + i,
                witnesses=frozenset({actor}),
            )
        )
    new_world = _append_events(world, new_events, seed, "add_filler")
    assert not validate(new_world)
    return new_world


def scramble_narration(world: World, strength: float, seed: int) -> World:
    n = len(world.events)
    if n < 2:
        return replace(world, narration_order=list(world.narration_order))
    max_inversions = n * (n - 1) // 2
    n_transpositions = int(round(strength * max_inversions))
    offset = _stable_int("scramble_mutate_offset", str(seed), modulus=n - 1)
    result = list(world.narration_order)
    for k in range(n_transpositions):
        pos = (offset + k) % (n - 1)
        result[pos], result[pos + 1] = result[pos + 1], result[pos]
    new_world = replace(world, narration_order=result)
    assert not validate(new_world)
    return new_world


def extend_chain(world: World, k: int, seed: int) -> World:
    subject = world.query.subject
    chain_moves = [
        e for e in world.events if isinstance(e, Move) and e.obj == subject
    ]
    location_ids = [l.id for l in world.locations]
    entity_ids = [e.id for e in world.entities]

    if chain_moves:
        cur_loc = chain_moves[-1].dst
    else:
        cur_loc = location_ids[0]

    start_time = max((e.time for e in world.events), default=-1) + 1
    new_events = []
    for i in range(k):
        remaining = [l for l in location_ids if l != cur_loc] or location_ids
        dst_idx = _stable_int("extend_chain_dst", str(seed), str(i), modulus=len(remaining))
        dst = remaining[dst_idx]
        actor_idx = _stable_int("extend_chain_actor", str(seed), str(i), modulus=len(entity_ids))
        actor = entity_ids[actor_idx]
        new_events.append(
            Move(
                obj=subject,
                src=cur_loc,
                dst=dst,
                actor=actor,
                time=start_time + i,
                witnesses=frozenset({actor}),
            )
        )
        cur_loc = dst

    new_world = _append_events(world, new_events, seed, "extend_chain")
    assert not validate(new_world)
    return new_world


def add_coref_pressure(world: World, n: int, seed: int) -> World:
    if not world.entities:
        return replace(world, events=list(world.events), narration_order=list(world.narration_order))
    target_gender = world.entities[0].gender
    same_gender = [e.id for e in world.entities if e.gender == target_gender]
    if not same_gender:
        same_gender = [world.entities[0].id]

    start_time = max((e.time for e in world.events), default=-1) + 1
    new_events = []
    for i in range(n):
        actor = same_gender[i % len(same_gender)]
        seed_val = _stable_int("mutate_coref_seed", str(seed), str(i), modulus=1_000_000)
        new_events.append(
            Filler(
                actor=actor,
                description_seed=seed_val,
                time=start_time + i,
                witnesses=frozenset({actor}),
            )
        )
    new_world = _append_events(world, new_events, seed, "add_coref_pressure")
    assert not validate(new_world)
    return new_world


ANSWER_PRESERVING = {
    "add_distractors",
    "add_filler",
    "scramble_narration",
    "add_coref_pressure",
}
ANSWER_CHANGING = {
    "extend_chain",
}
