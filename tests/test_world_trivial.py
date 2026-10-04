"""Tests for the full-witness (trivialized false-belief) transformation in
`cartographer.world.trivial`, its `stale_belief` foil, and the verdict
classifier's handling of full-witness records.

Literature: Malek et al. 2025 (arXiv 2507.07313) and SimpleToM (arXiv
2410.13648) -- when the classic false-belief story is trivialized (the agent
SEES the final move, so the answer is the true location), models still give
the trapped pre-move answer. `stale_belief` is the mirror image of
`final_true_state`.

Zero network calls, no LLM: pure simulation + template rendering.
"""

from __future__ import annotations

import dataclasses

import pytest

from cartographer.exec.evaluator import _apply_config, _default_base
from cartographer.render import build_name_map, render, verify
from cartographer.render.templates import _render_witness_sentence
from cartographer.report.verdict import classify_belief_failure
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.world import DimParams, Move, generate, validate
from cartographer.world.foils import foil_answers, foil_divergence
from cartographer.world.simulate import answer, simulate
from cartographer.world.trivial import (
    FullWitnessUnavailable,
    full_witness_applicable,
    is_full_witness_world,
    make_full_witness,
    stale_location,
)


def _mix_params(seed: int, **overrides) -> DimParams:
    """A seed-varied spread of belief worlds at witness_cutoff 0.0 (the
    classic false-belief regime): chain 2..10, scramble 0..1, coref 0..3,
    2..6 same-category distractors."""
    fields = dict(
        chain_length=2 + seed % 9,
        scramble=(seed % 5) / 4,
        coref_pressure=seed % 4,
        distractor_events=2 + seed % 5,
        distractor_similarity="same_category",
        filler_events=2,
        query_kind="belief",
    )
    fields.update(overrides)
    return DimParams(**fields)


def _true_location(world) -> str:
    return simulate(world).final_locations[world.query.subject]


def _applicable_worlds(n: int = 60):
    out = []
    for seed in range(n):
        world = generate(_mix_params(seed), seed)
        if full_witness_applicable(world):
            out.append((seed, world))
    return out


def _subject_moves(world) -> list[int]:
    return [
        i
        for i, ev in enumerate(world.events)
        if isinstance(ev, Move) and ev.obj == world.query.subject
    ]


# --- applicability -----------------------------------------------------------


def test_applicability_rate_over_200_belief_seeds() -> None:
    applicable = 0
    unavailable = []
    for seed in range(200):
        world = generate(_mix_params(seed), seed)
        if full_witness_applicable(world):
            applicable += 1
        else:
            unavailable.append(seed)
            # Reported unavailable, never silently kept.
            with pytest.raises(FullWitnessUnavailable):
                make_full_witness(world, seed)
    assert applicable / 200 >= 0.90, f"only {applicable}/200 applicable; unavailable={unavailable}"


def test_inapplicable_worlds_are_reported_unavailable() -> None:
    cases = [
        # Not a belief query.
        generate(_mix_params(3, query_kind="state"), 3),
        # chain_length 1: belief comes from a later Tell, which would
        # override what the agent saw -> trivialized answer != true location.
        generate(_mix_params(4, chain_length=1), 4),
        # Truncating cutoff: the agent missed more than the final move.
        generate(_mix_params(5, chain_length=8, witness_cutoff=0.9), 5),
    ]
    for world in cases:
        assert not full_witness_applicable(world)
        with pytest.raises(FullWitnessUnavailable):
            make_full_witness(world, 0)
    # An already-trivialized world is not re-trivializable.
    seed, world = _applicable_worlds(5)[0]
    fw = make_full_witness(world, seed)
    assert not full_witness_applicable(fw)


# --- answers -----------------------------------------------------------------


def test_full_witness_answer_is_true_location_and_original_is_stale() -> None:
    worlds = _applicable_worlds()
    assert len(worlds) >= 50
    for seed, world in worlds:
        fw = make_full_witness(world, seed)
        assert not validate(fw)
        true_loc = _true_location(world)
        stale = stale_location(world)
        # Trivialized: the right answer is the TRUE location, not the stale one.
        assert answer(fw).value == true_loc != stale
        # Original classic story: the right answer IS the stale location.
        assert answer(world).value == stale
        # The trap is the same location whichever world it's read off.
        assert stale_location(fw) == stale
        # stale == the pre-final-move location of the subject.
        assert stale == world.events[_subject_moves(world)[-1]].src
        # Reality itself is untouched.
        assert _true_location(fw) == true_loc


