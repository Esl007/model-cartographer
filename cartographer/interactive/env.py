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

COPY-FIDELITY DIMS (docs/spec-ticket-fabrication.md). All default to the
behaviour above, byte for byte; none touches ground truth (BFS plan, ticket
checking) -- only the form, timing and surroundings of the tickets:
  * `ticket_entropy` 0..1 -- the ticket's FORM, from opaque hex (`tkt-3f9a1c`)
    to pattern-suggestive strings that look derivable from context but are
    not (`ticket_depth_flag_1_482913`); the value is always random.
  * `ticket_length` -- hex characters in the ticket body (0 = legacy 6).
  * `carry_distance` / `n_live_tickets` -- CONTROLLED links: k producers at
    consecutive plan steps, each consumed exactly `carry_distance` steps
    later, so up to min(k, d) tickets are held at once. 0/0 = legacy links.
  * `decoy_tickets` -- every successful result also shows up to 3 labelled,
    plausible but wrong ticket-like strings (previous/example/cached).
An invalid ticket is classified by what was submitted: missing, decoy,
stale (an older ticket of the same producer), wrong_producer, corrupted
(a near-copy of the right one) or FABRICATED (seen nowhere).

ABLATIONS (answer-key preserving, for attributing a confirmed failure):
`restate_tickets` (every result restates the currently valid tickets),
`copy_instruction` (one system-prompt line: copy tickets exactly) and
`opaque_names` (tool and entity ids renamed to patternless tokens).

Everything is deterministic given (world, seed, spec, sequence of calls).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
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


def controlled_links(plan: list[Call], seed: int, n_live: int, carry: int) -> tuple[tuple[ValueLink, ...], tuple]:
    """k = `n_live` producers at consecutive plan steps, each consumed exactly
    d = `carry` steps later, so min(k, d) tickets are live at once (k is
    clamped to d). Returns
    (links, positions) with positions [(producer_step, consumer_step)].

    A consumer tool may not occur in the plan before its producer step, so
    following the optimal plan always has the ticket in hand (ground truth is
    unchanged). Values beyond what the plan can host are clamped; the
    realised carry and live counts are reported in `InteractiveEnv.summary`."""
    L = len(plan)
    if L < 2:
        return (), ()
    d = min(max(int(carry), 1), L - 1)
    # with k > d a consumer step would collide with a later producer step, so
    # at most d tickets can be live at once: k is clamped to d (and the plan)
    k = min(max(int(n_live), 1), d, L - d)
    tools = [c.tool for c in plan]
    first: dict[str, int] = {}
    for i, t in enumerate(tools):
        first.setdefault(t, i)
    starts = list(range(0, L - d - k + 1))
    rot = _sha_int(str(seed), "carry-start") % len(starts)
    best: list[tuple[ValueLink, tuple[int, int]]] = []
    for s in starts[rot:] + starts[:rot]:
        chosen: list[tuple[ValueLink, tuple[int, int]]] = []
        used: set[str] = set()
        for m in range(k):
            pi, ci = s + m, s + m + d
            tp, tc = tools[pi], tools[ci]
            if tp == tc or tp in used or tc in used or first[tc] <= pi:
                continue
            used |= {tp, tc}
            chosen.append((ValueLink(producer=tp, consumer=tc), (pi, ci)))
        if len(chosen) > len(best):
            best = chosen
        if len(best) == k:
            break
    return tuple(l for l, _ in best), tuple(pos for _, pos in best)


def _ticket_form(level: int, entity: str, digest: str, length: int) -> str:
    """Ticket text at a suggestiveness level (0 opaque .. 3 pattern-shaped)."""
    body = digest[:length]
    if level <= 0:
        return f"tkt-{body}"
    if level == 1:
        return f"tkt-{entity}-{body}"
    if level == 2:
        return f"ticket_{entity}_{body}"
    return f"ticket_{entity}_{str(int(digest, 16))[-max(length, 5):]}"


@dataclass(frozen=True)
class InteractiveSpec:
    value_links: float = 0.0  # fraction of dependency edges that carry a ticket
    fault_step: int = 0  # 1-based call number that fails transiently; 0 = none
    style: str = "explicit"  # tool-description style ("implicit" strips preconditions)
    # copy-fidelity dims (module docstring); defaults = legacy behaviour
    ticket_entropy: float = 0.0
    ticket_length: int = 0
    carry_distance: int = 0
    n_live_tickets: int = 0
    decoy_tickets: int = 0
    # answer-key-preserving ablations
    restate_tickets: bool = False
    copy_instruction: bool = False
    opaque_names: bool = False
    # Evidence Fidelity Campaign (docs/spec-evidence-fidelity-campaign.md; all default off):
    # A `ticket_guessable`: the issued ticket is the bare pattern scaffold `ticket_<entity>`, so
    #   regenerating the pattern IS copying (prior agrees); D `ticket_emphasis`: the ticket is shown
    #   as `<<value>>` at issuance (decoys likewise), the delimiters never part of the value;
    # `restate_chain`: the causal scaffold for the transfer arm -- every result restates which
    #   tool needs which producer's ticket (already in the tool descriptions; no ticket values).
    ticket_guessable: bool = False
    ticket_emphasis: bool = False
    restate_chain: bool = False

    @property
    def controlled(self) -> bool:
        return self.carry_distance > 0 or self.n_live_tickets > 0

    @property
    def ticket_level(self) -> int:
        return min(3, max(0, int(self.ticket_entropy * 4)))


