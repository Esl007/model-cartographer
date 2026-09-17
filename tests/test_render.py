"""Acceptance tests for Phase 2 rendering (cartographer.render) and the
Task A distractor-continuity fix in cartographer.world.generate/mutate.
"""

from __future__ import annotations

import re

from cartographer.render import NameMap, RenderedTask, build_name_map, render, verify
from cartographer.store.schema import Verdict
from cartographer.world import DimParams, Move, generate
from cartographer.world.simulate import answer as sim_answer


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

    story = rendered.prompt.split("\n\n")[0]
    sentences = story.split(". ")
    anchors_found = _ANCHOR_RE.findall(rendered.prompt)
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
