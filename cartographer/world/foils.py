"""Foil model: what a *shallow reasoner* would answer, vs. the true answer.

Motivation (see docs/lab-log.md, 2026-09-18 entry): across post-witness-fix
belief FAILs on Nano/Super/Ultra, the dominant failure mode is the model
answering the object's TRUE FINAL LOCATION (or some other superficially
plausible shortcut) instead of the queried agent's actual believed location.
Meanwhile the five scalar difficulty knobs barely move pass rates -- they
scale magnitude, not the salience of a plausible-but-wrong answer. This
module makes "how many wrong-but-plausible answers does this task admit, and
how late do they get planted" a measurable property of a generated `World`,
independently of any model call.

Everything here is pure simulation over an already-generated `World` plus
its `NameMap` (for turning location/entity ids into the surface strings a
verifier would compare against) -- no API calls, no randomness beyond what's
already baked into the world.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .model import Filler, Move, Query, Tell, Transfer, World
from .simulate import answer as sim_answer
from .simulate import simulate

if TYPE_CHECKING:
    # Only needed for type hints -- importing `render.naming` at module
    # scope would create a circular import, since `render/naming.py`
    # imports `world.model`, which runs through this package's `__init__`.
    from ..render.naming import NameMap


def _loc_name(loc_id: str | None, name_map: "NameMap") -> str | None:
    if loc_id is None:
        return None
    return name_map.location.get(loc_id, loc_id)


def _mention_order_locations(world: World) -> list[str]:
    """Location ids, in NARRATION order (not chronological), for every
    event that names a location -- i.e. every `Move` (destination) and
    `Tell` (claimed location). Used to build "last/first mentioned"
    foils and to weight lateness by mention position."""
    locs: list[str] = []
    for idx in world.narration_order:
        event = world.events[idx]
        if isinstance(event, Move):
            locs.append(event.dst)
        elif isinstance(event, Tell):
            locs.append(event.claimed_location)
    return locs


def _chain_moves_narration_order(world: World) -> list[int]:
    """Event indices of `Move`s on the query's subject object, in
    NARRATION order."""
    subject = world.query.subject
    return [
        idx
        for idx in world.narration_order
        if isinstance(world.events[idx], Move) and world.events[idx].obj == subject
    ]


def _last_witnessed_by_anyone(world: World) -> str | None:
    """Destination of the chronologically-last Move on the subject object
    that had ANY non-empty witness set (actor always counts as a witness
    for this purpose, since the actor by definition observes their own
    action) -- i.e. "the last time literally anybody saw this object move,
    where did it end up." A shallow reasoner who conflates "someone saw it
    happen" with "the query agent knows this" lands here."""
    subject = world.query.subject
    last_dst: str | None = None
    for event in world.events:  # chronological
        if isinstance(event, Move) and event.obj == subject and event.witnesses:
            last_dst = event.dst
    return last_dst


def _query_agent_own_last_move(world: World) -> str | None:
    """If the belief query's agent themselves ever acted as the mover of
    the subject object, the destination of their OWN last such move
    (chronological) -- the foil for "I did it, so I must know where it
    ended up," which ignores whether the agent kept witnessing afterward."""
    query = world.query
    if query.kind != "belief" or query.agent is None:
        return None
    subject = query.subject
    last_dst: str | None = None
    for event in world.events:
        if isinstance(event, Move) and event.obj == subject and event.actor == query.agent:
            last_dst = event.dst
    return last_dst


def _most_frequent_location(world: World) -> str | None:
    """The location that shows up most often as a Move destination for the
    subject object (ties broken by first occurrence in narration order) --
    the foil for "just say whichever room got mentioned the most.\""""
    subject = world.query.subject
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for pos, idx in enumerate(world.narration_order):
        event = world.events[idx]
        if isinstance(event, Move) and event.obj == subject:
            counts[event.dst] += 1
            first_seen.setdefault(event.dst, pos)
    if not counts:
        return None
    best = max(counts.items(), key=lambda kv: (kv[1], -first_seen[kv[0]]))
    return best[0]


