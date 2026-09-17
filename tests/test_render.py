"""Acceptance tests for Phase 2 rendering (cartographer.render) and the
Task A distractor-continuity fix in cartographer.world.generate/mutate.
"""

from __future__ import annotations

import re

from cartographer.render import NameMap, RenderedTask, build_name_map, render, verify
from cartographer.store.schema import Verdict
from cartographer.world import DimParams, Entity, Location, Move, Obj, Query, World, generate
from cartographer.world.simulate import answer as sim_answer
from cartographer.world.simulate import simulate


def _base_params(**overrides) -> DimParams:
    fields = dict(
        chain_length=3,
        scramble=0.3,
        coref_pressure=1,
        distractor_events=2,
        distractor_similarity="unrelated",
        filler_events=2,
        query_kind="state",
    )
    fields.update(overrides)
    return DimParams(**fields)


# --- Task A: distractor continuity -----------------------------------------


def test_distractor_moves_are_continuous() -> None:
    tiers = ["unrelated", "same_entities", "same_category", "near_duplicate"]
    for seed in range(20):
        tier = tiers[seed % len(tiers)]
        params = _base_params(
            chain_length=1 + (seed % 4),
            distractor_events=5 + (seed % 4),
            distractor_similarity=tier,
        )
        world = generate(params, seed=seed)

        moves_by_obj: dict[str, list[Move]] = {}
        for event in world.events:
            if isinstance(event, Move):
                moves_by_obj.setdefault(event.obj, []).append(event)

        for obj_id, moves in moves_by_obj.items():
            moves_sorted = sorted(moves, key=lambda m: m.time)
            cur_loc = moves_sorted[0].src
            for move in moves_sorted:
                assert move.src == cur_loc, (
                    f"seed={seed} tier={tier} obj={obj_id}: expected src "
                    f"{cur_loc!r}, got {move.src!r} (teleport)"
                )
                cur_loc = move.dst


# --- Render determinism -----------------------------------------------------


def test_render_determinism_same_seed() -> None:
    params = _base_params()
    world = generate(params, seed=5)
    name_map = build_name_map(world, seed=5)
    r1 = render(world, name_map, seed=5)
    r2 = render(world, name_map, seed=5)
    assert r1.prompt == r2.prompt
    assert r1.answer_key == r2.answer_key


def test_render_varies_with_seed_but_answer_key_stable() -> None:
    params = _base_params(chain_length=4, distractor_events=3, filler_events=3)
    world = generate(params, seed=9)
    name_map = build_name_map(world, seed=9)
    prompts = set()
    answer_keys = set()
    for render_seed in range(5):
        rendered = render(world, name_map, seed=render_seed)
        prompts.add(rendered.prompt)
        answer_keys.add(rendered.answer_key)
    assert len(prompts) >= 2
    assert len(answer_keys) == 1


# --- Name uniqueness ---------------------------------------------------------


def test_name_uniqueness_within_world() -> None:
    tiers = ["unrelated", "same_entities", "same_category", "near_duplicate"]
    kinds = ["state", "belief", "order"]
    for seed in range(30):
        params = _base_params(
            chain_length=1 + (seed % 6),
            coref_pressure=seed % 4,
            distractor_events=seed % 8,
            distractor_similarity=tiers[seed % len(tiers)],
            filler_events=seed % 5,
            query_kind=kinds[seed % len(kinds)],
        )
        world = generate(params, seed=seed)
        name_map = build_name_map(world, seed=seed)

        all_names = (
            list(name_map.entity.values())
            + list(name_map.obj.values())
            + list(name_map.location.values())
        )
        assert len(all_names) == len(set(all_names)), f"seed={seed}: duplicate names {all_names}"


# --- Time anchors -------------------------------------------------------------


_ANCHOR_RE = re.compile(r"At (\d+):(\d\d),")


