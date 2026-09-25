"""Tests for the TOOL-CALL PLANNING province (cartographer.toolworld).

Covers: determinism, validate()'s solvability guarantee + optimal-depth
recording, the verifier catching each planted failure mode, foil divergence,
malformed-JSON handling, adversarial seed search determinism, and a zoo of
synthetic "models" with planted failure rules proving the verifier + foil
model recover ground truth. Mirrors the discipline of tests/test_world.py,
tests/test_foils.py, and tests/test_evaluator.py.
"""

from __future__ import annotations

import json
from collections import deque

import pytest

from cartographer.store.schema import Verdict
from cartographer.toolworld import (
    Call,
    ToolDimParams,
    foil_divergence,
    generate,
    measure,
    parse_plan,
    render,
    search_adversarial_seeds,
    verify_plan,
)
from cartographer.toolworld.simulate import _enumerate_calls, apply_call, optimal_depth, validate


# --- helpers --------------------------------------------------------------


def _solve(world):
    """BFS returning the actual optimal call sequence (ground-truth planner)."""
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


def _plan_json(plan):
    return json.dumps([{"tool": c.tool, "args": c.args} for c in plan])


_GRID = [
    ToolDimParams(plan_depth=pd, decoy_tools=dc, precondition_chain=ch,
                  state_obscurity=so, irreversibility=irr, parallel_goals=pg)
    for pd in (1, 3, 5)
    for dc in (0, 3)
    for ch in (0, 1, 2)
    for so in (0.0, 0.6)
    for irr in (False, True)
    for pg in (1, 2)
]


# --- determinism ----------------------------------------------------------


@pytest.mark.parametrize("params", _GRID[:24])
def test_generation_is_deterministic(params):
    w1 = generate(params, 777)
    w2 = generate(params, 777)
    assert [t.id for t in w1.catalog] == [t.id for t in w2.catalog]
    assert w1.optimal_depth == w2.optimal_depth
    assert w1.budget == w2.budget
    assert w1.initial_state == w2.initial_state
    assert w1.goal == w2.goal
    assert render(w1, 777).prompt == render(w2, 777).prompt


def test_different_seeds_give_different_worlds():
    p = ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1)
    prompts = {render(generate(p, s), s).prompt for s in range(8)}
    assert len(prompts) > 1


# --- validate() solvability guarantee -------------------------------------


@pytest.mark.parametrize("params", _GRID)
def test_validate_proves_solvable_and_records_optimal_depth(params):
    w = generate(params, 20240)
    assert validate(w) == []
    assert w.optimal_depth is not None
    assert w.optimal_depth >= 1
    assert w.budget >= w.optimal_depth
    # The recorded optimal depth matches an independent BFS solve.
    plan = _solve(w)
    assert plan is not None
    assert len(plan) == w.optimal_depth


def test_difficulty_knobs_raise_optimal_depth():
    easy = generate(ToolDimParams(plan_depth=1, precondition_chain=0, parallel_goals=1), 5)
    hard = generate(ToolDimParams(plan_depth=4, precondition_chain=2, parallel_goals=2), 5)
    assert hard.optimal_depth > easy.optimal_depth


# --- plan_depth no longer saturates at the ladder height -------------------
# Regression for the "plan_depth silently saturates at 4" bug (docs/lab-log.md):
# once plan_depth exceeds the 4-step SERVICE_LADDER, each extra unit used to
# realize the SAME optimal depth. The fix adds one strictly-chained required
# step per excess unit, so realized optimal depth grows ~1:1 with plan_depth.


def test_plan_depth_grows_optimal_depth_past_ladder_height():
    # optimal_depth must strictly increase for every plan_depth step past 4,
    # not plateau (the pre-fix behavior plateaued at 7 for pd >= 4).
    depths = {
        pd: generate(ToolDimParams(plan_depth=pd), 20240).optimal_depth
        for pd in (2, 4, 6, 8, 10, 12, 16)
    }
    # pd=4 is the ladder-height boundary; beyond it, each +2 in plan_depth adds
    # +2 required steps (one chained flag-set per excess unit).
    assert depths[4] == 7
    assert depths[6] == 9
    assert depths[8] == 11
    assert depths[10] == 13
    assert depths[12] == 15
    assert depths[16] == 19
    # Strictly monotonic past the ladder height (no saturation).
    beyond = [depths[pd] for pd in (4, 6, 8, 10, 12, 16)]
    assert beyond == sorted(beyond)
    assert len(set(beyond)) == len(beyond)