def _first_mentioned_location(world: World) -> str | None:
    """The FIRST location mentioned in narration order for the subject
    object (Move destination or Tell claim) -- the state-query analogue of
    "last mentioned": a shallow reasoner who anchors on the opening of the
    story rather than tracking the object forward."""
    subject = world.query.subject
    for idx in world.narration_order:
        event = world.events[idx]
        if isinstance(event, Move) and event.obj == subject:
            return event.dst
        if isinstance(event, Tell) and event.obj == subject:
            return event.claimed_location
    return None


def _last_mentioned_location(world: World) -> str | None:
    """The LAST location mentioned in narration order for the subject
    object (Move destination or Tell claim), regardless of chronological
    time or witnessing -- the foil for "trust narration order, not the
    timestamps or who-saw-what.\""""
    subject = world.query.subject
    last: str | None = None
    for idx in world.narration_order:
        event = world.events[idx]
        if isinstance(event, Move) and event.obj == subject:
            last = event.dst
        elif isinstance(event, Tell) and event.obj == subject:
            last = event.claimed_location
    return last


def _narration_order_answer(world: World) -> str | None:
    """For `order` queries only: what you'd answer if you trusted
    NARRATION order (the order sentences appear in the prompt) instead of
    the events' real timestamps -- i.e. "first" if `query.pair[0]` appears
    earlier in `world.narration_order` than `query.pair[1]`, else "second".
    This is the key foil for order queries: the whole point of `scramble`
    is to make narration order diverge from chronological order, and a
    shallow reasoner who just reads top-to-bottom falls for it."""
    query = world.query
    if query.kind != "order" or query.pair is None:
        return None
    i, j = query.pair
    position = {idx: pos for pos, idx in enumerate(world.narration_order)}
    return "first" if position[i] < position[j] else "second"


def foil_answers(world: World, name_map: NameMap) -> dict[str, str | None]:
    """What a shallow reasoner would answer, by named foil, keyed to
    `world.query.kind`. Values are surface strings (matching what
    `render/templates.py`/`render/verify.py` would compare against), or
    `None` when a given foil doesn't apply to this world (e.g. the query
    agent never moved the object themselves).

    Belief-query foils:
      - `final_true_state`: the object's actual final location (the belief
        query's most damaging trap -- what the model would say if it
        collapsed belief into reality).
      - `last_mentioned_location`: last location named in NARRATION order.
      - `last_witnessed_by_anyone`: destination of the last move ANYONE
        witnessed (not necessarily the query agent).
      - `query_agent_own_last_move`: if the query agent themselves moved
        the object, their own last such move's destination.
      - `most_frequent_location`: the most-mentioned destination.

    State-query foils:
      - `first_mentioned_location`, `most_frequent_location`.

    Order-query foils:
      - `narration_order_answer`: "first"/"second" trusting narration order
        over real timestamps -- the key foil for this query kind.
    """
    query = world.query
    result = simulate(world)

    if query.kind == "belief":
        assert query.agent is not None
        final_true = result.final_locations.get(query.subject)
        foils = {
            "final_true_state": _loc_name(final_true, name_map),
            "last_mentioned_location": _loc_name(_last_mentioned_location(world), name_map),
            "last_witnessed_by_anyone": _loc_name(_last_witnessed_by_anyone(world), name_map),
            "query_agent_own_last_move": _loc_name(
                _query_agent_own_last_move(world), name_map
            ),
            "most_frequent_location": _loc_name(_most_frequent_location(world), name_map),
        }
        return foils

    if query.kind == "state":
        return {
            "first_mentioned_location": _loc_name(_first_mentioned_location(world), name_map),
            "most_frequent_location": _loc_name(_most_frequent_location(world), name_map),
        }

    if query.kind == "order":
        return {
            "narration_order_answer": _narration_order_answer(world),
        }

    raise ValueError(f"unknown query kind: {query.kind!r}")


def _true_answer_surface(world: World, name_map: NameMap) -> str:
    result = sim_answer(world)
    if result.kind in ("state", "belief"):
        if result.value == "unknown":
            return "unknown"
        return _loc_name(result.value, name_map)  # type: ignore[return-value]
    return result.value  # order: "first"/"second"


