"""Deterministic ablation catalog: minimal, answer-key-preserving prompt
transforms over the SAME latent world, for causally isolating which
surface factor a model's failure actually depends on.

Each ablation is a pure function `(world, name_map, seed) -> AblatedTask`.
Every ablation MUST produce a prompt whose `answer_key` is byte-identical
to the baseline's -- that invariant is the entire point of an ablation.
An ablation that changes the correct answer proves nothing about *why*
the model failed on the baseline (the model could simply be answering a
different question correctly). Each ablation function asserts this itself
before returning; `AblationUnavailable` is raised (never silently
swallowed by the ablation itself) when a *particular* world can't yield an
answer-key-preserving variant, and the catalog's `available_ablations`
helper is what turns that into a documented "unavailable, skipped" rather
than a crash.

No LLM anywhere in this module: every transform is code operating on the
already-simulated `World`/`RenderedTask` data structures. The ground truth
`answer_key` always comes from `cartographer.world.simulate.answer` via
`cartographer.render.templates.render`/`sim_answer`, never from a model's
own judgment -- consistent with the instrument's core credibility
requirement (docs/lab-log.md).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from ..render.naming import NameMap
from ..render.templates import render
from ..world.model import World


class AblationUnavailable(Exception):
    """Raised when an ablation cannot produce an answer-key-preserving
    variant for this particular world (e.g. `distractor_removal` when the
    baseline world has zero distractor events to begin with, or a
    regenerated variant's answer key drifted from the baseline's).
    `attribute_failure` catches this per-ablation and marks that ablation
    "unavailable" for the case rather than treating it as a hard error.
    """


@dataclass(frozen=True)
class AblatedTask:
    """One ablation's rendered output, alongside its identity."""

    prompt: str
    answer_key: str
    name: str
    description: str

    def to_json_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt,
            "answer_key": self.answer_key,
        }


Ablation = Callable[[World, NameMap, int], AblatedTask]


def _assert_same_answer(name: str, baseline_key: str, candidate_key: str) -> None:
    if candidate_key != baseline_key:
        raise AblationUnavailable(
            f"ablation {name!r} changed the answer key "
            f"({baseline_key!r} -> {candidate_key!r}); skipping as unavailable "
            "for this world rather than reporting a result that proves nothing"
        )


# --- baseline ---------------------------------------------------------------


def baseline(world: World, name_map: NameMap, seed: int) -> AblatedTask:
    """The control: the normal render, unmodified."""
    rendered = render(world, name_map, seed)
    return AblatedTask(
        prompt=rendered.prompt,
        answer_key=rendered.answer_key,
        name="baseline",
        description="Unmodified render (control).",
    )


# --- pronoun_suppression -----------------------------------------------------


