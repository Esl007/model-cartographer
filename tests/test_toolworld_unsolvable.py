"""Tests for the irrelevance/unanswerable axis: `mutate.make_unsolvable`,
the render-level decline affordance, and `verify_plan`'s inverted grading.

All offline -- no API calls.
"""

from __future__ import annotations

import json

from cartographer.exec.evaluator import _apply_tool_trap_config
from cartographer.store.schema import Verdict
from cartographer.toolworld.generate import ToolDimParams, generate
from cartographer.toolworld.mutate import (
    ANSWER_ELIMINATING,
    make_unsolvable,
    unsolvable_applicable,
)
from cartographer.toolworld.render import render
from cartographer.toolworld.simulate import optimal_depth, validate
from cartographer.toolworld.verify import verify_plan

BASE = ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1)


def _solvable_world(seed: int = 0):
    return generate(BASE, seed)


# --- make_unsolvable -------------------------------------------------------


def test_make_unsolvable_produces_provably_unreachable_goal():
    hits = 0
    for seed in range(12):
        world = _solvable_world(seed)
        assert not validate(world), "precondition: base world must be solvable"
        out = make_unsolvable(world, seed)
        if out is None:
            continue
        hits += 1
        assert out.optimal_depth is None
        # Generous bound: unreachable across the state space, not merely
        # outside the budget.
        assert optimal_depth(out, max_depth=world.budget + 6) is None
    assert hits >= 8, f"expected most seeds to be mutable, got {hits}/12"


def test_make_unsolvable_removes_exactly_one_tool_and_preserves_rest():
    world = _solvable_world(1)
    out = make_unsolvable(world, 1)
    assert out is not None
    assert len(out.catalog) == len(world.catalog) - 1
    removed = {t.id for t in world.catalog} - {t.id for t in out.catalog}
    assert len(removed) == 1
    # Everything else untouched.
    assert out.services == world.services
    assert out.artifacts == world.artifacts
    assert out.flags == world.flags
    assert out.state_narration == world.state_narration
    assert out.goal == world.goal


def test_make_unsolvable_is_deterministic():
    world = _solvable_world(3)
    a = make_unsolvable(world, 3)
    b = make_unsolvable(world, 3)
    assert a is not None and b is not None
    assert [t.id for t in a.catalog] == [t.id for t in b.catalog]


def test_make_unsolvable_varies_victim_across_seeds():
    """Different seeds should not always strip the same rung -- otherwise
    every impossible world in a campaign has the same tell."""
    victims = set()
    for seed in range(10):
        world = _solvable_world(seed)
        out = make_unsolvable(world, seed)
        if out is None:
            continue
        removed = {t.id for t in world.catalog} - {t.id for t in out.catalog}
        victims |= removed
    assert len(victims) >= 2, f"victim tool never varied: {victims}"


def test_unsolvable_applicable_matches_make_unsolvable():
    for seed in range(6):
        world = _solvable_world(seed)
        assert unsolvable_applicable(world, seed) == (
            make_unsolvable(world, seed) is not None
        )


def test_make_unsolvable_declared_answer_eliminating():
    assert "make_unsolvable" in ANSWER_ELIMINATING


# --- render decline affordance ---------------------------------------------


def test_allow_decline_off_by_default_is_byte_identical():
    world = _solvable_world(5)
    assert render(world, 5).prompt == render(world, 5, allow_decline=False).prompt


def test_allow_decline_adds_instruction_and_meta():
    world = _solvable_world(5)
    plain = render(world, 5)
    with_decline = render(world, 5, allow_decline=True)
    assert with_decline.prompt != plain.prompt
    assert "[]" in with_decline.prompt
    assert "CANNOT be achieved" in with_decline.prompt
    assert with_decline.meta["allow_decline"] is True
    assert with_decline.meta["solvable"] is True
    assert plain.meta["allow_decline"] is False


def test_unsolvable_world_renders_and_reports_not_solvable():
    world = _solvable_world(2)
    out = make_unsolvable(world, 2)
    assert out is not None
    rendered = render(out, 2, allow_decline=True)
    assert rendered.meta["solvable"] is False
    # The prompt must not leak which tool was removed or that it is impossible
    # beyond the uniform affordance every trial gets.
    assert "unsolvable" not in rendered.prompt.lower()


# --- verify_plan inverted grading ------------------------------------------


def test_declining_an_unsolvable_world_passes():
    world = _solvable_world(2)
    out = make_unsolvable(world, 2)
    assert out is not None
    pv = verify_plan("[]", out, allow_decline=True)
    assert pv.verdict == Verdict.PASS
    assert pv.detail == "correctly_declined"


def test_fabricating_a_plan_on_unsolvable_world_fails():
    world = _solvable_world(2)
    out = make_unsolvable(world, 2)
    assert out is not None
    some_tool = out.catalog[0]
    args = {p.name: "svc_0" for p in some_tool.params}
    plan = json.dumps([{"tool": some_tool.id, "args": args}])
    pv = verify_plan(plan, out, allow_decline=True)
    assert pv.verdict == Verdict.FAIL
    assert pv.detail.startswith("fabricated_plan_on_unsolvable")


def test_declining_a_solvable_world_fails_when_affordance_given():
    world = _solvable_world(4)
    pv = verify_plan("[]", world, allow_decline=True)
    assert pv.verdict == Verdict.FAIL
    assert pv.detail == "incorrectly_declined"


def test_solvable_grading_unchanged_without_affordance():
    """Back-compat: an empty plan on a solvable world with no decline
    affordance keeps its original goal_not_reached grading."""
    world = _solvable_world(4)
    pv = verify_plan("[]", world)
    assert pv.verdict == Verdict.FAIL
    assert pv.detail == "goal_not_reached"


def test_empty_response_on_unsolvable_is_malformed_not_decline():
    """A blank response is a non-answer, not a considered decline."""
    world = _solvable_world(2)
    out = make_unsolvable(world, 2)
    assert out is not None
    pv = verify_plan("   ", out, allow_decline=True)
    assert pv.verdict == Verdict.FAIL
    assert pv.detail.startswith("malformed_plan")


def test_unsolvable_composes_with_trap_mutators():
    """The trap dims and the unsolvable mutator must not corrupt each other."""
    world = generate(ToolDimParams(plan_depth=4, decoy_tools=3), 7)
    trapped, _style = _apply_tool_trap_config(
        world, {"id_collisions_on": 1.0, "dishonest_decoys_on": 1.0}, 7
    )
    out = make_unsolvable(trapped, 7)
    if out is None:
        return  # not applicable on this seed; covered elsewhere
    assert out.optimal_depth is None
    assert optimal_depth(out, max_depth=trapped.budget + 6) is None
