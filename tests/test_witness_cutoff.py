"""Acceptance tests for the witness_cutoff hardening (Task 1 of the
2026-09-17 generator-hardening plan in docs/lab-log.md).

`witness_cutoff` truncates the belief query agent's witnessing of the
causal chain partway through, so the object keeps moving unwitnessed
afterward -- directly probing the confirmed Nemotron Nano failure mode
("over-generalizes continuity of observation past the last witness cue").
"""

from __future__ import annotations

import dataclasses

from cartographer.render import build_name_map, render
from cartographer.world import DimParams, Query, generate, measure, validate
from cartographer.world.simulate import answer, simulate


def _base_belief_params(**overrides) -> DimParams:
    fields = dict(
        chain_length=10,
        scramble=0.2,
        coref_pressure=2,
        distractor_events=3,
        distractor_similarity="unrelated",
        filler_events=5,
        query_kind="belief",
    )
    fields.update(overrides)
    return DimParams(**fields)


def _event_actor_ids(event):
    from cartographer.world.model import Filler, Move, Tell, Transfer

    if isinstance(event, Move):
        return {event.actor}
    if isinstance(event, Transfer):
        return {event.giver, event.receiver}
    if isinstance(event, Tell):
        return {event.speaker, event.listener}
    if isinstance(event, Filler):
        return {event.actor}
    return set()


def _true_final_location(world) -> str:
    state_world = dataclasses.replace(
        world, query=Query(kind="state", subject=world.query.subject)
    )
    return answer(state_world).value


# --- 1a: cutoff worlds validate clean ---------------------------------------


def test_cutoff_worlds_validate_clean_across_seeds() -> None:
    for seed in range(30):
        for cutoff in (0.0, 0.3, 0.5, 0.7, 1.0):
            params = _base_belief_params(witness_cutoff=cutoff)
            world = generate(params, seed=seed)
            violations = validate(world)
            assert not violations, f"seed={seed} cutoff={cutoff}: {violations}"
            # well-posedness: agent has a formed belief (expect_unknown=False).
            assert answer(world).value != "unknown", f"seed={seed} cutoff={cutoff}"


# --- 1a/1d: high cutoff makes the belief answer diverge from the true final
# location in the overwhelming majority of worlds -- that IS the trap.


def test_high_cutoff_belief_diverges_from_true_final_location() -> None:
    diverge = 0
    total = 0
    for seed in range(200):
        params = _base_belief_params(witness_cutoff=0.7)
        world = generate(params, seed=seed)
        assert not validate(world)
        belief = answer(world).value
        true_final = _true_final_location(world)
        total += 1
        if belief != true_final:
            diverge += 1
    assert total == 200
    assert diverge / total >= 0.90, f"only {diverge}/{total} diverged"


def test_zero_cutoff_preserves_prior_behavior_shape() -> None:
    """At witness_cutoff=0.0 the agent still witnesses through the
    second-to-last chain move (the pre-hardening baseline), so witness_gap
    should be small (0 or 1) rather than the large gaps high cutoff produces."""
    for seed in range(20):
        params = _base_belief_params(witness_cutoff=0.0)
        world = generate(params, seed=seed)
        gap = measure(world)["witness_gap"]
        assert gap <= 1, f"seed={seed}: unexpectedly large gap {gap} at cutoff=0.0"


# --- 1c: witness_gap is monotone (non-decreasing) in witness_cutoff, in
# aggregate across seeds (per-seed it can be noisy due to actor-exclusion
# interactions, so we compare means across a seed population). -------------


def test_witness_gap_monotone_in_cutoff() -> None:
    cutoffs = [0.0, 0.25, 0.5, 0.75, 1.0]
    means = []
    for cutoff in cutoffs:
        gaps = []
        for seed in range(60):
            params = _base_belief_params(witness_cutoff=cutoff)
            world = generate(params, seed=seed)
            gaps.append(measure(world)["witness_gap"])
        means.append(sum(gaps) / len(gaps))
    for a, b in zip(means, means[1:]):
        assert b >= a - 1e-9, f"means not monotone: {means}"
    assert means[-1] > means[0], f"no overall increase: {means}"


