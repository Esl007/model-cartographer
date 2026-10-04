"""Acceptance tests for the Phase 1 latent world generator."""

from __future__ import annotations

from cartographer.store import ExperimentRecord, RenderMode, ResultsStore, Verdict
from cartographer.world import (
    ANSWER_CHANGING,
    ANSWER_PRESERVING,
    DimParams,
    Entity,
    Location,
    Move,
    Obj,
    Query,
    Tell,
    World,
    add_coref_pressure,
    add_distractors,
    add_filler,
    add_noop_distractors,
    add_relational_time,
    answer,
    extend_chain,
    generate,
    measure,
    scramble_narration,
    validate,
)


def _false_belief_world(marcus_witnesses_final_move: bool) -> World:
    entities = [
        Entity(id="elena", name="Elena", gender="f"),
        Entity(id="marcus", name="Marcus", gender="m"),
    ]
    locations = [
        Location(id="room_a", name="room_a"),
        Location(id="room_b", name="room_b"),
        Location(id="room_c", name="room_c"),
    ]
    objects = [Obj(id="blue_box", name="blue_box", category="container")]

    move1 = Move(
        obj="blue_box", src="room_a", dst="room_c", actor="elena",
        time=0, witnesses=frozenset({"elena"}),
    )
    tell = Tell(
        speaker="elena", listener="marcus", obj="blue_box",
        claimed_location="room_c", time=1, witnesses=frozenset({"marcus"}),
    )
    final_witnesses = {"elena", "marcus"} if marcus_witnesses_final_move else {"elena"}
    move2 = Move(
        obj="blue_box", src="room_c", dst="room_b", actor="elena",
        time=2, witnesses=frozenset(final_witnesses),
    )
    events = [move1, tell, move2]
    query = Query(kind="state", subject="blue_box")
    return World(
        entities=entities, objects=objects, locations=locations,
        events=events, narration_order=[0, 1, 2], query=query,
    )


def test_false_belief_state_and_belief_answers() -> None:
    world = _false_belief_world(marcus_witnesses_final_move=False)
    assert not validate(world)
    state_answer = answer(world)
    assert state_answer.value == "room_b"

    belief_world = World(
        entities=world.entities, objects=world.objects, locations=world.locations,
        events=world.events, narration_order=world.narration_order,
        query=Query(kind="belief", subject="blue_box", agent="marcus"),
    )
    assert not validate(belief_world)
    belief_answer = answer(belief_world)
    assert belief_answer.value == "room_c"


def test_false_belief_corrected_when_witnessed() -> None:
    world = _false_belief_world(marcus_witnesses_final_move=True)
    belief_world = World(
        entities=world.entities, objects=world.objects, locations=world.locations,
        events=world.events, narration_order=world.narration_order,
        query=Query(kind="belief", subject="blue_box", agent="marcus"),
    )
    assert not validate(belief_world)
    belief_answer = answer(belief_world)
    assert belief_answer.value == "room_b"


def test_belief_unknown_expected() -> None:
    world = _false_belief_world(marcus_witnesses_final_move=False)
    unknown_query = Query(
        kind="belief", subject="blue_box", agent="marcus", expect_unknown=True
    )
    stranger_world = World(
        entities=world.entities + [Entity(id="stranger", name="Stranger", gender="f")],
        objects=world.objects, locations=world.locations,
        events=world.events, narration_order=world.narration_order,
        query=Query(kind="belief", subject="blue_box", agent="stranger", expect_unknown=True),
    )
    assert not validate(stranger_world)
    assert answer(stranger_world).value == "unknown"


def test_belief_unknown_unexpected_violates() -> None:
    world = _false_belief_world(marcus_witnesses_final_move=False)
    stranger_world = World(
        entities=world.entities + [Entity(id="stranger", name="Stranger", gender="f")],
        objects=world.objects, locations=world.locations,
        events=world.events, narration_order=world.narration_order,
        query=Query(kind="belief", subject="blue_box", agent="stranger", expect_unknown=False),
    )
    violations = validate(stranger_world)
    assert violations


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


def test_determinism() -> None:
    params = _base_params()
    w1 = generate(params, seed=42)
    w2 = generate(params, seed=42)
    assert measure(w1) == measure(w2)
    assert answer(w1) == answer(w2)

    measures = set()
    for seed in range(5):
        w = generate(params, seed=seed)
        measures.add(tuple(sorted(measure(w).items())))
    assert len(measures) > 1


def test_monotonicity_chain_length() -> None:
    w_short = generate(_base_params(chain_length=2), seed=1)
    w_long = generate(_base_params(chain_length=8), seed=1)
    assert measure(w_long)["entity_state"] > measure(w_short)["entity_state"]


def test_monotonicity_distractors() -> None:
    w_none = generate(_base_params(distractor_events=0), seed=1)
    w_many = generate(_base_params(distractor_events=20), seed=1)
    assert measure(w_many)["distractors"] > measure(w_none)["distractors"]


