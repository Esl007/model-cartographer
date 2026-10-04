"""Canonical fact extraction from a latent `World`, and the deterministic
comparator used by the fidelity gate.

`world_facts` is the ground-truth fact list: every non-filler event in
`world.events`, in chronological order (NOT narration order — narration
order is a presentation detail; facts are about what actually happened),
translated to surface names via `name_map`. Filler events carry no state
and are excluded. Each fact also carries a `witnesses` field: the surface
names of entities present/observing per `event.witnesses`, EXCLUDING the
event's own actor-equivalent participants (who are already named by the
fact's other fields) — this is the LLM-rendering-path counterpart of
`cartographer.render.templates`'s witness sentences, added so the mandatory
fidelity gate holds LLM renderings to the same "don't erase bystanders"
standard as the template renderer (see docs/lab-log.md, 2026-09-17 witness
bug).

`facts_match` is a pure, deterministic comparator (no LLM involved) so the
fidelity gate's accept/reject decision is reproducible and auditable.
"""

from __future__ import annotations

import re
import string

from ..render.naming import NameMap
from ..render.templates import _time_anchor
from ..world.model import Filler, Move, NoOp, Tell, Transfer, World

_ARTICLES = {"the", "a", "an"}


def _normalize_field(value: str) -> str:
    """lowercase, strip punctuation, drop leading articles, collapse spaces."""
    value = value.strip().lower()
    value = value.translate(str.maketrans("", "", string.punctuation))
    tokens = [t for t in value.split() if t]
    while tokens and tokens[0] in _ARTICLES:
        tokens = tokens[1:]
    return " ".join(tokens)


def _fmt_time(time: int) -> str:
    """Same anchor format as templates.py, minus the leading 'At ' and
    trailing comma: "8:MM"."""
    anchor = _time_anchor(time)  # "At H:MM,"
    inner = anchor[len("At ") : -1]
    return inner


def _event_actor_ids(event) -> set[str]:
    """Entity ids already named by the fact's own type-specific fields —
    mirrors cartographer.render.templates._event_actor_ids."""
    if isinstance(event, Move):
        return {event.actor}
    if isinstance(event, Transfer):
        return {event.giver, event.receiver}
    if isinstance(event, Tell):
        return {event.speaker, event.listener}
    return set()


def _witness_names(event, name_map: NameMap) -> list[str]:
    non_actor = sorted(event.witnesses - _event_actor_ids(event))
    return [name_map.entity.get(eid, eid) for eid in non_actor]


def world_facts(world: World, name_map: NameMap) -> list[dict]:
    """Canonical fact list, one dict per non-filler event, in chronological
    (event-index / event-time) order."""
    facts: list[dict] = []
    for event in world.events:
        if isinstance(event, Move):
            facts.append(
                {
                    "time": _fmt_time(event.time),
                    "type": "move",
                    "actor": name_map.entity.get(event.actor, event.actor),
                    "object": name_map.obj.get(event.obj, event.obj),
                    "destination": name_map.location.get(event.dst, event.dst),
                    "witnesses": _witness_names(event, name_map),
                }
            )
        elif isinstance(event, Transfer):
            facts.append(
                {
                    "time": _fmt_time(event.time),
                    "type": "transfer",
                    "giver": name_map.entity.get(event.giver, event.giver),
                    "object": name_map.obj.get(event.obj, event.obj),
                    "receiver": name_map.entity.get(event.receiver, event.receiver),
                    "witnesses": _witness_names(event, name_map),
                }
            )
        elif isinstance(event, Tell):
            facts.append(
                {
                    "time": _fmt_time(event.time),
                    "type": "tell",
                    "speaker": name_map.entity.get(event.speaker, event.speaker),
                    "listener": name_map.entity.get(event.listener, event.listener),
                    "object": name_map.obj.get(event.obj, event.obj),
                    "claimed_location": name_map.location.get(
                        event.claimed_location, event.claimed_location
                    ),
                    "witnesses": _witness_names(event, name_map),
                }
            )
        elif isinstance(event, (Filler, NoOp)):
            # NoOps are inert (no state change) -- like Filler, they carry no
            # fact the fidelity gate must preserve.
            continue
        else:
            raise TypeError(f"unknown event type: {type(event)!r}")
    return facts


