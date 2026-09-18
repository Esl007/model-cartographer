"""Acceptance tests for the foil model (cartographer.world.foils) and the
adversarial seed search (cartographer.world.search).

Zero network calls anywhere -- everything here is pure simulation over
hand-built or generator-produced `World`s.
"""

from __future__ import annotations

import statistics

from cartographer.render import build_name_map, render
from cartographer.world import DimParams, Entity, Location, Move, Obj, Query, Tell, World, generate
from cartographer.world.foils import FoilProfile, foil_answers, foil_divergence
from cartographer.world.search import search_adversarial_seeds
from cartographer.world.simulate import validate

from tests.test_render import _check_surface_completeness


def _base_belief_params(**overrides) -> DimParams:
    fields = dict(
        chain_length=4,
        scramble=0.3,
        coref_pressure=1,
        distractor_events=2,
        distractor_similarity="same_category",
        filler_events=2,
        query_kind="belief",
    )
    fields.update(overrides)
    return DimParams(**fields)


# ---------------------------------------------------------------------------
# Hand-built worlds with known right answers (same idiom as test_world.py's
# _false_belief_world).
# ---------------------------------------------------------------------------


def _hand_built_belief_world(*, marcus_witnesses_final_move: bool) -> World:
    """Elena moves the box room_a -> room_c (witnessed only by Elena, and
    tells Marcus it's in room_c), then room_c -> room_b. Marcus witnesses
    the second move only if `marcus_witnesses_final_move`. True final
    location is always room_b.

    marcus_witnesses_final_move=False: Marcus's belief is stuck at room_c
    (from the Tell) -- a genuine false belief, diverging from true state
    room_b. This is the world foil_divergence must catch as a trap.

    marcus_witnesses_final_move=True: Marcus's belief updates to room_b,
    matching true state -- belief == reality, so `final_true_state` must
    NOT diverge.
    """
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
    query = Query(kind="belief", subject="blue_box", agent="marcus")
    return World(
        entities=entities, objects=objects, locations=locations,
        events=events, narration_order=[0, 1, 2], query=query,
    )


def test_belief_foils_exact_values_false_belief() -> None:
    """Marcus never sees the second move -- his belief is stuck at
    room_c (via the Tell), true state is room_b. Every foil that keys off
    "the last thing that happened" should point at room_b (the trap);
    `query_agent_own_last_move` should be None (Marcus never moved the box
    himself)."""
    world = _hand_built_belief_world(marcus_witnesses_final_move=False)
    assert not validate(world)
    name_map = build_name_map(world, seed=0)

    foils = foil_answers(world, name_map)
    room_b = name_map.location["room_b"]
    room_c = name_map.location["room_c"]

    assert foils["final_true_state"] == room_b
    assert foils["last_mentioned_location"] == room_b  # last Move.dst in narration order
    assert foils["last_witnessed_by_anyone"] == room_b  # Elena witnesses her own move2
    assert foils["query_agent_own_last_move"] is None  # Marcus never moved the box
    assert foils["most_frequent_location"] in (room_b, room_c)  # each dst mentioned once; tie-break rule

    profile = foil_divergence(world, name_map)
    assert profile.true_answer == room_c  # Marcus's actual belief
    assert "final_true_state" in profile.trap_kinds
    assert "last_mentioned_location" in profile.trap_kinds
    assert "last_witnessed_by_anyone" in profile.trap_kinds
    assert "query_agent_own_last_move" not in profile.trap_kinds  # None never diverges
    assert profile.n_diverging >= 3


def test_belief_reality_world_no_final_true_state_divergence() -> None:
    """Marcus DOES witness the final move -- his belief matches true state.
    `final_true_state` must not be a diverging trap kind here."""
    world = _hand_built_belief_world(marcus_witnesses_final_move=True)
    assert not validate(world)
    name_map = build_name_map(world, seed=0)

    profile = foil_divergence(world, name_map)
    assert profile.true_answer == name_map.location["room_b"]
    assert "final_true_state" not in profile.trap_kinds


def test_query_agent_own_last_move_foil() -> None:
    """When the query agent's own last move IS also their last witnessed
    event (they never see the object move again afterward), their true
    belief equals their own last move's destination -- so
    `query_agent_own_last_move` does NOT diverge (it correctly predicts
    the agent's belief), while `final_true_state` DOES diverge once a
    third party moves the object again afterward. This is the case where
    "I did it, so I know where it is" happens to be the right heuristic,
    distinguishing this foil from the (wrong) belief-reality-collapse
    foil `final_true_state`."""
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
        obj="blue_box", src="room_a", dst="room_b", actor="marcus",
        time=0, witnesses=frozenset({"marcus"}),
    )
    move2 = Move(
        obj="blue_box", src="room_b", dst="room_c", actor="elena",
        time=1, witnesses=frozenset({"elena"}),
    )
    query = Query(kind="belief", subject="blue_box", agent="marcus")
    world = World(
        entities=entities, objects=objects, locations=locations,
        events=[move1, move2], narration_order=[0, 1], query=query,
    )
    assert not validate(world)
    name_map = build_name_map(world, seed=0)
    foils = foil_answers(world, name_map)
    assert foils["query_agent_own_last_move"] == name_map.location["room_b"]
    assert foils["final_true_state"] == name_map.location["room_c"]

    profile = foil_divergence(world, name_map)
    assert profile.true_answer == name_map.location["room_b"]  # Marcus's true belief
    assert "query_agent_own_last_move" not in profile.trap_kinds  # correctly predicts the belief
    assert "final_true_state" in profile.trap_kinds  # room_c != Marcus's belief room_b


