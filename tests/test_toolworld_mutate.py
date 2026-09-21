"""Tests for `cartographer.toolworld.mutate` -- the tool-planning province's
structural trap mutators -- plus the `greedy_lookahead0` foil (Task 2) and
the planted-planner zoo validation (Task 4). Mirrors the discipline of
`tests/test_toolworld.py` and `tests/test_world_mutate.py`: determinism,
ANSWER_PRESERVING invariants across many seeds, and synthetic "models" with
planted failure rules proving the verifier recovers ground truth. Zero live
API calls anywhere in this file.
"""

from __future__ import annotations

import json
from collections import deque

from cartographer.store.schema import Verdict
from cartographer.toolworld.foils import foil_greedy_lookahead0
from cartographer.toolworld.generate import ToolDimParams, generate
from cartographer.toolworld.model import Call, ToolWorld
from cartographer.toolworld.mutate import (
    ANSWER_CHANGING,
    ANSWER_PRESERVING,
    dishonest_decoys,
    id_collision,
    implicit_preconditions,
    interleaved_goals,
    interleaved_goals_applicable,
    poison_ladder,
    zero_slack,
)
from cartographer.toolworld.render import render
from cartographer.toolworld.simulate import _enumerate_calls, apply_call, validate
from cartographer.toolworld.verify import verify_plan

SEEDS_20 = list(range(20))


def _plan_json(plan: list[Call]) -> str:
    return json.dumps([{"tool": c.tool, "args": c.args} for c in plan])


def _solve(world: ToolWorld) -> list[Call] | None:
    """Bounded BFS ground-truth optimal planner (mirrors test_toolworld.py's
    `_solve`)."""
    if world.goal.satisfied(world.initial_state):
        return []
    calls = _enumerate_calls(world)
    visited = {world.initial_state}
    frontier = deque([(world.initial_state, [])])
    while frontier:
        st, path = frontier.popleft()
        for c in calls:
            step = apply_call(world, st, c)
            if not step.ok or step.state in visited:
                continue
            npath = path + [c]
            if world.goal.satisfied(step.state):
                return npath
            visited.add(step.state)
            frontier.append((step.state, npath))
    return None


# ---------------------------------------------------------------------------
# Basic sanity / determinism per mutator
# ---------------------------------------------------------------------------


def test_mutator_registry_lists_all_six():
    assert ANSWER_PRESERVING == {"dishonest_decoys", "implicit_preconditions", "id_collision"}
    assert ANSWER_CHANGING == {"poison_ladder", "zero_slack", "interleaved_goals"}


def test_poison_ladder_deterministic_and_solvable():
    for s in SEEDS_20:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=1, precondition_chain=1), s)
        a = poison_ladder(w, s)
        b = poison_ladder(w, s)
        assert [t.id for t in a.catalog] == [t.id for t in b.catalog]
        assert not validate(a)
        assert any(t.id == "poison_trap" for t in a.catalog)


def test_dishonest_decoys_preserves_optimal_depth_and_strips_confession():
    for s in SEEDS_20:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=3, precondition_chain=1), s)
        dw = dishonest_decoys(w, s)
        assert dw.optimal_depth == w.optimal_depth
        for t in dw.catalog:
            if t.decoy:
                assert "Has no effect" not in t.description
                assert t.effect(w.initial_state, {}) == w.initial_state


def test_implicit_preconditions_render_has_zero_precondition_sentences():
    for s in SEEDS_20[:5]:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1), s)
        ip = implicit_preconditions(w, s)
        assert ip.optimal_depth == w.optimal_depth
        rendered = render(w, s, style="implicit")
        assert "Precondition:" not in rendered.prompt
        explicit = render(w, s, style="explicit")
        assert "Precondition:" in explicit.prompt


def test_zero_slack_tightens_budget_to_optimal():
    for s in SEEDS_20:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1), s)
        zw = zero_slack(w, s)
        assert zw.budget == zw.optimal_depth
        assert not validate(zw)


def test_id_collision_preserves_optimal_depth_and_ids_are_unique():
    for s in SEEDS_20:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1), s)
        iw = id_collision(w, s)
        assert iw.optimal_depth == w.optimal_depth
        ids = [t.id for t in iw.catalog]
        assert len(ids) == len(set(ids))
        names = [t.name for t in iw.catalog]
        assert names == [t.name for t in w.catalog]