def test_events_order_and_other_witnesses_unchanged() -> None:
    for seed, world in _applicable_worlds(40):
        fw = make_full_witness(world, seed)
        agent = world.query.agent
        subject_moves = set(_subject_moves(world))
        assert fw.narration_order == world.narration_order
        assert fw.query == world.query
        assert fw.relative_times == world.relative_times
        assert fw.entities == world.entities and fw.locations == world.locations
        assert fw.objects == world.objects
        assert len(fw.events) == len(world.events)
        for i, (old, new) in enumerate(zip(world.events, fw.events)):
            assert type(old) is type(new)
            if i in subject_moves:
                assert agent in new.witnesses
                assert new.witnesses - {agent} == old.witnesses - {agent}
                assert dataclasses.replace(new, witnesses=old.witnesses) == old
            else:
                assert new == old
        # The only witness change is the agent being added to chain moves
        # (in a classic world: exactly the final one).
        added = [i for i in subject_moves if agent not in world.events[i].witnesses]
        assert added == [max(subject_moves)]


# --- rendering ---------------------------------------------------------------


def test_rendered_trivialized_story_shows_agent_witnessing_final_move() -> None:
    for seed, world in _applicable_worlds(40):
        fw = make_full_witness(world, seed)
        name_map = build_name_map(fw, seed)
        assert name_map == build_name_map(world, seed)
        agent = fw.query.agent
        final_idx = _subject_moves(fw)[-1]

        rendered = render(fw, name_map, seed)
        original = render(world, name_map, seed)
        meta = rendered.meta

        witness_positions = [
            k
            for k, (ev_idx, is_w) in enumerate(
                zip(meta["sentence_event_idx"], meta["sentence_is_witness"])
            )
            if ev_idx == final_idx and is_w
        ]
        assert len(witness_positions) == 1, f"seed={seed}: no witness line on the final move"
        k = witness_positions[0]
        # The pre-pronoun witness sentence names the agent ...
        anchored = fw.events[final_idx].time not in fw.relative_times
        raw = _render_witness_sentence(
            fw.events[final_idx], final_idx, seed, name_map, anchored=anchored
        )
        agent_name = name_map.entity[agent]
        assert raw is not None and agent_name in raw
        # ... and it reaches the story either verbatim or with the agent's
        # name pronominalized (and the pronoun's referent recorded as the agent).
        story = rendered.prompt.split("\n\n")[0]
        pronouns = [
            r["pronoun"]
            for r in meta["pronoun_referents"]
            if r["sentence_index"] == k and r["entity_id"] == agent
        ]
        variants = [raw] + [raw.replace(agent_name, p, 1) for p in pronouns]
        assert any(v[:1].upper() + v[1:] in story for v in variants), (seed, raw)
        # The classic story has NO witness line on its final move.
        assert not any(
            ev_idx == final_idx and is_w
            for ev_idx, is_w in zip(
                original.meta["sentence_event_idx"], original.meta["sentence_is_witness"]
            )
        )
        # Answer keys differ: true location vs stale location.
        assert rendered.answer_key == name_map.location[_true_location(world)]
        assert original.answer_key == name_map.location[stale_location(world)]


# --- foils -------------------------------------------------------------------


def test_stale_belief_and_final_true_state_are_mirror_foils() -> None:
    for seed, world in _applicable_worlds(40):
        fw = make_full_witness(world, seed)
        name_map = build_name_map(fw, seed)
        classic = foil_divergence(world, name_map)
        trivial = foil_divergence(fw, name_map)
        # Classic world: stale_belief is absent (no-op); final_true_state traps.
        assert "stale_belief" not in classic.foils
        assert "final_true_state" in classic.trap_kinds
        # Full-witness world: stale_belief traps; final_true_state is the truth.
        assert is_full_witness_world(fw) and not is_full_witness_world(world)
        assert trivial.foils["stale_belief"] == name_map.location[stale_location(world)]
        assert "stale_belief" in trivial.trap_kinds
        assert "final_true_state" not in trivial.trap_kinds
        assert trivial.foils["final_true_state"] == trivial.true_answer
        assert trivial.foils["stale_belief"] == classic.true_answer
    # Ordinary (non-full-witness) worlds keep the classic belief key set.
    world = generate(_mix_params(2), 2)
    assert "stale_belief" not in foil_answers(world, build_name_map(world, 2))


