"""Structural (literature-driven) narrative stressors: no-op distractors and
relational time. Both are answer-key-preserving; the "zoo" below pins that
down and checks that each stressor defeats exactly the shallow reader it
targets, and nothing else.

- noop_distractors: GSM-NoOp (Mirzadeh et al. 2024) -- seemingly relevant
  but inert clauses about the queried object.
- relational_time: Premise Order Matters (Chen et al. 2024) -- clock times
  replaced by true relative cues, so (with scramble) narration order is no
  longer recoverable from timestamps.

No model calls anywhere: every "reader" here is a deterministic fake.
"""

from __future__ import annotations

import re
from dataclasses import replace

from cartographer.exec.evaluator import _apply_config, _default_base
from cartographer.llmrender.facts import world_facts
from cartographer.render import build_name_map, render
from cartographer.render.templates import (
    _CUE_AFTER,
    _CUE_BEFORE,
    _NOOP_TEMPLATES,
    _event_clause,
)
from cartographer.world import (
    ANSWER_PRESERVING,
    DimParams,
    Move,
    NoOp,
    add_noop_distractors,
    add_relational_time,
    answer,
    generate,
    measure,
    simulate,
)
from cartographer.world.mutate import critical_pairs

SEEDS = range(200)


def _params(qk: str, **kw) -> DimParams:
    base = dict(
        chain_length=4,
        scramble=0.0,
        coref_pressure=1,
        distractor_events=2,
        distractor_similarity="same_category",
        filler_events=2,
        query_kind=qk,
    )
    base.update(kw)
    return DimParams(**base)


# --- fake readers -------------------------------------------------------------


def _noop_gullible_state(world) -> str:
    """Treats every NoOp as a real move of its object to its `loc`."""
    loc: dict[str, str] = {}
    for e in world.events:
        if isinstance(e, Move):
            loc[e.obj] = e.dst
        elif isinstance(e, NoOp):
            loc[e.obj] = e.loc
    return loc[world.query.subject]


_ANCHOR_RE = re.compile(r"^At (\d{1,2}):(\d{2}), ")
_SENT_SPLIT = re.compile(r"(?<=\.)\s+(?=[A-Z])")


def _story_sentences(rendered) -> list[str]:
    story = rendered.prompt.rsplit("\n\n", 1)[0]
    sentences = _SENT_SPLIT.split(story)
    assert len(sentences) == len(rendered.meta["sentence_event_idx"]), "sentence split drifted"
    return sentences


def _clock(sentence: str) -> int | None:
    m = _ANCHOR_RE.match(sentence)
    if not m:
        return None
    return (int(m.group(1)) - 8) * 60 + int(m.group(2))


def _event_sentence_positions(rendered) -> dict[int, int]:
    """event idx -> index of its (non-witness) sentence."""
    return {
        ev: k
        for k, (ev, wit) in enumerate(
            zip(rendered.meta["sentence_event_idx"], rendered.meta["sentence_is_witness"])
        )
        if not wit
    }


def _narration_order_reader(world, rendered) -> str:
    """Trusts clock times when BOTH compared events carry one; otherwise
    assumes narration order == time order (ignores relative cues)."""
    i, j = world.query.pair
    sentences = _story_sentences(rendered)
    pos = _event_sentence_positions(rendered)
    ci, cj = _clock(sentences[pos[i]]), _clock(sentences[pos[j]])
    if ci is not None and cj is not None:
        return "first" if ci < cj else "second"
    return "first" if pos[i] < pos[j] else "second"


# --- cue parser / checker -----------------------------------------------------


def _parse_constraints(world, rendered, name_map) -> tuple[dict[int, int], list[tuple[int, int]]]:
    """Parse the rendered story back into (clock times by event, list of
    (before, after) precedence constraints stated by relative cues).

    The cue's referent is the previously narrated event; an explicit cue
    ("After <clause>,") must name exactly that event."""
    sentences = _story_sentences(rendered)
    ev_of = rendered.meta["sentence_event_idx"]
    is_wit = rendered.meta["sentence_is_witness"]
    clocks: dict[int, int] = {}
    prec: list[tuple[int, int]] = []
    prev_event: int | None = None
    generic_after = [c for c in _CUE_AFTER if "{" not in c]
    generic_before = [c for c in _CUE_BEFORE if "{" not in c]
    for k, sent in enumerate(sentences):
        ev = ev_of[k]
        if is_wit[k]:
            # A witness sentence must not leak a clock time for a cued event.
            if world.events[ev].time in world.relative_times:
                assert _clock(sent) is None, sent
            continue
        t = _clock(sent)
        if t is not None:
            clocks[ev] = t
        else:
            assert prev_event is not None, "first narrated sentence must be clock-anchored"
            relation = None
            for c in generic_after:
                if sent.startswith(c + " "):
                    relation = "after"
            for c in generic_before:
                if sent.startswith(c + " ") and relation is None:
                    relation = "before"
            m = re.match(r"^(After|Before) (.+?), ", sent)
            if relation is None and m:
                clause = m.group(2)
                assert clause == _event_clause(world.events[prev_event], name_map), (clause, sent)
                relation = m.group(1).lower()
            assert relation is not None, f"unparseable cue sentence: {sent!r}"
            prec.append((prev_event, ev) if relation == "after" else (ev, prev_event))
        prev_event = ev
    return clocks, prec