def test_interleaved_goals_only_applies_at_parallel_goals_2():
    w1 = generate(ToolDimParams(plan_depth=3, decoy_tools=1, parallel_goals=1), 3)
    assert not interleaved_goals_applicable(w1)
    out1 = interleaved_goals(w1, 3)
    assert out1 is w1

    w2 = generate(ToolDimParams(plan_depth=3, decoy_tools=1, precondition_chain=0, parallel_goals=2), 3)
    assert interleaved_goals_applicable(w2)
    out2 = interleaved_goals(w2, 3)
    assert out2 is not w2
    assert not validate(out2)


def test_interleaved_goals_deterministic():
    w = generate(ToolDimParams(plan_depth=3, decoy_tools=1, precondition_chain=0, parallel_goals=2), 11)
    a = interleaved_goals(w, 11)
    b = interleaved_goals(w, 11)
    assert [t.id for t in a.catalog] == [t.id for t in b.catalog]
    assert a.optimal_depth == b.optimal_depth


# ---------------------------------------------------------------------------
# Task 2: greedy_lookahead0 foil -- trap-salience signal
# ---------------------------------------------------------------------------


def test_greedy_lookahead0_passes_unmutated_worlds():
    for s in SEEDS_20:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1), s)
        plan = foil_greedy_lookahead0(w)
        assert verify_plan(_plan_json(plan), w).verdict == Verdict.PASS


def test_greedy_lookahead0_fails_on_some_poison_ladder_worlds():
    n_fail = 0
    for s in range(30):
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=1, precondition_chain=1), s)
        pw = poison_ladder(w, s)
        plan = foil_greedy_lookahead0(pw)
        if verify_plan(_plan_json(plan), pw).verdict != Verdict.PASS:
            n_fail += 1
    assert n_fail > 0, "greedy_lookahead0 never fell into the poison_ladder trap"


def test_greedy_lookahead0_fails_on_interleaved_goals_worlds():
    n_app = 0
    n_fail = 0
    for s in range(20):
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=1, precondition_chain=0, parallel_goals=2), s)
        iw = interleaved_goals(w, s)
        if iw is w:
            continue
        n_app += 1
        plan = foil_greedy_lookahead0(iw)
        if verify_plan(_plan_json(plan), iw).verdict != Verdict.PASS:
            n_fail += 1
    assert n_app > 0
    assert n_fail == n_app, "greedy_lookahead0 should fail EVERY interleaved_goals world"


def test_greedy_lookahead0_wired_into_foil_divergence():
    from cartographer.toolworld.foils import FOILS, foil_divergence

    assert "greedy_lookahead0" in FOILS
    w = generate(ToolDimParams(plan_depth=3, decoy_tools=1, precondition_chain=1), 5)
    pw = poison_ladder(w, 5)
    profile = foil_divergence(pw)
    assert "greedy_lookahead0" in profile.foil_details


# ---------------------------------------------------------------------------
# Task 4: zoo validation -- planted fake planners
# ---------------------------------------------------------------------------


class GreedyPlanner:
    """Lookahead-0 planner: repeatedly calls the first currently-valid
    non-decoy tool. Must PASS un-mutated worlds; FAIL poison_ladder and
    interleaved_goals worlds."""

    def plan(self, world: ToolWorld) -> str:
        return _plan_json(foil_greedy_lookahead0(world))


class PerfectPlanner:
    """Bounded-BFS planner (reuses `_solve`). Must PASS every mutated-but-
    solvable world, including zero_slack (it finds the exact optimum)."""

    def plan(self, world: ToolWorld) -> str:
        solution = _solve(world)
        assert solution is not None
        return _plan_json(solution)


class VerbosePlanner:
    """Perfect plan plus 2 redundant no-op decoy calls appended. PASSes
    normal worlds (decoys don't change state, and there's slack in the
    budget); FAILs zero_slack worlds with `budget_exceeded_plan` (no slack
    for the two extra calls)."""

    def plan(self, world: ToolWorld) -> str:
        solution = _solve(world)
        assert solution is not None
        decoy = next((t for t in world.catalog if t.decoy), None)
        extra: list[Call] = []
        if decoy is not None:
            svc = world.services[0].id if world.services else "svc_0"
            args = {p.name: svc for p in decoy.params if p.kind == "service"}
            extra = [Call(tool=decoy.id, args=args), Call(tool=decoy.id, args=args)]
        else:
            # No decoy offered in this world -- fall back to repeating the
            # first solution call twice (still redundant length, since the
            # verifier evaluates length against budget regardless of the
            # specific padding tool).
            extra = [solution[0], solution[0]] if solution else []
        return _plan_json(solution + extra)


