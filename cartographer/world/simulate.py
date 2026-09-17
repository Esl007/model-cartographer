"""Forward simulation over a `World`: ground truth is computed, never authored.

`simulate` walks the chronological event log once and derives final object
locations, holders, and per-entity beliefs. `answer` reads the query's
ground-truth value off a `SimResult`. `validate` flags well-posedness
violations without raising, so callers can assert on the returned list.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Move, Query, Tell, Transfer, World


@dataclass(frozen=True)
class SimResult:
    final_locations: dict[str, str]
    holder: dict[str, str | None]
    beliefs: dict[tuple[str, str], str]


@dataclass(frozen=True)
class Answer:
    value: str
    kind: str


def simulate(world: World) -> SimResult:
    final_locations: dict[str, str] = {}
    holder: dict[str, str | None] = {}
    beliefs: dict[tuple[str, str], str] = {}

    for event in world.events:
        if isinstance(event, Move):
            final_locations[event.obj] = event.dst
            for witness in event.witnesses:
                beliefs[(witness, event.obj)] = event.dst
        elif isinstance(event, Transfer):
            holder[event.obj] = event.receiver
            if event.obj in final_locations:
                for witness in event.witnesses:
                    beliefs[(witness, event.obj)] = final_locations[event.obj]
        elif isinstance(event, Tell):
            beliefs[(event.listener, event.obj)] = event.claimed_location

    return SimResult(
        final_locations=final_locations, holder=holder, beliefs=beliefs
    )


def answer(world: World) -> Answer:
    result = simulate(world)
    query = world.query
    if query.kind == "state":
        return Answer(value=result.final_locations[query.subject], kind="state")
    if query.kind == "belief":
        assert query.agent is not None
        value = result.beliefs.get((query.agent, query.subject), "unknown")
        return Answer(value=value, kind="belief")
    if query.kind == "order":
        assert query.pair is not None
        i, j = query.pair
        ti, tj = world.events[i].time, world.events[j].time
        return Answer(value="first" if ti < tj else "second", kind="order")
    raise ValueError(f"unknown query kind: {query.kind!r}")


def validate(world: World) -> list[str]:
    violations: list[str] = []

    entity_ids = {e.id for e in world.entities}
    object_ids = {o.id for o in world.objects}
    location_ids = {l.id for l in world.locations}

    if sorted(world.narration_order) != list(range(len(world.events))):
        violations.append("narration_order is not a permutation of event indices")

    for event in world.events:
        if isinstance(event, Move):
            if event.src not in location_ids:
                violations.append(f"Move references undefined src location {event.src!r}")
            if event.dst not in location_ids:
                violations.append(f"Move references undefined dst location {event.dst!r}")
            if event.actor not in entity_ids:
                violations.append(f"Move references undefined actor {event.actor!r}")
            if event.obj not in object_ids:
                violations.append(f"Move references undefined obj {event.obj!r}")
        elif isinstance(event, Transfer):
            if event.giver not in entity_ids:
                violations.append(f"Transfer references undefined giver {event.giver!r}")
            if event.receiver not in entity_ids:
                violations.append(f"Transfer references undefined receiver {event.receiver!r}")
            if event.obj not in object_ids:
                violations.append(f"Transfer references undefined obj {event.obj!r}")

    query = world.query
    if query.kind == "belief":
        result = simulate(world)
        has_belief = (query.agent, query.subject) in result.beliefs
        if not has_belief and not query.expect_unknown:
            violations.append(
                f"belief query on ({query.agent!r}, {query.subject!r}) has no "
                "belief entry but expect_unknown=False"
            )
    if query.kind == "order" and query.pair is not None:
        i, j = query.pair
        if world.events[i].time == world.events[j].time:
            violations.append("order query references events with equal time")

    return violations