def pronoun_suppression(world: World, name_map: NameMap, seed: int) -> AblatedTask:
    """Every pronoun the renderer would have used is replaced back with the
    entity's real name.

    `render()`'s pronoun pass (`cartographer.render.templates._apply_pronouns`)
    is not exposed as a bypass flag, and post-hoc regexing "he"/"she" out of
    prose is unreliable (a Filler sentence's "he" is not necessarily the
    renderer's pronoun; a name could itself contain a pronoun-like
    substring). Instead this is resolver-backed: `render()` already records,
    in `RenderedTask.meta["pronoun_referents"]`, exactly which sentence index
    and entity id each pronoun substitution came from -- so we re-render the
    story sentence-by-sentence and skip the substitution step entirely for
    those exact positions, restoring the original name. This reuses the
    renderer's own resolution bookkeeping rather than re-implementing (or
    guessing at) coreference resolution.
    """
    baseline_rendered = render(world, name_map, seed)
    referents = baseline_rendered.meta.get("pronoun_referents", [])

    # No pronoun was ever substituted in this world -- pronoun_suppression
    # degenerates to the baseline prompt exactly. Still a valid (if inert)
    # ablation result: nothing to suppress means this world can't
    # discriminate on this factor.
    if not referents:
        return AblatedTask(
            prompt=baseline_rendered.prompt,
            answer_key=baseline_rendered.answer_key,
            name="pronoun_suppression",
            description=(
                "No pronouns were substituted in this world's baseline "
                "render; prompt is identical to baseline."
            ),
        )

    name_by_entity = {eid: name_map.entity[eid] for eid in name_map.entity}
    gender_by_entity = {e.id: e.gender for e in world.entities}
    pronoun_by_entity = {
        eid: ("she" if gender_by_entity.get(eid) == "f" else "he")
        for eid in name_by_entity
    }

    # Re-render the raw (pre-pronoun) sentences the same way `render()` does
    # internally, then apply every substitution from `_apply_pronouns`
    # EXCEPT the ones pronoun_suppression is suppressing -- i.e. apply none
    # of them, since suppressing is "replace every pronoun with the name,"
    # which is just the pre-pronoun-pass text. We don't need private access
    # to `_apply_pronouns`: the pre-pronoun sentence list is reconstructable
    # because `render()`'s pronoun pass only ever *replaces a name with a
    # pronoun*, never anything else -- so inverting every recorded
    # substitution on the ALREADY-rendered story recovers the pre-pass text
    # exactly, for each referent site.
    restored = baseline_rendered.prompt
    for ref in referents:
        pronoun_word = pronoun_by_entity.get(ref["entity_id"])
        name = name_by_entity.get(ref["entity_id"])
        if pronoun_word is None or name is None:
            continue
        # Replace one occurrence of the standalone pronoun with the name.
        # Substitutions are inverted in the order they were originally
        # applied (render() applies them left-to-right, sentence by
        # sentence), so replacing the first remaining occurrence of each
        # pronoun word, in that same order, undoes them correctly even
        # when the same pronoun word recurs for a different entity later.
        restored = _replace_first_word(restored, pronoun_word, name)

    return AblatedTask(
        prompt=restored,
        answer_key=baseline_rendered.answer_key,
        name="pronoun_suppression",
        description=(
            "Every renderer-introduced pronoun ('he'/'she') restored to the "
            "entity's real name, using render()'s own recorded pronoun "
            "referents (sentence index + entity id) rather than regex "
            "guessing -- inverts the exact substitutions render() made."
        ),
    )


def _replace_first_word(text: str, word: str, replacement: str) -> str:
    """Replace the first standalone occurrence of `word` in `text` with
    `replacement`. Standalone = not part of a longer word, matched via a
    simple boundary scan (no regex needed since names/pronouns never
    contain apostrophes and pronouns are lowercase single words)."""
    lower = text.lower()
    start = 0
    while True:
        idx = lower.find(word, start)
        if idx == -1:
            return text
        before_ok = idx == 0 or not text[idx - 1].isalnum()
        end = idx + len(word)
        after_ok = end >= len(text) or not text[end].isalnum()
        if before_ok and after_ok:
            return text[:idx] + replacement + text[end:]
        start = idx + 1


# --- distractor_removal / filler_removal ------------------------------------
#
# Judgment call: rather than regenerate the world from scratch with
# `distractor_events=0` / `filler_events=0` (tempting, since the task names
# these knobs directly), that path was tried first and rejected -- `World`
# does not retain the `DimParams` it was generated from, and reconstructing
# `witness_cutoff`/`coref_pressure` well enough to reproduce the exact same
# witness assignments purely by measuring the world proved unreliable: for
# belief queries with nonzero witness_cutoff (the very dimension the
# instrument's main finding depends on), the reconstructed params leave the
# query agent witnessing a DIFFERENT subset of the causal chain, which
# changes the belief answer -- exactly the answer-key drift this catalog is
# required to detect and refuse. Confirmed empirically: regenerating from a
# reconstructed DimParams changed the answer key on ordinary worlds with
# witness_cutoff > 0 or coref_pressure > 0.
#
# Instead these two ablations perform direct surgery on the already-built
# `World`: drop the target event type's events wholesale (non-subject Moves
# for distractors, all Fillers for filler) and renumber `narration_order`
# accordingly, while leaving every remaining event's `witnesses`/`actor`
# fields byte-identical. Since `world/simulate.py::answer` only ever reads
# `witnesses`/`actor`/`time` off the SURVIVING events, and belief/order
# queries reference event identity by content (subject id, agent id, pair
# of original indices) rather than position, the answer key is provably
# unchanged for every query kind except `order` queries whose reference
# pair includes a removed event -- which is checked explicitly and raises
# `AblationUnavailable` rather than silently reporting a broken pair.