class NameCaller:
    """Uses tool NAMES instead of ids in the `tool` field of each call.
    FAILs id_collision worlds with `unknown_tool` (names != the mangled
    ids the verifier expects)."""

    def plan(self, world: ToolWorld) -> str:
        solution = _solve(world)
        assert solution is not None
        by_id = {t.id: t for t in world.catalog}
        renamed = [Call(tool=by_id[c.tool].name, args=c.args) for c in solution]
        return _plan_json(renamed)


def test_zoo_greedy_planner_passes_normal_fails_poison_and_interleaved():
    for s in SEEDS_20:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1), s)
        assert verify_plan(GreedyPlanner().plan(w), w).verdict == Verdict.PASS

    n_poison_fail = 0
    for s in range(30):
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=1, precondition_chain=1), s)
        pw = poison_ladder(w, s)
        if verify_plan(GreedyPlanner().plan(pw), pw).verdict != Verdict.PASS:
            n_poison_fail += 1
    assert n_poison_fail > 0

    n_inter_fail = 0
    n_inter_app = 0
    for s in range(20):
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=1, precondition_chain=0, parallel_goals=2), s)
        iw = interleaved_goals(w, s)
        if iw is w:
            continue
        n_inter_app += 1
        if verify_plan(GreedyPlanner().plan(iw), iw).verdict != Verdict.PASS:
            n_inter_fail += 1
    assert n_inter_app > 0
    assert n_inter_fail == n_inter_app


def test_zoo_perfect_planner_passes_every_mutated_solvable_world():
    mutators = [
        lambda w, s: poison_ladder(w, s),
        lambda w, s: dishonest_decoys(w, s),
        lambda w, s: id_collision(w, s),
        lambda w, s: zero_slack(w, s),
    ]
    for s in SEEDS_20[:10]:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1), s)
        for mutate in mutators:
            mw = mutate(w, s)
            assert verify_plan(PerfectPlanner().plan(mw), mw).verdict == Verdict.PASS

    w2 = generate(ToolDimParams(plan_depth=3, decoy_tools=1, precondition_chain=0, parallel_goals=2), 3)
    iw2 = interleaved_goals(w2, 3)
    assert iw2 is not w2
    assert verify_plan(PerfectPlanner().plan(iw2), iw2).verdict == Verdict.PASS


def test_zoo_verbose_planner_passes_normal_fails_zero_slack():
    for s in SEEDS_20[:10]:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1), s)
        assert verify_plan(VerbosePlanner().plan(w), w).verdict == Verdict.PASS

        zw = zero_slack(w, s)
        pv = verify_plan(VerbosePlanner().plan(zw), zw)
        assert pv.verdict == Verdict.FAIL
        assert pv.detail == "budget_exceeded_plan"


def test_zoo_namecaller_fails_id_collision_with_unknown_tool():
    for s in SEEDS_20[:10]:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1), s)
        iw = id_collision(w, s)
        pv = verify_plan(NameCaller().plan(iw), iw)
        assert pv.verdict == Verdict.FAIL
        assert pv.detail.startswith("unknown_tool")


# ---------------------------------------------------------------------------
# Full 613-plus regression sentinel: verify.py's additive zero_slack branch
# does not perturb ordinary (non-zero-slack) budget-exceeded worlds.
# ---------------------------------------------------------------------------


def test_ordinary_budget_exceeded_detail_unchanged():
    w = generate(ToolDimParams(plan_depth=1, decoy_tools=1), 14)
    decoy = next(t for t in w.catalog if t.decoy)
    huge = [Call(tool=decoy.id, args={"service": w.services[0].id})] * (w.budget + 1)
    pv = verify_plan(_plan_json(huge), w)
    assert pv.verdict == Verdict.FAIL
    assert pv.detail.startswith("budget_exceeded:")
    assert pv.detail != "budget_exceeded_plan"


