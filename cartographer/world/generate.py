"""Deterministic world generation from `DimParams` + seed.

All choices are derived from sha256 streams over stable string parts (same
idiom as `cartographer/synthetic/rules.py`'s `_stable_float`), never the
`random` module, so `generate(params, seed)` is a pure function.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .model import Entity, Filler, Location, Move, Obj, Query, Tell, Transfer, World
from .simulate import validate

_FIRST_NAMES_F = ["Elena", "Marcus", "Priya", "Sofia", "Noor", "Ingrid", "Tamsin", "Wren"]
_FIRST_NAMES_M = ["Marcus", "David", "Jamal", "Oscar", "Felix", "Hugo", "Leon", "Rafi"]
_LOCATION_NAMES = ["room_a", "room_b", "room_c", "room_d", "room_e", "room_f", "room_g", "room_h"]
_OBJECT_CATEGORIES = ["container", "document", "tool", "food", "device"]
_OBJECT_NAMES = ["box", "folder", "wrench", "basket", "tablet", "case", "jar", "bag"]


def _stable_int(*parts: str, modulus: int) -> int:
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    as_int = int(digest[:15], 16)
    return as_int % modulus


def _stable_float(*parts: str) -> float:
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    as_int = int(digest[:15], 16)
    return as_int / float(16**15)


@dataclass(frozen=True)
class DimParams:
    chain_length: int
    scramble: float
    coref_pressure: int
    distractor_events: int
    distractor_similarity: str  # "unrelated" | "same_entities" | "same_category" | "near_duplicate"
    filler_events: int
    query_kind: str  # "state" | "belief" | "order"
    expect_unknown: bool = False
    witness_cutoff: float = 0.0
    """Fraction (0..1) of the causal chain after which the belief query
    agent's witnessing of the chain STOPS while the object keeps moving.

    At 0.0: current behavior (agent witnesses every chain event except the
    final one, per the existing belief-chain logic). At higher values, the
    agent only witnesses the first `ceil(witness_cutoff-complement)`... see
    `_witness_cutoff_index` for the exact split. The agent keeps appearing
    in the story afterward via ordinary filler sentences (so their absence
    from later Move witness clauses reads as "present but not looking," not
    "vanished") -- this makes the correct belief answer an EARLY location,
    maximally divergent from the object's true final location, directly
    probing the continuity-of-observation failure mode documented in
    docs/lab-log.md (Nemotron Nano finding). Only has any effect for
    belief queries; ignored otherwise.
    """


def _make_entities(n: int, seed: int) -> list[Entity]:
    entities = []
    for i in range(n):
        gender = "f" if _stable_int("gender", str(seed), str(i), modulus=2) == 0 else "m"
        pool = _FIRST_NAMES_F if gender == "f" else _FIRST_NAMES_M
        name = pool[_stable_int("ename", str(seed), str(i), modulus=len(pool))]
        entities.append(Entity(id=f"ent_{i}", name=f"{name}_{i}", gender=gender))
    return entities


def _make_locations(n: int) -> list[Location]:
    return [
        Location(id=f"loc_{i}", name=_LOCATION_NAMES[i % len(_LOCATION_NAMES)])
        for i in range(n)
    ]


def _make_objects(n: int, seed: int) -> list[Obj]:
    objects = []
    for i in range(n):
        cat = _OBJECT_CATEGORIES[
            _stable_int("ocat", str(seed), str(i), modulus=len(_OBJECT_CATEGORIES))
        ]
        name = _OBJECT_NAMES[_stable_int("oname", str(seed), str(i), modulus=len(_OBJECT_NAMES))]
        objects.append(Obj(id=f"obj_{i}", name=f"{name}_{i}", category=cat))
    return objects


def _witness_cutoff_last_index(chain_length: int, witness_cutoff: float) -> int:
    """Last chain-event index (0-based) the belief query agent witnesses.

    At witness_cutoff==0.0 this must reproduce the pre-existing behavior:
    the agent witnesses every chain event except the very last one (i.e.
    last witnessed index = chain_length - 2, or chain_length - 1 if
    chain_length == 1 since there's nothing else to witness).

    As witness_cutoff -> 1.0, the agent's witnessing collapses toward only
    the first event (index 0), so the object accumulates the maximum
    number of further unwitnessed moves -- the correct belief answer then
    sits maximally early/divergent from the true final location.
    """
    if chain_length <= 1:
        return 0
    default_last = chain_length - 2  # cutoff==0.0 baseline
    # Linearly interpolate the "last witnessed index" down from default_last
    # (at cutoff=0.0) to 0 (at cutoff=1.0), always witnessing at least the
    # first event.
    last = default_last - witness_cutoff * default_last
    return max(0, min(default_last, round(last)))


def generate(params: DimParams, seed: int) -> World:
    n_entities = max(3, params.coref_pressure + 3)
    n_locations = max(4, params.chain_length + 2)
    n_objects = max(2, 1 + (params.distractor_events > 0))

    entities = _make_entities(n_entities, seed)
    locations = _make_locations(n_locations)
    objects = _make_objects(n_objects, seed)

    subject = objects[0]
    entity_ids = [e.id for e in entities]
    location_ids = [l.id for l in locations]

    events: list = []
    time_counter = [0]

    def next_time() -> int:
        t = time_counter[0]
        time_counter[0] += 1
        return t

    def pick_entity(*parts: str, exclude: set[str] | None = None) -> str:
        exclude = exclude or set()
        candidates = [e for e in entity_ids if e not in exclude] or entity_ids
        idx = _stable_int(*parts, str(seed), modulus=len(candidates))
        return candidates[idx]

    def pick_location(*parts: str, exclude: set[str] | None = None) -> str:
        exclude = exclude or set()
        candidates = [l for l in location_ids if l not in exclude] or location_ids
        idx = _stable_int(*parts, str(seed), modulus=len(candidates))
        return candidates[idx]

    query_agent = pick_entity("query_agent") if params.query_kind == "belief" else None

    last_witnessed_chain_idx = (
        _witness_cutoff_last_index(params.chain_length, params.witness_cutoff)
        if params.query_kind == "belief"
        else -1
    )

    chain_events: list[int] = []
    cur_loc = pick_location("start_loc")
    for k in range(params.chain_length):
        dst = pick_location("chain_dst", str(k), exclude={cur_loc})

        past_cutoff = (
            params.query_kind == "belief"
            and query_agent is not None
            and not params.expect_unknown
            and k > last_witnessed_chain_idx
        )
        actor_exclude = {query_agent} if past_cutoff and query_agent is not None else None
        actor = pick_entity("chain_actor", str(k), exclude=actor_exclude)

        witness_set = {actor}
        if params.query_kind == "belief" and query_agent is not None and not params.expect_unknown:
            is_last = k == params.chain_length - 1
            if not is_last and k <= last_witnessed_chain_idx:
                witness_set.add(query_agent)

        move = Move(
            obj=subject.id,
            src=cur_loc,
            dst=dst,
            actor=actor,
            time=next_time(),
            witnesses=frozenset(witness_set),
        )
        events.append(move)
        chain_events.append(move.time)
        cur_loc = dst

    if (
        params.query_kind == "belief"
        and query_agent is not None
        and not params.expect_unknown
        and params.chain_length >= 1
    ):
        already_witnesses_chain = any(
            query_agent in events[i].witnesses for i in range(len(events))
        )
        if not already_witnesses_chain:
            speaker = pick_entity("tell_speaker")
            claimed = pick_location("tell_claim")
            tell = Tell(
                speaker=speaker,
                listener=query_agent,
                obj=subject.id,
                claimed_location=claimed,
                time=next_time(),
                witnesses=frozenset({query_agent}),
            )
            events.append(tell)

    def similarity_tier_obj(tier: str, idx: int) -> str:
        if tier == "same_entities":
            return subject.id
        elif tier == "same_category" and n_objects > 1:
            same_cat = [o for o in objects if o.category == subject.category and o.id != subject.id]
            return same_cat[0].id if same_cat else objects[-1].id
        elif tier == "near_duplicate" and n_objects > 1:
            return objects[-1].id
        elif tier == "unrelated" and n_objects > 1:
            return objects[-1].id
        else:
            return objects[-1].id if n_objects > 1 else subject.id

    # Track each distractor object's current location so its Move chain is
    # continuous (each Move.src == that object's location after its prior
    # Move), independent of the causal-chain subject's own location.
    distractor_cur_loc: dict[str, str] = {}
    for i in range(params.distractor_events):
        obj_id = similarity_tier_obj(params.distractor_similarity, i)
        actor = pick_entity("distractor_actor", str(i))
        if params.distractor_similarity == "same_entities":
            actor = pick_entity("chain_actor", str(i % max(params.chain_length, 1)))
        if obj_id == subject.id:
            src = cur_loc
        elif obj_id in distractor_cur_loc:
            src = distractor_cur_loc[obj_id]
        else:
            src = pick_location("distractor_start", obj_id, params.distractor_similarity)
        dst = pick_location("distractor_dst", str(i), params.distractor_similarity, exclude={src})
        events.append(
            Move(
                obj=obj_id,
                src=src,
                dst=dst,
                actor=actor,
                time=next_time(),
                witnesses=frozenset({actor}),
            )
        )
        if obj_id == subject.id:
            cur_loc = dst
        else:
            distractor_cur_loc[obj_id] = dst

    for i in range(params.filler_events):
        actor = pick_entity("filler_actor", str(i))
        seed_val = _stable_int("filler_seed", str(i), modulus=1_000_000)
        events.append(
            Filler(
                actor=actor,
                description_seed=seed_val,
                time=next_time(),
                witnesses=frozenset({actor}),
            )
        )

    # Witness-cutoff filler: guarantee the belief query agent keeps
    # appearing in the story (via ordinary Filler mechanics) after their
    # witnessing of the causal chain has stopped, so their disappearance
    # doesn't just read as "left the scene" -- they're still around, doing
    # unrelated things, just not looking at the object anymore. Only
    # emitted when the cutoff actually truncated witnessing (i.e. there is
    # at least one further, unwitnessed chain move after the agent's last
    # witnessed one).
    if (
        params.query_kind == "belief"
        and query_agent is not None
        and not params.expect_unknown
        and 0 <= last_witnessed_chain_idx < params.chain_length - 1
    ):
        n_cutoff_filler = min(3, params.chain_length - 1 - last_witnessed_chain_idx)
        for i in range(n_cutoff_filler):
            seed_val = _stable_int("cutoff_filler_seed", str(i), modulus=1_000_000)
            events.append(
                Filler(
                    actor=query_agent,
                    description_seed=seed_val,
                    time=next_time(),
                    witnesses=frozenset({query_agent}),
                )
            )

    if params.coref_pressure > 0 and events:
        same_gender_pool = [e for e in entities if e.gender == entities[0].gender]
        for i in range(params.coref_pressure):
            actor = same_gender_pool[i % len(same_gender_pool)].id if same_gender_pool else pick_entity(
                "coref_actor", str(i)
            )
            seed_val = _stable_int("coref_filler_seed", str(i), modulus=1_000_000)
            events.append(
                Filler(
                    actor=actor,
                    description_seed=seed_val,
                    time=next_time(),
                    witnesses=frozenset({actor}),
                )
            )

    chronological_order = list(range(len(events)))
    narration_order = _scramble_order(chronological_order, params.scramble, seed)

    if params.query_kind == "state":
        query = Query(kind="state", subject=subject.id)
    elif params.query_kind == "belief":
        query = Query(
            kind="belief",
            subject=subject.id,
            agent=query_agent,
            expect_unknown=params.expect_unknown,
        )
    elif params.query_kind == "order":
        if len(events) >= 2:
            pair = (0, len(events) - 1)
            if events[pair[0]].time == events[pair[1]].time:
                pair = (0, 1) if len(events) > 1 else (0, 0)
        else:
            pair = (0, 0)
        query = Query(kind="order", subject=subject.id, pair=pair)
    else:
        raise ValueError(f"unknown query_kind: {params.query_kind!r}")

    world = World(
        entities=entities,
        objects=objects,
        locations=locations,
        events=events,
        narration_order=narration_order,
        query=query,
    )

    assert not validate(world)
    return world


def _scramble_order(order: list[int], scramble: float, seed: int) -> list[int]:
    n = len(order)
    if n < 2:
        return list(order)
    max_inversions = n * (n - 1) // 2
    n_transpositions = int(round(scramble * max_inversions))
    offset = _stable_int("scramble_offset", str(seed), modulus=n - 1)
    result = list(order)
    for k in range(n_transpositions):
        pos = (offset + k) % (n - 1)
        result[pos], result[pos + 1] = result[pos + 1], result[pos]
    return result