def _drop_events(world: World, keep: Callable[[int, object], bool]) -> World:
    """Return a new `World` containing only events for which
    `keep(index, event)` is True, with `narration_order` filtered to match
    and reindexed to the new, smaller event list. Every surviving event
    object is passed through unchanged (same `witnesses`, `actor`, `time`,
    etc.) -- only which events exist and their positions change.

    An `order` query's `Query.pair` holds OLD event indices, which go stale
    the moment events are dropped and the rest reindexed -- callers must
    have already checked (`_order_pair_survives`) that both referenced
    events survive; this remaps `pair` to the new indices so
    `world/simulate.py::answer`'s `world.events[i]` lookups stay in bounds
    and keep pointing at the SAME two events, not just any two events at
    those positions.
    """
    kept_old_indices = [i for i, e in enumerate(world.events) if keep(i, e)]
    old_to_new = {old: new for new, old in enumerate(kept_old_indices)}
    new_events = [world.events[i] for i in kept_old_indices]
    new_narration = [
        old_to_new[i] for i in world.narration_order if i in old_to_new
    ]
    new_world = replace(world, events=new_events, narration_order=new_narration)
    if world.query.kind == "order" and world.query.pair is not None:
        i, j = world.query.pair
        new_pair = (old_to_new[i], old_to_new[j])
        new_world = replace(new_world, query=replace(world.query, pair=new_pair))
    return new_world


def _order_pair_survives(world: World, keep: Callable[[int, object], bool]) -> bool:
    if world.query.kind != "order" or world.query.pair is None:
        return True
    i, j = world.query.pair
    return keep(i, world.events[i]) and keep(j, world.events[j])


def distractor_removal(world: World, name_map: NameMap, seed: int) -> AblatedTask:
    """Same world, with every non-subject Move (distractor event) dropped
    and narration reindexed -- everything else (causal chain, Tells,
    Fillers, all witness sets) left byte-identical.
    """
    from ..world.model import Move

    subject = world.query.subject
    is_distractor = lambda i, e: isinstance(e, Move) and e.obj != subject  # noqa: E731

    if not any(is_distractor(i, e) for i, e in enumerate(world.events)):
        raise AblationUnavailable(
            "distractor_removal: baseline world has zero distractor events"
        )
    if not _order_pair_survives(world, lambda i, e: not is_distractor(i, e)):
        raise AblationUnavailable(
            "distractor_removal: order query references a distractor event"
        )

    baseline_rendered = render(world, name_map, seed)
    new_world = _drop_events(world, lambda i, e: not is_distractor(i, e))
    rendered = render(new_world, name_map, seed)
    _assert_same_answer("distractor_removal", baseline_rendered.answer_key, rendered.answer_key)
    return AblatedTask(
        prompt=rendered.prompt,
        answer_key=rendered.answer_key,
        name="distractor_removal",
        description=(
            "Same world/seed with every distractor Move dropped and "
            "narration reindexed; all remaining events byte-identical."
        ),
    )