def _entailed(n: int, clocks: dict[int, int], prec: list[tuple[int, int]], a: int, b: int) -> bool:
    succ: dict[int, set[int]] = {i: set() for i in range(n)}
    anchored = sorted(clocks, key=clocks.get)
    for x, y in zip(anchored, anchored[1:]):
        succ[x].add(y)
    for x, y in prec:
        succ[x].add(y)
    stack, seen = [a], {a}
    while stack:
        u = stack.pop()
        if u == b:
            return True
        for v in succ[u] - seen:
            seen.add(v)
            stack.append(v)
    return False


# --- 1. answer keys identical with/without each mutation (200 seeds) ---------


def test_noop_answer_keys_identical_200_seeds() -> None:
    for qk in ("state", "belief", "order"):
        for seed in SEEDS:
            n = 1 + seed % 8
            plain = generate(_params(qk, scramble=0.2), seed)
            stressed = generate(_params(qk, scramble=0.2, noop_distractors=n), seed)
            assert answer(plain) == answer(stressed), (qk, seed)
            assert simulate(plain) == simulate(stressed), (qk, seed)
            # The mutator applied directly is equally inert.
            assert answer(add_noop_distractors(plain, n, seed + 1)) == answer(plain)


def test_relational_answer_keys_identical_200_seeds() -> None:
    for qk in ("state", "belief", "order"):
        for seed in SEEDS:
            frac = (seed % 10 + 1) / 10
            for scr in (0.0, 0.3):
                plain = generate(_params(qk, scramble=scr), seed)
                stressed = generate(_params(qk, scramble=scr, relational_time=frac), seed)
                assert answer(plain) == answer(stressed), (qk, seed, frac, scr)
                nm = build_name_map(stressed, seed)
                assert render(stressed, nm, seed).answer_key == render(
                    replace(stressed, relative_times=frozenset()), nm, seed
                ).answer_key


# --- 2. no-op zoo ------------------------------------------------------------


def test_noop_gullible_reader_fails_state_iff_noop_targets_subject() -> None:
    hits = misses = 0
    for seed in SEEDS:
        n = seed % 9  # 0..8
        world = generate(_params("state", scramble=0.2, noop_distractors=n), seed)
        truth = answer(world).value
        targets_subject = any(
            isinstance(e, NoOp) and e.obj == world.query.subject for e in world.events
        )
        fooled = _noop_gullible_state(world) != truth
        assert fooled == (n > 0 and targets_subject), (seed, n)
        hits += fooled
        misses += (n > 0) and not targets_subject
    assert hits > 100  # the stressor actually bites on most stressed seeds
    assert misses > 0  # and the iff's other branch is exercised


def test_noop_locations_always_differ_from_current_location() -> None:
    for seed in range(60):
        world = generate(_params("belief", noop_distractors=5), seed)
        loc: dict[str, str] = {}
        for e in world.events:
            if isinstance(e, Move) and e.obj not in loc:
                loc[e.obj] = e.src
        for e in world.events:
            if isinstance(e, Move):
                loc[e.obj] = e.dst
            elif isinstance(e, NoOp):
                assert e.loc != loc[e.obj], (seed, e)


def test_noop_never_a_compared_event_and_count_measured() -> None:
    for seed in range(100):
        n = seed % 9
        world = generate(_params("order", scramble=0.3, noop_distractors=n), seed)
        i, j = world.query.pair
        assert not isinstance(world.events[i], NoOp)
        assert not isinstance(world.events[j], NoOp)
        assert measure(world)["noop_distractors"] == float(n)
        assert all(e.time == k for k, e in enumerate(world.events))


def test_noop_renders_with_at_least_six_variants() -> None:
    templates = [t for variants in _NOOP_TEMPLATES.values() for t in variants]
    assert len(templates) >= 6
    stems = [t.split("{actor}", 1)[1].split("{", 1)[0] for t in templates]
    seen: set[int] = set()
    for seed in range(120):
        world = generate(_params("state", noop_distractors=4), seed)
        story = render(world, build_name_map(world, seed), seed).prompt
        for k, stem in enumerate(stems):
            if stem in story:
                seen.add(k)
    assert len(seen) >= 6, seen