# --- planted fakes + verdict classifier ----------------------------------------


def _planted_records(n: int = 12):
    """(seed, latent_params, original world, full-witness world, name_map)
    built through the SAME `_default_base` + `_apply_config` path that
    `report.verdict._regenerate` uses, so records reconstruct exactly."""
    out = []
    for seed in range(200):
        latent = {
            "chain_length": float(2 + seed % 6),
            "scramble": (seed % 3) / 2,
            "coref_pressure": float(seed % 3),
            "distractor_events": 2.0,
            "filler_events": 2.0,
            "witness_cutoff": 0.0,
        }
        world = generate(_apply_config(_default_base("belief"), latent), seed)
        if not full_witness_applicable(world):
            continue
        fw = make_full_witness(world, seed)
        out.append((seed, latent, world, fw, build_name_map(fw, seed)))
        if len(out) >= n:
            break
    assert len(out) == n
    return out


def _record(seed, latent, response, full_witness: bool, verdict) -> ExperimentRecord:
    params = dict(latent)
    if full_witness:
        params["full_witness"] = 1.0
    return ExperimentRecord(
        run_id="run-trivial",
        model_name="fake/template-matcher",
        latent_params=params,
        realized_dims={},
        render_mode=RenderMode.TEMPLATE,
        task_seed=seed,
        trial_seed=0,
        verdict=verdict,
        rendering_seed=seed,
        response_text=response,
        verifier_detail="kind=belief;campaign=1",
    )


def test_template_matcher_fails_iff_full_witness_and_is_classified_stale_belief() -> None:
    for seed, latent, world, fw, name_map in _planted_records():
        stale_surface = name_map.location[stale_location(world)]
        # The template-matcher always answers the pre-final-move location.
        classic_verdict = verify(stale_surface, render(world, name_map, seed), name_map)
        trivial_verdict = verify(stale_surface, render(fw, name_map, seed), name_map)
        assert classic_verdict == Verdict.PASS
        assert trivial_verdict == Verdict.FAIL
        record = _record(seed, latent, stale_surface, full_witness=True, verdict=trivial_verdict)
        assert classify_belief_failure(record) == "stale_belief"


def test_final_true_state_reader_passes_on_full_witness_worlds() -> None:
    for seed, latent, world, fw, name_map in _planted_records():
        true_surface = name_map.location[_true_location(world)]
        trivial_verdict = verify(true_surface, render(fw, name_map, seed), name_map)
        classic_verdict = verify(true_surface, render(world, name_map, seed), name_map)
        assert trivial_verdict == Verdict.PASS
        assert classic_verdict == Verdict.FAIL
        # On the classic world the same reader is the final_true_state failure.
        record = _record(seed, latent, true_surface, full_witness=False, verdict=classic_verdict)
        assert classify_belief_failure(record) == "final_true_state"


def test_unavailable_full_witness_record_is_not_silently_rescored() -> None:
    # chain_length 1 -> belief via a later Tell -> full witness unavailable.
    latent = {"chain_length": 1.0, "witness_cutoff": 0.0}
    world = generate(_apply_config(_default_base("belief"), latent), 7)
    assert not full_witness_applicable(world)
    record = _record(7, latent, "anything", full_witness=True, verdict=Verdict.FAIL)
    assert classify_belief_failure(record) is None


# --- determinism -------------------------------------------------------------


def test_determinism() -> None:
    for seed in (0, 11, 23):
        a = generate(_mix_params(seed), seed)
        b = generate(_mix_params(seed), seed)
        assert full_witness_applicable(a) == full_witness_applicable(b)
        assert stale_location(a) == stale_location(b)
        if full_witness_applicable(a):
            fa, fb = make_full_witness(a, seed), make_full_witness(b, seed)
            assert fa == fb
            # `seed` does not influence the transformation.
            assert make_full_witness(a, seed + 1) == fa
            nm = build_name_map(fa, seed)
            assert render(fa, nm, seed).prompt == render(fb, nm, seed).prompt