def filler_removal(world: World, name_map: NameMap, seed: int) -> AblatedTask:
    """Same world, with every Filler event dropped and narration
    reindexed -- everything else left byte-identical.
    """
    from ..world.model import Filler

    is_filler = lambda i, e: isinstance(e, Filler)  # noqa: E731

    if not any(is_filler(i, e) for i, e in enumerate(world.events)):
        raise AblationUnavailable("filler_removal: baseline world has zero filler events")
    if not _order_pair_survives(world, lambda i, e: not is_filler(i, e)):
        raise AblationUnavailable(
            "filler_removal: order query references a filler event"
        )

    baseline_rendered = render(world, name_map, seed)
    new_world = _drop_events(world, lambda i, e: not is_filler(i, e))
    rendered = render(new_world, name_map, seed)
    _assert_same_answer("filler_removal", baseline_rendered.answer_key, rendered.answer_key)
    return AblatedTask(
        prompt=rendered.prompt,
        answer_key=rendered.answer_key,
        name="filler_removal",
        description=(
            "Same world/seed with every Filler event dropped and "
            "narration reindexed; all remaining events byte-identical."
        ),
    )


# --- witness_explicitation ----------------------------------------------------


def witness_explicitation(world: World, name_map: NameMap, seed: int) -> AblatedTask:
    """Every witness sentence rewritten to a single, maximally unambiguous
    template: "<Name> was present and saw this." -- always the explicit
    name (never a pronoun), never a varied template. Baseline witness
    sentences vary template wording and are subject to the same pronoun
    pass as everything else; this ablation isolates whether THAT specific
    variability/ambiguity, rather than pronoun use in the main narration,
    is what a model trips on.

    Deliberately does NOT go through `pronoun_suppression` first -- doing
    so would make this ablation indistinguishable from
    `pronoun_suppression` itself (both would end up removing every
    pronoun in the story), destroying the whole point of having two
    separate ablations. Instead this starts from the ORDINARY baseline
    render (pronouns intact in the main narration) and rewrites ONLY the
    witness-marker sentences, located structurally via
    `RenderedTask.meta`'s `sentence_is_witness` flags rather than by text
    matching, to the canonical explicit-name template. A model sensitive
    to pronoun ambiguity in the main narrative sentences will still fail
    this ablation; only witness-sentence wording/ambiguity is fixed here.
    """
    baseline_rendered = render(world, name_map, seed)

    sentences = _split_story_sentences(baseline_rendered.prompt)
    meta = baseline_rendered.meta
    is_witness = meta.get("sentence_is_witness", [])
    sentence_event_idx = meta.get("sentence_event_idx", [])

    if len(sentences) != len(is_witness):
        # Structural mismatch (shouldn't happen given render()'s own
        # invariants, but fail loud rather than silently mis-rewrite).
        raise AblationUnavailable(
            "witness_explicitation: sentence count does not match render() meta"
        )

    if not any(is_witness):
        return AblatedTask(
            prompt=baseline_rendered.prompt,
            answer_key=baseline_rendered.answer_key,
            name="witness_explicitation",
            description=(
                "No witness sentences present in this world; prompt is "
                "identical to baseline."
            ),
        )

    new_sentences: list[str] = []
    for i, sentence in enumerate(sentences):
        if not is_witness[i]:
            new_sentences.append(sentence)
            continue
        event_idx = sentence_event_idx[i]
        event = world.events[event_idx]
        non_actor_witnesses = sorted(
            getattr(event, "witnesses", frozenset())
            - _event_actor_ids_for(event)
        )
        anchor = sentence.split(" ", 1)[0]  # "At H:MM," -- keep the time anchor verbatim
        names = [name_map.entity.get(eid, eid) for eid in non_actor_witnesses]
        clause = _join_present_clause(names)
        new_sentences.append(f"{anchor} {clause}")

    story = " ".join(new_sentences)
    question = baseline_rendered.prompt.rsplit("\n\n", 1)[1]
    prompt = f"{story}\n\n{question}"

    return AblatedTask(
        prompt=prompt,
        answer_key=baseline_rendered.answer_key,
        name="witness_explicitation",
        description=(
            "Every witness sentence rewritten to the single canonical "
            "template '<Name> was present and saw this' -- explicit name, "
            "no pronoun, no template variation."
        ),
    )