def test_order_query_narration_order_foil() -> None:
    """Two events out of chronological order in narration: the
    narration_order_answer foil must diverge from the true (timestamp-
    based) answer."""
    entities = [Entity(id="elena", name="Elena", gender="f")]
    locations = [Location(id="room_a", name="room_a"), Location(id="room_b", name="room_b")]
    objects = [Obj(id="blue_box", name="blue_box", category="container")]
    move1 = Move(obj="blue_box", src="room_a", dst="room_b", actor="elena", time=0, witnesses=frozenset({"elena"}))
    move2 = Move(obj="blue_box", src="room_b", dst="room_a", actor="elena", time=1, witnesses=frozenset({"elena"}))
    query = Query(kind="order", subject="blue_box", pair=(0, 1))
    # narration_order = [1, 0]: event 1 (time=1) is narrated BEFORE event 0 (time=0).
    world = World(
        entities=entities, objects=objects, locations=locations,
        events=[move1, move2], narration_order=[1, 0], query=query,
    )
    assert not validate(world)
    name_map = build_name_map(world, seed=0)

    foils = foil_answers(world, name_map)
    assert foils["narration_order_answer"] == "second"  # pair[0] narrated after pair[1]

    profile = foil_divergence(world, name_map)
    assert profile.true_answer == "first"  # time=0 < time=1
    assert "narration_order_answer" in profile.trap_kinds
    assert profile.salience_weighted_score > 0.0


def test_witness_cutoff_world_diverges() -> None:
    """A generator-produced witness_cutoff world (agent's witnessing stops
    early while the object keeps moving) must have `final_true_state`
    diverging -- this is the documented Nano/Super/Ultra failure mode the
    foil model exists to detect."""
    found_diverging = False
    for seed in range(20):
        params = _base_belief_params(chain_length=6, witness_cutoff=0.8, scramble=0.0)
        world = generate(params, seed=seed)
        if validate(world):
            continue
        name_map = build_name_map(world, seed=seed)
        profile = foil_divergence(world, name_map)
        if "final_true_state" in profile.trap_kinds:
            found_diverging = True
            break
    assert found_diverging


def test_no_witness_cutoff_can_still_be_trivial() -> None:
    """A single-move belief world where the agent witnesses the only move,
    with no distractors/filler/coref pressure to introduce other location
    mentions (belief == reality, no gap at all, nothing else to mention):
    no foil should diverge -- this is the "trivially easy" case the foil
    model is supposed to recognize as such."""
    params = _base_belief_params(
        chain_length=1, witness_cutoff=0.0, scramble=0.0,
        coref_pressure=0, distractor_events=0, filler_events=0,
    )
    world = generate(params, seed=1)
    assert not validate(world)
    name_map = build_name_map(world, seed=1)
    profile = foil_divergence(world, name_map)
    assert profile.n_diverging == 0
    assert profile.trap_kinds == ()


def test_state_query_foils() -> None:
    params = _base_belief_params(chain_length=5, query_kind="state", scramble=0.5)
    world = generate(params, seed=5)
    assert not validate(world)
    name_map = build_name_map(world, seed=5)
    foils = foil_answers(world, name_map)
    assert set(foils) == {"first_mentioned_location", "most_frequent_location"}


def test_belief_query_foil_keys() -> None:
    params = _base_belief_params()
    world = generate(params, seed=2)
    assert not validate(world)
    name_map = build_name_map(world, seed=2)
    foils = foil_answers(world, name_map)
    assert set(foils) == {
        "final_true_state",
        "last_mentioned_location",
        "last_witnessed_by_anyone",
        "query_agent_own_last_move",
        "most_frequent_location",
    }


def test_order_query_foil_keys() -> None:
    params = _base_belief_params(query_kind="order", scramble=0.5)
    world = generate(params, seed=9)
    assert not validate(world)
    name_map = build_name_map(world, seed=9)
    foils = foil_answers(world, name_map)
    assert set(foils) == {"narration_order_answer"}


# ---------------------------------------------------------------------------
# Adversarial search
# ---------------------------------------------------------------------------