@pytest.mark.parametrize("pd", [6, 8, 10, 12, 16])
def test_excess_plan_depth_worlds_validate_across_seeds(pd):
    # Solvable via bounded BFS AND deterministic for plan_depth well past the
    # ladder height, over many seeds.
    for seed in range(30):
        w = generate(ToolDimParams(plan_depth=pd), seed)
        assert validate(w) == []
        assert w.optimal_depth is not None and w.optimal_depth >= 1
        assert w.budget >= w.optimal_depth
        w2 = generate(ToolDimParams(plan_depth=pd), seed)
        assert w.optimal_depth == w2.optimal_depth
        assert render(w, seed).prompt == render(w2, seed).prompt


_BACKCOMPAT_COMBOS = [
    dict(plan_depth=1),
    dict(plan_depth=2),
    dict(plan_depth=3),
    dict(plan_depth=4),
    dict(plan_depth=4, decoy_tools=3, precondition_chain=2),
    dict(plan_depth=3, decoy_tools=0, precondition_chain=1, parallel_goals=2,
         irreversibility=True),
    dict(plan_depth=2, precondition_chain=2, state_obscurity=0.6, parallel_goals=2),
    dict(plan_depth=4, precondition_chain=0, decoy_tools=2, irreversibility=True),
]


@pytest.mark.parametrize("combo", _BACKCOMPAT_COMBOS)
def test_plan_depth_le_ladder_height_is_a_strict_noop(combo):
    # Back-compat guard: for plan_depth <= 4 the excess-depth machinery must be
    # a strict no-op -- no extra flag entities and no extra tools vs. the same
    # world built with excess-depth structurally impossible. We assert the
    # generated world introduces NO `depth_flag_*` entities or `set_depth_flag_*`
    # tools, so past campaign records reproduce byte-for-byte from latent_params.
    for seed in (20240, 5, 777, 999):
        w = generate(ToolDimParams(**combo), seed)
        assert not any(f.id.startswith("depth_flag_") for f in w.flags)
        assert not any(t.id.startswith("set_depth_flag_") for t in w.catalog)


def test_trap_mutators_compose_at_high_plan_depth():
    # All five structural trap dims applied at plan_depth=10 must still leave a
    # world that validates as solvable (docs/lab-log.md trap-composition guard).
    from cartographer.exec.evaluator import _apply_tool_trap_config

    cfg = {
        "poison_traps": 1.0,
        "dishonest_decoys_on": 1.0,
        "implicit_preconditions_on": 1.0,
        "zero_slack_on": 1.0,
        "id_collisions_on": 1.0,
    }
    for seed in range(30):
        w = generate(
            ToolDimParams(plan_depth=10, precondition_chain=1, parallel_goals=2,
                          decoy_tools=2),
            seed,
        )
        mutated, style = _apply_tool_trap_config(w, cfg, seed)
        assert style in ("explicit", "implicit")
        assert validate(mutated) == []


# --- the verifier PASSES a correct plan (inc. inefficient) -----------------


@pytest.mark.parametrize("params", _GRID[:24])
def test_optimal_plan_passes(params):
    w = generate(params, 4242)
    pv = verify_plan(_plan_json(_solve(w)), w)
    assert pv.verdict == Verdict.PASS
    assert pv.detail == "goal_reached"
    assert pv.overhead == 0


def test_inefficient_but_correct_plan_passes_with_overhead():
    w = generate(ToolDimParams(plan_depth=3, decoy_tools=2), 4242)
    plan = _solve(w)
    decoy = next(t for t in w.catalog if t.decoy)
    plan2 = plan + [Call(tool=decoy.id, args={"service": w.services[0].id})]
    pv = verify_plan(_plan_json(plan2), w)
    assert pv.verdict == Verdict.PASS
    assert pv.overhead == 1


# --- the verifier catches each planted failure mode ------------------------


def test_precondition_violation_detected():
    # Jump straight to a late advance tool without climbing the ladder first.
    w = generate(ToolDimParams(plan_depth=4, precondition_chain=0), 11)
    late = next(t for t in w.catalog if "to 'deployed'" in t.description)
    pv = verify_plan(
        _plan_json([Call(tool=late.id, args={"service": w.services[0].id})]), w
    )
    assert pv.verdict == Verdict.FAIL
    assert pv.detail.startswith("precondition_violation")
    assert pv.failed_step == 0


def test_goal_not_reached_detected():
    w = generate(ToolDimParams(plan_depth=3, decoy_tools=1), 12)
    decoy = next(t for t in w.catalog if t.decoy)
    pv = verify_plan(
        _plan_json([Call(tool=decoy.id, args={"service": w.services[0].id})]), w
    )
    assert pv.verdict == Verdict.FAIL
    assert pv.detail == "goal_not_reached"