def test_monotonicity_scramble() -> None:
    w_flat = generate(_base_params(scramble=0.0, chain_length=6, distractor_events=4, filler_events=4), seed=7)
    w_scrambled = generate(_base_params(scramble=1.0, chain_length=6, distractor_events=4, filler_events=4), seed=7)
    assert measure(w_flat)["temporal"] == 0.0
    assert measure(w_scrambled)["temporal"] > 0.5


def test_well_posedness_across_worlds() -> None:
    kinds = ["state", "belief", "order"]
    for seed in range(50):
        kind = kinds[seed % 3]
        params = _base_params(
            chain_length=1 + (seed % 5),
            scramble=(seed % 10) / 10,
            coref_pressure=seed % 3,
            distractor_events=seed % 7,
            distractor_similarity=["unrelated", "same_entities", "same_category", "near_duplicate"][seed % 4],
            filler_events=seed % 4,
            query_kind=kind,
            expect_unknown=False,
        )
        world = generate(params, seed=seed)
        assert not validate(world), f"seed={seed} kind={kind}"
        if kind == "belief":
            assert answer(world).value != "unknown"


def test_answer_preservation() -> None:
    changed = 0
    for seed in range(10):
        params = _base_params(chain_length=1 + (seed % 4), query_kind="state")
        world = generate(params, seed=seed)
        original_answer = answer(world)

        for op_name in ANSWER_PRESERVING:
            op = {
                "add_distractors": lambda w, s: add_distractors(w, 3, "unrelated", s),
                "add_filler": lambda w, s: add_filler(w, 3, s),
                "scramble_narration": lambda w, s: scramble_narration(w, 0.5, s),
                "add_coref_pressure": lambda w, s: add_coref_pressure(w, 2, s),
                "add_noop_distractors": lambda w, s: add_noop_distractors(w, 3, s),
                "add_relational_time": lambda w, s: add_relational_time(w, 0.5, s),
            }[op_name]
            mutated = op(world, seed + 100)
            assert answer(mutated) == original_answer, f"{op_name} changed answer at seed={seed}"

        extended = extend_chain(world, 2, seed + 200)
        if answer(extended) != original_answer:
            changed += 1

    assert changed >= 8


def test_answer_changing_set_declared() -> None:
    assert "extend_chain" in ANSWER_CHANGING
    assert ANSWER_PRESERVING.isdisjoint(ANSWER_CHANGING)


def test_side_effect_measurement() -> None:
    params = _base_params(distractor_events=1)
    world = generate(params, seed=3)
    before = measure(world)
    mutated = add_distractors(world, 10, "unrelated", seed=99)
    after = measure(mutated)
    assert after["context_length"] > before["context_length"]
    assert after["distractors"] > before["distractors"]


def test_store_integration(tmp_path) -> None:
    params = _base_params()
    world = generate(params, seed=5)
    realized = measure(world)
    record = ExperimentRecord(
        run_id="world-run-1",
        model_name="latent_world",
        latent_params={
            "chain_length": float(params.chain_length),
            "scramble": params.scramble,
            "coref_pressure": float(params.coref_pressure),
            "distractor_events": float(params.distractor_events),
            "filler_events": float(params.filler_events),
        },
        realized_dims=realized,
        render_mode=RenderMode.SYNTHETIC,
        task_seed=5,
        trial_seed=0,
        verdict=Verdict.PASS,
    )
    store = ResultsStore(tmp_path / "world_db.sqlite3")
    store.append(record)
    fetched = store.by_run("world-run-1")
    assert len(fetched) == 1
    assert fetched[0].realized_dims == realized


def test_widened_state_difficulty_generates_clean_across_seeds() -> None:
    from cartographer.render import build_name_map, render

    for seed in range(5):
        params = _base_params(
            chain_length=30,
            scramble=0.9,
            coref_pressure=4,
            distractor_events=40,
            distractor_similarity="near_duplicate",
            filler_events=80,
            query_kind="state",
        )
        world = generate(params, seed=seed)
        assert not validate(world), f"seed={seed}"

        realized = measure(world)
        assert realized["entity_state"] == 30, f"seed={seed} entity_state={realized['entity_state']}"

    # Render one such world: name uniqueness holds and prompt renders without error.
    params = _base_params(
        chain_length=30,
        scramble=0.9,
        coref_pressure=4,
        distractor_events=40,
        distractor_similarity="near_duplicate",
        filler_events=80,
        query_kind="state",
    )
    world = generate(params, seed=0)
    name_map = build_name_map(world, seed=0)

    assert len(set(name_map.entity.values())) == len(name_map.entity)
    assert len(set(name_map.location.values())) == len(name_map.location)
    assert len(set(name_map.obj.values())) == len(name_map.obj)

    rendered = render(world, name_map, seed=0)
    assert isinstance(rendered.prompt, str)
    assert rendered.prompt.strip()
