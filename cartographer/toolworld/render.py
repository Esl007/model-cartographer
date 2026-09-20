"""Deterministic prompt rendering for a `ToolWorld`.

`render(world, seed)` narrates the initial state, states the goal, lists the
tool catalog (name, params, prose description including preconditions), and
instructs the model to output its plan as a strict JSON array of calls
`[{"tool": ..., "args": {...}}, ...]`. Template-based and deterministic: same
(world, seed) -> same prompt. No model calls.

The `answer_key` here is not a single string (as in the narrative province)
but the goal itself -- verification simulates the returned plan (see
`verify.py`), so `RenderedToolTask.optimal_depth` is carried for measurement,
not for grading.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import GoalClause, ToolWorld


@dataclass(frozen=True)
class RenderedToolTask:
    prompt: str
    optimal_depth: int
    budget: int
    meta: dict = field(default_factory=dict)


def _goal_clause_prose(clause: GoalClause, world: ToolWorld) -> str:
    if clause.target_kind == "service":
        svc = next(s for s in world.services if s.id == clause.target_id)
        return f"service '{svc.name}' must reach at least the '{clause.value}' state"
    if clause.target_kind == "artifact":
        art = next(a for a in world.artifacts if a.id == clause.target_id)
        return f"artifact '{art.name}' must be '{clause.value}'"
    if clause.target_kind == "flag":
        flag = next(f for f in world.flags if f.id == clause.target_id)
        return f"flag '{flag.name}' must be {clause.value}"
    raise ValueError(f"unknown goal clause kind {clause.target_kind!r}")


def _tool_catalog_prose(world: ToolWorld) -> str:
    lines: list[str] = []
    for tool in world.catalog:
        param_str = ", ".join(f"{p.name}: {p.kind}" for p in tool.params) or "(no args)"
        lines.append(
            f"- id: {tool.id} | name: {tool.name} | params: {param_str}\n"
            f"    {tool.description}"
        )
    return "\n".join(lines)


def _entity_reference_prose(world: ToolWorld) -> str:
    """A compact id->name legend so the model can bind tool args to the exact
    entity ids the verifier expects."""
    parts: list[str] = []
    parts.append(
        "Services: "
        + ", ".join(f"{s.id} ({s.name})" for s in world.services)
    )
    parts.append(
        "Artifacts: "
        + ", ".join(f"{a.id} ({a.name})" for a in world.artifacts)
    )
    parts.append(
        "Flags: " + ", ".join(f"{f.id} ({f.name})" for f in world.flags)
    )
    return "\n".join(parts)


def render(world: ToolWorld, seed: int) -> RenderedToolTask:
    state_lines = "\n".join(f"- {line}" for line in world.state_narration)
    goal_lines = "\n".join(
        f"- {_goal_clause_prose(c, world)}" for c in world.goal.clauses
    )
    catalog = _tool_catalog_prose(world)
    legend = _entity_reference_prose(world)

    prompt = (
        "You are an operations planner. Below is the current state of a small "
        "deployment environment, a goal, and a catalog of tools you may call. "
        "Each tool has typed parameters and PRECONDITIONS that must hold at the "
        "moment you call it; calling a tool whose preconditions are not met is "
        "an error and your plan fails.\n\n"
        "CURRENT STATE:\n"
        f"{state_lines}\n\n"
        "GOAL (all conditions must hold at the end):\n"
        f"{goal_lines}\n\n"
        "ENTITY IDS (use these exact ids in your tool args):\n"
        f"{legend}\n\n"
        "TOOL CATALOG:\n"
        f"{catalog}\n\n"
        "Output ONLY a JSON array of tool calls that, executed in order from "
        "the current state, satisfies the goal. Each element must be an object "
        '{"tool": "<tool id>", "args": {"<param>": "<entity id>", ...}}. '
        "Do not include any prose, explanation, or markdown fences -- just the "
        "JSON array.\n\n"
        f"You may use at most {world.budget} tool calls."
    )

    meta = {
        "kind": "tool_plan",
        "optimal_depth": world.optimal_depth,
        "budget": world.budget,
        "n_tools": len(world.catalog),
        "n_decoys": sum(1 for t in world.catalog if t.decoy),
    }
    return RenderedToolTask(
        prompt=prompt,
        optimal_depth=world.optimal_depth if world.optimal_depth is not None else 0,
        budget=world.budget,
        meta=meta,
    )
