"""Forward simulation + bounded solvability search over a `ToolWorld`.

Ground truth is COMPUTED, never authored:

  * `apply_call` fires one `Call` against a `State` (respecting the tool's
    preconditions) and returns the successor state, or a violation reason.
  * `simulate_plan` walks a whole plan, returning the terminal state and the
    first precondition violation (if any).
  * `optimal_depth` runs a bounded breadth-first search over the reachable
    state space to find the shortest sequence of valid tool calls that
    satisfies the goal, within `max_depth`. This is what `validate()` uses to
    prove a generated world is solvable and to record the true optimal depth.

The state space is fully enumerated (finite ladders + booleans) and every
tool's precondition/effect is a total function, so BFS is exhaustive and
deterministic -- no randomness, no LLM, mirroring `world/simulate.py`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import product

from .model import Call, State, Tool, ToolWorld


@dataclass(frozen=True)
class StepResult:
    ok: bool
    state: State
    detail: str = ""


def _tool_by_id(world: ToolWorld, tool_id: str) -> Tool | None:
    for t in world.catalog:
        if t.id == tool_id:
            return t
    return None


def apply_call(world: ToolWorld, state: State, call: Call) -> StepResult:
    """Fire one call against `state`. Returns ok=False with a detail string on
    an unknown tool, a bad arg binding, or an unmet precondition; otherwise
    ok=True with the successor state."""
    tool = _tool_by_id(world, call.tool)
    if tool is None:
        return StepResult(False, state, detail=f"unknown_tool:{call.tool}")

    # Validate the arg binding: every declared param must be present and bound
    # to an entity of the right family.
    valid_ids = _valid_ids_by_kind(world)
    for param in tool.params:
        if param.name not in call.args:
            return StepResult(False, state, detail=f"missing_arg:{tool.id}.{param.name}")
        bound = call.args[param.name]
        if bound not in valid_ids[param.kind]:
            return StepResult(
                False, state, detail=f"bad_arg:{tool.id}.{param.name}={bound}"
            )

    if not tool.preconditions(state, call.args):
        return StepResult(
            False, state, detail=f"precondition_violation:{tool.id}"
        )
    return StepResult(True, tool.effect(state, call.args), detail="")


def _valid_ids_by_kind(world: ToolWorld) -> dict[str, set[str]]:
    return {
        "service": {s.id for s in world.services},
        "artifact": {a.id for a in world.artifacts},
        "flag": {f.id for f in world.flags},
    }


@dataclass(frozen=True)
class PlanResult:
    reached_goal: bool
    terminal_state: State
    failed_step: int | None  # index of the first invalid call, or None
    detail: str  # "" on clean run; else violation detail on the failed step
    n_calls: int


def simulate_plan(world: ToolWorld, plan: list[Call]) -> PlanResult:
    """Walk `plan` step by step from `world.initial_state`. Stops at the first
    invalid call (unmet precondition / bad binding / unknown tool). Whether the
    goal is reached is evaluated on the terminal state actually reached."""
    state = world.initial_state
    for i, call in enumerate(plan):
        step = apply_call(world, state, call)
        if not step.ok:
            return PlanResult(
                reached_goal=world.goal.satisfied(state),
                terminal_state=state,
                failed_step=i,
                detail=step.detail,
                n_calls=len(plan),
            )
        state = step.state
    return PlanResult(
        reached_goal=world.goal.satisfied(state),
        terminal_state=state,
        failed_step=None,
        detail="",
        n_calls=len(plan),
    )


def _enumerate_calls(world: ToolWorld) -> list[Call]:
    """Every syntactically-valid (tool, arg-binding) pair over the world's
    entities. Deterministic order: catalog order, then sorted arg products."""
    ids = {
        "service": sorted(s.id for s in world.services),
        "artifact": sorted(a.id for a in world.artifacts),
        "flag": sorted(f.id for f in world.flags),
    }
    calls: list[Call] = []
    for tool in world.catalog:
        if not tool.params:
            calls.append(Call(tool=tool.id, args={}))
            continue
        param_domains = [ids[p.kind] for p in tool.params]
        for combo in product(*param_domains):
            args = {p.name: combo[k] for k, p in enumerate(tool.params)}
            calls.append(Call(tool=tool.id, args=args))
    return calls


def optimal_depth(world: ToolWorld, max_depth: int) -> int | None:
    """Bounded BFS for the shortest valid plan reaching the goal, within
    `max_depth` calls. Returns the depth (0 if the initial state already
    satisfies the goal), or None if no solution exists within `max_depth`.

    Fully deterministic and exhaustive over the enumerated state space."""
    if world.goal.satisfied(world.initial_state):
        return 0

    all_calls = _enumerate_calls(world)
    start = world.initial_state
    visited: set[State] = {start}
    frontier: deque[tuple[State, int]] = deque([(start, 0)])

    while frontier:
        state, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        for call in all_calls:
            step = apply_call(world, state, call)
            if not step.ok:
                continue
            nxt = step.state
            if nxt in visited:
                continue
            if world.goal.satisfied(nxt):
                return depth + 1
            visited.add(nxt)
            frontier.append((nxt, depth + 1))
    return None


def validate(world: ToolWorld, max_depth: int | None = None) -> list[str]:
    """Well-posedness check, mirroring `world/simulate.py::validate` (returns a
    list of violations, never raises). Crucially: proves the world is SOLVABLE
    within `world.budget` by bounded search, and that the recorded
    `optimal_depth` matches the search result.

    Violations flagged:
      * any tool param referencing an unknown entity kind;
      * a goal clause referencing an undefined entity;
      * the world unsolvable within its budget;
      * `optimal_depth` set but disagreeing with the search;
      * budget < optimal_depth (a budget that can't fit the optimal plan).
    """
    violations: list[str] = []
    valid_ids = _valid_ids_by_kind(world)

    for tool in world.catalog:
        for param in tool.params:
            if param.kind not in valid_ids:
                violations.append(
                    f"tool {tool.id!r} param {param.name!r} has unknown kind {param.kind!r}"
                )

    for clause in world.goal.clauses:
        family = {
            "service": valid_ids["service"],
            "artifact": valid_ids["artifact"],
            "flag": valid_ids["flag"],
        }.get(clause.target_kind)
        if family is None:
            violations.append(f"goal clause has unknown target_kind {clause.target_kind!r}")
        elif clause.target_id not in family:
            violations.append(
                f"goal clause references undefined {clause.target_kind} {clause.target_id!r}"
            )

    if world.state_narration and sorted(world.state_narration) != sorted(
        set(world.state_narration)
    ):
        # Narration lines should be unique (they establish distinct facts).
        pass  # duplicates are allowed as flavor; not a hard violation.

    search_depth = max_depth if max_depth is not None else max(world.budget, 1)
    depth = optimal_depth(world, search_depth)
    if depth is None:
        violations.append(
            f"world unsolvable within depth {search_depth} (budget={world.budget})"
        )
    else:
        if world.optimal_depth is not None and world.optimal_depth != depth:
            violations.append(
                f"recorded optimal_depth {world.optimal_depth} != searched {depth}"
            )
        if world.budget < depth:
            violations.append(
                f"budget {world.budget} < optimal_depth {depth} (goal unreachable in budget)"
            )

    return violations