_FIELDS_BY_TYPE = {
    "move": ("actor", "object", "destination"),
    "transfer": ("giver", "object", "receiver"),
    "tell": ("speaker", "listener", "object", "claimed_location"),
}


def _normalized_fact(fact: dict) -> tuple:
    """A hashable, normalized signature: (type, time, normalized field values...)."""
    ftype = fact.get("type")
    fields = _FIELDS_BY_TYPE.get(ftype, ())
    time = fact.get("time", "")
    values = tuple(_normalize_field(str(fact.get(f, ""))) for f in fields)
    return (ftype, time, values)


def facts_match(expected: list[dict], extracted: list[dict]) -> tuple[bool, list[str]]:
    """Deterministic comparator between expected (ground-truth) facts and
    extracted (LLM-parsed) facts.

    Match rule: a fact matches iff type, time (exact string match), and all
    type-specific fields match after normalization (lowercase, punctuation
    stripped, leading article dropped).

    Violations:
    - "missing_fact: <detail>" for an expected fact with no matching extracted fact.
    - "hallucinated_fact: <detail>" for an extracted fact with no matching expected fact.
    - "altered_fact: <detail>" for an extracted fact that shares expected's time+type
      but differs in field values (reported instead of a missing/hallucinated pair
      when there is a unique same-time-and-type expected fact to compare against).
    - "missing_witness: <detail>" for a matched (or altered) fact pair where an
      expected witness name is absent from the extracted fact's "witnesses" list.
      A fact with no "witnesses" key is treated as an empty list (backward
      compatible with fact dicts that predate this field).
    """
    violations: list[str] = []

    expected_sigs = [_normalized_fact(f) for f in expected]
    extracted_sigs = [_normalized_fact(f) for f in extracted]

    extracted_matched = [False] * len(extracted_sigs)
    expected_matched = [False] * len(expected_sigs)
    matched_pairs: list[tuple[int, int]] = []

    # Pass 1: exact signature matches (type + time + all fields).
    for ei, esig in enumerate(expected_sigs):
        for xi, xsig in enumerate(extracted_sigs):
            if extracted_matched[xi]:
                continue
            if esig == xsig:
                expected_matched[ei] = True
                extracted_matched[xi] = True
                matched_pairs.append((ei, xi))
                break

    # Pass 2: for unmatched expected facts, look for an unmatched extracted
    # fact with the same (type, time) — that is an "altered_fact" (field
    # mismatch) rather than a separate missing+hallucinated pair.
    for ei, esig in enumerate(expected_sigs):
        if expected_matched[ei]:
            continue
        etype, etime, _ = esig
        for xi, xsig in enumerate(extracted_sigs):
            if extracted_matched[xi]:
                continue
            xtype, xtime, _ = xsig
            if xtype == etype and xtime == etime:
                expected_matched[ei] = True
                extracted_matched[xi] = True
                matched_pairs.append((ei, xi))
                violations.append(
                    f"altered_fact: expected {expected[ei]!r} but extracted {extracted[xi]!r}"
                )
                break

    # Remaining unmatched expected facts are genuinely missing.
    for ei, esig in enumerate(expected_sigs):
        if not expected_matched[ei]:
            violations.append(f"missing_fact: {expected[ei]!r}")

    # Remaining unmatched extracted facts are hallucinated.
    for xi, xsig in enumerate(extracted_sigs):
        if not extracted_matched[xi]:
            violations.append(f"hallucinated_fact: {extracted[xi]!r}")

    # Pass 3: for every matched (or altered) pair, every expected witness
    # name must also appear (after normalization) in the extracted fact's
    # witness list. This is independent of the core signature match so it
    # doesn't change pass 1/2 semantics for callers/tests that never set a
    # "witnesses" key.
    for ei, xi in matched_pairs:
        expected_witnesses = [_normalize_field(str(w)) for w in expected[ei].get("witnesses", [])]
        extracted_witnesses = {_normalize_field(str(w)) for w in extracted[xi].get("witnesses", [])}
        for raw, normalized in zip(expected[ei].get("witnesses", []), expected_witnesses):
            if normalized not in extracted_witnesses:
                violations.append(
                    f"missing_witness: expected witness {raw!r} for fact {expected[ei]!r} "
                    f"not found in extracted fact {extracted[xi]!r}"
                )

    ok = len(violations) == 0
    return ok, violations
