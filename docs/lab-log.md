# Lab log

Running record of findings, bugs, and decisions — including the ones that
turned out to be wrong. Kept for the writeup's methodology section and so
we don't re-litigate settled questions. Append newest entries at the top.
Status values: `open`, `fixed`, `confirmed`, `retracted`, `decided`.

---

## 2026-09-17 — BUG (critical, fixed) — witness information never reaches rendered text

**What we saw:** Belief campaign against Nemotron Super on Nebius found the
*easiest possible* belief config (chain=2, scramble=0, coref=0,
distractors=2) only passes 3/5 trials — below the decomposition engine's
70% threshold, so every decompose attempt returned `unexplained` with
"baseline did not pass under probing."

**First read (wrong):** looked like a real finding — "Nemotron Super has
an intrinsic false-belief floor around 60%, decoupled from surface
difficulty."

**Root cause, confirmed by reading both failed transcripts and the code:**
`cartographer/world/simulate.py` assigns a belief to every entity in an
event's `witnesses` set, not just the `actor`. `cartographer/render/templates.py`
only ever interpolates `{actor}` into sentence templates — grepping the
entire render module for "witness" returns zero matches. So when the
simulator says a bystander witnessed and therefore believes something, the
rendered story never places that bystander in the scene. The model reads
pure text and correctly answers "unknown" for an agent it was never told
was present. Two transcript examples: Gideon asked-about but only
described doing unrelated actions (reorganizing papers, coffee); Lena
asked-about but not mentioned in the story at all.

**Blast radius:** every belief-query result to date is contaminated —
today's four campaign boundaries (chain_length, scramble, coref_pressure,
distractor_events), and the earlier pilot's belief-collapse curve
(0.83→0.17→0.00 across difficulty cells). State and order results are
unaffected (no witness dependency).

