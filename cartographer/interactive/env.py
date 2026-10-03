"""The interactive environment: a `ToolWorld` executed one call at a time.

Three things are layered on top of the Province II world, none of which
changes its ground truth (the BFS-optimal plan length is identical):

* VALUE LINKS. Some steps of the optimal plan directly enable a later step
  (removing the earlier call makes the later one fail). On a chosen subset of
  those dependency edges, the earlier tool returns an opaque ticket
  (`tkt-3f9a1c`) and the later tool must be passed that exact ticket. Tickets
  are derived from sha256 over the world seed and the producer's call count,
  so they cannot be predicted from the prompt: the only way to get one is to
  make the call and read the result. A plan written blind cannot succeed.
* FAULTS. Optionally, call number `fault_step` fails transiently ("temporarily
  unavailable; nothing changed; retry") without touching state. Recovering
  means reading the error and retrying.
* PER-STEP DISTANCE. After every call the remaining BFS distance to the goal is
  recomputed, so a trajectory shows exactly the first step that failed to
  make progress -- not just whether the episode ended well.

Everything is deterministic given (world, seed, spec, sequence of calls).
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field, replace

from ..toolworld.model import Call, State, Tool, ToolWorld
from ..toolworld.render import _entity_reference_prose, _goal_clause_prose, _strip_precondition_sentences
from ..toolworld.simulate import _enumerate_calls, apply_call, optimal_depth

TRANSIENT_ERROR = (
    "transient: the service is temporarily unavailable and nothing was changed. "
    "Retry the same call."
)


def _sha_int(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:15], 16)


def optimal_plan(world: ToolWorld, max_depth: int) -> list[Call] | None:
    """Bounded BFS returning one shortest plan (deterministic: same
    enumeration order as `simulate.optimal_depth`), or None if unreachable."""
    start = world.initial_state
    if world.goal.satisfied(start):
        return []
    all_calls = _enumerate_calls(world)
    parent: dict[State, tuple[State, Call] | None] = {start: None}
    frontier: deque[tuple[State, int]] = deque([(start, 0)])
    while frontier:
        state, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        for call in all_calls:
            step = apply_call(world, state, call)
            if not step.ok or step.state in parent:
                continue
            parent[step.state] = (state, call)
            if world.goal.satisfied(step.state):
                plan: list[Call] = []
                cur: State = step.state
                while parent[cur] is not None:
                    prev, c = parent[cur]  # type: ignore[misc]
                    plan.append(c)
                    cur = prev
                return list(reversed(plan))
            frontier.append((step.state, depth + 1))
    return None


def _runs_ok(world: ToolWorld, calls: list[Call]) -> State:
    """Execute `calls` from the initial state, skipping any that fail."""
    state = world.initial_state
    for c in calls:
        r = apply_call(world, state, c)
        if r.ok:
            state = r.state
    return state


def dependency_edges(world: ToolWorld, plan: list[Call]) -> list[tuple[int, int]]:
    """(i, j) for each plan step j whose DIRECT prerequisite is step i: the
    latest earlier step whose removal makes step j's call fail. At most one
    producer per consumer; a producer may feed several consumers."""
    edges: list[tuple[int, int]] = []
    for j in range(1, len(plan)):
        for i in range(j - 1, -1, -1):
            if plan[i].tool == plan[j].tool:
                continue
            state = _runs_ok(world, plan[:i] + plan[i + 1 : j])
            if not apply_call(world, state, plan[j]).ok:
                edges.append((i, j))
                break
    return edges


@dataclass(frozen=True)
class ValueLink:
    producer: str  # tool id whose successful call returns a ticket
    consumer: str  # tool id that must be passed that ticket


def choose_links(
    world: ToolWorld, plan: list[Call], seed: int, fraction: float
) -> tuple[ValueLink, ...]:
    """The first round(fraction * n) dependency edges in a seed-dependent
    order. The order is fixed per seed, so a higher fraction is always a
    superset of a lower one -- the difficulty ray is monotone."""
    fraction = min(max(fraction, 0.0), 1.0)
    seen: set[tuple[str, str]] = set()
    candidates: list[ValueLink] = []
    for i, j in dependency_edges(world, plan):
        key = (plan[i].tool, plan[j].tool)
        # One ticket per consumer tool: if two edges share a consumer, keep the first.
        if key in seen or any(link.consumer == key[1] for link in candidates):
            continue
        seen.add(key)
        candidates.append(ValueLink(producer=key[0], consumer=key[1]))
    candidates.sort(key=lambda l: (_sha_int(str(seed), "link", l.producer, l.consumer), l.consumer))
    k = int(round(fraction * len(candidates)))
    return tuple(candidates[:k])


@dataclass(frozen=True)
class InteractiveSpec:
    value_links: float = 0.0  # fraction of dependency edges that carry a ticket
    fault_step: int = 0  # 1-based call number that fails transiently; 0 = none
    style: str = "explicit"  # tool-description style ("implicit" strips preconditions)


@dataclass
class StepRecord:
    step: int
    tool: str
    args: dict
    ok: bool
    error_kind: str | None
    result: dict
    distance_after: int | None


@dataclass
class InteractiveEnv:
    world: ToolWorld
    seed: int
    spec: InteractiveSpec = field(default_factory=InteractiveSpec)

    def __post_init__(self) -> None:
        self.plan = optimal_plan(self.world, max_depth=max(self.world.budget, 1)) or []
        self.links = choose_links(self.world, self.plan, self.seed, self.spec.value_links)
        self.producer_for = {l.consumer: l.producer for l in self.links}
        self.producers = {l.producer for l in self.links}
        self.state = self.world.initial_state
        self.tickets: dict[str, str] = {}
        self.producer_calls: dict[str, int] = {}
        self.call_budget = self.world.budget + (1 if self.spec.fault_step else 0)
        self.steps: list[StepRecord] = []
        self._fault_fired = False
        self._dist: dict[State, int | None] = {}
        self.initial_distance = self.distance(self.state)

    # -- ground truth ------------------------------------------------------
    def distance(self, state: State) -> int | None:
        if state not in self._dist:
            probe = replace(self.world, initial_state=state, optimal_depth=None)
            self._dist[state] = optimal_depth(probe, max_depth=max(self.world.budget, 1))
        return self._dist[state]

    @property
    def goal_reached(self) -> bool:
        return self.world.goal.satisfied(self.state)

    @property
    def calls_left(self) -> int:
        return self.call_budget - len(self.steps)

    # -- acting ------------------------------------------------------------
    def step(self, tool: str, args: dict) -> dict:
        n = len(self.steps) + 1
        args = {str(k): v for k, v in (args or {}).items()}
        ok, kind = False, None
        if self.spec.fault_step and n == self.spec.fault_step and not self._fault_fired:
            self._fault_fired = True
            kind, result = "transient", {"status": "error", "error": TRANSIENT_ERROR}
        elif tool in self.producer_for and args.get("ticket") != self.tickets.get(self.producer_for[tool]):
            producer = self.producer_for[tool]
            kind = "invalid_ticket"
            result = {
                "status": "error",
                "error": (
                    f"invalid_ticket: `ticket` must be the exact ticket returned by the most "
                    f"recent successful call to {producer}."
                ),
            }
        else:
            call_args = {k: str(v) for k, v in args.items() if k != "ticket"}
            r = apply_call(self.world, self.state, Call(tool=tool, args=call_args))
            if not r.ok:
                kind = r.detail.split(":", 1)[0] or "invalid_call"
                result = {"status": "error", "error": r.detail}
            else:
                ok = True
                self.state = r.state
                result = {"status": "ok"}
                if tool in self.producers:
                    count = self.producer_calls.get(tool, 0) + 1
                    self.producer_calls[tool] = count
                    ticket = "tkt-" + hashlib.sha256(
                        f"{self.seed}|{tool}|{count}".encode("utf-8")
                    ).hexdigest()[:6]
                    self.tickets[tool] = ticket
                    result["ticket"] = ticket
        self.steps.append(
            StepRecord(n, tool, args, ok, kind, result, self.distance(self.state))
        )
        return result

    # -- presentation ------------------------------------------------------
    def tools_schema(self) -> list[dict]:
        ids = {
            "service": [s.id for s in self.world.services],
            "artifact": [a.id for a in self.world.artifacts],
            "flag": [f.id for f in self.world.flags],
        }
        out = []
        for t in self.world.catalog:
            desc = t.description
            if self.spec.style == "implicit":
                desc = _strip_precondition_sentences(desc)
            props: dict = {
                p.name: {"type": "string", "description": f"a {p.kind} id, one of: {', '.join(ids[p.kind])}"}
                for p in t.params
            }
            required = [p.name for p in t.params]
            if t.id in self.producers:
                desc += " On success it returns a `ticket`."
            if t.id in self.producer_for:
                producer = self.producer_for[t.id]
                props["ticket"] = {
                    "type": "string",
                    "description": f"the exact ticket returned by the most recent successful call to {producer}",
                }
                required.append("ticket")
                desc += f" Requires `ticket` from the most recent successful call to {producer}."
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.id,
                        "description": desc,
                        "parameters": {"type": "object", "properties": props, "required": required},
                    },
                }
            )
        return out

    def opening_messages(self) -> list[dict]:
        w = self.world
        user = (
            "CURRENT STATE:\n" + "\n".join(f"- {l}" for l in w.state_narration)
            + "\n\nGOAL (all conditions must hold):\n"
            + "\n".join(f"- {_goal_clause_prose(c, w)}" for c in w.goal.clauses)
            + "\n\nENTITY IDS:\n" + _entity_reference_prose(w)
            + f"\n\nYou may make at most {self.call_budget} tool calls."
        )
        system = (
            "You operate a deployment environment through the provided tools. Call one tool "
            "at a time and read each result before deciding the next call. Some tools return "
            "a ticket that a later tool needs: pass it exactly as returned. If a call fails, "
            "read the error and adjust. When the goal is reached, reply DONE without calling "
            "a tool."
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    # -- outcome -----------------------------------------------------------
    def summary(self) -> dict:
        errors = [s for s in self.steps if not s.ok]
        first_dev = None
        prev = self.initial_distance
        for s in self.steps:
            d = s.distance_after
            if prev is not None and (d is None or d >= prev):
                first_dev = s.step
                break
            prev = d
        return {
            "goal_reached": self.goal_reached,
            "n_calls": len(self.steps),
            "optimal": self.initial_distance,
            "n_errors": len(errors),
            "first_error_kind": errors[0].error_kind if errors else None,
            "invalid_tickets": sum(1 for s in errors if s.error_kind == "invalid_ticket"),
            "recovered": bool(errors) and self.goal_reached,
            "first_deviation_step": first_dev,
            "n_links": len(self.links),
            "final_distance": self.distance(self.state),
        }

    def transcript(self) -> str:
        return json.dumps(
            [
                {"step": s.step, "tool": s.tool, "args": s.args, "ok": s.ok,
                 "error_kind": s.error_kind, "result": s.result, "distance_after": s.distance_after}
                for s in self.steps
            ]
        )