def test_search_is_deterministic() -> None:
    params = _base_belief_params(chain_length=5, witness_cutoff=0.5)
    result1 = search_adversarial_seeds(params, base_seed=123, n_candidates=15, top_k=3)
    result2 = search_adversarial_seeds(params, base_seed=123, n_candidates=15, top_k=3)
    assert result1 == result2

    # Different base_seed => (with overwhelming likelihood) a different pick.
    result3 = search_adversarial_seeds(params, base_seed=999, n_candidates=15, top_k=3)
    assert [s for s, _ in result1] != [s for s, _ in result3] or result1 == result3


def test_search_top1_scores_at_least_as_high_as_any_individual_candidate() -> None:
    params = _base_belief_params(chain_length=5, witness_cutoff=0.5)
    top = search_adversarial_seeds(params, base_seed=7, n_candidates=20, top_k=1)
    assert len(top) == 1
    winner_score = top[0][1].salience_weighted_score

    from cartographer.world.search import _derive_seed

    for i in range(20):
        seed = _derive_seed(7, i)
        world = generate(params, seed)
        if validate(world):
            continue
        name_map = build_name_map(world, seed)
        profile = foil_divergence(world, name_map)
        assert profile.salience_weighted_score <= winner_score


def test_search_require_kinds_filters() -> None:
    params = _base_belief_params(chain_length=5, witness_cutoff=0.5)
    results = search_adversarial_seeds(
        params, base_seed=11, n_candidates=25, top_k=5, require_kinds=("final_true_state",)
    )
    for _, profile in results:
        assert "final_true_state" in profile.trap_kinds


def test_search_skips_invalid_worlds() -> None:
    """require_kinds asking for something structurally impossible at this
    config still returns an empty (not error-raising) list."""
    params = _base_belief_params(chain_length=1, witness_cutoff=0.0)
    results = search_adversarial_seeds(
        params, base_seed=3, n_candidates=10, top_k=5,
        require_kinds=("query_agent_own_last_move",),
    )
    # query_agent_own_last_move is basically never populated at chain_length=1
    # baseline params (agent doesn't act as mover) -- assert it doesn't crash
    # and returns a list (possibly empty).
    assert isinstance(results, list)


def test_adversarial_worlds_pass_validate_and_surface_completeness() -> None:
    """Adversarial-selected worlds must still be well-posed and render with
    full surface completeness (every belief-setting event's witness is
    actually mentioned in the rendered prose) -- reuses the existing
    surface-completeness helper from test_render.py rather than
    duplicating its logic."""
    params = _base_belief_params(chain_length=6, witness_cutoff=0.6, scramble=0.4)
    winners = search_adversarial_seeds(params, base_seed=42, n_candidates=15, top_k=3)
    assert winners
    for seed, profile in winners:
        world = generate(params, seed)
        assert not validate(world)
        name_map = build_name_map(world, seed)
        rendered = render(world, name_map, seed)
        _check_surface_completeness(world, name_map, rendered, label=f"adversarial seed={seed}")


def test_key_empirical_search_concentrates_traps() -> None:
    """The empirical claim the search exists to prove: across ~50 seeds at
    a fixed mid-difficulty belief config, the mean salience_weighted_score
    of `search_adversarial_seeds(top_k=1)` results is substantially higher
    (>= 1.5x) than the mean over unselected random seeds at the same
    config. If this weren't true, the search would be doing nothing
    useful -- just picking a seed no more adversarial than average."""
    params = _base_belief_params(chain_length=6, witness_cutoff=0.5, scramble=0.3, coref_pressure=1)

    n_trials = 50
    unselected_scores: list[float] = []
    selected_scores: list[float] = []

    for trial in range(n_trials):
        base_seed = 10_000 + trial * 97  # spread out, deterministic

        # Unselected: just the single seed a normal (non-adversarial) run
        # would have used.
        from cartographer.world.search import _derive_seed

        plain_seed = _derive_seed(base_seed, 0)
        world = generate(params, plain_seed)
        if validate(world):
            continue
        name_map = build_name_map(world, plain_seed)
        unselected_scores.append(foil_divergence(world, name_map).salience_weighted_score)

        winners = search_adversarial_seeds(params, base_seed, n_candidates=40, top_k=1)
        assert winners
        selected_scores.append(winners[0][1].salience_weighted_score)

    assert len(unselected_scores) >= 30  # sanity: most trials produced a valid world
    mean_unselected = statistics.mean(unselected_scores)
    mean_selected = statistics.mean(selected_scores)

    assert mean_unselected > 0.0, "baseline config should have SOME salient traps on average"
    assert mean_selected >= 1.5 * mean_unselected, (
        f"adversarial search did not concentrate traps: "
        f"selected mean={mean_selected:.3f} vs unselected mean={mean_unselected:.3f}"
    )
