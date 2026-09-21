"""Foil model for the tool-planning province: what a *shallow planner* would
emit, versus a plan that actually reaches the goal.

Motivation mirrors `world/foils.py`: the scalar difficulty knobs scale
magnitude, but what actually traps a weak planner is the presence of a
plausible-but-wrong shortcut. This module makes "does this world admit a
tempting shallow plan that FAILS" a measurable property of a generated
`ToolWorld`, with zero API calls -- so adversarial seed search (see
`search.py`) can select trap-dense worlds for free.

Named foils (all pure functions of the world):

  * `greedy_goal_tool`  -- call the single tool whose name/description most
    resembles the goal, ignoring preconditions. (e.g. jump straight to the
    tool that produces the goal ladder state without climbing to it.)
  * `narration_order`   -- call the offered tools in catalog order, once each,
    with a default arg binding. (Trusts the order tools are listed.)
  * `decoy_bait`        -- lead with the highest-surface-similarity decoy tool,
    then the greedy goal tool. (Falls for the plausible distractor.)

A foil "diverges" when its plan FAILS verification (i.e. it does NOT reach the
goal). A world where every foil reaches the goal anyway is trivially easy; a
world where all foils fail is a genuine trap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Call, GoalClause, Tool, ToolWorld
from .simulate import simulate_plan
from .verify import verify_plan
from ..store.schema import Verdict


def _primary_service_goal(world: ToolWorld) -> GoalClause | None:
    for c in world.goal.clauses:
        if c.target_kind == "service":
            return c
    return None


def _default_service_arg(world: ToolWorld, clause: GoalClause | None) -> str:
    if clause is not None and clause.target_kind == "service":
        return clause.target_id
    return world.services[0].id if world.services else "svc_0"


def _tool_arg_for(world: ToolWorld, tool: Tool, target_service: str) -> dict[str, str]:
    """A default arg binding for `tool`: bind service params to the target
    service, artifact/flag params to the first entity of that kind."""
    args: dict[str, str] = {}
    for p in tool.params:
        if p.kind == "service":
            args[p.name] = target_service
        elif p.kind == "artifact":
            args[p.name] = world.artifacts[0].id if world.artifacts else "art_0"
        elif p.kind == "flag":
            args[p.name] = world.flags[0].id if world.flags else "flag_0"
    return args


def foil_greedy_goal_tool(world: ToolWorld) -> list[Call]:
    """Call the advance tool that produces the goal ladder state directly,
    ignoring that the service must be climbed to the prior rung first."""
    clause = _primary_service_goal(world)
    target = _default_service_arg(world, clause)
    goal_state = clause.value if clause is not None else None

    # Find the advance tool whose `to_state` == goal_state by description match.
    best: Tool | None = None
    for tool in world.catalog:
        if tool.decoy or tool.destructive:
            continue
        if goal_state is not None and f"to '{goal_state}'" in tool.description:
            best = tool
            break
    if best is None:
        # fall back to the first non-decoy advance tool
        for tool in world.catalog:
            if not tool.decoy and tool.name == "provision":
                best = tool
                break
    if best is None:
        return []
    return [Call(tool=best.id, args=_tool_arg_for(world, best, target))]


def foil_narration_order(world: ToolWorld) -> list[Call]:
    """Use the tools in the order they're listed in the catalog, once each,
    with the default arg binding -- the 'read top to bottom' planner."""
    clause = _primary_service_goal(world)
    target = _default_service_arg(world, clause)
    return [
        Call(tool=tool.id, args=_tool_arg_for(world, tool, target))
        for tool in world.catalog
    ]


def foil_decoy_bait(world: ToolWorld) -> list[Call]:
    """Lead with the highest-surface-similarity decoy, then the greedy goal
    tool -- the planner baited by the plausible distractor."""
    clause = _primary_service_goal(world)
    target = _default_service_arg(world, clause)
    decoys = [t for t in world.catalog if t.decoy]
    plan: list[Call] = []
    if decoys:
        d = decoys[0]
        plan.append(Call(tool=d.id, args=_tool_arg_for(world, d, target)))
    plan.extend(foil_greedy_goal_tool(world))
    return plan


def foil_greedy_lookahead0(world: ToolWorld, max_steps: int = 64) -> list[Call]:
    """Simulate the literal greedy policy with zero lookahead: repeatedly
    call the FIRST currently-valid non-decoy tool by catalog order (default
    arg binding toward the primary service goal), applying its effect, until
    either the goal is satisfied or no non-decoy tool is currently valid (or
    `max_steps` is hit). This is the trap-salience foil for the structural
    mutators (`poison_ladder`, `interleaved_goals`): a planner that commits
    to "any currently-valid advance is safe" without checking whether it
    forecloses later steps. On an un-mutated world this greedy policy is
    exactly the safe strategy the province's generator guarantees (every
    state's valid tools only ever advance toward the goal), so it should
    reach the goal; on poison_ladder/interleaved_goals worlds it should not.
    """
    clause = _primary_service_goal(world)
    target = _default_service_arg(world, clause)

    state = world.initial_state
    plan: list[Call] = []
    for _ in range(max_steps):
        if world.goal.satisfied(state):
            break
        fired = False
        for tool in world.catalog:
            if tool.decoy:
                continue
            args = _tool_arg_for(world, tool, target)
            if not all(p.name in args for p in tool.params):
                continue
            if not tool.preconditions(state, args):
                continue
            state = tool.effect(state, args)
            plan.append(Call(tool=tool.id, args=args))
            fired = True
            break
        if not fired:
            break
    return plan


FOILS = {
    "greedy_goal_tool": foil_greedy_goal_tool,
    "narration_order": foil_narration_order,
    "decoy_bait": foil_decoy_bait,
    "greedy_lookahead0": foil_greedy_lookahead0,
}


def foil_plans(world: ToolWorld) -> dict[str, list[Call]]:
    return {name: fn(world) for name, fn in FOILS.items()}


@dataclass(frozen=True)
class ToolFoilProfile:
    """Summary of how much this world traps a shallow planner. `n_diverging`
    counts foils whose plan FAILS to reach the goal; a world with
    `n_diverging == 0` is trivially easy (every shortcut works). `trap_kinds`
    names the diverging foils."""

    optimal_depth: int
    n_diverging: int = 0
    divergence_score: float = 0.0
    trap_kinds: tuple[str, ...] = ()
    foil_details: dict[str, str] = field(default_factory=dict)


def foil_divergence(world: ToolWorld) -> ToolFoilProfile:
    """A foil diverges when its plan does NOT reach the goal. `divergence_score`
    weights each diverging foil by how early it goes wrong relative to the
    optimal depth (a foil that fails on its very first call is a more salient
    trap than one that only fails at the end), normalized to 0..1 per foil."""
    plans = foil_plans(world)
    optimal = world.optimal_depth if world.optimal_depth is not None else 0

    diverging: list[str] = []
    score = 0.0
    details: dict[str, str] = {}
    for name, plan in plans.items():
        pv = verify_plan(_plan_to_json(plan), world)
        details[name] = pv.detail
        if pv.verdict != Verdict.PASS:
            diverging.append(name)
            # earliness: 1.0 if it fails on the first call, decaying toward 0
            # as the failing step approaches the optimal depth.
            if pv.failed_step is not None and optimal > 0:
                earliness = 1.0 - (pv.failed_step / max(optimal, 1))
                score += max(0.0, min(1.0, earliness))
            else:
                score += 0.5  # goal_not_reached / malformed: moderate salience

    return ToolFoilProfile(
        optimal_depth=optimal,
        n_diverging=len(diverging),
        divergence_score=score,
        trap_kinds=tuple(sorted(diverging)),
        foil_details=details,
    )


def _plan_to_json(plan: list[Call]) -> str:
    import json

    return json.dumps([{"tool": c.tool, "args": c.args} for c in plan])
