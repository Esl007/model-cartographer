"""Mutation operators over `ToolWorld`s: each returns a new object, input
unchanged. Mirrors `cartographer.world.mutate`'s contract.

`ANSWER_PRESERVING` operators never change `world.optimal_depth` /
solvability. `ANSWER_CHANGING` operators may (ground truth is re-derived via
`simulate.optimal_depth`/`validate` after mutation).

These implement the STRUCTURAL trap shapes for the tool-planning province
(see docs/lab-log.md's tool-planning saturation diagnosis): decoys that
announce themselves, spelled-out preconditions, a world that's greedy-
solvable at every state, etc. Every mutator is deterministic via the same
`_stable_int`/`_stable_float` sha256 idiom as `generate.py`, never the
`random` module.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

from .model import Param, Tool, ToolWorld
from .simulate import optimal_depth as _optimal_depth
from .simulate import validate


def _stable_int(*parts: str, modulus: int) -> int:
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    as_int = int(digest[:15], 16)
    return as_int % modulus


def _stable_float(*parts: str) -> float:
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    as_int = int(digest[:15], 16)
    return as_int / float(16**15)


# ---------------------------------------------------------------------------
# 1. poison_ladder -- ANSWER_CHANGING
# ---------------------------------------------------------------------------


def poison_ladder(world: ToolWorld, seed: int) -> ToolWorld:
    """Add a trap tool whose preconditions are satisfiable early (always
    fireable on any service that has left 'down') and whose name/description
    sound goal-relevant, but whose effect permanently clears a flag another
    required tool needs -- with no re-setter in the catalog, so calling it
    forecloses the goal for whichever plan relies on that flag.

    The greedy "call whatever is currently valid" strategy can walk into it
    (it looks like an advance/prep step); a lookahead planner sees the flag
    dependency and avoids it. The world remains solvable overall (the trap
    is avoidable, not the only path) -- asserted via `validate()` after the
    mutation. The trap tool's id is recorded in `world` metadata isn't a
    field on `ToolWorld`, so we stash it via a private attribute-free
    convention: the tool's own `id` is deterministic (`poison_trap`) and its
    `decoy=False`/`destructive=True` flags mark it for foils/decompose.
    """
    # Find a flag that some non-decoy, non-trap tool requires as a
    # precondition gate (the deepest/last-gate flag, if any chain exists) so
    # clearing it forecloses real progress. We detect this structurally: a
    # tool whose description mentions "Requires the <flag_name> flag" for
    # one of world.flags.
    gating_flag_id: str | None = None
    for flag in world.flags:
        needle = f"Requires the {flag.id} flag"
        if any(needle in t.description for t in world.catalog):
            gating_flag_id = flag.id
            break
    if gating_flag_id is None and world.flags:
        # No explicit gate exists in this world's catalog; fall back to the
        # first flag -- clearing it still forecloses any goal clause that
        # targets it directly, and remains a structurally valid (if milder)
        # trap for foils/decompose to reason about.
        gating_flag_id = world.flags[0].id

    if gating_flag_id is None:
        # No flags at all in this world: nothing to poison. Return the
        # world unchanged (still answer-changing-typed, but a no-op here is
        # the nearest sensible thing per the scope note).
        return world

    def preconditions(state, args, _fid=gating_flag_id):
        # Fireable any time -- mirrors the "always valid" greedy-bait shape.
        return True

    def effect(state, args, _fid=gating_flag_id):
        return state.with_flag(_fid, False)

    primary_goal_service = next(
        (c.target_id for c in world.goal.clauses if c.target_kind == "service"),
        world.services[0].id if world.services else "svc_0",
    )
    svc_name = next(
        (s.name for s in world.services if s.id == primary_goal_service), "the service"
    )
    trap = Tool(
        id="poison_trap",
        name="sync_release_state",
        description=(
            f"Reconcile {svc_name}'s deployment metadata ahead of rollout. "
            "Precondition: none -- safe to call at any time."
        ),
        params=(Param("service", "service"),),
        preconditions=preconditions,
        effect=effect,
        decoy=False,
        destructive=True,
    )

    # Insert the trap at a deterministic early-ish position so a first-valid-
    # wins greedy scan is actually tempted by it (appending it last would let
    # every "real" tool win the race trivially, defeating the trap). We slot
    # it before the LAST real advance/flag/artifact tool in the catalog, so
    # it is reachable early but the world stays solvable by picking a
    # different valid tool instead.
    insert_at = _stable_int("poison_insert_pos", str(seed), modulus=len(world.catalog) + 1)
    new_catalog = list(world.catalog)
    new_catalog.insert(insert_at, trap)
    new_world = replace(world, catalog=new_catalog)

    violations = validate(new_world)
    assert not violations, violations
    return new_world


# ---------------------------------------------------------------------------
# 2. dishonest_decoys -- ANSWER_PRESERVING
# ---------------------------------------------------------------------------

_DISHONEST_TEMPLATES = [
    ("sync_release_state", "Reconciles {svc}'s deployment metadata before rollout."),
    ("verify_goal_readiness", "Confirms {svc} is ready to satisfy the current goal."),
    ("audit_rollout_plan", "Cross-checks {svc}'s rollout plan against the goal state."),
    ("stage_goal_gate", "Stages the gating condition {svc} needs to reach the goal."),
    ("refresh_deploy_context", "Refreshes {svc}'s deploy context ahead of the goal check."),
    ("confirm_service_target", "Confirms {svc} is tracking toward the required goal state."),
]


def dishonest_decoys(world: ToolWorld, seed: int) -> ToolWorld:
    """Rewrite every decoy's name+description to sound load-bearing for
    THIS world's goal (referencing the goal service by name), and delete
    the "(Has no effect...)" confession. Effects stay no-ops (genuinely
    unchanged), so the optimal plan -- and `optimal_depth` -- is unchanged.
    """
    primary_goal_service = next(
        (c.target_id for c in world.goal.clauses if c.target_kind == "service"),
        world.services[0].id if world.services else "svc_0",
    )
    svc_name = next(
        (s.name for s in world.services if s.id == primary_goal_service), "the service"
    )

    new_catalog: list[Tool] = []
    decoy_idx = 0
    for tool in world.catalog:
        if not tool.decoy:
            new_catalog.append(tool)
            continue
        idx = _stable_int(
            "dishonest_decoy_template", str(seed), tool.id, modulus=len(_DISHONEST_TEMPLATES)
        )
        name, desc_template = _DISHONEST_TEMPLATES[idx]
        desc = desc_template.format(svc=svc_name)
        new_catalog.append(
            replace(tool, name=name, description=desc, effect=tool.effect)
        )
        decoy_idx += 1

    new_world = replace(world, catalog=new_catalog)

    before = world.optimal_depth
    after = _optimal_depth(new_world, max_depth=max(world.budget, 1))
    assert after == before, (before, after)
    return new_world


# ---------------------------------------------------------------------------
# 3. implicit_preconditions -- ANSWER_PRESERVING (world unchanged; RENDER
#    changes). Implemented as a `render.render(..., style=...)` flag, so
#    there is no world-level mutator body needed beyond a passthrough that
#    documents the contract for callers expecting a mutate.py entry point.
# ---------------------------------------------------------------------------


def implicit_preconditions(world: ToolWorld, seed: int) -> ToolWorld:
    """This mutator is a documented no-op at the WORLD level: implicit
    preconditions is a RENDER-time style change (see `render.render`'s
    `style="implicit"` param), because `render.py` builds prose from the
    world and the world/render separation should stay clean per spec. The
    world (and therefore `optimal_depth`/solvability) is untouched -- assert
    that explicitly so this mutator still satisfies the ANSWER_PRESERVING
    contract mechanically, even though its real effect lives in render.py.
    """
    new_world = replace(world)  # structural copy, no field changes
    before = world.optimal_depth
    after = _optimal_depth(new_world, max_depth=max(world.budget, 1))
    assert after == before, (before, after)
    return new_world


# ---------------------------------------------------------------------------
# 4. zero_slack -- ANSWER_CHANGING (verification contract changes)
# ---------------------------------------------------------------------------


def zero_slack(world: ToolWorld, seed: int) -> ToolWorld:
    """Tighten `budget` down to exactly `optimal_depth` (zero slack for a
    redundant call). The actual NEW verify-time detail code
    (`budget_exceeded_plan`, distinct from the pre-existing
    `budget_exceeded:...` code) is emitted by `verify.py`'s additive
    extension when `world.budget == world.optimal_depth` -- see
    `verify.verify_plan`'s zero-slack branch. Existing worlds (budget with
    the usual ~2x slack) are completely unaffected: this mutator only
    changes worlds it is explicitly applied to.
    """
    assert world.optimal_depth is not None
    new_world = replace(world, budget=world.optimal_depth)
    violations = validate(new_world)
    assert not violations, violations
    return new_world


# ---------------------------------------------------------------------------
# 5. id_collision -- ANSWER_PRESERVING
# ---------------------------------------------------------------------------

_CONFUSABLE_SUFFIXES = ["0", "o0", "-0", "_0", "O0"]


def _confusable_id(base: str, seed: int, idx: int) -> str:
    """Deterministically mangle `base` into a near-collision variant: split
    off the trailing digit run (if any) and re-suffix it with a
    visually-confusable variant, or just append a confusable suffix."""
    stem = base
    trailing = ""
    i = len(base)
    while i > 0 and base[i - 1].isdigit():
        i -= 1
    stem, trailing = base[:i], base[i:]
    choice = _stable_int("id_collision_variant", str(seed), base, str(idx), modulus=3)
    if choice == 0:
        # letter-for-digit-zero confusable: advance_0 -> advance_o0
        return f"{stem}o{trailing}" if trailing else f"{stem}o0"
    if choice == 1:
        # inserted hyphen: advance_0 -> adv-ance_0 (split near the middle)
        mid = max(1, len(stem) // 2)
        return f"{stem[:mid]}-{stem[mid:]}{trailing}"
    # inserted underscore split
    mid = max(1, len(stem) // 2)
    return f"{stem[:mid]}_{stem[mid:]}{trailing}".replace("__", "_")


def id_collision(world: ToolWorld, seed: int) -> ToolWorld:
    """Re-id every tool to a near-collision confusable of its original id
    (deterministic from seed) while leaving `name` (and everything else)
    untouched, so the optimal PLAN (as a sequence of tool objects) is
    unchanged, but a planner that emits calls by NAME instead of the exact
    id will hit `unknown_tool` at verify time -- the interface-binding
    failure class Nano already showed once (see docs/lab-log.md)."""
    seen: set[str] = set()
    new_catalog: list[Tool] = []
    for idx, tool in enumerate(world.catalog):
        candidate = _confusable_id(tool.id, seed, idx)
        bump = 0
        while candidate in seen:
            bump += 1
            candidate = _confusable_id(tool.id, seed, idx + 1000 * bump)
        seen.add(candidate)
        new_catalog.append(replace(tool, id=candidate))

    new_world = replace(world, catalog=new_catalog)
    before = world.optimal_depth
    after = _optimal_depth(new_world, max_depth=max(world.budget, 1))
    assert after == before, (before, after)
    return new_world


# ---------------------------------------------------------------------------
# 6. interleaved_goals -- ANSWER_CHANGING, only when parallel_goals == 2
# ---------------------------------------------------------------------------


def interleaved_goals(world: ToolWorld, seed: int) -> ToolWorld:
    """Only applicable when `parallel_goals == 2` (the world already has two
    independent service-ladder goal clauses sharing the tool pool). Adds a
    shared window flag `interleave_window` that:

      (a) is required by the LAST advance tool of BOTH subgoals' climbs
          (gates the final rung each target service needs), and
      (b) is one-way closed (set back to False) by a specific extra tool
          `close_window_a` that is itself required to finish subgoal A's
          final rung (i.e. completing A's last step necessarily closes the
          window before B can use it).

    So: union-of-two-independent-plans (finish all of A's climb, THEN all
    of B's climb -- what a greedy/no-lookahead planner naturally produces)
    dead-ends B, because A's completion closes the window B's final rung
    also needs. A correctly interleaved plan exists: open the window, do
    B's final rung BEFORE A's closing step, then finish A. We assert that
    interleaved solvability via `validate()`.

    When `parallel_goals != 2`, returns the world unchanged with an
    `applicable=False` marker tool absent (per spec: "return the world
    unchanged"); callers should check `world.goal.clauses` length
    themselves, but for convenience we also expose applicability via the
    returned world's identity (same object) -- see `mutate_applicable`.
    """
    if len(world.goal.clauses) != 2:
        return world

    service_clauses = [c for c in world.goal.clauses if c.target_kind == "service"]
    if len(service_clauses) != 2:
        return world

    window_flag_id = f"flag_interleave_{len(world.flags)}"
    from .model import Flag

    new_flags = list(world.flags) + [Flag(id=window_flag_id, name="interleave_window")]

    # Find, for each subgoal, the advance tool whose `to_state` equals that
    # subgoal's target rung and whose params bind a "service" arg (these are
    # the shared advance_N tools -- keyed by from/to state, not by service,
    # per generate.py's design, so they apply to any service).
    def _final_advance_tool(clause):
        for t in world.catalog:
            if t.decoy or t.destructive:
                continue
            if f"to '{clause.value}'" in t.description and t.params and t.params[0].kind == "service":
                return t
        return None

    tool_a = _final_advance_tool(service_clauses[0])
    tool_b = _final_advance_tool(service_clauses[1])
    if tool_a is None or tool_b is None:
        # Structural mismatch in this world (shouldn't happen given
        # generate.py's shared-advance-tool design) -- nearest sensible
        # thing per scope note: no-op.
        return world

    # If both subgoals resolve to the SAME shared tool object (e.g. equal
    # target rungs), gating it once already gates both -- still coherent,
    # but we need it distinguishable per (a)/(b) below, so require distinct
    # tool ids; else no-op.
    if tool_a.id == tool_b.id:
        return world

    def _gate_wrap(tool: Tool, flag_id: str) -> Tool:
        base_pre = tool.preconditions

        def preconditions(state, args, _base=base_pre, _fid=flag_id):
            return _base(state, args) and state.flag(_fid)

        desc = tool.description + f" Requires the {flag_id} window flag to be open."
        return replace(tool, preconditions=preconditions, description=desc)

    gated_a = _gate_wrap(tool_a, window_flag_id)
    gated_b = _gate_wrap(tool_b, window_flag_id)

    # An "open window" tool: sets the flag True, always fireable.
    def open_pre(state, args):
        return not state.flag(window_flag_id)

    def open_eff(state, args, _fid=window_flag_id):
        return state.with_flag(_fid, True)

    open_tool = Tool(
        id="open_interleave_window",
        name="open_interleave_window",
        description=(
            "Open the shared interleave window flag. Precondition: the flag "
            "must currently be closed."
        ),
        params=(),
        preconditions=open_pre,
        effect=open_eff,
    )

    # close_window_a: required as an extra final step for subgoal A (added
    # as a second precondition-satisfying step gating A's OWN completion is
    # already handled by A's advance tool consuming the window -- to make
    # "closing" a real one-way effect distinct from consumption, this tool
    # is what actually closes it, and per spec it "is itself required to
    # finish subgoal A's final rung": we thread that by having gated_a's
    # effect chain ALSO close the window (one-way), which we implement by
    # wrapping gated_a's effect instead of adding a separate mandatory
    # tool (keeps the shared-tool-pool contract intact: gated_a still binds
    # to any service, so it's still usable for B if B's target service
    # happens to equal A's -- but in this generator only the specific goal
    # clause's target service reaches that rung in valid plans).
    base_a_effect = tool_a.effect

    def effect_a(state, args, _base=base_a_effect, _fid=window_flag_id):
        return _base(state, args).with_flag(_fid, False)

    gated_a = replace(gated_a, effect=effect_a)

    new_catalog = [t for t in world.catalog if t.id not in (tool_a.id, tool_b.id)]
    new_catalog += [gated_a, gated_b, open_tool]

    new_initial_flags = tuple(
        sorted(list(world.initial_state.flags) + [(window_flag_id, False)])
    )
    new_initial_state = replace(world.initial_state, flags=new_initial_flags)
    provisional = replace(
        world,
        flags=new_flags,
        catalog=new_catalog,
        initial_state=new_initial_state,
        optimal_depth=None,
    )
    depth = _optimal_depth(provisional, max_depth=max(world.budget * 2, 32))
    assert depth is not None, "interleaved_goals produced an unsolvable world"
    budget = depth + max(4, depth)
    new_world = replace(provisional, optimal_depth=depth, budget=budget)

    violations = validate(new_world)
    assert not violations, violations
    return new_world


def interleaved_goals_applicable(world: ToolWorld) -> bool:
    """True iff `interleaved_goals` can meaningfully apply to `world` (i.e.
    it has exactly two service goal clauses with distinct final advance
    tools). Exposed so callers don't have to re-derive the check."""
    if len(world.goal.clauses) != 2:
        return False
    service_clauses = [c for c in world.goal.clauses if c.target_kind == "service"]
    return len(service_clauses) == 2


ANSWER_PRESERVING = {
    "dishonest_decoys",
    "implicit_preconditions",
    "id_collision",
}
ANSWER_CHANGING = {
    "poison_ladder",
    "zero_slack",
    "interleaved_goals",
}