def _mention_lateness(world: World, target_surface: str | None, name_map: NameMap) -> float:
    """How late (0..1, 1 = very last sentence) `target_surface` is first
    established in narration order, used to weight a diverging foil by
    salience. Looks at every location-naming event (Move destination, Tell
    claim) plus the order query's own event pair; falls back to 0.0 (least
    salient) if the surface string never appears via that path -- e.g. an
    "unknown"/absent foil, which can't be a late trap because it was never
    planted anywhere in the story.

    Weighting formula: for a candidate surface string s, let
    `positions(s)` be the set of narration positions (0-indexed, over the
    location-naming subsequence only) where s is mentioned. Lateness is
    `max(positions(s)) / (n - 1)` (0.0 if n <= 1), i.e. the fraction-of-the-
    way-through-the-story of s's LAST mention -- a wrong answer planted
    only at the very end of the story (right before the question) is
    maximally salient (lateness -> 1.0); one only mentioned in the opening
    sentence is minimally salient (lateness -> 0.0 or close to it).
    """
    if target_surface is None:
        return 0.0

    if world.query.kind == "order":
        # For order queries there's no "location mention" sequence to walk;
        # use the query pair's own narration positions instead. The
        # narration_order_answer foil is "late" exactly when the event that
        # makes it wrong (the one out of chronological order) is mentioned
        # near the end of the story.
        query = world.query
        assert query.pair is not None
        position = {idx: pos for pos, idx in enumerate(world.narration_order)}
        n = len(world.narration_order)
        if n <= 1:
            return 0.0
        last_pos = max(position[query.pair[0]], position[query.pair[1]])
        return last_pos / (n - 1)

    surfaces_in_order = []
    for idx in world.narration_order:
        event = world.events[idx]
        if isinstance(event, Move) and event.obj == world.query.subject:
            surfaces_in_order.append(_loc_name(event.dst, name_map))
        elif isinstance(event, Tell) and event.obj == world.query.subject:
            surfaces_in_order.append(_loc_name(event.claimed_location, name_map))

    n = len(surfaces_in_order)
    if n == 0:
        return 0.0
    matching_positions = [pos for pos, s in enumerate(surfaces_in_order) if s == target_surface]
    if not matching_positions:
        return 0.0
    if n <= 1:
        return 0.0
    return max(matching_positions) / (n - 1)


@dataclass(frozen=True)
class FoilProfile:
    """Salience-weighted summary of how much this generated world traps a
    shallow reasoner. A world where NO foil diverges (`n_diverging == 0`)
    is trivially easy: the lazy/shortcut answer happens to be correct. A
    world where many foils diverge, especially late-mentioned ones, is a
    genuine trap.
    """

    true_answer: str
    foils: dict[str, str | None] = field(default_factory=dict)
    n_diverging: int = 0
    salience_weighted_score: float = 0.0
    trap_kinds: tuple[str, ...] = ()


def foil_divergence(world: World, name_map: NameMap) -> FoilProfile:
    """Compute the `FoilProfile` for `world`.

    A foil "diverges" when its answer differs from the true answer AND is
    not `None` (a foil that doesn't apply, e.g. `query_agent_own_last_move`
    when the agent never moved the object, contributes nothing -- it isn't
    a trap because it was never planted).

    `salience_weighted_score` sums each diverging foil's lateness weight
    (see `_mention_lateness`'s docstring for the exact formula) -- so a
    world with one foil diverging at the very last sentence scores higher
    than one with several foils all diverging in the opening sentence.
    """
    true_answer = _true_answer_surface(world, name_map)
    foils = foil_answers(world, name_map)

    diverging: list[str] = []
    score = 0.0
    for name, value in foils.items():
        if value is None or value == true_answer:
            continue
        diverging.append(name)
        score += _mention_lateness(world, value, name_map)

    return FoilProfile(
        true_answer=true_answer,
        foils=foils,
        n_diverging=len(diverging),
        salience_weighted_score=score,
        trap_kinds=tuple(sorted(diverging)),
    )
