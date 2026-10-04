"""Latent world representation for Phase 1: witnessed event logs.

A `World` is a chronological event log over entities/objects/locations, plus
a `narration_order` (the presentation order, possibly scrambled relative to
chronology) and a `Query` whose ground-truth answer is derived purely by
forward simulation (see `simulate.py`), never authored by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    gender: str  # "f" or "m"; used for pronoun-collision / coreference potential


@dataclass(frozen=True)
class Obj:
    id: str
    name: str
    category: str


@dataclass(frozen=True)
class Location:
    id: str
    name: str


@dataclass(frozen=True)
class Move:
    """`actor` relocates `obj` from `src` to `dst`."""

    obj: str
    src: str
    dst: str
    actor: str
    time: int
    witnesses: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Transfer:
    """`giver` hands `obj` to `receiver`; `receiver` becomes the new holder."""

    giver: str
    obj: str
    receiver: str
    time: int
    witnesses: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Tell:
    """`speaker` claims to `listener` that `obj` is at `claimed_location`.

    The claim may be false relative to the world's true state. `listener`
    always witnesses a Tell (it is how they come to believe the claim).
    """

    speaker: str
    listener: str
    obj: str
    claimed_location: str
    time: int
    witnesses: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Filler:
    """No state effect; exists to pad context length / narration."""

    actor: str
    description_seed: int
    time: int
    witnesses: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class NoOp:
    """A seemingly relevant but INERT clause about `obj` (GSM-NoOp-style
    distractor, Mirzadeh et al. 2024): `actor` considers / declines / plans /
    is asked to relocate `obj` to `loc`, but nothing happens.

    `kind` is one of "declined" | "negated" | "planned" | "refused" (the
    surface family; `other` is the asking entity for "refused"). The
    simulator applies it as a no-op: no location, holder, or belief changes,
    even for its witnesses -- so a reader who treats it as a real move is
    wrong by construction.
    """

    actor: str
    obj: str
    loc: str
    kind: str
    time: int
    witnesses: frozenset[str] = field(default_factory=frozenset)
    other: str | None = None


NOOP_KINDS = ("declined", "negated", "planned", "refused")

Event = Move | Transfer | Tell | Filler | NoOp


@dataclass(frozen=True)
class Query:
    kind: str  # "state" | "belief" | "order"
    subject: str  # obj id
    agent: str | None = None  # for belief queries
    pair: tuple[int, int] | None = None  # event indices, for order queries
    expect_unknown: bool = False


@dataclass(frozen=True)
class World:
    entities: list[Entity]
    objects: list[Obj]
    locations: list[Location]
    events: list[Event]  # chronological order, indexed by event.time
    narration_order: list[int]  # permutation of event indices: presentation order
    query: Query
    relative_times: frozenset[int] = frozenset()
    """Event TIMES (not indices) whose sentences drop the clock anchor and
    instead carry a relative cue ("Later, ...", "Before that, ...") stated
    against the previously NARRATED event. Keyed by time so it survives
    reindexing ablations that keep surviving events' times. Empty = every
    sentence carries its clock time (pre-relational_time behavior)."""