def test_empty_plan_fails_when_goal_unmet():
    w = generate(ToolDimParams(plan_depth=2), 13)
    pv = verify_plan("[]", w)
    assert pv.verdict == Verdict.FAIL
    assert pv.detail == "goal_not_reached"


def test_budget_exceeded_detected():
    w = generate(ToolDimParams(plan_depth=1, decoy_tools=1), 14)
    decoy = next(t for t in w.catalog if t.decoy)
    huge = [Call(tool=decoy.id, args={"service": w.services[0].id})] * (w.budget + 1)
    pv = verify_plan(_plan_json(huge), w)
    assert pv.verdict == Verdict.FAIL
    assert pv.detail.startswith("budget_exceeded")


# --- malformed JSON handling ----------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not json at all",
        "the plan is: provision then deploy",
        '{"tool": 5}',
        '[{"foo": 1}]',
        "[1, 2, 3]",
        '[{"tool": ""}]',
    ],
)
def test_malformed_plan_fails(bad):
    w = generate(ToolDimParams(plan_depth=2), 15)
    pv = verify_plan(bad, w)
    assert pv.verdict == Verdict.FAIL
    assert pv.detail.startswith("malformed_plan")


def test_fenced_and_prefixed_json_still_parses():
    w = generate(ToolDimParams(plan_depth=2), 16)
    plan = _solve(w)
    body = _plan_json(plan)
    for wrapped in (
        f"Here is my plan:\n```json\n{body}\n```\nDone.",
        f"Reasoning: first provision...\n{body}",
        f"```\n{body}\n```",
    ):
        assert verify_plan(wrapped, w).verdict == Verdict.PASS


def test_single_object_coerced_to_one_element_plan():
    w = generate(ToolDimParams(plan_depth=1, precondition_chain=0), 17)
    plan = _solve(w)
    assert len(plan) == 1
    single = json.dumps({"tool": plan[0].tool, "args": plan[0].args})
    assert verify_plan(single, w).verdict == Verdict.PASS


def test_parse_plan_missing_args_ok():
    plan = parse_plan('[{"tool": "advance_0"}]')
    assert plan == [Call(tool="advance_0", args={})]


# --- foils diverge exactly when they should --------------------------------


def test_foils_diverge_on_a_gated_trap_world():
    # A world requiring a real ladder climb + gate chain: the precondition-
    # ignoring foils must fail to reach the goal. (The narration_order foil,
    # which calls every offered tool once, can sometimes stumble into a valid
    # ordering -- that non-divergence is legitimate and covered by the zoo
    # bookkeeping test, so we only require the two greedy foils here.)
    w = generate(ToolDimParams(plan_depth=4, decoy_tools=2, precondition_chain=1), 999)
    prof = foil_divergence(w)
    assert "greedy_goal_tool" in prof.trap_kinds
    assert "decoy_bait" in prof.trap_kinds
    assert prof.n_diverging >= 2
    assert prof.divergence_score > 0.0


def test_greedy_foil_does_not_diverge_on_a_one_step_world():
    # plan_depth=1, no chain: the greedy "call the goal tool" foil is actually
    # correct, so it must NOT be counted as a diverging trap.
    w = generate(ToolDimParams(plan_depth=1, decoy_tools=0, precondition_chain=0,
                               irreversibility=False, parallel_goals=1), 3)
    prof = foil_divergence(w)
    assert "greedy_goal_tool" not in prof.trap_kinds


# --- adversarial seed search ----------------------------------------------


def test_adversarial_search_is_deterministic_and_picks_high_divergence():
    p = ToolDimParams(plan_depth=3, decoy_tools=3, precondition_chain=1)
    a = search_adversarial_seeds(p, base_seed=100, n_candidates=20, top_k=3)
    b = search_adversarial_seeds(p, base_seed=100, n_candidates=20, top_k=3)
    assert [s for s, _ in a] == [s for s, _ in b]
    # sorted descending by divergence score
    scores = [prof.divergence_score for _, prof in a]
    assert scores == sorted(scores, reverse=True)


def test_adversarial_search_require_kinds_filters():
    p = ToolDimParams(plan_depth=4, decoy_tools=3, precondition_chain=1)
    winners = search_adversarial_seeds(
        p, base_seed=7, n_candidates=20, top_k=5, require_kinds=("decoy_bait",)
    )
    for _, prof in winners:
        assert "decoy_bait" in prof.trap_kinds