**Fix (not yet done):**
1. Render non-actor witnesses into prose ("Gideon looked on as Petra
   carried the tablet...").
2. Add witness facts to the LLM fidelity-gate schema (`cartographer/llmrender/facts.py`)
   so LLM renderings are held to the same standard.
3. Add a surface-completeness test: run the round-trip extractor on our
   OWN template output and assert every fact the verifier's answer depends
   on is textually present — this would have caught the bug at Phase 2.
4. Re-run the belief campaign once fixed.

**Why this is still a good outcome:** the decomposition engine's refusal
to explain an unreliable baseline, plus full-transcript logging, is what
surfaced this in minutes instead of shipping a false finding. Caught by
the instrument's own audit trail before reaching any public claim.

**Fix applied:** `cartographer/render/templates.py` now appends a witness
sentence after any event's main sentence when that event has non-actor
witnesses (per `event.witnesses`) — a small seed-picked template pool
("{witness} was nearby and saw this happen.", "{witness} happened to be
watching.", "{witness} noticed this from across the room."; a `{witnesses}`
variant joins multiple names with "and"/commas). The witness sentence
reuses the parent event's own `_time_anchor`, so it carries the same anchor
format every other sentence does. No clause is added for the event's own
actor-equivalent participants (already implied) or when
`event.witnesses - actors` is empty, so the "unknown" (no-belief) path
stays reachable. Witness names feed into the existing generic
`_apply_pronouns` pass as ordinary participants, so pronoun bookkeeping
("unique most-recent same-gender mention within 2 sentences") required no
special-casing — it already operates over whatever sentence/participant
lists it's given; a 200-seed independent cross-check against belief-query
worlds with witness sentences found zero pronoun-resolution mismatches, so
`test_pronoun_safety` needed no changes to its assertions.

Added `sentence_event_idx`/`sentence_is_witness`/`witness_sentences` to
`RenderedTask.meta` so tests (and any future caller) can align sentences
back to source events without guessing, since a strict 1-sentence-per-event
correspondence no longer holds. Updated
`test_time_anchors_consistent_with_latent_chronology` (which only exercises
"state" queries, so it never actually hits witness sentences, per
`generate.py`'s belief-only witness assignment) to assert `witness_sentences
== 0` explicitly, and added a sibling
`test_time_anchors_consistent_with_witness_sentences` that forces belief
queries and re-checks the same per-sentence anchor-matches-latent-time
invariant through the new meta fields.

Added `tests/test_render.py::test_surface_completeness_witnesses_are_rendered`
— the actual regression test for this bug class: it independently replays
`world/simulate.py`'s belief bookkeeping to find, for every
`(entity_id, obj_id)` belief key, the LAST event that set it (a later event
silently overwrites an earlier belief write for the same key, so only the
final setter matters for what the text needs to justify), then asserts that
entity is a rendered participant (main-sentence actor-equivalent, or
witness-sentence witness) of that event. Runs across 40 generated
belief-query worlds plus 3 hand-built synthetic worlds (same idiom as
`test_world.py`'s `_false_belief_world`) to force a 2-non-actor-witness case,
since the generator itself only ever adds at most one extra witness per
belief chain. Confirmed coverage of 0/1/2+ non-actor-witness cases and that
some entity somewhere still ends up with no belief (so "unknown" stays
reachable) — this test would have failed against the pre-fix renderer and
passes now.

Secondary fix (LLM-rendering hygiene, item 7) also completed since it
stayed clean: `cartographer/llmrender/facts.py::world_facts()` now emits a
`witnesses` list per fact (surface names, `event.witnesses` minus the
fact's own actor-equivalent fields); `facts_match()` gained a third
comparison pass — independent of the core type+time+fields signature match,
so it doesn't disturb existing missing/hallucinated/altered semantics —
that walks every matched fact pair and reports `missing_witness: ...` for
any expected witness name absent from the extracted fact's witness list
(normalized the same way other fields are, so case/article differences
don't false-positive). `EXTRACT_PROMPT_TEMPLATE` in
`cartographer/llmrender/extract.py` and `RENDER_PROMPT_TEMPLATE` in
`cartographer/llmrender/renderer.py` were updated to document/request the
new field. Four new FakeClient-mocked tests added to `tests/test_llmrender.py`
(witnesses field present and typed correctly, non-actor witness surfaced
and actor excluded, a dropped witness triggers `missing_witness`, and
case/article variance in witness names is tolerated). All 21
`test_llmrender.py` tests pass; zero network calls anywhere.

Full suite: 124/124 passing (up from a 118-test baseline; net +6 new tests:
2 in `test_render.py`, 4 in `test_llmrender.py`). No belief campaign has
been re-run yet post-fix — that's the natural next step before trusting any
new belief-query numbers.

---

## 2026-09-17 — DECISION (decided) — switch dev campaign from Groq to Nebius/Nemotron mid-run

**Context:** live belief campaign on Groq (`gpt-oss-120b` stand-in) was
projected at ~7h worst case due to free-tier rate limits (30 RPM / 6,000
TPM) causing multi-minute backoff stalls on a meaningful fraction of calls.

**Considered:** (a) multiple Groq accounts — rejected, ToS explicitly
prohibits multi-account rate-limit evasion, risks suspension; (b) Groq
Developer tier (free, 10x limits) — viable but still not the real
submission model; (c) switch to Nebius/Nemotron now.

**Decided:** switch. Nebius default limits (~60 RPM / ~400,000 TPM, plus
auto-scaling) are ~66x the Groq free tier on TPM, Nemotron pricing is
cheap ($0.05–2.20/1M tokens depending on tier), and the results directly
count toward the submission instead of being a throwaway rehearsal.
Killed the Groq process cleanly; added real Nemotron pricing to
`pricing.py`; campaign re-ran on `nvidia/nemotron-3-super-120b-a12b` in
~9 minutes total for $0.10 (vs. the Groq run's hours and higher spend for
non-submission-valid data).

---

## 2026-09-17 — BUG (fixed) — pilot execution errors

**What we saw:** 15 ERROR verdicts across early pilot runs on Groq.

**Root causes, all confirmed from stored records:** (1) 10x HTTP 429 —
Groq free-tier rate limiting; (2) 3x cap-hit — reasoning models
(`gpt-oss-*`) exhaust small `max_tokens` budgets on internal reasoning
before emitting an answer, returning empty text; (3) 2x anomaly — API
returned a length-capped empty response without setting `finish_reason`.

**Fix:** client now retries 429/5xx/timeout with exponential backoff
honoring `Retry-After`; auto-retries empty length-capped responses at a
larger token budget; distinguishes genuine empty "stop" answers
(`empty_answer=1`) from infra failures and cap-hits (`cost_capped`/`cap_hit`)
in `verifier_detail`. 95/95 tests green after the fix.

---

## 2026-09-17 — FINDING (confirmed, pre-witness-bug) — state tracking has no boundary through chain_length 30

Both Groq stand-ins (`gpt-oss-120b`) and real Nemotron Super pass 100%
(Nemotron: 29/30, one incidental fail) through 30-step object-state
chains. Not affected by the witness-rendering bug (state questions don't
depend on witnesses). Real negative result: state tracking is not the
discriminating dimension for these models at this scale — the generator's
range would need to go well past 30 to find a boundary, if one exists at
all in a realistic range.

---

## 2026-09-14/15 — DECISION (decided) — rejected two reframings of the project

**Reframing 1 (a GPT critique):** proposed dropping "capability
mapping + router" for "budget-constrained active discovery of capability
boundaries" as a research-paper-style contribution.

**Why rejected:** literature check found this exact framing heavily
published (ACE, ProbeLLM, SEA, AutoBencher) — adopting it would mean
competing against funded labs on their own turf, the worst possible bet
for a 6-week hackathon. The critique also silently dropped the one
genuinely novel piece (failure ablation/decomposition) without engaging
with it.

**Reframing considered and partially adopted:** router demoted from
headline to downstream demo (RouteLLM/NotDiamond/OpenRouter already own
that space); synthetic validation harness adopted as phase 0 (this was a
good idea from the critique, kept).

**Final locked framing:** capability frontier mapping via adaptive
probing (methodology borrowed from published IRT/CAT work, cited openly)
+ failure decomposition into causal factors/interactions (no prior art
found — the actual novel contribution) + router as closing demo.

---

## Template for new entries

```
## YYYY-MM-DD — TYPE (status) — one-line summary

**What we saw:** ...
**Root cause / reasoning:** ...
**Fix / decision / next step:** ...
```