# ---------------------------------------------------------------------------
# Provenance-bug regression: ToolBreakingPointEvaluator must persist the five
# trap 0/1 dims IN latent_params/realized_dims proper, not just inside the
# verifier_detail string. See docs/lab-log.md "Provenance bug found during
# verification" entry -- a live resample round was wasted (~$0.15 of Ultra
# calls) because reconstructing a world from latent_params alone silently
# yielded the UN-mutated world when the trap dims lived only in
# verifier_detail. This test builds a record with traps on, then
# reconstructs the world from latent_params ALONE (mirroring exactly what a
# downstream /atlas or /findings resample tool would do) and asserts the
# re-rendered prompt is byte-identical to the one actually sent to the
# model. Against the pre-fix code (latent_params missing the five trap
# keys), the reconstructed world has no traps applied and this assertion
# fails.
#
# NOTE: existing records in camp-tool_bp-super-001 / camp-tool_bp-ultra-001
# predate this fix and still need the verifier_detail fallback for trap
# provenance -- do NOT migrate/rewrite them (the store is append-only).
# ---------------------------------------------------------------------------


class _FixedPlanClient:
    """Fake client that always returns a fixed plan JSON. Zero network."""

    def __init__(self, text="[]"):
        self._text = text
        self.calls = 0

    def chat(self, model, prompt, max_tokens=1500, max_tokens_retry=4000, **kwargs):
        from cartographer.exec.client import ChatResult

        self.calls += 1
        return ChatResult(text=self._text, tokens_in=10, tokens_out=2, latency_ms=1.0,
                          finish_reason="stop")


def test_tool_breaking_point_evaluator_latent_params_carry_trap_dims(tmp_path):
    """The regression test for the provenance bug: reconstruct the world
    from a persisted record's latent_params ALONE and check the re-rendered
    prompt matches byte-for-byte."""
    from cartographer.exec.evaluator import (
        ToolBreakingPointEvaluator,
        ToolDimParams,
        _apply_tool_trap_config,
        _default_tool_base,
    )
    from cartographer.toolworld.generate import generate as tool_generate
    from cartographer.toolworld.render import render as tool_render
    from cartographer.store.store import ResultsStore

    store = ResultsStore(str(tmp_path / "camp.sqlite"))
    client = _FixedPlanClient()
    evaluator = ToolBreakingPointEvaluator(
        client=client,
        model="fake/Model",
        store=store,
        run_id="camp-tool-bp-test-001",
        cost_cap_usd=10.0,
    )

    config = {
        "poison_traps": 1.0,
        "dishonest_decoys_on": 1.0,
        "implicit_preconditions_on": 1.0,
        "zero_slack_on": 1.0,
        "id_collisions_on": 1.0,
        "precondition_chain": 2.0,
        "parallel_goals": 2.0,
    }
    trial_seed = 7

    evaluator(config, trial_seed)

    records = store.by_run("camp-tool-bp-test-001")
    assert len(records) == 1
    record = records[0]

    # The whole point: every trap dim must be present in latent_params
    # (not just buried in verifier_detail).
    trap_keys = {
        "poison_traps", "dishonest_decoys_on", "implicit_preconditions_on",
        "zero_slack_on", "id_collisions_on",
    }
    assert trap_keys <= set(record.latent_params.keys())
    for k in trap_keys:
        assert record.latent_params[k] == 1.0
    # Also surfaced in realized_dims so /atlas and /findings can bucket on
    # them like any other dim.
    assert trap_keys <= set(record.realized_dims.keys())
    for k in trap_keys:
        assert record.realized_dims[k] == 1.0

    # Reconstruct the world from latent_params ALONE (as a downstream
    # resample tool would): scalars feed ToolDimParams, latent_params
    # itself is the trap config for _apply_tool_trap_config.
    base = _default_tool_base()
    recon_params = ToolDimParams(
        plan_depth=int(record.latent_params["plan_depth"]),
        decoy_tools=int(record.latent_params["decoy_tools"]),
        precondition_chain=int(record.latent_params["precondition_chain"]),
        state_obscurity=record.latent_params["state_obscurity"],
        irreversibility=bool(record.latent_params["irreversibility"]),
        parallel_goals=int(record.latent_params["parallel_goals"]),
    )
    assert recon_params == ToolDimParams(
        plan_depth=base.plan_depth,
        decoy_tools=base.decoy_tools,
        precondition_chain=2,
        state_obscurity=base.state_obscurity,
        irreversibility=base.irreversibility,
        parallel_goals=2,
    )

    recon_world = tool_generate(recon_params, record.task_seed)
    recon_world, recon_style = _apply_tool_trap_config(
        recon_world, record.latent_params, record.task_seed
    )
    recon_rendered = tool_render(recon_world, record.task_seed, style=recon_style)

    assert recon_rendered.prompt == record.prompt_text
