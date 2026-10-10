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


import re

_PRECONDITION_SENTENCE_RE = re.compile(r"\s*Precondition:[^.]*\.")


def _strip_precondition_sentences(description: str) -> str:
    """Remove every "Precondition: ..." sentence from a tool description
    (used by `style="implicit"` rendering -- see `render`)."""
    return _PRECONDITION_SENTENCE_RE.sub("", description).strip()


_MECHANICS_PARAGRAPH = (
    "ENVIRONMENT MECHANICS (apply these general rules to the specific tools "
    "below -- individual tool descriptions do not restate them): a service "
    "can only be configured once it has been provisioned, can only be "
    "deployed once configured, and can only be health-checked once deployed "
    "-- i.e. each rung of the down -> provisioned -> configured -> deployed "
    "-> healthy ladder requires the previous rung. An artifact can only be "
    "published once it has been built. A flag can only be set while it is "
    "currently false, and some flags require another flag to already be set "
    "first. Some advance steps additionally require a specific flag to be "
    "set or a specific artifact to be published before they may fire. Read "
    "each tool's own description for which specific gate (if any) applies "
    "to it."
)


def _tool_catalog_prose(world: ToolWorld, style: str = "explicit", ids_emphasis: bool = False) -> str:
    lines: list[str] = []
    for tool in world.catalog:
        param_str = ", ".join(f"{p.name}: {p.kind}" for p in tool.params) or "(no args)"
        desc = tool.description
        if style == "implicit":
            desc = _strip_precondition_sentences(desc)
        tid = f"`{tool.id}`" if ids_emphasis else tool.id
        lines.append(
            f"- id: {tid} | name: {tool.name} | params: {param_str}\n"
            f"    {desc}"
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


_DECLINE_PARAGRAPH = (
    "IMPORTANT: it is possible that the goal CANNOT be achieved with the "
    "tools in this catalog. Decide for yourself whether a valid plan exists. "
    "If one does, output it. If no sequence of the available tools can "
    "satisfy the goal, output exactly [] (an empty JSON array) instead. Do "
    "not invent tools that are not listed, and do not output a partial or "
    "best-effort plan that fails to reach the goal -- an empty array is the "
    "correct answer in that case."
)


def render(
    world: ToolWorld,
    seed: int,
    style: str = "explicit",
    allow_decline: bool = False,
    restate_ids: bool = False,
    ids_emphasis: bool = False,
) -> RenderedToolTask:
    """Render `world` to a prompt. `style="explicit"` (default) is the
    original rendering: every tool spells out its own "Precondition: ..."
    sentence. `style="implicit"` strips those sentences from every tool
    description and instead states the general rules ONCE, in a single
    "environment mechanics" paragraph up top -- the model must map the
    general rule to the specific call itself. The underlying `world` (and
    therefore ground truth / verification) is identical either way; only the
    prose changes -- see `mutate.implicit_preconditions`, which documents
    this as a render-level mutator to keep the world/render split clean.

    `allow_decline=True` adds the irrelevance affordance: the prompt tells
    the model the goal may be impossible and that `[]` is the correct answer
    if so (see `mutate.make_unsolvable`). `restate_ids` / `ids_emphasis` are the
    Evidence Fidelity Campaign's C and D presentations (docs/spec-evidence-
    fidelity-campaign.md); both off renders byte-identically to before. This MUST be set uniformly across
    every trial in a campaign that contains any unsolvable world -- if only
    the unsolvable ones carried the instruction, its mere presence would
    give the answer away. It is off by default so existing solvable-only
    campaigns render byte-identically to before.
    """
    if style not in ("explicit", "implicit"):
        raise ValueError(f'style must be "explicit" or "implicit", got {style!r}')

    state_lines = "\n".join(f"- {line}" for line in world.state_narration)
    goal_lines = "\n".join(
        f"- {_goal_clause_prose(c, world)}" for c in world.goal.clauses
    )
    catalog = _tool_catalog_prose(world, style=style, ids_emphasis=ids_emphasis)
    legend = _entity_reference_prose(world)

    mechanics = f"\n{_MECHANICS_PARAGRAPH}\n\n" if style == "implicit" else "\n"

    prompt = (
        "You are an operations planner. Below is the current state of a small "
        "deployment environment, a goal, and a catalog of tools you may call. "
        "Each tool has typed parameters and PRECONDITIONS that must hold at the "
        "moment you call it; calling a tool whose preconditions are not met is "
        "an error and your plan fails."
        f"{mechanics}"
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
        + (f"\n\n{_DECLINE_PARAGRAPH}" if allow_decline else "")
        # Evidence Fidelity Campaign C (salience): every catalog id restated verbatim at the tail,
        # nearest the decision point. Information-identical: the ids are all in the catalog above,
        # in the same order (restating only the plan's tools would add WHICH tools matter).
        + ("\n\nTool ids, verbatim (use them exactly as written): "
           + ", ".join(t.id for t in world.catalog) if restate_ids else "")
    )

    meta = {
        "kind": "tool_plan",
        "optimal_depth": world.optimal_depth,
        "budget": world.budget,
        "n_tools": len(world.catalog),
        "n_decoys": sum(1 for t in world.catalog if t.decoy),
        "style": style,
        "allow_decline": allow_decline,
        "solvable": world.optimal_depth is not None,
    }
    return RenderedToolTask(
        prompt=prompt,
        optimal_depth=world.optimal_depth if world.optimal_depth is not None else 0,
        budget=world.budget,
        meta=meta,
    )
