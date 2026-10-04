"""Render a `World` into a natural-language prompt with surface names.

`render(world, name_map, seed)` narrates `world.narration_order` using
seed-picked sentence templates, applies a pronoun-substitution pass, and
appends a question derived from `world.query`. The `answer_key` is always
computed via `world/simulate.py::answer`, translated to a surface name (or
"unknown"/"first"/"second") through `name_map` — never authored directly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..world.model import Filler, Move, NoOp, Query, Tell, Transfer, World
from ..world.simulate import answer as sim_answer
from .naming import NameMap

_MINUTES_PER_STEP = 1
_START_HOUR = 8


def _stable_int(*parts: str, modulus: int) -> int:
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    as_int = int(digest[:15], 16)
    return as_int % modulus


def _time_anchor(time: int) -> str:
    total_minutes = _START_HOUR * 60 + time * _MINUTES_PER_STEP
    hour = total_minutes // 60
    minute = total_minutes % 60
    return f"At {hour}:{minute:02d},"


_MOVE_TEMPLATES = [
    "{actor} carried {obj} to {loc}.",
    "{actor} moved {obj} into {loc}.",
    "{actor} took {obj} and placed it in {loc}.",
    "{actor} brought {obj} over to {loc}.",
]
_TRANSFER_TEMPLATES = [
    "{giver} handed {obj} to {receiver}.",
    "{giver} passed {obj} over to {receiver}.",
    "{giver} gave {receiver} {obj}.",
    "{giver} handed off {obj} to {receiver}.",
]
_TELL_TEMPLATES = [
    "{speaker} told {listener} that {obj} was in {loc}.",
    "{speaker} mentioned to {listener} that {obj} could be found in {loc}.",
    "{speaker} let {listener} know {obj} was in {loc}.",
    "{speaker} said to {listener} that {obj} was in {loc}.",
]
_FILLER_TEMPLATES = [
    "{actor} checked their email.",
    "{actor} stretched and looked out the window.",
    "{actor} refilled a cup of coffee.",
    "{actor} made a quick phone call.",
    "{actor} reorganized some papers on the desk.",
    "{actor} glanced at the clock.",
]
# NoOp (inert) templates, keyed by NoOp.kind. Every variant states
# explicitly that the relocation did NOT happen, so the story stays
# well-posed; the stressor is salience, not ambiguity.
_NOOP_TEMPLATES = {
    "declined": [
        "{actor} thought about moving {obj} to {loc}, but left it where it was.",
        "{actor} picked up {obj} to take it to {loc}, then changed their mind and set it back down where it had been.",
    ],
    "negated": [
        "{actor} did not take {obj} to {loc}.",
        "{actor} almost carried {obj} into {loc}, but decided against it.",
    ],
    "planned": [
        "{actor} planned to bring {obj} to {loc} later, but never did.",
        "{actor} meant to move {obj} into {loc}, but it never happened.",
    ],
    "refused": [
        "{actor} refused when {other} asked for {obj} to be moved to {loc}.",
        "{actor} was asked by {other} to carry {obj} to {loc}, but declined and left it alone.",
    ],
}
# Relative-time cues (relational_time), stated against the previously
# narrated event. Index 3 of each list is the explicit-referent form.
_CUE_AFTER = ["Later,", "Afterward,", "After that,", "After {clause},"]
_CUE_BEFORE = ["Before that,", "Earlier,", "Earlier the same morning,", "Before {clause},"]
_WITNESS_SINGLE_TEMPLATES = [
    "{witness} was nearby and saw this happen.",
    "{witness} happened to be watching.",
    "{witness} noticed this from across the room.",
]
_WITNESS_MULTI_TEMPLATES = [
    "{witnesses} were nearby and saw this happen.",
    "{witnesses} happened to be watching.",
    "{witnesses} noticed this from across the room.",
]


def _event_actor_ids(event) -> set[str]:
    """Entity ids already implied as 'present' by the main sentence — no
    separate witness clause is owed to these even if they're also listed in
    event.witnesses."""
    if isinstance(event, Move):
        return {event.actor}
    if isinstance(event, Transfer):
        return {event.giver, event.receiver}
    if isinstance(event, Tell):
        return {event.speaker, event.listener}
    if isinstance(event, Filler):
        return {event.actor}
    if isinstance(event, NoOp):
        return {event.actor} | ({event.other} if event.other else set())
    return set()


def _join_names(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _render_witness_sentence(
    event, idx: int, seed: int, name_map: NameMap, anchored: bool = True
) -> str | None:
    """A sentence marking the presence of any non-actor witnesses to `event`,
    sharing `event`'s time anchor (or no anchor at all when the event is
    narrated with a relative cue, so the clock time cannot leak). Returns
    None if there are none."""
    non_actor_witnesses = sorted(getattr(event, "witnesses", frozenset()) - _event_actor_ids(event))
    if not non_actor_witnesses:
        return None
    anchor = _time_anchor(event.time) if anchored else ""
    names = [_entity_name(eid, name_map) for eid in non_actor_witnesses]
    if len(names) == 1:
        template = _WITNESS_SINGLE_TEMPLATES[
            _stable_int("tpl_witness1", str(seed), str(idx), modulus=len(_WITNESS_SINGLE_TEMPLATES))
        ]
        sentence = template.format(witness=names[0])
    else:
        template = _WITNESS_MULTI_TEMPLATES[
            _stable_int("tpl_witnessN", str(seed), str(idx), modulus=len(_WITNESS_MULTI_TEMPLATES))
        ]
        sentence = template.format(witnesses=_join_names(names))
    return f"{anchor} {sentence}" if anchor else sentence


@dataclass(frozen=True)
class RenderedTask:
    prompt: str
    answer_key: str
    meta: dict = field(default_factory=dict)


def _entity_name(entity_id: str, name_map: NameMap) -> str:
    return name_map.entity.get(entity_id, entity_id)


def _obj_name(obj_id: str, name_map: NameMap) -> str:
    return name_map.obj.get(obj_id, obj_id)


def _loc_name(loc_id: str, name_map: NameMap) -> str:
    return name_map.location.get(loc_id, loc_id)


def _event_participants(event) -> list[str]:
    """Entity ids mentioned by name in the sentence, in mention order."""
    if isinstance(event, Move):
        return [event.actor]
    if isinstance(event, Transfer):
        return [event.giver, event.receiver]
    if isinstance(event, Tell):
        return [event.speaker, event.listener]
    if isinstance(event, Filler):
        return [event.actor]
    if isinstance(event, NoOp):
        # Only the actor is pronoun-eligible: the asker of a "refused" NoOp
        # is named in object position, where "she"/"he" would be ungrammatical.
        return [event.actor]
    return []


def _render_event_sentence(
    event, idx: int, seed: int, name_map: NameMap, anchor: str | None = None
) -> str:
    anchor = _time_anchor(event.time) if anchor is None else anchor
    if isinstance(event, Move):
        template = _MOVE_TEMPLATES[
            _stable_int("tpl_move", str(seed), str(idx), modulus=len(_MOVE_TEMPLATES))
        ]
        sentence = template.format(
            actor=_entity_name(event.actor, name_map),
            obj=_obj_name(event.obj, name_map),
            loc=_loc_name(event.dst, name_map),
        )
    elif isinstance(event, Transfer):
        template = _TRANSFER_TEMPLATES[
            _stable_int("tpl_transfer", str(seed), str(idx), modulus=len(_TRANSFER_TEMPLATES))
        ]
        sentence = template.format(
            giver=_entity_name(event.giver, name_map),
            receiver=_entity_name(event.receiver, name_map),
            obj=_obj_name(event.obj, name_map),
        )
    elif isinstance(event, Tell):
        template = _TELL_TEMPLATES[
            _stable_int("tpl_tell", str(seed), str(idx), modulus=len(_TELL_TEMPLATES))
        ]
        sentence = template.format(
            speaker=_entity_name(event.speaker, name_map),
            listener=_entity_name(event.listener, name_map),
            obj=_obj_name(event.obj, name_map),
            loc=_loc_name(event.claimed_location, name_map),
        )
    elif isinstance(event, Filler):
        template = _FILLER_TEMPLATES[
            _stable_int("tpl_filler", str(event.description_seed), modulus=len(_FILLER_TEMPLATES))
        ]
        sentence = template.format(actor=_entity_name(event.actor, name_map))
    elif isinstance(event, NoOp):
        variants = _NOOP_TEMPLATES[event.kind]
        template = variants[
            _stable_int("tpl_noop", str(seed), str(idx), modulus=len(variants))
        ]
        sentence = template.format(
            actor=_entity_name(event.actor, name_map),
            other=_entity_name(event.other, name_map) if event.other else "someone",
            obj=_obj_name(event.obj, name_map),
            loc=_loc_name(event.loc, name_map),
        )
    else:
        raise TypeError(f"unknown event type: {type(event)!r}")
    return f"{anchor} {sentence}"


def _event_clause(event, name_map: NameMap) -> str | None:
    """Past-tense clause naming `event`, for explicit relative cues
    ("After Silas moved the wrench to the lobby, ..."). None for NoOps --
    an inert event is never used as a temporal referent."""
    if isinstance(event, Move):
        return (
            f"{_entity_name(event.actor, name_map)} moved {_obj_name(event.obj, name_map)}"
            f" to {_loc_name(event.dst, name_map)}"
        )
    if isinstance(event, Transfer):
        return (
            f"{_entity_name(event.giver, name_map)} handed {_obj_name(event.obj, name_map)}"
            f" to {_entity_name(event.receiver, name_map)}"
        )
    if isinstance(event, Tell):
        return (
            f"{_entity_name(event.speaker, name_map)} told {_entity_name(event.listener, name_map)}"
            f" about {_obj_name(event.obj, name_map)}"
        )
    if isinstance(event, Filler):
        template = _FILLER_TEMPLATES[
            _stable_int("tpl_filler", str(event.description_seed), modulus=len(_FILLER_TEMPLATES))
        ]
        return template.format(actor=_entity_name(event.actor, name_map)).rstrip(".")
    return None


def _relative_cue(
    world: World, pos: int, seed: int, name_map: NameMap, clause_counts: dict[str, int]
) -> tuple[str, int, str]:
    """(cue text, referent event index, "after"|"before") for the event at
    narration position `pos` (> 0), stated against the previously narrated
    event and TRUE under the real chronology."""
    idx = world.narration_order[pos]
    ref = world.narration_order[pos - 1]
    event, ref_event = world.events[idx], world.events[ref]
    relation = "after" if ref_event.time < event.time else "before"
    pool = _CUE_AFTER if relation == "after" else _CUE_BEFORE
    choice = _stable_int("tpl_cue", str(seed), str(idx), modulus=len(pool))
    clause = _event_clause(ref_event, name_map)
    explicit_ok = (
        clause is not None
        and clause_counts.get(clause, 0) == 1
        and not (set(_event_participants(ref_event)) & _event_actor_ids(event))
    )
    if choice == len(pool) - 1 and not explicit_ok:
        choice = _stable_int("tpl_cue_generic", str(seed), str(idx), modulus=len(pool) - 1)
    cue = pool[choice].format(clause=clause) if choice == len(pool) - 1 else pool[choice]
    return cue, ref, relation


def _apply_pronouns(
    sentences: list[str], event_participants: list[list[str]], world: World, name_map: NameMap
) -> tuple[list[str], dict]:
    """Replace eligible name mentions with pronouns.

    Rule: an entity's name -> she/he only when it is the unique most-recently
    -named entity of its gender in the text so far, and was named within the
    previous 2 sentences. First mention of any entity is never pronouned.
    """
    gender_by_entity = {e.id: e.gender for e in world.entities}
    name_by_entity = {eid: name_map.entity[eid] for eid in gender_by_entity}

    # last_named_sentence_idx[entity_id] = sentence index of most recent mention
    last_named_idx: dict[str, int] = {}
    seen_entities: set[str] = set()
    pronoun_count = 0
    collision_count = 0
    intended_referents: list[dict] = []  # for meta/testing
    out_sentences: list[str] = []

    for i, (sentence, participants) in enumerate(zip(sentences, event_participants)):
        new_sentence = sentence
        for entity_id in participants:
            name = name_by_entity[entity_id]
            gender = gender_by_entity[entity_id]

            eligible = False
            if entity_id in seen_entities:
                # find the most-recently-named entity of this gender (before this sentence)
                same_gender_recent = [
                    (eid, idx) for eid, idx in last_named_idx.items()
                    if gender_by_entity.get(eid) == gender
                ]
                if same_gender_recent:
                    most_recent_eid, most_recent_idx = max(same_gender_recent, key=lambda t: t[1])
                    if (
                        most_recent_eid == entity_id
                        and (i - most_recent_idx) <= 2
                    ):
                        # must be UNIQUE most-recent: no tie with another same-gender entity
                        tied = [
                            eid for eid, idx in same_gender_recent
                            if idx == most_recent_idx and eid != entity_id
                        ]
                        if not tied:
                            eligible = True

            # A pronoun-eligible *collision*: some other same-gender entity was
            # named within the previous 2 sentences (regardless of whether the
            # current mention itself ends up pronouned) — a proxy for referential
            # ambiguity pressure driven by coref_pressure.
            same_gender_recent_others = [
                (eid, idx) for eid, idx in last_named_idx.items()
                if gender_by_entity.get(eid) == gender and eid != entity_id and (i - idx) <= 2
            ]
            if same_gender_recent_others:
                collision_count += 1

            if eligible:
                pronoun = "she" if gender == "f" else "he"
                # Replace the first standalone occurrence of the name in this sentence.
                if name in new_sentence:
                    new_sentence = new_sentence.replace(name, pronoun, 1)
                    pronoun_count += 1
                    intended_referents.append({"sentence_index": i, "entity_id": entity_id, "pronoun": pronoun})

            seen_entities.add(entity_id)
            last_named_idx[entity_id] = i

        out_sentences.append(new_sentence)

    meta = {
        "pronouns_used": pronoun_count,
        "pronoun_referents": intended_referents,
        "pronoun_collisions": collision_count,
    }
    return out_sentences, meta


_EVENT_DESC_TEMPLATES = {
    Move: "{actor} moving {obj} to {loc}",
    Transfer: "{giver} handing {obj} to {receiver}",
    Tell: "{speaker} telling {listener} about {obj}",
    Filler: "{actor}'s aside",
}


def _describe_event(event, name_map: NameMap) -> str:
    if isinstance(event, Move):
        return _EVENT_DESC_TEMPLATES[Move].format(
            actor=_entity_name(event.actor, name_map),
            obj=_obj_name(event.obj, name_map),
            loc=_loc_name(event.dst, name_map),
        )
    if isinstance(event, Transfer):
        return _EVENT_DESC_TEMPLATES[Transfer].format(
            giver=_entity_name(event.giver, name_map),
            obj=_obj_name(event.obj, name_map),
            receiver=_entity_name(event.receiver, name_map),
        )
    if isinstance(event, Tell):
        return _EVENT_DESC_TEMPLATES[Tell].format(
            speaker=_entity_name(event.speaker, name_map),
            listener=_entity_name(event.listener, name_map),
            obj=_obj_name(event.obj, name_map),
        )
    if isinstance(event, Filler):
        return _EVENT_DESC_TEMPLATES[Filler].format(actor=_entity_name(event.actor, name_map))
    if isinstance(event, NoOp):
        return (
            f"{_entity_name(event.actor, name_map)} considering moving "
            f"{_obj_name(event.obj, name_map)} to {_loc_name(event.loc, name_map)}"
        )
    raise TypeError(f"unknown event type: {type(event)!r}")


def _render_question(world: World, name_map: NameMap) -> tuple[str, str]:
    query = world.query
    result = sim_answer(world)
    if query.kind == "state":
        obj_name = _obj_name(query.subject, name_map)
        prompt = f"Where is {obj_name} now? Answer with only the location name."
        answer_key = "unknown" if result.value == "unknown" else _loc_name(result.value, name_map)
        return prompt, answer_key
    if query.kind == "belief":
        assert query.agent is not None
        agent_name = _entity_name(query.agent, name_map)
        obj_name = _obj_name(query.subject, name_map)
        prompt = (
            f"Where does {agent_name} believe {obj_name} is? "
            "Answer with only the location name (or 'unknown' if they have no belief)."
        )
        answer_key = "unknown" if result.value == "unknown" else _loc_name(result.value, name_map)
        return prompt, answer_key
    if query.kind == "order":
        assert query.pair is not None
        i, j = query.pair
        desc_a = _describe_event(world.events[i], name_map)
        desc_b = _describe_event(world.events[j], name_map)
        prompt = (
            f"Which happened first: {desc_a} or {desc_b}? "
            "Answer 'first' if the former, 'second' if the latter."
        )
        return prompt, result.value
    raise ValueError(f"unknown query kind: {query.kind!r}")


def render(world: World, name_map: NameMap, seed: int) -> RenderedTask:
    sentences: list[str] = []
    participants_per_sentence: list[list[str]] = []
    sentence_event_idx: list[int] = []
    sentence_is_witness: list[bool] = []
    witness_sentence_count = 0
    time_cues: list[dict] = []
    clause_counts: dict[str, int] = {}
    if world.relative_times:
        for ev in world.events:
            c = _event_clause(ev, name_map)
            if c is not None:
                clause_counts[c] = clause_counts.get(c, 0) + 1
    for pos, idx in enumerate(world.narration_order):
        event = world.events[idx]
        cued = pos > 0 and event.time in world.relative_times
        anchor = None
        if cued:
            anchor, ref, relation = _relative_cue(world, pos, seed, name_map, clause_counts)
            time_cues.append(
                {"sentence_index": len(sentences), "event_idx": idx, "ref_event_idx": ref,
                 "relation": relation, "cue": anchor}
            )
        sentences.append(_render_event_sentence(event, idx, seed, name_map, anchor=anchor))
        participants_per_sentence.append(_event_participants(event))
        sentence_event_idx.append(idx)
        sentence_is_witness.append(False)

        witness_sentence = _render_witness_sentence(event, idx, seed, name_map, anchored=not cued)
        if witness_sentence is not None:
            sentences.append(witness_sentence)
            non_actor_witnesses = sorted(
                getattr(event, "witnesses", frozenset()) - _event_actor_ids(event)
            )
            participants_per_sentence.append(non_actor_witnesses)
            sentence_event_idx.append(idx)
            sentence_is_witness.append(True)
            witness_sentence_count += 1

    sentences, pronoun_meta = _apply_pronouns(sentences, participants_per_sentence, world, name_map)
    # Un-anchored (relative-cue) witness sentences can now begin with a
    # pronoun; anchored sentences always start with "At", so this is a
    # no-op for every pre-relational_time story.
    sentences = [x[:1].upper() + x[1:] for x in sentences]

    story = " ".join(sentences)
    question, answer_key = _render_question(world, name_map)
    prompt = f"{story}\n\n{question}"

    meta = {
        "narration_order": list(world.narration_order),
        "witness_sentences": witness_sentence_count,
        "sentence_event_idx": sentence_event_idx,
        "sentence_is_witness": sentence_is_witness,
        "query_kind": world.query.kind,
        "time_cues": time_cues,
        **pronoun_meta,
    }
    return RenderedTask(prompt=prompt, answer_key=answer_key, meta=meta)