def test_time_anchors_consistent_with_latent_chronology() -> None:
    params = _base_params(
        chain_length=5, scramble=0.8, distractor_events=4, filler_events=3, coref_pressure=2
    )
    world = generate(params, seed=11)
    name_map = build_name_map(world, seed=11)
    rendered = render(world, name_map, seed=11)

    anchors_found = _ANCHOR_RE.findall(rendered.prompt)
    # No witness sentences are expected in a "state" query world (default
    # kind for _base_params) since witnesses beyond the actor are only
    # added for belief queries — one anchor per event should still hold.
    assert rendered.meta["witness_sentences"] == 0
    assert len(anchors_found) == len(world.events)

    # narration order differs from chronological time order (scramble=0.8 on
    # enough events should guarantee this)
    times_in_narration = [world.events[i].time for i in world.narration_order]
    assert times_in_narration != sorted(times_in_narration)

    # each anchor's stated minute matches the corresponding event's latent time
    for idx, narrated_event_idx in enumerate(world.narration_order):
        hour, minute = anchors_found[idx]
        total_minutes = int(hour) * 60 + int(minute)
        expected_total_minutes = 8 * 60 + world.events[narrated_event_idx].time
        assert total_minutes == expected_total_minutes


def test_time_anchors_consistent_with_witness_sentences() -> None:
    """Same invariant as above, but for belief-query worlds where witness
    sentences are interleaved: every anchor (including witness-sentence
    anchors) must match the latent time of the event it narrates, using
    `sentence_event_idx`/`sentence_is_witness` meta to align sentences to
    events (there is no longer a strict 1:1 sentence-to-event correspondence
    once witness sentences exist)."""
    found_one_with_witnesses = False
    for seed in range(20):
        params = _base_params(
            chain_length=3, scramble=0.8, distractor_events=2, filler_events=2,
            coref_pressure=1, query_kind="belief",
        )
        world = generate(params, seed=seed)
        name_map = build_name_map(world, seed=seed)
        rendered = render(world, name_map, seed=seed)

        if not rendered.meta["witness_sentences"]:
            continue
        found_one_with_witnesses = True

        anchors_found = _ANCHOR_RE.findall(rendered.prompt)
        sentence_event_idx = rendered.meta["sentence_event_idx"]
        assert len(anchors_found) == len(sentence_event_idx)

        for (hour, minute), event_idx in zip(anchors_found, sentence_event_idx):
            total_minutes = int(hour) * 60 + int(minute)
            expected_total_minutes = 8 * 60 + world.events[event_idx].time
            assert total_minutes == expected_total_minutes

        # witness sentences never precede their own event's main sentence
        seen_main_for: set[int] = set()
        for is_witness, event_idx in zip(rendered.meta["sentence_is_witness"], sentence_event_idx):
            if not is_witness:
                seen_main_for.add(event_idx)
            else:
                assert event_idx in seen_main_for

    assert found_one_with_witnesses, "no seed in range produced witness sentences"


# --- Pronoun safety -----------------------------------------------------------


def _resolve_pronouns_independently(rendered: RenderedTask, world) -> list[dict]:
    """Re-implements the SAME rule as templates._apply_pronouns, but purely
    from meta (narration_order) + world, to cross-check against intended
    referents recorded in meta by the renderer.
    """
    # We trust the renderer's own bookkeeping of intended referents (meta)
    # and merely assert internal consistency: every recorded referent must
    # have been unique-most-recent-within-2 at the time it was substituted.
    gender_by_entity = {e.id: e.gender for e in world.entities}
    last_named_idx: dict[str, int] = {}
    seen: set[str] = set()

    participants_per_sentence = []
    for idx in world.narration_order:
        event = world.events[idx]
        if hasattr(event, "actor") and not hasattr(event, "giver") and not hasattr(event, "speaker"):
            participants_per_sentence.append([event.actor])
        elif hasattr(event, "giver"):
            participants_per_sentence.append([event.giver, event.receiver])
        elif hasattr(event, "speaker"):
            participants_per_sentence.append([event.speaker, event.listener])
        else:
            participants_per_sentence.append([])

    resolved = []
    referent_by_sentence = {r["sentence_index"]: r for r in rendered.meta["pronoun_referents"]}

    for i, participants in enumerate(participants_per_sentence):
        for entity_id in participants:
            gender = gender_by_entity[entity_id]
            eligible = False
            if entity_id in seen:
                same_gender_recent = [
                    (eid, idx) for eid, idx in last_named_idx.items()
                    if gender_by_entity.get(eid) == gender
                ]
                if same_gender_recent:
                    most_recent_eid, most_recent_idx = max(same_gender_recent, key=lambda t: t[1])
                    if most_recent_eid == entity_id and (i - most_recent_idx) <= 2:
                        tied = [
                            eid for eid, idx in same_gender_recent
                            if idx == most_recent_idx and eid != entity_id
                        ]
                        if not tied:
                            eligible = True
            if eligible and i in referent_by_sentence:
                ref = referent_by_sentence[i]
                assert ref["entity_id"] == entity_id, (
                    f"sentence {i}: renderer intended {ref['entity_id']} but "
                    f"independent resolver picked {entity_id}"
                )
                resolved.append(ref)
            seen.add(entity_id)
            last_named_idx[entity_id] = i
    return resolved