def test_witness_gap_zero_for_non_belief_and_inactive_cutoff() -> None:
    params = _base_belief_params(query_kind="state", witness_cutoff=0.9)
    world = generate(dataclasses.replace(params, query_kind="state"), seed=1)
    assert measure(world)["witness_gap"] == 0.0


# --- surface-completeness must still hold: witness sentences only appear for
# events the entity is an actual recorded witness of. -----------------------


def test_surface_completeness_holds_under_cutoff() -> None:
    for seed in range(20):
        for cutoff in (0.3, 0.7, 1.0):
            params = _base_belief_params(witness_cutoff=cutoff)
            world = generate(params, seed=seed)
            name_map = build_name_map(world, seed=seed)
            rendered = render(world, name_map, seed=seed)

            result = simulate(world)
            setters: dict[tuple[str, str], int] = {}
            for idx, event in enumerate(world.events):
                for witness in getattr(event, "witnesses", frozenset()):
                    obj_id = getattr(event, "obj", None)
                    if obj_id is None:
                        continue
                    key = (witness, obj_id)
                    # Move/Transfer overwrite; last setter wins, matching simulate().
                    setters[key] = idx
                if hasattr(event, "listener"):
                    setters[(event.listener, event.obj)] = idx

            assert set(setters) == set(result.beliefs), (
                f"seed={seed} cutoff={cutoff}: belief-setter reconstruction mismatch"
            )

            sentence_event_idx = rendered.meta["sentence_event_idx"]
            sentence_is_witness = rendered.meta["sentence_is_witness"]

            for (entity_id, obj_id), event_idx in setters.items():
                event = world.events[event_idx]
                is_actor_equiv = entity_id in _event_actor_ids(event)
                found = False
                for sent_idx, ev_idx in enumerate(sentence_event_idx):
                    if ev_idx != event_idx:
                        continue
                    if is_actor_equiv and not sentence_is_witness[sent_idx]:
                        found = True
                        break
                    if not is_actor_equiv and sentence_is_witness[sent_idx]:
                        # witness sentence for this event: entity must be a
                        # recorded non-actor witness of it.
                        non_actor_witnesses = getattr(event, "witnesses", frozenset()) - _event_actor_ids(event)
                        if entity_id in non_actor_witnesses:
                            found = True
                            break
                assert found, (
                    f"seed={seed} cutoff={cutoff}: entity {entity_id!r}'s belief about "
                    f"{obj_id!r} set by event {event_idx} has no matching rendered mention"
                )


# --- 1b: combined belief-difficulty ceiling ---------------------------------


def test_combined_ceiling_config_renders_and_validates_clean() -> None:
    for seed in range(10):
        params = DimParams(
            chain_length=25,
            scramble=0.9,
            coref_pressure=8,
            distractor_events=40,
            distractor_similarity="near_duplicate",
            filler_events=60,
            query_kind="belief",
            witness_cutoff=0.7,
        )
        world = generate(params, seed=seed)
        violations = validate(world)
        assert not violations, f"seed={seed}: {violations}"

        realized = measure(world)
        assert realized["entity_state"] == 25, f"seed={seed}"
        assert realized["witness_gap"] > 0, f"seed={seed}: expected a real witness gap"

        name_map = build_name_map(world, seed=seed)
        assert len(set(name_map.entity.values())) == len(name_map.entity), f"seed={seed}"
        assert len(set(name_map.location.values())) == len(name_map.location), f"seed={seed}"
        assert len(set(name_map.obj.values())) == len(name_map.obj), f"seed={seed}"

        rendered = render(world, name_map, seed=seed)
        assert isinstance(rendered.prompt, str)
        assert rendered.prompt.strip()
