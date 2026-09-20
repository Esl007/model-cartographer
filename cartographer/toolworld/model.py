"""Latent world representation for the TOOL-CALL PLANNING province.

A `ToolWorld` is a small typed devops-flavored state (services, machines,
artifacts) plus a `GOAL` predicate and a `catalog` of typed TOOLS. Each tool
has typed parameters, PRECONDITIONS over the state, and deterministic
EFFECTS (state transitions). Ground truth -- "does a plan of length <= budget
reach the goal" and "what is the optimal depth" -- is derived purely by
bounded forward search over this latent structure (see `simulate.py`), never
authored by hand. This mirrors `cartographer/world/model.py`'s role for the
narrative province.

State model (deliberately small + enumerable so bounded BFS is cheap):

  * Each `Service` is in exactly one of a fixed ladder of states
    (`down -> provisioned -> configured -> deployed -> healthy`). Tools move a
    service up (or, for destructive tools, back down) the ladder.
  * Each `Artifact` (e.g. a build image, a migration) is either `absent`,
    `built`, or `published`.
  * A small set of boolean `flags` (e.g. `maintenance_window_open`) that
    gate tools and are toggled by other tools -- the substrate for real
    dependency chains.

A `Tool` is a pure description: given `args` binding its params to concrete
entity ids, `preconditions(state, args)` says whether it may fire now, and
`apply(state, args)` returns the successor `State`. Both are total functions
over the enumerated state space, so search never has to guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable

# The fixed service-state ladder. Index in this tuple is the "level"; tools
# that advance a service move it to the next level up, destructive tools can
# knock it back down.
SERVICE_LADDER: tuple[str, ...] = (
    "down",
    "provisioned",
    "configured",
    "deployed",
    "healthy",
)
SERVICE_LEVEL: dict[str, int] = {s: i for i, s in enumerate(SERVICE_LADDER)}

# Artifact lifecycle.
ARTIFACT_STATES: tuple[str, ...] = ("absent", "built", "published")


@dataclass(frozen=True)
class Service:
    id: str
    name: str


@dataclass(frozen=True)
class Artifact:
    id: str
    name: str


@dataclass(frozen=True)
class Flag:
    id: str
    name: str


@dataclass(frozen=True)
class State:
    """A fully-enumerated world state: every service's ladder state, every
    artifact's lifecycle state, every flag's boolean. Frozen + hashable so it
    can key a BFS `visited` set."""

    services: tuple[tuple[str, str], ...]  # sorted (service_id, ladder_state)
    artifacts: tuple[tuple[str, str], ...]  # sorted (artifact_id, lifecycle)
    flags: tuple[tuple[str, bool], ...]  # sorted (flag_id, value)

    def service_state(self, sid: str) -> str:
        for k, v in self.services:
            if k == sid:
                return v
        raise KeyError(sid)

    def artifact_state(self, aid: str) -> str:
        for k, v in self.artifacts:
            if k == aid:
                return v
        raise KeyError(aid)

    def flag(self, fid: str) -> bool:
        for k, v in self.flags:
            if k == fid:
                return v
        raise KeyError(fid)

    def with_service(self, sid: str, ladder_state: str) -> "State":
        services = tuple(
            sorted((k, ladder_state if k == sid else v) for k, v in self.services)
        )
        return replace(self, services=services)

    def with_artifact(self, aid: str, lifecycle: str) -> "State":
        artifacts = tuple(
            sorted((k, lifecycle if k == aid else v) for k, v in self.artifacts)
        )
        return replace(self, artifacts=artifacts)

    def with_flag(self, fid: str, value: bool) -> "State":
        flags = tuple(sorted((k, value if k == fid else v) for k, v in self.flags))
        return replace(self, flags=flags)


def make_state(
    services: dict[str, str], artifacts: dict[str, str], flags: dict[str, bool]
) -> State:
    return State(
        services=tuple(sorted(services.items())),
        artifacts=tuple(sorted(artifacts.items())),
        flags=tuple(sorted(flags.items())),
    )


@dataclass(frozen=True)
class Param:
    """One typed parameter of a tool. `kind` is one of "service", "artifact",
    "flag" -- the entity family a concrete argument must belong to."""

    name: str
    kind: str  # "service" | "artifact" | "flag"


@dataclass(frozen=True)
class Tool:
    """A pure, typed operation. `preconditions` and `effect` are closures over
    `(state, args)` where `args` maps each param name to a concrete entity id.

    Metadata for adversarial/foil analysis:
      * `decoy`: never appears on any optimal plan (planted as a plausible
        distractor).
      * `destructive`: at least one effect is irreversible in the sense that
        it can knock a service back down the ladder / unpublish an artifact,
        so a wrong destructive call can dead-end the goal under a budget.
    """

    id: str
    name: str
    description: str
    params: tuple[Param, ...]
    preconditions: Callable[[State, dict[str, str]], bool]
    effect: Callable[[State, dict[str, str]], State]
    decoy: bool = False
    destructive: bool = False


@dataclass(frozen=True)
class GoalClause:
    """One conjunct of the goal predicate. Either a service must be at (or
    above) a ladder state, an artifact at a lifecycle state, or a flag at a
    value. The full goal is the conjunction of all clauses."""

    target_kind: str  # "service" | "artifact" | "flag"
    target_id: str
    # For services: minimum ladder state (>= in ladder order). For artifacts:
    # exact lifecycle. For flags: exact bool.
    value: str | bool

    def satisfied(self, state: State) -> bool:
        if self.target_kind == "service":
            return SERVICE_LEVEL[state.service_state(self.target_id)] >= SERVICE_LEVEL[
                self.value  # type: ignore[index]
            ]
        if self.target_kind == "artifact":
            return state.artifact_state(self.target_id) == self.value
        if self.target_kind == "flag":
            return state.flag(self.target_id) == self.value
        raise ValueError(f"unknown goal target_kind: {self.target_kind!r}")


@dataclass(frozen=True)
class Goal:
    clauses: tuple[GoalClause, ...]

    def satisfied(self, state: State) -> bool:
        return all(c.satisfied(state) for c in self.clauses)


@dataclass(frozen=True)
class Call:
    """One tool call in a plan: a tool id and its concrete arg binding."""

    tool: str
    args: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolWorld:
    """The full latent instance: entities, the tool catalog offered to the
    model, the described initial state, and the goal. `optimal_depth` is the
    ground-truth minimum plan length, filled in by `validate()` /
    `generate()` via bounded search (None until proven solvable)."""

    services: list[Service]
    artifacts: list[Artifact]
    flags: list[Flag]
    catalog: list[Tool]
    initial_state: State
    goal: Goal
    # Narration order over the "described events" that establish the initial
    # state -- a permutation of indices into `state_narration`. Higher
    # state_obscurity scrambles/indirects this.
    state_narration: list[str]
    optimal_depth: int | None = None
    budget: int = 0