def test_pronoun_safety() -> None:
    for seed in range(30):
        params = _base_params(
            chain_length=1 + (seed % 5),
            coref_pressure=3 + (seed % 3),
            distractor_events=seed % 4,
            filler_events=seed % 4,
        )
        world = generate(params, seed=seed)
        name_map = build_name_map(world, seed=seed)
        rendered = render(world, name_map, seed=seed)

        resolved = _resolve_pronouns_independently(rendered, world)
        # every intended referent recorded by the renderer must have been
        # reproduced by the independent resolver
        assert len(resolved) == len(rendered.meta["pronoun_referents"])


# --- Pronoun pressure ----------------------------------------------------------


def test_pronoun_pressure_increases_with_coref_pressure() -> None:
    low_counts = []
    high_counts = []
    for seed in range(10):
        params_low = _base_params(coref_pressure=0, chain_length=3, scramble=0.0)
        params_high = _base_params(coref_pressure=4, chain_length=3, scramble=0.0)
        w_low = generate(params_low, seed=seed)
        w_high = generate(params_high, seed=seed)
        nm_low = build_name_map(w_low, seed=seed)
        nm_high = build_name_map(w_high, seed=seed)
        r_low = render(w_low, nm_low, seed=seed)
        r_high = render(w_high, nm_high, seed=seed)
        low_counts.append(r_low.meta["pronoun_collisions"])
        high_counts.append(r_high.meta["pronoun_collisions"])

    avg_low = sum(low_counts) / len(low_counts)
    avg_high = sum(high_counts) / len(high_counts)
    assert avg_low < avg_high


# --- Surface completeness (witness-bug regression) -----------------------------
#
# Every fact a belief-query answer depends on must be textually present in the
# rendered story. Concretely: for every (entity_id, obj_id) key that
# world/simulate.py::simulate's beliefs dict assigns, the LAST event (in
# world.events order) that wrote that key must have rendered a sentence in
# which that entity is named (its surface name or a pronoun substituted for
# it) -- either because the entity is one of the event's "actor-equivalent"
# participants (already named in the main sentence) or because it is a
# non-actor witness (named in the appended witness sentence). This is the
# regression test for the 2026-09-17 witness-rendering bug.

from cartographer.render.templates import _event_actor_ids
from cartographer.world.model import Move, Tell, Transfer


def _last_belief_setters(world) -> dict[tuple[str, str], int]:
    """Mirror world/simulate.py::simulate's belief bookkeeping, but record
    the index (into world.events) of the LAST event that wrote each
    (entity_id, obj_id) belief key, since a later event silently overwrites
    an earlier one for the same key."""
    setters: dict[tuple[str, str], int] = {}
    final_locations: dict[str, str] = {}

    for event_idx, event in enumerate(world.events):
        if isinstance(event, Move):
            final_locations[event.obj] = event.dst
            for witness in event.witnesses:
                setters[(witness, event.obj)] = event_idx
        elif isinstance(event, Transfer):
            if event.obj in final_locations:
                for witness in event.witnesses:
                    setters[(witness, event.obj)] = event_idx
        elif isinstance(event, Tell):
            setters[(event.listener, event.obj)] = event_idx

    return setters