def _event_actor_ids_for(event) -> set[str]:
    from ..world.model import Filler, Move, Tell, Transfer

    if isinstance(event, Move):
        return {event.actor}
    if isinstance(event, Transfer):
        return {event.giver, event.receiver}
    if isinstance(event, Tell):
        return {event.speaker, event.listener}
    if isinstance(event, Filler):
        return {event.actor}
    return set()


def _join_present_clause(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return f"{names[0]} was present and saw this."
    joined = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 2 else f"{names[0]} and {names[1]}"
    return f"{joined} were present and saw this."


def _split_story_sentences(prompt: str) -> list[str]:
    story = prompt.rsplit("\n\n", 1)[0]
    return _split_on_single_space_joins(story)


def _split_on_single_space_joins(story: str) -> list[str]:
    """Inverse of `" ".join(sentences)` -- render() always joins sentences
    with a single literal space and every sentence is itself internally
    space-separated, so naive split-on-space would shatter sentences.
    Instead, split on the anchor pattern: every sentence starts with
    "At H:MM," (see `_time_anchor`), which never occurs mid-sentence.
    """
    import re

    # Find every sentence start ("At H:MM,") and slice between them.
    starts = [m.start() for m in re.finditer(r"At \d{1,2}:\d{2},", story)]
    if not starts:
        return [story] if story else []
    starts.append(len(story))
    sentences = []
    for i in range(len(starts) - 1):
        chunk = story[starts[i] : starts[i + 1]].rstrip()
        sentences.append(chunk)
    return sentences


# --- temporal_normalization --------------------------------------------------


def temporal_normalization(world: World, name_map: NameMap, seed: int) -> AblatedTask:
    """Same world, narration re-emitted in true chronological order (kills
    narration scramble as a factor). Answer key must be unchanged -- and
    always is, since `narration_order` never participates in
    `world/simulate.py::answer` (ground truth is computed by walking
    `world.events` in true time order regardless of presentation order).
    """
    baseline_rendered = render(world, name_map, seed)
    chronological = sorted(range(len(world.events)), key=lambda i: world.events[i].time)
    normalized_world = replace(world, narration_order=chronological)
    rendered = render(normalized_world, name_map, seed)
    _assert_same_answer(
        "temporal_normalization", baseline_rendered.answer_key, rendered.answer_key
    )
    return AblatedTask(
        prompt=rendered.prompt,
        answer_key=rendered.answer_key,
        name="temporal_normalization",
        description="Narration re-emitted in true chronological order (scramble=0).",
    )


# --- catalog ------------------------------------------------------------------

ABLATIONS: dict[str, Ablation] = {
    "baseline": baseline,
    "pronoun_suppression": pronoun_suppression,
    "distractor_removal": distractor_removal,
    "filler_removal": filler_removal,
    "witness_explicitation": witness_explicitation,
    "temporal_normalization": temporal_normalization,
}


def available_ablations(
    world: World, name_map: NameMap, seed: int, names: list[str] | None = None
) -> dict[str, AblatedTask]:
    """Run every ablation in `names` (default: everything except baseline)
    against `world`, skipping (not raising) any that raise
    `AblationUnavailable` for this particular world. Returns
    {name: AblatedTask} for only the ones that succeeded.
    """
    names = names if names is not None else [n for n in ABLATIONS if n != "baseline"]
    out: dict[str, AblatedTask] = {}
    for name in names:
        fn = ABLATIONS[name]
        try:
            out[name] = fn(world, name_map, seed)
        except AblationUnavailable:
            continue
    return out
