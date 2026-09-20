"""Deterministic tool-world generation from `ToolDimParams` + seed.

Same SHA256-stream idiom as `world/generate.py` (`_stable_int`/`_stable_float`
over stable string parts, never the `random` module), so
`generate(params, seed)` is a pure function: identical (params, seed) always
yields the identical world, goal, and tool catalog.

Difficulty knobs (the tool-planning province's analog of `DimParams`):

  * `plan_depth`         -- min tool calls on the optimal plan (the generator
                            builds a real dependency chain this deep).
  * `decoy_tools`        -- count of plausible-but-never-needed tools mixed
                            into the offered catalog.
  * `precondition_chain` -- longest dependency chain: a tool gated behind
                            another tool's effect, behind another's, ...
  * `state_obscurity`    -- 0..1, how much of the initial state must be
                            inferred from narrated earlier events vs. stated
                            directly (scrambles + indirects `state_narration`).
  * `irreversibility`    -- when True, at least one destructive tool is
                            offered whose wrong use can dead-end the goal.
  * `parallel_goals`     -- 1 or 2 subgoals that share the service pool.

`validate()` proves solvability within budget by bounded BFS and records the
true `optimal_depth`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .model import (
    ARTIFACT_STATES,
    SERVICE_LADDER,
    SERVICE_LEVEL,
    Artifact,
    Flag,
    Goal,
    GoalClause,
    Param,
    Service,
    Tool,
    ToolWorld,
    make_state,
)
from .simulate import optimal_depth, validate

_SERVICE_NAMES = [
    "auth-api", "billing", "gateway", "search", "notifier", "ledger",
    "scheduler", "ingest", "reporting", "cache", "webhooks", "registry",
]
_ARTIFACT_NAMES = [
    "release-image", "db-migration", "config-bundle", "seed-data",
    "tls-cert", "helm-chart",
]
_FLAG_NAMES = [
    "maintenance_window_open", "secrets_synced", "feature_gate_on",
    "backup_taken", "quota_raised",
]


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


@dataclass(frozen=True)
class ToolDimParams:
    plan_depth: int = 3
    decoy_tools: int = 2
    precondition_chain: int = 1
    state_obscurity: float = 0.0
    irreversibility: bool = False
    parallel_goals: int = 1

    def __post_init__(self) -> None:
        if self.plan_depth < 1:
            raise ValueError("plan_depth must be >= 1")
        if self.parallel_goals not in (1, 2):
            raise ValueError("parallel_goals must be 1 or 2")
        if self.precondition_chain < 0:
            raise ValueError("precondition_chain must be >= 0")


# --- Tool builders --------------------------------------------------------
#
# Each builder returns a Tool whose preconditions/effects are closures. The
# service ladder is: down -> provisioned -> configured -> deployed -> healthy.
# "Advance" tools each move a service one rung up, but each rung's tool is
# gated on the previous rung being reached (a natural precondition chain), and
# some rungs additionally require a flag or a published artifact (the injected
# dependency chain).


def _advance_tool(
    tool_id: str,
    from_state: str,
    to_state: str,
    verb: str,
    gate_flag: str | None = None,
    gate_artifact: str | None = None,
) -> Tool:
    def preconditions(state, args, _from=from_state, _flag=gate_flag, _art=gate_artifact):
        sid = args["service"]
        if state.service_state(sid) != _from:
            return False
        if _flag is not None and not state.flag(_flag):
            return False
        if _art is not None and state.artifact_state(_art) != "published":
            return False
        return True

    def effect(state, args, _to=to_state):
        return state.with_service(args["service"], _to)

    gate_desc = ""
    if gate_flag is not None:
        gate_desc += f" Requires the {gate_flag} flag to be set."
    if gate_artifact is not None:
        gate_desc += f" Requires the {gate_artifact} artifact to be published."
    desc = (
        f"{verb} a service, moving it from '{from_state}' to '{to_state}'. "
        f"Precondition: the service must currently be '{from_state}'.{gate_desc}"
    )
    return Tool(
        id=tool_id,
        name=verb.replace(" ", "_").lower(),
        description=desc,
        params=(Param("service", "service"),),
        preconditions=preconditions,
        effect=effect,
    )


def _build_artifact_tool(tool_id: str, artifact_id: str) -> Tool:
    def preconditions(state, args, _aid=artifact_id):
        return state.artifact_state(_aid) == "absent"

    def effect(state, args, _aid=artifact_id):
        return state.with_artifact(_aid, "built")

    return Tool(
        id=tool_id,
        name="build_artifact",
        description=(
            f"Build the {artifact_id} artifact (moves it from 'absent' to "
            "'built'). Precondition: the artifact must be 'absent'."
        ),
        params=(Param("artifact", "artifact"),),
        preconditions=preconditions,
        effect=effect,
    )


def _publish_artifact_tool(tool_id: str, artifact_id: str) -> Tool:
    def preconditions(state, args, _aid=artifact_id):
        return state.artifact_state(_aid) == "built"

    def effect(state, args, _aid=artifact_id):
        return state.with_artifact(_aid, "published")

    return Tool(
        id=tool_id,
        name="publish_artifact",
        description=(
            f"Publish the {artifact_id} artifact (moves it from 'built' to "
            "'published'). Precondition: the artifact must be 'built'."
        ),
        params=(Param("artifact", "artifact"),),
        preconditions=preconditions,
        effect=effect,
    )


def _set_flag_tool(tool_id: str, flag_id: str, requires_flag: str | None = None) -> Tool:
    def preconditions(state, args, _fid=flag_id, _req=requires_flag):
        if state.flag(_fid):
            return False
        if _req is not None and not state.flag(_req):
            return False
        return True

    def effect(state, args, _fid=flag_id):
        return state.with_flag(_fid, True)

    req_desc = f" Requires the {requires_flag} flag first." if requires_flag else ""
    return Tool(
        id=tool_id,
        name="set_flag",
        description=(
            f"Set the {flag_id} flag to true. Precondition: it must currently "
            f"be false.{req_desc}"
        ),
        params=(Param("flag", "flag"),),
        preconditions=preconditions,
        effect=effect,
    )


def _teardown_tool(tool_id: str) -> Tool:
    """Destructive: knocks a service all the way back to 'down'. Always fireable
    on any non-'down' service, so a wrong call can undo progress -- and under a
    tight budget, dead-end the goal."""

    def preconditions(state, args):
        return state.service_state(args["service"]) != "down"

    def effect(state, args):
        return state.with_service(args["service"], "down")

    return Tool(
        id=tool_id,
        name="teardown_service",
        description=(
            "Tear down a service, resetting it all the way back to 'down'. "
            "This is DESTRUCTIVE and irreversible in the sense that all "
            "advancement progress on that service is lost."
        ),
        params=(Param("service", "service"),),
        preconditions=preconditions,
        effect=effect,
        destructive=True,
    )


def _decoy_tool(tool_id: str, idx: int) -> Tool:
    """A plausible-sounding tool that never advances any goal: its effect is a
    no-op-ish toggle on its own private flag namespace (we model it as a
    precondition that's satisfiable but whose effect changes nothing relevant
    to any goal). Kept syntactically valid so a shallow planner can call it."""
    verbs = [
        ("audit_logs", "Audit the log stream for a service (read-only)."),
        ("warm_cache", "Warm the cache for a service (best-effort, no state change)."),
        ("ping_healthcheck", "Ping a service's healthcheck endpoint (diagnostic only)."),
        ("rotate_metrics", "Rotate the metrics buffer for a service (housekeeping)."),
        ("tag_release", "Attach a cosmetic release tag to a service (no effect on state)."),
        ("notify_oncall", "Notify the on-call engineer about a service (informational)."),
    ]
    verb_name, verb_desc = verbs[idx % len(verbs)]

    def preconditions(state, args):
        # Fireable whenever the service exists in any state (always true here).
        return True

    def effect(state, args):
        return state  # genuinely no-op: decoys never change goal-relevant state.

    return Tool(
        id=tool_id,
        name=verb_name,
        description=verb_desc + " (Has no effect on deployment state.)",
        params=(Param("service", "service"),),
        preconditions=preconditions,
        effect=effect,
        decoy=True,
    )


# --- Generation -----------------------------------------------------------


def generate(params: ToolDimParams, seed: int) -> ToolWorld:
    # Entity pools sized to the difficulty. plan_depth advancement steps need
    # up to plan_depth service rungs; we cap the target service's required
    # ladder climb at len(SERVICE_LADDER)-1 and use artifact/flag gates to
    # extend depth beyond the ladder height.
    n_services = max(2, 1 + params.parallel_goals)
    n_artifacts = max(2, params.precondition_chain + 1)
    n_flags = max(2, params.precondition_chain + 1)

    services = [
        Service(id=f"svc_{i}", name=_svc_name(i, seed)) for i in range(n_services)
    ]
    artifacts = [
        Artifact(id=f"art_{i}", name=_ARTIFACT_NAMES[i % len(_ARTIFACT_NAMES)])
        for i in range(n_artifacts)
    ]
    flags = [
        Flag(id=f"flag_{i}", name=_FLAG_NAMES[i % len(_FLAG_NAMES)])
        for i in range(n_flags)
    ]

    # --- Build the required dependency chain for the primary goal ----------
    # Target service must climb the ladder up to a target rung. We choose the
    # target rung so the *ladder* portion of the optimal plan is
    # min(plan_depth, ladder_height). Any remaining required depth is added as
    # a precondition_chain of artifact build->publish and flag-set steps that
    # gate the higher rungs.
    catalog: list[Tool] = []
    ladder_height = len(SERVICE_LADDER) - 1  # 4 advancement steps max

    primary_service = services[0].id
    # How many ladder rungs the primary goal demands.
    ladder_steps = min(params.plan_depth, ladder_height)
    target_rung = SERVICE_LADDER[ladder_steps]

    # Precondition-chain steps: each adds a prerequisite that must be
    # established before a specific ladder rung's advance tool can fire.
    # Chain element i gates rung (i % ladder_steps)+1 ... but to keep it a
    # genuine *chain* (each depends on the previous), we chain the flags:
    # flag_1 requires flag_0, flag_2 requires flag_1, and the deepest gated
    # rung requires the deepest flag / a published artifact.
    chain_len = params.precondition_chain

    # Flag chain: flag_0 (free) -> flag_1 (needs flag_0) -> ...
    chain_flag_ids: list[str] = []
    for i in range(min(chain_len, n_flags)):
        fid = flags[i].id
        chain_flag_ids.append(fid)
        requires = chain_flag_ids[i - 1] if i > 0 else None
        catalog.append(_set_flag_tool(f"set_flag_{i}", fid, requires_flag=requires))

    # Artifact chain (build -> publish) for at least one artifact, used as a
    # gate on a ladder rung when chain_len calls for it.
    gate_artifact_id: str | None = None
    if chain_len >= 1:
        gate_artifact_id = artifacts[0].id
        catalog.append(_build_artifact_tool("build_art_0", gate_artifact_id))
        catalog.append(_publish_artifact_tool("publish_art_0", gate_artifact_id))

    # The advance tools for the primary service's ladder climb. Distribute the
    # gates: the last rung requires the deepest flag (if any) AND the published
    # artifact (if any) -- forcing the chain to be completed first.
    deepest_flag = chain_flag_ids[-1] if chain_flag_ids else None
    for step in range(ladder_steps):
        frm = SERVICE_LADDER[step]
        to = SERVICE_LADDER[step + 1]
        is_last = step == ladder_steps - 1
        gate_flag = deepest_flag if (is_last and deepest_flag is not None) else None
        gate_art = gate_artifact_id if (is_last and gate_artifact_id is not None) else None
        catalog.append(
            _advance_tool(
                f"advance_{step}",
                from_state=frm,
                to_state=to,
                verb=_advance_verb(step),
                gate_flag=gate_flag,
                gate_artifact=gate_art,
            )
        )

    # --- Second (parallel) goal -------------------------------------------
    goal_clauses: list[GoalClause] = [
        GoalClause(target_kind="service", target_id=primary_service, value=target_rung)
    ]
    if params.parallel_goals == 2:
        secondary_service = services[1].id
        # A shorter independent climb sharing the same advance tools (they're
        # keyed by from/to ladder state, not by service, so they apply to any
        # service) -- the two goals share the tool pool, as required.
        sec_rung_steps = max(1, min(2, ladder_steps))
        sec_rung = SERVICE_LADDER[sec_rung_steps]
        # Ensure advance tools for the lower rungs exist (they do: steps 0..).
        goal_clauses.append(
            GoalClause(
                target_kind="service", target_id=secondary_service, value=sec_rung
            )
        )

    # --- Destructive tool (irreversibility) --------------------------------
    if params.irreversibility:
        catalog.append(_teardown_tool("teardown"))

    # --- Decoy tools -------------------------------------------------------
    for d in range(params.decoy_tools):
        catalog.append(_decoy_tool(f"decoy_{d}", d))

    # Shuffle the catalog deterministically so the correct tools aren't always
    # first (the narration_order foil depends on offered order).
    catalog = _stable_shuffle(catalog, seed, "catalog")

    # --- Initial state -----------------------------------------------------
    init_services = {s.id: "down" for s in services}
    init_artifacts = {a.id: "absent" for a in artifacts}
    init_flags = {f.id: False for f in flags}
    initial_state = make_state(init_services, init_artifacts, init_flags)

    goal = Goal(clauses=tuple(goal_clauses))

    # --- State narration (obscurity) ---------------------------------------
    narration = _build_state_narration(
        services, artifacts, flags, params.state_obscurity, seed
    )

    # Budget: generous slack above the true optimum so an inefficient-but-valid
    # plan still fits. We compute the true optimum first via search.
    provisional = ToolWorld(
        services=services,
        artifacts=artifacts,
        flags=flags,
        catalog=catalog,
        initial_state=initial_state,
        goal=goal,
        state_narration=narration,
        optimal_depth=None,
        budget=64,  # loose ceiling for the initial search
    )
    depth = optimal_depth(provisional, max_depth=32)
    if depth is None:
        # Should never happen given how we construct the chain; fail loudly.
        raise RuntimeError("generated tool-world is unsolvable within depth 32")

    budget = depth + max(4, depth)  # allow up to ~2x the optimum, floored at +4
    world = ToolWorld(
        services=services,
        artifacts=artifacts,
        flags=flags,
        catalog=catalog,
        initial_state=initial_state,
        goal=goal,
        state_narration=narration,
        optimal_depth=depth,
        budget=budget,
    )

    assert not validate(world), validate(world)
    return world


def _svc_name(i: int, seed: int) -> str:
    idx = _stable_int("svc_name", str(seed), str(i), modulus=len(_SERVICE_NAMES))
    # De-collide by suffixing the index.
    return f"{_SERVICE_NAMES[idx]}-{i}"


_ADVANCE_VERBS = ["Provision", "Configure", "Deploy", "Health-check"]


def _advance_verb(step: int) -> str:
    return _ADVANCE_VERBS[step % len(_ADVANCE_VERBS)]


def _stable_shuffle(items: list, seed: int, tag: str) -> list:
    """Deterministic Fisher-Yates using the sha256 stream, no `random`."""
    result = list(items)
    n = len(result)
    for i in range(n - 1, 0, -1):
        j = _stable_int(tag, str(seed), str(i), modulus=i + 1)
        result[i], result[j] = result[j], result[i]
    return result


def _build_state_narration(
    services, artifacts, flags, obscurity: float, seed: int
) -> list[str]:
    """Lines describing the initial state. At obscurity 0 they state facts
    directly ("auth-api is currently down"). At higher obscurity, some facts
    are phrased indirectly as narrated history the reader must fold forward
    ("Yesterday the team tore down auth-api"), and the presentation order is
    scrambled -- so more of the true initial state must be inferred rather than
    read off directly. The FACTS conveyed are identical either way; obscurity
    only changes directness + order."""
    direct = [f"{s.name} is currently down (not yet provisioned)." for s in services]
    direct += [f"The {a.name} artifact does not exist yet." for a in artifacts]
    direct += [f"The {f.name} flag is off." for f in flags]

    if obscurity <= 0.0:
        return direct

    indirect_map = {
        s.name: f"At the end of last week, {s.name} was fully torn down and has not been touched since."
        for s in services
    }
    indirect_map.update(
        {
            a.name: f"No one has built the {a.name} artifact this cycle."
            for a in artifacts
        }
    )
    indirect_map.update(
        {f.name: f"The {f.name} flag was never enabled after the last reset." for f in flags}
    )

    lines: list[str] = []
    all_entities = [s.name for s in services] + [a.name for a in artifacts] + [
        f.name for f in flags
    ]
    for k, ent in enumerate(all_entities):
        use_indirect = _stable_float("obscure", str(seed), ent, str(k)) < obscurity
        if use_indirect:
            lines.append(indirect_map[ent])
        else:
            # find the matching direct line
            lines.append(next(d for d in direct if ent in d))

    # Scramble presentation order proportionally to obscurity.
    return _scramble_lines(lines, obscurity, seed)


def _scramble_lines(lines: list[str], amount: float, seed: int) -> list[str]:
    n = len(lines)
    if n < 2:
        return list(lines)
    max_swaps = n * (n - 1) // 2
    n_swaps = int(round(amount * max_swaps))
    offset = _stable_int("narr_scramble", str(seed), modulus=n - 1)
    result = list(lines)
    for k in range(n_swaps):
        pos = (offset + k) % (n - 1)
        result[pos], result[pos + 1] = result[pos + 1], result[pos]
    return result