def _entity_named_in_event_sentences(
    rendered: RenderedTask, name_map: NameMap, world, entity_id: str, event_idx: int
) -> bool:
    """True iff `entity_id` is a rendered participant (main sentence or
    witness sentence) of the event at `event_idx`, per the renderer's own
    sentence/event/participant bookkeeping in meta -- structurally, not by
    fragile substring search (pronoun substitution can replace the literal
    name)."""
    event = world.events[event_idx]
    if entity_id in _event_actor_ids(event):
        return True  # named in the main sentence (or implied, per spec)
    non_actor_witnesses = event.witnesses - _event_actor_ids(event)
    return entity_id in non_actor_witnesses


def _check_surface_completeness(world, name_map, rendered, label: str) -> set[int]:
    """Assert every belief the simulator assigns is backed by a rendered
    mention of the believing entity, tied to the event that (last) set that
    belief. Returns the set of non-actor-witness-counts observed (0/1/2+)
    across the belief-setting events in this world, for coverage bookkeeping."""
    result = simulate(world)
    setters = _last_belief_setters(world)
    assert set(setters) == set(result.beliefs), (
        f"{label}: belief-setter reconstruction disagrees with simulate()"
    )

    witness_counts_seen: set[int] = set()
    for (entity_id, obj_id), event_idx in setters.items():
        assert _entity_named_in_event_sentences(rendered, name_map, world, entity_id, event_idx), (
            f"{label}: entity {entity_id!r}'s belief about {obj_id!r} was set by "
            f"event {event_idx} but that entity is never rendered as present "
            "for that event (the witness-rendering bug)"
        )
        event = world.events[event_idx]
        n = len(event.witnesses - _event_actor_ids(event))
        witness_counts_seen.add(min(n, 2))  # bucket 2+ together

    return witness_counts_seen


def _multi_witness_world(seed: int) -> World:
    """A hand-built world (same idiom as test_world.py's _false_belief_world)
    whose belief-setting Move has TWO non-actor witnesses -- the generator
    never produces this on its own (it adds at most one extra witness per
    belief chain), so this covers the "multiple witnesses" case explicitly."""
    entities = [
        Entity(id="elena", name="Elena", gender="f"),
        Entity(id="marcus", name="Marcus", gender="m"),
        Entity(id="priya", name="Priya", gender="f"),
        Entity(id="david", name="David", gender="m"),
    ]
    locations = [
        Location(id="room_a", name="room_a"),
        Location(id="room_b", name="room_b"),
    ]
    objects = [Obj(id="blue_box", name="blue_box", category="container")]
    move = Move(
        obj="blue_box", src="room_a", dst="room_b", actor="elena", time=0,
        witnesses=frozenset({"elena", "marcus", "priya"}),
    )
    query = Query(kind="belief", subject="blue_box", agent="marcus")
    return World(
        entities=entities, objects=objects, locations=locations,
        events=[move], narration_order=[0], query=query,
    )