# --- measure ---------------------------------------------------------------


def test_measure_recomputes_from_world_not_params():
    p = ToolDimParams(plan_depth=3, decoy_tools=4, precondition_chain=2,
                      state_obscurity=1.0, irreversibility=True, parallel_goals=2)
    w = generate(p, 88)
    m = measure(w)
    assert m["realized_plan_depth"] == float(w.optimal_depth)
    assert m["decoy_count"] == 4.0
    assert m["irreversibility"] == 1.0
    assert m["parallel_goals"] == 2.0
    assert 0.0 <= m["state_obscurity"] <= 1.0
    assert m["catalog_size"] == float(len(w.catalog))


# --- ZOO VALIDATION: synthetic models with planted failure rules -----------
#
# Each "model" is a deterministic function of the prompt/world that emits a
# plan; the planted rule dictates exactly which worlds it should fail on. We
# assert the verifier's PASS/FAIL recovers that ground truth.


def _perfect_model(world) -> str:
    return _plan_json(_solve(world))


def _greedy_model(world) -> str:
    """Always jumps straight to the goal-producing advance tool, ignoring
    preconditions -- the `greedy_goal_tool` foil. Must fail exactly the worlds
    where the goal isn't reachable in one such call (i.e. anything requiring a
    real climb)."""
    from cartographer.toolworld.foils import foil_greedy_goal_tool

    return _plan_json(foil_greedy_goal_tool(world))


def _decoy_model(world) -> str:
    """Always calls a decoy tool only -- never changes goal state, so it must
    FAIL every world (decoys are no-ops)."""
    decoy = next((t for t in world.catalog if t.decoy), None)
    if decoy is None:
        return "[]"
    return _plan_json([Call(tool=decoy.id, args={"service": world.services[0].id})])


def _malformed_model(world) -> str:
    return "I think you should provision the service and then deploy it."


ZOO_SEEDS = list(range(30))


def test_zoo_perfect_model_scores_100pct():
    for s in ZOO_SEEDS:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=2, precondition_chain=1), s)
        assert verify_plan(_perfect_model(w), w).verdict == Verdict.PASS


def test_zoo_decoy_model_fails_everything():
    for s in ZOO_SEEDS:
        w = generate(ToolDimParams(plan_depth=3, decoy_tools=2), s)
        pv = verify_plan(_decoy_model(w), w)
        assert pv.verdict == Verdict.FAIL
        # A decoy-only plan reaches no goal state.
        assert pv.detail in ("goal_not_reached",)


def test_zoo_greedy_model_fails_exactly_the_gated_worlds():
    """The precondition-ignoring model must FAIL every world whose goal needs
    more than the single greedy call, and PASS exactly the trivial 1-step
    worlds where the greedy call happens to be the whole solution."""
    for s in ZOO_SEEDS:
        deep = generate(ToolDimParams(plan_depth=4, precondition_chain=1), s)
        assert verify_plan(_greedy_model(deep), deep).verdict == Verdict.FAIL

        trivial = generate(
            ToolDimParams(plan_depth=1, decoy_tools=0, precondition_chain=0,
                          irreversibility=False, parallel_goals=1),
            s,
        )
        # optimal depth 1: the greedy goal-tool call IS the solution.
        assert trivial.optimal_depth == 1
        assert verify_plan(_greedy_model(trivial), trivial).verdict == Verdict.PASS


def test_zoo_malformed_model_always_malformed_fail():
    for s in ZOO_SEEDS:
        w = generate(ToolDimParams(plan_depth=2), s)
        pv = verify_plan(_malformed_model(w), w)
        assert pv.verdict == Verdict.FAIL
        assert pv.detail.startswith("malformed_plan")


# --- campaign wiring (end-to-end, no network) ------------------------------


class _EmptyPlanClient:
    """Fake client that always returns an empty JSON plan -> deterministic
    FAIL (goal_not_reached). Zero network."""

    def __init__(self):
        self.calls = 0

    def chat(self, model, prompt, max_tokens=1500, max_tokens_retry=4000, **kwargs):
        from cartographer.exec.client import ChatResult

        self.calls += 1
        return ChatResult(text="[]", tokens_in=10, tokens_out=2, latency_ms=1.0,
                          finish_reason="stop")


