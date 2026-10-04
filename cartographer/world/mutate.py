"""Mutation operators over `World`s: each returns a new object, input unchanged.

`ANSWER_PRESERVING` operators never change `answer(world)`. `ANSWER_CHANGING`
operators are expected to (for the query kinds they target).
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from .model import NOOP_KINDS, Filler, Move, NoOp, Obj, Query, Tell, Transfer, World
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

    # Each distractor object's existing location, from its most recent Move
    # in the world so far (if any), so appended Moves chain continuously.
    cur_loc: dict[str, str] = {}
    for e in world.events:
        if isinstance(e, Move):
            cur_loc[e.obj] = e.dst

    start_time = max((e.time for e in world.events), default=-1) + 1
    new_events = []
    for i in range(n):
        idx = _stable_int("mutate_distractor_obj", similarity, str(seed), str(i), modulus=len(non_subject))
        obj_id = non_subject[idx]
        if obj_id in cur_loc:
            src = cur_loc[obj_id]
        else:
            src_idx = _stable_int("mutate_distractor_src", similarity, str(seed), obj_id, modulus=len(location_ids))
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
        cur_loc[obj_id] = dst
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


# --- structural stressor 1: no-op distractors (GSM-NoOp) --------------------


def _validate_noops(world: World) -> list[str]:
    entity_ids = {e.id for e in world.entities}
    object_ids = {o.id for o in world.objects}
    location_ids = {l.id for l in world.locations}
    bad: list[str] = []
    for e in world.events:
        if isinstance(e, NoOp):
            if e.loc not in location_ids or e.actor not in entity_ids or e.obj not in object_ids:
                bad.append(f"NoOp references undefined id: {e!r}")
            if e.other is not None and (e.other not in entity_ids or e.other == e.actor):
                bad.append(f"NoOp has invalid `other`: {e!r}")
            if e.kind not in NOOP_KINDS:
                bad.append(f"NoOp has unknown kind {e.kind!r}")
    return bad


def _locations_before_slot(world: World, slot: int) -> dict[str, str]:
    """Object locations just before chronological slot `slot` (i.e. after
    applying events[:slot]). An object never moved before `slot` is placed
    at the `src` of its first Move, if it has one."""
    loc: dict[str, str] = {}
    for e in world.events:
        if isinstance(e, Move) and e.obj not in loc:
            loc[e.obj] = e.src
    for e in world.events[:slot]:
        if isinstance(e, Move):
            loc[e.obj] = e.dst
    return loc


def add_noop_distractors(world: World, n: int, seed: int) -> World:
    """Insert `n` inert NoOp events about the queried object (and, for
    belief queries, often performed by the query agent) at seed-stable
    CHRONOLOGICAL positions. Answer-preserving: the simulator ignores NoOps.

    Guarantees (relied on by tests/test_world_frontier.py):
    - each NoOp's `loc` differs from its object's true location at that
      moment, so "treat it as a real move" always yields a wrong location;
    - if any NoOp targets the query subject, at least one of them sits
      AFTER the subject's last real Move -- so a reader that treats NoOps as
      moves answers a wrong final location on state queries;
    - an order query's compared pair is remapped to the same two original
      events (NoOps are never compared events).
    Events are re-timed to their new chronological index (relative order of
    the original events is unchanged); `relative_times` is remapped.
    """
    if n <= 0:
        return replace(world, events=list(world.events), narration_order=list(world.narration_order))
    subject = world.query.subject
    entity_ids = [e.id for e in world.entities]
    location_ids = [l.id for l in world.locations]
    n_old = len(world.events)
    moved_objects = sorted({e.obj for e in world.events if isinstance(e, Move)})
    others = [o for o in moved_objects if o != subject]
    subject_moves = [i for i, e in enumerate(world.events) if isinstance(e, Move) and e.obj == subject]
    last_subject_move = subject_moves[-1] if subject_moves else -1

    # Decide targets first: ~3/4 target the subject; the rest (when another
    # moved object exists) target a different object but are still often
    # performed by the query agent.
    targets: list[str] = []
    for i in range(n):
        coin = _stable_int("noop_target", str(seed), str(i), modulus=4)
        targets.append(others[_stable_int("noop_other", str(seed), str(i), modulus=len(others))]
                       if (coin == 0 and others) else subject)
    anchor_i = next((i for i, t in enumerate(targets) if t == subject), None)

    inserts: list[tuple[int, int, NoOp]] = []  # (slot, i, event-without-final-time)
    for i in range(n):
        if i == anchor_i:
            lo = last_subject_move + 1
            slot = lo + _stable_int("noop_slot_anchor", str(seed), str(i), modulus=n_old - lo + 1)
        else:
            slot = _stable_int("noop_slot", str(seed), str(i), modulus=n_old + 1)
        obj = targets[i]
        cur = _locations_before_slot(world, slot).get(obj)
        choices = [l for l in location_ids if l != cur] or location_ids
        loc = choices[_stable_int("noop_loc", str(seed), str(i), modulus=len(choices))]
        kind = NOOP_KINDS[_stable_int("noop_kind", str(seed), str(i), modulus=len(NOOP_KINDS))]
        agent = world.query.agent
        if agent is not None and _stable_int("noop_agent_actor", str(seed), str(i), modulus=2) == 0:
            actor = agent
        else:
            actor = entity_ids[_stable_int("noop_actor", str(seed), str(i), modulus=len(entity_ids))]
        other = None
        if kind == "refused":
            pool = [e for e in entity_ids if e != actor]
            other = pool[_stable_int("noop_asker", str(seed), str(i), modulus=len(pool))]
        witnesses = {actor} | ({other} if other else set())
        if agent is not None and _stable_int("noop_agent_sees", str(seed), str(i), modulus=2) == 0:
            witnesses.add(agent)
        inserts.append(
            (slot, i, NoOp(actor=actor, obj=obj, loc=loc, kind=kind, time=-1,
                           witnesses=frozenset(witnesses), other=other))
        )
    inserts.sort(key=lambda t: (t[0], t[1]))

    # Merge: old event k goes after every insert with slot <= k.
    merged: list = []  # (old_index | None, event)
    ins_ptr = 0
    for k in range(n_old + 1):
        while ins_ptr < len(inserts) and inserts[ins_ptr][0] == k:
            merged.append((None, inserts[ins_ptr][2]))
            ins_ptr += 1
        if k < n_old:
            merged.append((k, world.events[k]))
    new_index_of_old: dict[int, int] = {}
    old_time_to_new: dict[int, int] = {}
    new_events = []
    for new_idx, (old_idx, ev) in enumerate(merged):
        if old_idx is not None:
            new_index_of_old[old_idx] = new_idx
            old_time_to_new[ev.time] = new_idx
        new_events.append(replace(ev, time=new_idx))

    # Narration: each NoOp is narrated right after the narration position
    # of its chronological predecessor (or first, if it has none), so it
    # sits where an unscrambled story would put it.
    narration = [new_index_of_old[i] for i in world.narration_order]
    for new_idx, (old_idx, ev) in enumerate(merged):
        if old_idx is not None:
            continue
        pred = new_idx - 1
        if pred < 0:
            narration.insert(0, new_idx)
        else:
            narration.insert(narration.index(pred) + 1, new_idx)

    query = world.query
    if query.pair is not None:
        query = replace(query, pair=(new_index_of_old[query.pair[0]], new_index_of_old[query.pair[1]]))
    new_world = replace(
        world,
        events=new_events,
        narration_order=narration,
        query=query,
        relative_times=frozenset(old_time_to_new[t] for t in world.relative_times if t in old_time_to_new),
    )
    assert not validate(new_world)
    assert not _validate_noops(new_world)
    return new_world


# --- structural stressor 2: relational time (Premise Order Matters) ---------


def _cue_edges(world: World, relative_times: frozenset[int]) -> dict[int, set[int]]:
    """Precedence graph (u -> v means "u is entailed to precede v") over
    event indices, from what the RENDERED text states: clock-anchored
    events are totally ordered by their stated times; each cued event is
    ordered against its narration predecessor (the cue's referent)."""
    succ: dict[int, set[int]] = {i: set() for i in range(len(world.events))}
    anchored = sorted(
        (i for i in range(len(world.events)) if world.events[i].time not in relative_times),
        key=lambda i: world.events[i].time,
    )
    for a, b in zip(anchored, anchored[1:]):
        succ[a].add(b)
    for p, idx in enumerate(world.narration_order):
        if p == 0 or world.events[idx].time not in relative_times:
            continue
        ref = world.narration_order[p - 1]
        if world.events[ref].time < world.events[idx].time:
            succ[ref].add(idx)
        else:
            succ[idx].add(ref)
    return succ


def _reaches(succ: dict[int, set[int]], a: int, b: int) -> bool:
    stack, seen = [a], {a}
    while stack:
        u = stack.pop()
        if u == b:
            return True
        for v in succ[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return False


def critical_pairs(world: World) -> list[tuple[int, int]]:
    """(earlier, later) event-index pairs whose chronological order the
    reader MUST be able to recover for the query to be well-posed."""
    q = world.query
    ev = world.events
    if q.kind == "order":
        assert q.pair is not None
        i, j = q.pair
        return [(i, j) if ev[i].time < ev[j].time else (j, i)]
    if q.kind == "state":
        writers = [i for i, e in enumerate(ev) if isinstance(e, Move) and e.obj == q.subject]
    else:  # belief: every event that writes beliefs[(agent, subject)]
        writers = [
            i for i, e in enumerate(ev)
            if (isinstance(e, Move) and e.obj == q.subject and q.agent in e.witnesses)
            or (isinstance(e, Transfer) and e.obj == q.subject and q.agent in e.witnesses)
            or (isinstance(e, Tell) and e.obj == q.subject and e.listener == q.agent)
        ]
    if len(writers) < 2:
        return []
    writers.sort(key=lambda i: ev[i].time)
    last = writers[-1]
    return [(w, last) for w in writers[:-1]]


def relational_order_entailed(world: World, relative_times: frozenset[int] | None = None) -> bool:
    rt = world.relative_times if relative_times is None else relative_times
    succ = _cue_edges(world, rt)
    return all(_reaches(succ, a, b) for a, b in critical_pairs(world))


def add_relational_time(
    world: World, fraction: float, seed: int, invert_order_pair: bool = False
) -> World:
    """Drop the clock anchor from ~`fraction` of narrated events, replacing
    it (at render time) with a relative cue against the previously narrated
    event that is TRUE under the real chronology. Answer-preserving.

    - The first narrated event always keeps its clock time (a cue needs a
      referent).
    - For order queries the two compared events are cued first; with
      `invert_order_pair` the chronologically-earlier one is re-narrated
      immediately AFTER the later one ("... Before that, ..."), so narration
      order contradicts time order and the cue is the only evidence.
    - Well-posedness repair: while the query's critical pairs are not
      entailed by the stated anchors + cues, cued events are restored to
      clock anchors (answer-critical events first, deterministic order), so
      the realized fraction can be below `fraction` (see `measure`).
    """
    fraction = max(0.0, min(1.0, float(fraction)))
    n = len(world.events)
    if fraction <= 0.0 or n < 2:
        return replace(world, relative_times=frozenset())
    narration = list(world.narration_order)
    query = world.query
    priority: list[int] = []
    if query.kind == "order" and query.pair is not None:
        i, j = query.pair
        early, late = (i, j) if world.events[i].time < world.events[j].time else (j, i)
        if invert_order_pair:
            narration.remove(early)
            narration.insert(narration.index(late) + 1, early)
        priority = [early, late]
    world = replace(world, narration_order=narration)

    first = narration[0]
    target = max(1, int(round(fraction * n)))
    ranked = sorted(
        (i for i in range(n) if i not in priority),
        key=lambda i: _stable_int("reltime_rank", str(seed), str(i), modulus=1 << 30),
    )
    chosen = [i for i in priority + ranked if i != first][:max(target, len([p for p in priority if p != first]))]
    cued = {world.events[i].time for i in chosen}

    # Repair until every critical pair is entailed. Restore the critical
    # events (answer-determining writers) first -- once they are all
    # anchored their order is stated outright -- then anything else.
    crit_members = {x for pair in critical_pairs(world) for x in pair}
    restore_order = [i for i in chosen if i in crit_members] + [
        i for i in reversed(chosen) if i not in crit_members
    ]
    for i in restore_order:
        if relational_order_entailed(world, frozenset(cued)):
            break
        cued.discard(world.events[i].time)
    new_world = replace(world, relative_times=frozenset(cued))
    assert relational_order_entailed(new_world)
    assert not validate(new_world)
    return new_world


ANSWER_PRESERVING = {
    "add_distractors",
    "add_filler",
    "scramble_narration",
    "add_coref_pressure",
    "add_noop_distractors",
    "add_relational_time",
}
ANSWER_CHANGING = {
    "extend_chain",
}