def test_surface_completeness_witnesses_are_rendered() -> None:
    """Regression test for the witness-rendering bug: every belief the
    simulator assigns must be backed by a rendered mention of the believing
    entity. Spans belief queries with 0, 1, and multiple non-actor
    witnesses, and confirms the "unknown" (no belief) path stays reachable."""
    witness_counts_seen: set[int] = set()
    seen_non_believer = False  # "unknown" path must stay reachable somewhere

    for seed in range(40):
        params = _base_params(
            chain_length=1 + (seed % 5),
            scramble=0.4,
            coref_pressure=seed % 3,
            distractor_events=seed % 4,
            filler_events=seed % 4,
            query_kind="belief",
        )
        world = generate(params, seed=seed)
        name_map = build_name_map(world, seed=seed)
        rendered = render(world, name_map, seed=seed)

        witness_counts_seen |= _check_surface_completeness(world, name_map, rendered, label=f"seed={seed}")

        result = simulate(world)
        all_entities = {e.id for e in world.entities}
        believers = {eid for (eid, obj_id) in result.beliefs if obj_id == world.query.subject}
        if believers != all_entities:
            seen_non_believer = True

    # Explicitly cover the multi-witness case (the generator alone never
    # produces 2+ non-actor witnesses on a belief-setting event).
    for seed in range(3):
        world = _multi_witness_world(seed)
        name_map = build_name_map(world, seed=seed)
        rendered = render(world, name_map, seed=seed)
        witness_counts_seen |= _check_surface_completeness(world, name_map, rendered, label=f"multi-witness seed={seed}")

    assert 0 in witness_counts_seen, "no world produced a belief set by an event with zero non-actor witnesses"
    assert 1 in witness_counts_seen, "no world produced a belief set by an event with exactly one non-actor witness"
    assert 2 in witness_counts_seen, "no world produced a belief set by an event with multiple non-actor witnesses"
    assert seen_non_believer, "no seed left any entity without a belief -- 'unknown' path looks unreachable"


# --- Verifier ------------------------------------------------------------------


def _fake_rendered(answer_key: str, kind: str, location_names: list[str]) -> tuple[RenderedTask, NameMap]:
    rendered = RenderedTask(
        prompt="irrelevant",
        answer_key=answer_key,
        meta={"query_kind": kind},
    )
    name_map = NameMap(
        entity={}, obj={}, location={f"loc_{i}": n for i, n in enumerate(location_names)}
    )
    return rendered, name_map


def test_verifier_table() -> None:
    rendered, name_map = _fake_rendered("the storage room", "state", ["the storage room", "the kitchen"])

    assert verify("The storage room", rendered, name_map) == Verdict.PASS
    assert verify("the Storage Room.", rendered, name_map) == Verdict.PASS
    assert verify("It's in the kitchen.", rendered, name_map) == Verdict.FAIL
    assert verify("It's in the storage room and the kitchen.", rendered, name_map) == Verdict.FAIL

    rendered_unk, name_map_unk = _fake_rendered("unknown", "belief", ["the storage room", "the kitchen"])
    assert verify("unknown", rendered_unk, name_map_unk) == Verdict.PASS
    assert verify("They have no belief about it.", rendered_unk, name_map_unk) == Verdict.PASS
    assert verify("the storage room", rendered_unk, name_map_unk) == Verdict.FAIL

    rendered_order, _ = _fake_rendered("first", "order", [])
    assert verify("first", rendered_order, name_map) == Verdict.PASS
    assert verify("second", rendered_order, name_map) == Verdict.FAIL
    rendered_order2, _ = _fake_rendered("second", "order", [])
    assert verify("second", rendered_order2, name_map) == Verdict.PASS
    assert verify("first", rendered_order2, name_map) == Verdict.FAIL

    assert verify("", rendered, name_map) == Verdict.ERROR
    assert verify("   ", rendered, name_map) == Verdict.ERROR


# --- End-to-end oracle ----------------------------------------------------------


def test_end_to_end_oracle() -> None:
    kinds = ["state", "belief", "order"]
    for seed in range(20):
        params = _base_params(
            chain_length=1 + (seed % 5),
            coref_pressure=seed % 3,
            distractor_events=seed % 5,
            filler_events=seed % 4,
            query_kind=kinds[seed % 3],
        )
        world = generate(params, seed=seed)
        name_map = build_name_map(world, seed=seed)
        rendered = render(world, name_map, seed=seed)

        assert verify(rendered.answer_key, rendered, name_map) == Verdict.PASS

        if world.query.kind in ("state", "belief") and rendered.answer_key != "unknown":
            # pick a location name that is definitely wrong
            wrong = None
            for loc_id, name in name_map.location.items():
                if name != rendered.answer_key:
                    wrong = name
                    break
            if wrong is not None:
                assert verify(wrong, rendered, name_map) == Verdict.FAIL