class _SolvingClient:
    """Fake client that reconstructs the exact world from the prompt-embedded
    config is not possible; instead it solves whatever world the evaluator
    hands it by re-deriving it from (config, trial_seed). We cheat by having
    the evaluator's world reachable: this client is only used via the direct
    evaluator-call test where we pass the world in through a closure."""

    def __init__(self, plan_json_for_prompt):
        self._fn = plan_json_for_prompt
        self.calls = 0

    def chat(self, model, prompt, max_tokens=1500, max_tokens_retry=4000, **kwargs):
        from cartographer.exec.client import ChatResult

        self.calls += 1
        return ChatResult(text=self._fn(prompt), tokens_in=10, tokens_out=8,
                          latency_ms=1.0, finish_reason="stop")


def test_run_tool_planning_campaign_persists_kind_tool_plan(tmp_path):
    from cartographer.exec.evaluator import run_tool_planning_campaign
    from cartographer.store.store import ResultsStore

    db = str(tmp_path / "camp.sqlite")
    store = ResultsStore(db)
    try:
        client = _EmptyPlanClient()
        state = run_tool_planning_campaign(
            client, store, run_id="camp-tool-001", model="fake/Model",
            budget=30, cost_cap=1.0, parallelism=1, verify="manual",
        )
        records = store.by_run("camp-tool-001")
        assert records
        for r in records:
            assert "kind=tool_plan" in (r.verifier_detail or "")
        # empty-plan model fails everything with a real goal
        assert all(r.verdict == Verdict.FAIL for r in records if r.prompt_text)
        # realized dims present and recomputed
        scored = [r for r in records if r.realized_dims]
        assert scored
        assert "realized_plan_depth" in scored[0].realized_dims
    finally:
        store.close()


def test_tool_planning_evaluator_passes_a_solved_world(tmp_path):
    from cartographer.exec.evaluator import ToolPlanningEvaluator, _apply_tool_config, _task_seed
    from cartographer.store.store import ResultsStore

    db = str(tmp_path / "eval.sqlite")
    store = ResultsStore(db)
    try:
        config = {"plan_depth": 3.0, "decoy_tools": 2.0, "precondition_chain": 1.0,
                  "state_obscurity": 0.0, "parallel_goals": 1.0}
        trial_seed = 5
        # Reconstruct exactly what the evaluator will build, and solve it.
        from cartographer.toolworld.generate import ToolDimParams as _TDP
        params = _apply_tool_config(_TDP(), config)
        seed = _task_seed(config, trial_seed)
        world = generate(params, seed)
        target_plan = _plan_json(_solve(world))

        client = _SolvingClient(lambda prompt: target_plan)
        ev = ToolPlanningEvaluator(
            client, "fake/Model", store, run_id="eval-tool-001", cost_cap_usd=1.0,
        )
        passed = ev(config, trial_seed)
        assert passed is True
        rec = store.by_run("eval-tool-001")[0]
        assert rec.verdict == Verdict.PASS
        assert "kind=tool_plan" in rec.verifier_detail
        assert "optimal_depth=" in rec.verifier_detail
        assert rec.realized_dims["plan_overhead"] == 0.0
    finally:
        store.close()


def test_findings_capability_kinds_includes_tool_plan():
    from cartographer.viewer.findings import CAPABILITY_KINDS

    assert "tool_plan" in CAPABILITY_KINDS


def test_atlas_and_findings_pick_up_tool_plan_kind():
    from cartographer.store.schema import ExperimentRecord, RenderMode
    from cartographer.viewer import atlas as atlas_mod
    from cartographer.viewer import findings as findings_mod

    rec = ExperimentRecord(
        run_id="r", model_name="m", latent_params={}, realized_dims={},
        render_mode=RenderMode.TEMPLATE, task_seed=0, trial_seed=0,
        verdict=Verdict.PASS, verifier_detail="kind=tool_plan;campaign=1",
        tokens_in=1, tokens_out=1, cost_usd=0.0, latency_ms=1.0,
    )
    assert atlas_mod._query_kind_of(rec) == "tool_plan"
    assert findings_mod._query_kind_of(rec) == "tool_plan"


def test_zoo_foil_model_recovers_ground_truth():
    """The foil model's `n_diverging` must equal the number of the three
    named shallow strategies that actually fail verification on that world --
    i.e. the foil bookkeeping matches independent verification."""
    from cartographer.toolworld.foils import foil_plans

    for s in ZOO_SEEDS:
        w = generate(ToolDimParams(plan_depth=4, decoy_tools=2, precondition_chain=1), s)
        prof = foil_divergence(w)
        indep_fail = 0
        for name, plan in foil_plans(w).items():
            if verify_plan(_plan_json(plan), w).verdict != Verdict.PASS:
                indep_fail += 1
        assert prof.n_diverging == indep_fail