def test_noop_is_skipped_by_llm_fact_list() -> None:
    world = generate(_params("state", noop_distractors=3), 5)
    plain_facts = world_facts(world, build_name_map(world, 5))
    assert len(plain_facts) == sum(
        1 for e in world.events if not isinstance(e, NoOp) and type(e).__name__ != "Filler"
    )


# --- 3. relational-time zoo --------------------------------------------------


def test_narration_reader_fails_order_iff_relational_and_scramble() -> None:
    for seed in range(80):
        for frac in (0.0, 0.3, 0.8):
            for scr in (0.0, 0.2, 0.5):
                world = generate(_params("order", scramble=scr, relational_time=frac), seed)
                rendered = render(world, build_name_map(world, seed), seed)
                fooled = _narration_order_reader(world, rendered) != rendered.answer_key
                assert fooled == (frac > 0 and scr > 0), (seed, frac, scr)


def test_relational_cues_parse_back_consistent_with_true_order() -> None:
    """Checker: parse cues back out of the prose and confirm the TRUE
    chronology satisfies every stated constraint, every stated clock time is
    the true one, and the query's answer-critical order is entailed."""
    n_cued = 0
    for qk in ("state", "belief", "order"):
        for seed in range(70):
            for scr in (0.0, 0.4):
                world = generate(
                    _params(qk, scramble=scr, relational_time=0.6, noop_distractors=seed % 3),
                    seed,
                )
                nm = build_name_map(world, seed)
                rendered = render(world, nm, seed)
                clocks, prec = _parse_constraints(world, rendered, nm)
                for ev, t in clocks.items():
                    assert world.events[ev].time == t
                for a, b in prec:
                    assert world.events[a].time < world.events[b].time, (qk, seed, a, b)
                # every event is either clock-anchored or carries exactly one cue
                assert len(clocks) + len(prec) == len(world.events)
                n_cued += len(world.events) - len(clocks)
                for a, b in critical_pairs(world):
                    assert _entailed(len(world.events), clocks, prec, a, b), (qk, seed, a, b)
    assert n_cued > 500


def test_relational_fraction_measured_and_first_sentence_anchored() -> None:
    for seed in range(50):
        world = generate(_params("order", scramble=0.3, relational_time=0.5), seed)
        m = measure(world)["relational_time"]
        assert 0.3 <= m <= 0.75, (seed, m)
        assert world.events[world.narration_order[0]].time not in world.relative_times
        plain = generate(_params("order", scramble=0.3), seed)
        assert measure(plain)["relational_time"] == 0.0
        assert plain.relative_times == frozenset()


# --- 4. determinism / config plumbing ----------------------------------------


def test_render_deterministic_with_both_stressors() -> None:
    for qk in ("state", "belief", "order"):
        for seed in range(30):
            p = _params(qk, scramble=0.3, noop_distractors=3, relational_time=0.5)
            w1, w2 = generate(p, seed), generate(p, seed)
            assert w1 == w2
            r1 = render(w1, build_name_map(w1, seed), seed)
            r2 = render(w2, build_name_map(w2, seed), seed)
            assert r1 == r2


def test_defaults_leave_worlds_byte_identical() -> None:
    for qk in ("state", "belief", "order"):
        for seed in range(30):
            a = generate(_params(qk, scramble=0.2), seed)
            b = generate(_params(qk, scramble=0.2, noop_distractors=0, relational_time=0.0), seed)
            assert a == b
            assert not any(isinstance(e, NoOp) for e in a.events)


def test_both_dims_flow_through_apply_config() -> None:
    base = _default_base("state")
    params = _apply_config(base, {"noop_distractors": 3.0, "relational_time": 0.4})
    assert params.noop_distractors == 3 and isinstance(params.noop_distractors, int)
    assert params.relational_time == 0.4 and isinstance(params.relational_time, float)
    world = generate(params, 11)
    realized = measure(world)
    assert realized["noop_distractors"] == 3.0
    assert realized["relational_time"] > 0.0


def test_mutators_declared_answer_preserving() -> None:
    assert {"add_noop_distractors", "add_relational_time"} <= ANSWER_PRESERVING


def test_relational_mutator_inverts_order_pair_in_narration() -> None:
    for seed in range(40):
        plain = generate(_params("order", scramble=0.0), seed)
        w = add_relational_time(plain, 0.5, seed, invert_order_pair=True)
        i, j = w.query.pair
        early, late = (i, j) if w.events[i].time < w.events[j].time else (j, i)
        pos = {ev: k for k, ev in enumerate(w.narration_order)}
        assert pos[early] == pos[late] + 1
        assert w.events[early].time in w.relative_times
        assert answer(w) == answer(plain)
