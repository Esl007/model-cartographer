"""Realized dimension measurement for a `ToolWorld`, recomputed from the
actual generated instance (never echoing `ToolDimParams`).

Mirrors `world/measure.py`'s role. The dims here are the tool-planning
province's difficulty axes, named to be self-describing on the atlas/findings
pages:

  * realized_plan_depth  -- the true optimal plan length (BFS ground truth).
  * decoy_count          -- number of decoy tools in the offered catalog.
  * chain_length         -- longest precondition dependency chain actually
                            present (gated advance/flag/artifact steps).
  * catalog_size         -- total tools offered (search-space breadth).
  * state_obscurity      -- fraction of state-narration lines phrased
                            indirectly (must be inferred, not read off).
  * irreversibility      -- 1.0 if a destructive tool is offered, else 0.0.
  * parallel_goals       -- number of goal conjuncts.
"""

from __future__ import annotations

from .model import ToolWorld


def _longest_precondition_chain(world: ToolWorld) -> int:
    """Length of the longest gate chain: count the distinct gating layers the
    optimal plan is forced through. We approximate structurally: flag-set tools
    that require another flag form a chain, plus the artifact build->publish
    pair, plus the number of ladder rungs that are gated. This is a structural
    measurement over the catalog, not a re-derivation of ToolDimParams."""
    # Flag requirement chain: follow `requires` links encoded by which
    # set_flag tools mention "Requires the ... flag first" -- but we don't have
    # that structured, so measure via the catalog ids we emitted.
    n_flag_setters = sum(1 for t in world.catalog if t.name == "set_flag")
    has_artifact_chain = any(t.name == "build_artifact" for t in world.catalog) and any(
        t.name == "publish_artifact" for t in world.catalog
    )
    chain = n_flag_setters + (2 if has_artifact_chain else 0)
    return chain


def measure(world: ToolWorld) -> dict[str, float]:
    realized_plan_depth = float(
        world.optimal_depth if world.optimal_depth is not None else 0
    )
    decoy_count = float(sum(1 for t in world.catalog if t.decoy))
    catalog_size = float(len(world.catalog))
    chain_length = float(_longest_precondition_chain(world))
    irreversibility = 1.0 if any(t.destructive for t in world.catalog) else 0.0
    parallel_goals = float(len(world.goal.clauses))

    total_lines = len(world.state_narration)
    indirect = sum(
        1
        for line in world.state_narration
        if line.startswith("At the end of last week")
        or line.startswith("No one has built")
        or line.startswith("The ") and "was never enabled" in line
    )
    state_obscurity = (indirect / total_lines) if total_lines else 0.0

    return {
        "realized_plan_depth": realized_plan_depth,
        "decoy_count": decoy_count,
        "chain_length": chain_length,
        "catalog_size": catalog_size,
        "state_obscurity": float(state_obscurity),
        "irreversibility": irreversibility,
        "parallel_goals": parallel_goals,
    }