@dataclass
class StepRecord:
    step: int
    tool: str
    args: dict
    ok: bool
    error_kind: str | None
    result: dict
    distance_after: int | None
    ticket_error: str | None = None  # invalid_ticket subtype (missing/decoy/stale/...)


@dataclass
class InteractiveEnv:
    world: ToolWorld
    seed: int
    spec: InteractiveSpec = field(default_factory=InteractiveSpec)

    def __post_init__(self) -> None:
        self.plan = optimal_plan(self.world, max_depth=max(self.world.budget, 1)) or []
        if self.spec.controlled:
            self.links, self.link_positions = controlled_links(
                self.plan, self.seed, self.spec.n_live_tickets, self.spec.carry_distance)
        else:
            self.links = choose_links(self.world, self.plan, self.seed, self.spec.value_links)
            first: dict[str, int] = {}
            for i, c in enumerate(self.plan):
                first.setdefault(c.tool, i)
            self.link_positions = tuple(
                (first[l.producer], first[l.consumer]) for l in self.links
                if l.producer in first and l.consumer in first)
        self.issued: dict[str, tuple[str, int]] = {}  # ticket -> (producer, call count)
        self.decoys_shown: set[str] = set()
        self._fwd, self._rev = self._build_rename() if self.spec.opaque_names else ({}, {})
        self.producer_for = {l.consumer: l.producer for l in self.links}
        self.producers = {l.producer for l in self.links}
        self.state = self.world.initial_state
        self.tickets: dict[str, str] = {}
        self.producer_calls: dict[str, int] = {}
        self.call_budget = self.world.budget + (1 if self.spec.fault_step else 0)
        self.steps: list[StepRecord] = []
        self._fault_fired = False
        # A `ticket_guessable` world is unusable when two producers on the plan would issue the same
        # scaffold (`ticket_<entity>` collides): the evaluator records it ERROR, never a model result.
        self.guessable_unavailable = False
        if self.spec.ticket_guessable:
            ents = [self._entity_for(c.tool, {k: str(v) for k, v in c.args.items()})
                    for c in self.plan if c.tool in self.producers]
            self.guessable_unavailable = len(ents) != len(set(ents))
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

    # -- naming (opaque_names ablation) --------------------------------------
    def _build_rename(self) -> tuple[dict, dict]:
        w = self.world
        ids = [t.id for t in w.catalog] + [s.id for s in w.services] + [a.id for a in w.artifacts] \
            + [f.id for f in w.flags]
        fwd: dict[str, str] = {}
        used: set[str] = set()
        alphabet = "bcdfghjkmnpqrstvwxz"
        for i, orig in enumerate(dict.fromkeys(ids)):
            k = 0
            while True:
                h = _sha_int(str(self.seed), "opaque", orig, str(k))
                tok = alphabet[h % 19] + "".join("bcdfghjkmnpqrstvwxz23456789"[(h >> (5 * j)) % 27] for j in range(1, 5))
                if tok not in used:
                    break
                k += 1
            used.add(tok)
            fwd[orig] = tok
        return fwd, {v: k for k, v in fwd.items()}

    def _out(self, text: str) -> str:
        """Rename every id in model-facing text (identity unless opaque_names)."""
        if not self._fwd or not text:
            return text
        if not hasattr(self, "_rx"):
            alts = "|".join(re.escape(k) for k in sorted(self._fwd, key=len, reverse=True))
            self._rx = re.compile(rf"(?<![A-Za-z0-9_\-])({alts})(?![A-Za-z0-9_\-])")
        return self._rx.sub(lambda m: self._fwd[m.group(1)], text)

    def _out_result(self, result: dict) -> dict:
        if not self._fwd:
            return result
        return {k: (self._out(v) if isinstance(v, str) and k == "error" else v) for k, v in result.items()}

    def _entity_for(self, tool: str, call_args: dict) -> str:
        raw = next(iter(call_args.values()), tool) if call_args else tool
        return self._out(str(raw)).replace("-", "_")

    def _bare(self, value):
        """A submitted ticket with `ticket_emphasis` delimiters removed (identity otherwise)."""
        if self.spec.ticket_emphasis and isinstance(value, str):
            v = value.strip()
            if v.startswith("<<") and v.endswith(">>"):
                return v[2:-2].strip()
        return value

    def _shown(self, ticket: str) -> str:
        return f"<<{ticket}>>" if self.spec.ticket_emphasis else ticket

    def _new_ticket(self, tool: str, count: int, entity: str) -> str:
        if self.spec.ticket_guessable:
            return f"ticket_{entity}"
        digest = hashlib.sha256(f"{self.seed}|{tool}|{count}".encode("utf-8")).hexdigest()
        length = min(max(self.spec.ticket_length, 4), 64) if self.spec.ticket_length else 6
        return _ticket_form(self.spec.ticket_level, entity, digest, length)

    def _decoys(self, n: int, entity: str) -> dict:
        out: dict[str, str] = {}
        length = min(max(self.spec.ticket_length, 4), 64) if self.spec.ticket_length else 6
        for k, label in enumerate(("previous_ticket", "example_ticket", "cached_ticket")[: self.spec.decoy_tickets]):
            digest = hashlib.sha256(f"{self.seed}|decoy|{n}|{k}".encode("utf-8")).hexdigest()
            out[label] = _ticket_form(self.spec.ticket_level, entity, digest, length)
            self.decoys_shown.add(out[label])
            out[label] = self._shown(out[label])
        return out

    def classify_ticket_error(self, submitted, producer: str) -> str:
        submitted = self._bare(submitted)
        expected = self.tickets.get(producer)
        if submitted is None or str(submitted).strip() == "":
            return "missing"
        sub = str(submitted).strip()
        if sub in self.decoys_shown:
            return "decoy"
        if sub in self.issued:
            return "stale" if self.issued[sub][0] == producer else "wrong_producer"
        # "corrupted" = a near-copy of the ticket's RANDOM part. Comparing whole
        # strings would credit a pattern-shaped fabrication for sharing the
        # `ticket_<entity>_` scaffold with a pattern-shaped real ticket.
        if expected is not None:
            body = re.split(r"[-_]", expected)[-1]
            tail = re.split(r"[-_]", sub)[-1]
            if difflib.SequenceMatcher(None, tail, body).ratio() >= 0.75:
                return "corrupted"
        return "fabricated"

    # -- acting ------------------------------------------------------------
    def step(self, tool: str, args: dict) -> dict:
        n = len(self.steps) + 1
        args = {str(k): v for k, v in (args or {}).items()}
        if self._rev:  # opaque_names: translate model-facing ids back
            tool = self._rev.get(tool, tool)
            args = {k: (self._rev.get(v, v) if isinstance(v, str) and k != "ticket" else v) for k, v in args.items()}
        ok, kind, ticket_error = False, None, None
        if self.spec.fault_step and n == self.spec.fault_step and not self._fault_fired:
            self._fault_fired = True
            kind, result = "transient", {"status": "error", "error": TRANSIENT_ERROR}
        elif tool in self.producer_for and self._bare(args.get("ticket")) != self.tickets.get(self.producer_for[tool]):
            producer = self.producer_for[tool]
            kind = "invalid_ticket"
            ticket_error = self.classify_ticket_error(args.get("ticket"), producer)
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
                entity = self._entity_for(tool, call_args)
                if tool in self.producers:
                    count = self.producer_calls.get(tool, 0) + 1
                    self.producer_calls[tool] = count
                    ticket = self._new_ticket(tool, count, entity)
                    self.tickets[tool] = ticket
                    self.issued[ticket] = (tool, count)
                    result["ticket"] = self._shown(ticket)
                if self.spec.decoy_tickets:
                    result.update(self._decoys(n, entity))
        if self.spec.restate_tickets and self.tickets:
            result["valid_tickets"] = {
                f"from {self._out(p)}": t for p, t in sorted(self.tickets.items())
            }
        if self.spec.restate_chain and self.producer_for:
            result["ticket_dependencies"] = {
                self._out(c): f"needs the ticket from the most recent successful call to {self._out(p)}"
                for c, p in sorted(self.producer_for.items())
            }
        result = self._out_result(result)
        self.steps.append(
            StepRecord(n, tool, args, ok, kind, result, self.distance(self.state), ticket_error)
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
            if self._fwd:
                desc = self._out(desc)
                props = {k: {**v, "description": self._out(v["description"])} for k, v in props.items()}
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": self._out(t.id),
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
        if self.spec.copy_instruction:
            system += (" Ticket values are opaque: always copy them exactly as returned, "
                       "never construct or guess them.")
        return [{"role": "system", "content": system}, {"role": "user", "content": self._out(user)}]

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
            "ticket_errors": self._ticket_error_counts(),
            "fabricated_tickets": sum(1 for s in self.steps if s.ticket_error == "fabricated"),
            "carry_distance": (
                sum(c - p for p, c in self.link_positions) / len(self.link_positions)
                if self.link_positions else 0.0
            ),
            "max_live_tickets": max(
                (sum(1 for p, c in self.link_positions if p <= t < c) for t in range(len(self.plan))),
                default=0,
            ),
        }

    def _ticket_error_counts(self) -> dict:
        out: dict[str, int] = {}
        for st in self.steps:
            if st.ticket_error:
                out[st.ticket_error] = out.get(st.ticket_error, 0) + 1
        return out

    def transcript(self) -> str:
        return json.dumps(
            [
                {"step": s.step, "tool": s.tool, "args": s.args, "ok": s.ok,
                 "error_kind": s.error_kind, "result": s.result, "distance_after": s.distance_after,
                 **({"ticket_error": s.ticket_error} if s.ticket_error else {})}
                for s in self.steps
            ]
        )
