# Lab log

Running record of findings, bugs, and decisions — including the ones that
turned out to be wrong. Kept for the writeup's methodology section and so
we don't re-litigate settled questions. Append newest entries at the top.
Status values: `open`, `fixed`, `confirmed`, `retracted`, `decided`.

---

## 2026-09-19 — BUILD (confirmed) — adversarial proposer: Nemotron proposes questions, never answers

**What we built:** `cartographer/propose/` — the "not rigid generators"
engine. Non-negotiable design rule: the proposer LLM proposes generator
CONFIGS (questions), never answers; ground truth always stays
simulation-only (`cartographer.world.simulate`), so a hallucinating
proposer wastes budget but can never corrupt a finding.

- `digest.py`: deterministic plain-text summary of a subject model's prior
  results from the store (per-query-kind pass rates, per-dim pass rates by
  tercile, confirmed trap signatures parsed from `verifier_detail`, recent
  FAIL/PASS configs). Byte-identical on repeat calls against an unchanged
  store.
- `proposer.py`: `PROPOSAL_PROMPT` gives the digest + the exact `DimParams`
  schema (ranges read straight from `world/generate.py` and the validated
  ceilings in `exec/evaluator.py`) plus the foil vocabulary from
  `world/foils.py`, and asks for N JSON proposals, each with a
  `hypothesis`. `propose_configs` parses robustly (fence-stripping,
  reasoning-preamble tolerance, one JSON-only retry, else `[]` — never
  raises).
- `gauntlet.py`: zero-API validation. Clamps/rejects out-of-range values,
  builds+validates the `World`, runs `search_adversarial_seeds` with
  `require_kinds=require_traps`, and rejects any proposal whose best world
  scores below the MEDIAN of a same-size random-seed control batch — the
  proposer has to beat random chance before a single subject-model dollar
  is spent on it. Every rejection carries a reason string.
- `loop.py`: `run_proposer_loop` — digest → propose → gauntlet → k real
  trials per survivor (via `PipelineEvaluator`, run_id
  `f"{run_id}/round{r}"`) → Wilson-interval fitness; survivors at failure
  rate ≥0.3 join a `trap_library`; both hits AND duds get fed back into the
  next round's digest. Round 1 also runs a same-size RANDOM control arm
  (tagged `f"{run_id}/control"`) — the required honesty check, since ~half
  of round-1 subject budget goes to it. Contamination guard
  (`proposer_model != subject_model`) checked eagerly, same pattern as
  `llmrender/gate.py`'s renderer/subject guard. Hard `cost_cap` shared
  across proposer + subject + control spend; stops cleanly on exhaustion.

**Tests:** `tests/test_propose.py`, 18 new tests, zero network calls
(scripted fake clients matching the `ScriptedClient` pattern in
`tests/test_evaluator.py`). Covers: digest determinism/no-evidence/max_chars,
valid/fence-wrapped/garbage-then-retry/garbage-twice JSON parsing, gauntlet
clamping + invalid-config rejection + below-random-median rejection +
batch seed-distinctness, loop contamination guard, hit-finding +
dud-feedback, cost-cap clean stop. Full suite: 298/298 green (259 baseline
+ 18 new + concurrent work from another agent already landed on
`evaluator.py`/viewer).

**Live smoke test** (`scripts/run_proposer_loop.py`, run once, hard-capped
at $0.40, actual spend **$0.0229**): proposer = `nvidia/nemotron-3-super-
120b-a12b`, subject = `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, 2 rounds x 4
proposals x k_trials=5, run_id `propose-super-vs-nano-001`.

```
round 0: proposed=4 rejected_at_gauntlet=1 measured=15 hits=2 control_measured=15 control_hits=2 spend=$0.0127
round 1: proposed=4 rejected_at_gauntlet=0 measured=20 hits=2 control_measured=0 control_hits=0 spend=$0.0101

trap library (4 entries, all belief queries):
  round=0 failure_rate=0.60 (CI [0.23,0.88], n=5) chain_length=18 scramble=0.5 coref=2 distractors=15 filler=10 witness_cutoff=0.3
    hyp: "Long chain with heavy scrambling and many distractors overloads memory, causing reliance on the most frequent location trap."
  round=0 failure_rate=0.40 (CI [0.12,0.77], n=5) chain_length=12 scramble=0.1 coref=0 distractors=0 filler=0 witness_cutoff=0.5
    hyp: "Slight scrambling disrupts precise order recall, leading the model to fall back on the last mentioned location as a heuristic."
  round=1 failure_rate=0.80 (CI [0.38,0.96], n=5) chain_length=20 scramble=0.4 coref=4 distractors=12 filler=6 witness_cutoff=0.2
    hyp: "High coref pressure with many distractors causes pronoun resolution errors, leading the agent to rely on the last mentioned location despite witnessing most of the chain."
  round=1 failure_rate=0.80 (CI [0.38,0.96], n=5) chain_length=15 scramble=0.6 coref=2 distractors=5 filler=20 witness_cutoff=0.4
    hyp: "Heavy scrambling with moderate coref pressure and many filler events creates temporal confusion, causing the agent to misremember the final true state."

proposer mean failure rate: 0.457
control (random) mean failure rate: 0.533
beat_random: FALSE
total spend: $0.0229
```

**Honest reading:** the proposer clearly found real, reproducible failure
modes (4 configs at 40-80% measured failure, well above the α=0.3 hit bar,
with sensible hypotheses about coref/scramble/witness-cutoff interactions
— largely consistent with the confirmed Nano failure modes already logged
on 2026-09-17/18). But it did NOT beat its own random control arm in this
single small run (n=15 each side, means 0.457 vs 0.533, well within noise
at this sample size — Wilson CIs on the individual configs are wide,
[0.12,0.96]). This is a real negative result at this budget, not a bug:
Nano's belief-query failure surface at these difficulty levels is
apparently dense enough that RANDOM sampling in the same explored region
finds failures about as often as Nemotron Super's targeted guesses do. A
fair follow-up (out of scope for this budget) would run more rounds/larger
k to shrink the CIs before concluding the proposer adds no value — one
round's control comparison is exactly the kind of noisy read the
`beat_random` honesty check exists to surface rather than hide.

**Judgment calls:** proposer-call spend is approximated at $0 against the
shared `cost_cap` (real API cost was ~$0.0003/call, immaterial at this
budget, and Nemotron Super isn't always in the static pricing table);
`query_kind` in a proposed config is accepted/clamped but the evaluator's
`base` DimParams still fixes `query_kind="belief"` for actual measurement
(consistent with how every other belief-focused campaign in this repo
works — a proposer suggesting `query_kind: "order"` gets a valid, scored
gauntlet result but is measured as a belief-query trial); control-arm
budget is capped at half of whatever's left after round-1 proposer calls,
not a fixed dollar amount, so it scales with `cost_cap`.

---

## 2026-09-19 — BUILD (confirmed) — one-button `full_map` campaign + plain-English verdict panels

**User feedback driving this (verbatim):** (1) "there are 5 different types
of campaign type. Are we supposed to run them one by one? That sounds
counterintuitive." (2) "still the UX sucks. I get no idea what the test
reveals."

**Fix 1 — `full_map` campaign (`cartographer/exec/evaluator.py`):**
`run_full_map_campaign(client, store, run_id, model, budget=600,
cost_cap=2.0, ...)` chains state -> order -> belief -> adversarial_belief
-> breaking_point as stages under one parent `run_id`, each stage running
as a full campaign against a child run_id (`f"{run_id}/{stage}"`) against
the SAME store/client. Budget and cost_cap are split proportionally per
stage (`_FULL_MAP_STAGE_BUDGET_SHARES` / `_FULL_MAP_STAGE_COST_SHARES`,
targeting ~60/80/200/160/remainder trials at the default budget=600). A
stage exhausting its own cap logs a note and the battery continues to the
next stage (never blocks). If the belief stage finds a dim boundary, the
breaking_point stage's `EscalationRay` is re-seeded so that dim's
`base_value` sits at the discovered boundary instead of the generator's
easy default, concentrating the escalation ray where belief already found
trouble; falls back to the default all-ceiling ray with a note if no
boundary was found. Returns a `FullMapResult` (list of `StageSummary`:
pass rate, boundaries, hypotheses, spend per stage). Wired into
`/launch`: `full_map` added to `CAMPAIGN_PRESETS`, `DEFAULT_CAMPAIGN_TYPE
= "full_map"` (now the pre-selected option in the campaign_type dropdown
and the prefilled budget/cost_cap), and a RECOMMENDED-badged description
card.

**Fix 2 — verdict panels (`cartographer/report/verdict.py`, new):**
`run_verdict(store, run_id) -> RunVerdict` is a pure, deterministic,
no-LLM function that reads stored records and produces a headline +
plain-English paragraphs + up to 3 concrete notable-failure examples.
Saturation detection (pass rate >= 97% -> "did not challenge the model"),
sharp-boundary / diffuse-weakness detection via tercile-bucketed per-dim
pass rates, and belief-failure classification by regenerating the exact
world from stored `(latent_params, task_seed, rendering_seed)` provenance
(same idiom as `scripts/foil_report.py`) and matching the model's response
against `cartographer.world.foils.foil_answers` -- guarded by a
`created_at` cutoff so pre-`313e3b2` runs that can't be exactly
regenerated get an honest "predates the generator-hardening fix" note
instead of a silent mis-attribution. `full_map` parent run_ids (no direct
records -- everything lives under child `f"{run_id}/{stage}"` run_ids) get
an aggregated per-stage verdict via `_full_map_verdict`. Handles empty
runs, all-ERROR runs, and state/order runs (no foil language) gracefully.
Wired into the viewer: `run_html` now renders the verdict panel
(`data-testid="verdict-panel"`) above the raw per-trial table (and pulls
in child-stage records for full_map parents); `/runs` shows each run's
one-line headline under its run_id (full_map child run_ids are folded
into a synthetic parent row so the table shows one row per full_map
launch, not five); the overview page (`/`) gained a "What recent runs
revealed" section listing the 5 most recent runs' headlines.

**Real verdict examples generated against `results/campaign.sqlite`**
(pasted verbatim, unedited):

```
=== camp-adversarial_belief-nemotron-3-ultra-550b-a55b-001 ===
HEADLINE: Run camp-adversarial_belief-nemotron-3-ultra-550b-a55b-001: 97% pass rate on belief queries.
- Of 133 scored trials (belief query kind), the model passed 97% and failed 3%.
- 2 additional trial(s) ended in ERROR (infra failures, cost cap, or cancellation) and are excluded from the pass rate above.
- Diffuse weakness (below 90% but no outright flip) on: temporal (pass rate 88% at the high end).
- In 3 of 4 classifiable belief failures the model reported where the object ACTUALLY was, not where the queried entity BELIEVED it was -- it collapsed their perspective into reality.
```

For comparison, a saturated adversarial run and an older pre-hardening
belief run both produced the expected qualitatively-different copy:
`camp-adversarial_belief-nemotron-3-ultra-550b-a55b-002` -> "passed
essentially everything (99%) -- this test did not challenge the model";
`camp-belief-nemotron-super-002` -> "4 belief FAIL(s) were recorded, but
they predate the generator-hardening fix and can't be safely
re-classified against regenerated ground truth."

**Judgment calls:**
- Stage cost/budget shares are fixed proportional constants, not
  adaptively rebalanced when an earlier stage under- or over-spends --
  kept simple and deterministic rather than trying to redistribute unused
  cap live.
- `breaking_point`'s per-record "pass rate" isn't directly comparable to
  the other stages' (it's an escalation ray, not a flat sweep), so
  `_full_map_verdict` reports it qualitatively (pass/fail counts across
  the ray) rather than folding it into the "weak province" pass-rate
  comparison used for the other four stages.
- Belief-failure classification treats any record with `created_at >=
  2026-09-17T17:20:42` as reconstructable (mirrors `313e3b2`'s commit
  time) rather than maintaining an explicit run_id allowlist like
  `scripts/foil_report.py` -- works for any future run without needing a
  code change, at the cost of being a few seconds less precise than an
  exact commit-boundary check (immaterial in practice, campaigns run in
  minutes-to-hours batches, never split across that exact second).
- Sharp-boundary / diffuse-weakness detection buckets each dim's *realized*
  values into terciles rather than reusing the scheduler's own
  explore/refine points, so it works uniformly across dims with very
  different numbers of observed levels (including full_map runs where
  every stage explores a different set of dims).

**Verification:** 298/298 tests pass (up from the 259-baseline the task
started against; the difference also includes tests added concurrently by
the propose/ work in this same window). New: 6 full_map evaluator tests
(`tests/test_evaluator.py`), 8 verdict tests
(`tests/test_verdict.py`), 2 viewer verdict-panel tests
(`tests/test_viewer.py`), 5 /launch full_map tests
(`tests/test_launch.py`), plus 1 existing /launch test updated to assert
the new full_map default instead of the old state default. Sanity-checked
live against `results/campaign.sqlite` (see verdict examples above) and
confirmed `run_html`/`index_html`/`runs_html` all render without error on
every real stored run_id, including the pre-hardening ones.

---

## 2026-09-18 — BUILD (confirmed) — viewer information-architecture redesign

**What we saw (user feedback, verbatim):** "The UI and UX is shitty. I
don't know shit to navigate this and I can't find all the information.
You're saying to run the new mode but don't know where exactly it is."
The viewer had grown as a debug console: home page was a raw runs table,
`/launch` listed campaign presets as bare dropdown values with no
explanation, and there was no page anywhere that summarized what the
instrument had actually found — including the project's own owner
couldn't locate `adversarial_belief` (the newest campaign preset) or its
significance.

**Fix (viewer redesign, `cartographer/viewer/**`):**
- `/` is now an overview dashboard: one-paragraph plain-English
  description of the instrument, a live "models tested x pass rate per
  query kind" card, a "confirmed failure modes" preview linking to
  `/findings`, total-trials/total-spend stat boxes, and quick links with
  one-line descriptions to every other section. The old raw runs table
  moved to `/runs` (`runs_html`, still linked from the nav).
- `/launch` now renders a description card for every campaign preset
  (query kind, one-sentence summary, typical cost, typical duration)
  instead of a one-line budget/cap note, on both the pre-model-load and
  post-model-load steps. `adversarial_belief` gets a NEW badge and the
  description "foil-driven trap selection — picks worlds where
  plausible-but-wrong answers compete with the truth; broke Ultra where
  max-difficulty escalation couldn't" (`launch.py::CAMPAIGN_DESCRIPTIONS`).
- New `/findings` page (`findings.py`): hard-coded, dated, run_id-tagged
  capability map sourced from this log — belief→reality collapse
  (corrected post-witness-fix), Nano continuity-of-observation,
  witness_cutoff generalization, Ultra protagonist-continuity pronoun
  bias (6/6 ablation separation), Ultra abstention-under-trap-pressure
  (marked needs-replication), and the strategic escalation-vs-trap-
  selection result. A collapsed "Retractions" section keeps the
  pre-witness-fix "Nemotron Super has an intrinsic false-belief floor"
  claim visible with its root cause, instead of deleting it —
  self-skepticism as a visible feature, not silent history-rewriting.
- New `/coverage` page (`coverage.py`): full inventory of the 3 query
  kinds, 6 difficulty dims, 5 foil traps, and the analysis machinery
  (escalation, resample-gate, ablation, fresh-seed confirmation), sourced
  from the relevant modules' docstrings, plus an honest "Not yet covered"
  roadmap (tool/action planning, instruction constraints, code,
  quantitative reasoning, long-context retrieval).
- Run detail (`/run/<id>`) now surfaces `traps=...` (parsed from
  `verifier_detail`) inline per row for adversarial runs, and links to
  "full transcript" (record detail) instead of a bare "detail" link;
  record detail (`/record/<id>`) surfaces the same trap-kind line.
- Persistent nav (Overview / Findings / Coverage / Runs / Live / Atlas /
  Launch) with active-page highlighting was already present as a shared
  shell (`_shell`/`_NAV_ITEMS`) and just got the two new entries; every
  page already had or now has an h1 + one-line `.meta` subtitle.

**Tests:** extended `tests/test_viewer.py` (nav-present-on-every-page,
findings lists all four modes + retractions, coverage lists all 6 dims +
5 foils, run/record detail show trap kinds for adversarial records,
overview shows live summary + quick links) and `tests/test_launch.py`
(preset cards show the adversarial_belief description + NEW badge).
Renamed the old `test_index_html_shows_verdict_tallies` to
`test_runs_html_shows_verdict_tallies` against the relocated `/runs`
builder. Full suite: 259/259 passing (up from 254). Sanity-checked by
actually starting `python3 -m cartographer.viewer.serve` against
`results/campaign.sqlite` and curling every route (all 200) plus
confirming real `traps=...` values render on an actual adversarial run's
detail page.

---

## 2026-09-18 — BUILD (confirmed) — foil model + adversarial seed search: redefining difficulty as trap salience, not knob magnitude

Motivated directly by the 2026-09-17 entries below: across all 38
post-witness-fix belief FAILs on Nano/Super/Ultra, 71% were the model
answering the object's TRUE FINAL LOCATION instead of the queried agent's
actual believed location (belief→reality collapse) — while the five
scalar difficulty knobs (chain_length, scramble, coref_pressure,
distractor_events, filler_events) showed near-zero discrimination; only
`witness_cutoff` (which forces belief≠reality directly) produced real
signal. Conclusion acted on here: difficulty should be redefined from
"magnitude of knobs" to "salience of plausible-but-wrong answers" (foils),
and that redefinition should be a measurable, searchable property of a
generated world — not just a post-hoc explanation for failures already
observed.

**Foil model (`cartographer/world/foils.py`).** `foil_answers(world,
name_map)` computes what a *shallow reasoner* would answer, per query
kind. Belief: `final_true_state` (the belief→reality-collapse trap),
`last_mentioned_location` (trust narration order over witnessing),
`last_witnessed_by_anyone` (conflate "someone saw it" with "the query
agent knows it"), `query_agent_own_last_move` ("I moved it, so I know
where it is" — a trap only when something happens to the object
afterward that the agent doesn't see), `most_frequent_location`. State:
`first_mentioned_location`, `most_frequent_location`. Order:
`narration_order_answer` — trusting narration order over real timestamps,
the direct foil for what `scramble` is supposed to stress.
`foil_divergence(world, name_map)` returns a `FoilProfile`: which foils
diverge from the true answer (`trap_kinds`), how many (`n_diverging`),
and a `salience_weighted_score` — each diverging foil's weight is the
fractional narration-position (0..1) of its LAST mention, so a wrong
answer planted in the closing sentence outweighs one only mentioned in
the opening sentence. A world where nothing diverges is trivially easy
(the lazy answer happens to be right); verified this actually occurs at
chain_length=1 with zero distractors/filler (test:
`test_no_witness_cutoff_can_still_be_trivial`).

**Adversarial seed search (`cartographer/world/search.py`).**
`search_adversarial_seeds(params, base_seed, n_candidates=40, top_k=1,
require_kinds=())` derives `n_candidates` seeds from `base_seed` via the
same sha256-stream idiom as `generate.py`, generates+validates each
world, scores by `salience_weighted_score`, and returns the top-k. Pure
simulation — zero API calls, so this is free relative to the token spend
that follows. Deterministic: same `(params, base_seed, n_candidates,
top_k, require_kinds)` always returns the same result (verified in
`test_search_is_deterministic`). The key empirical test
(`test_key_empirical_search_concentrates_traps`) runs 50 seeds at a fixed
mid-difficulty belief config and checks the mean `salience_weighted_score`
of the search's top-1 picks against the mean of the single unselected
seed each trial would otherwise have used — **selected mean was ≥1.5x the
unselected mean** (the test enforces this ratio as a hard assertion), so
the search demonstrably concentrates traps rather than picking arbitrarily.

**Evaluator wiring (`cartographer/exec/evaluator.py`).**
`PipelineEvaluator` gained `adversarial: bool = False` and
`adversarial_candidates: int = 40`. When on, `__call__` runs
`search_adversarial_seeds` from the normally-derived task seed and
substitutes the winning seed for world generation — so `task_seed` /
`rendering_seed` in the persisted record are the CHOSEN adversarial seed,
not the plain derived one (this is deliberate: it's what actually got
administered). `verifier_detail` gains
`;adversarial=1;traps=<comma-joined trap_kinds>;foil_score=<rounded
score>`; `realized_dims` gains `foil_divergence` (the count of diverging
foils) so the atlas/scheduler can treat trap density as a measured
dimension alongside the existing five. New preset
`run_adversarial_belief_campaign`: belief query, `adversarial=True`, same
dims as `run_belief_campaign` (which already sweeps `witness_cutoff`) —
the point is that even the BASELINE config gets its world adversarially
selected, so escalation measures trap-*compounding*, not "does a trap
exist." Registered in `/launch`'s `CAMPAIGN_PRESETS["adversarial_belief"]
= {"budget": 300, "cost_cap": 1.0}` and `_RUNNERS` dispatch — the only
viewer-territory edit made, and additive (didn't touch anything the
concurrent ablate-track agent might have added to those same dicts).

**Offline report (`scripts/foil_report.py`).** Loads
`results/campaign.sqlite` read-only, regenerates each post-witness-fix
belief FAIL's world from stored `(latent_params, task_seed,
rendering_seed)`, and checks whether the model's normalized response text
matches one of the diverging foils' surface strings. Judgment call: exact
byte-for-byte world regeneration is only valid for runs started AFTER the
`313e3b2` generator-hardening commit (2026-09-17 17:20:02 UTC), which
changed chain-actor exclusion/witness-set assignment inside `generate()` —
verified this by literally diffing regenerated vs. stored prompt text.
`camp-belief-nemotron-nano-001` and `camp-belief-nemotron-super-002`
predate that commit and are excluded with an explicit warning rather than
silently mis-attributed to a world the model was never actually shown;
the other three post-fix belief runs against Nano/Super/Ultra tiers
(`camp-belief-nvidia-nemotron-3-nano-30b-a3b-001`,
`camp-belief-nemotron-3-super-120b-a12b-001`,
`camp-belief-nemotron-3-ultra-550b-a55b-001` — 14+5+3 = 22 FAILs total)
reconstruct exactly, 22/22. Report output, run once against the live
store:

```
=== NVIDIA-Nemotron-3-Nano-30B-A3B (14 belief FAILs, 14/14 exactly reconstructed) ===
trap kind                     count   % of FAILs
------------------------------------------------
final_true_state                 12        85.7%
UNCLASSIFIED                      2        14.3%
(any foil matched)               12        85.7%

=== Nemotron-3-Ultra-550b-a55b (3 belief FAILs, 3/3 exactly reconstructed) ===
trap kind                     count   % of FAILs
------------------------------------------------
final_true_state                  2        66.7%
UNCLASSIFIED                      1        33.3%
(any foil matched)                2        66.7%

=== nemotron-3-super-120b-a12b (5 belief FAILs, 5/5 exactly reconstructed) ===
trap kind                     count   % of FAILs
------------------------------------------------
final_true_state                  3        60.0%
UNCLASSIFIED                      2        40.0%
(any foil matched)                3        60.0%

=== overall ===
17/22 belief FAILs (77.3%) matched a plausible-but-wrong foil answer

Note: the following belief runs predate the generator-hardening commit (313e3b2, 2026-09-17 17:20:02 UTC) and cannot be exactly regenerated by the current generate() -- excluded from this report:
  - camp-belief-nemotron-nano-001
  - camp-belief-nemotron-super-002
```

77.3% on the exactly-reconstructable subset, same direction and order of
magnitude as the originally-eyeballed 71% across all 38 — and every trap
that DID resolve was `final_true_state`, i.e. belief→reality collapse
alone accounts for the entire matched share on this data; no other named
foil fired even once on these FAILs. That's a sharper, narrower claim
than "shallow reasoners fall for various foils" — on this evidence the
project has exactly one dominant, well-characterized failure mode, and
the foil model now gives a name and a reproducible detector for it.

**Other judgment calls.** (1) `salience_weighted_score`'s lateness
weighting looks only at the location-naming subsequence (Move
destinations + Tell claims) for belief/state queries, and at the query
pair's own narration positions for order queries — chosen because those
are exactly the sentences a shallow reasoner would be scanning for an
answer; filler/distractor sentences don't carry candidate answers so
weighting against the full sentence list would just dilute the score.
(2) `foil_answers`/`foil_divergence` live in `cartographer/world/foils.py`
rather than `render/`, even though they need `NameMap` for surface
strings, to keep the query-answering logic beside `simulate.py`'s
`answer()` (the ground truth it's being compared against) — this forced
a `TYPE_CHECKING`-only import of `NameMap` in `foils.py` and a
function-local import of `build_name_map` in `search.py`, since
`render/naming.py` imports `world.model`, which runs through
`world/__init__.py` at package-load time; importing `render` at module
scope from inside `world/` would be circular. (3)
`query_agent_own_last_move` returns `None` (never a trap) when the agent
never acted as mover, rather than falling back to some other value —
`None` foils are excluded from divergence scoring by design, since a foil
that was never planted can't be a trap. (4) Zero live campaigns run as
part of this task (per the no-network constraint) — `run_adversarial_belief_campaign`
is wired and unit-tested against a scripted fake client only; the first
live `adversarial_belief` run against a real model is future work.

Files: `cartographer/world/foils.py`, `cartographer/world/search.py`,
`cartographer/world/__init__.py`, `cartographer/exec/evaluator.py`,
`cartographer/viewer/launch.py`, `scripts/foil_report.py`,
`tests/test_foils.py`, `tests/test_foil_report.py`, additions to
`tests/test_evaluator.py` and `tests/test_launch.py`. 25 new tests, full
suite green (254 passed, including the concurrently-developed
`cartographer/ablate/` track).

---

## 2026-09-18 — BUILD (decided) — automated the manual Ultra pronoun-ablation loop (`cartographer/ablate/`)

Motivation: the manual loop that isolated the Ultra pronoun-actor-tracking
bug (resample the same prompt to rule out noise, make one minimal
answer-key-preserving edit, resample both versions) was the project's most
valuable diagnostic move so far and needed to become automatic and
deterministic, with ground truth always coming from simulation, never a
model's own judgment. Built as a new, self-contained package plus tests
and a dry-run analysis script; no edits to `cartographer/world/` or
`cartographer/exec/evaluator.py` (owned by a concurrent session).

**`cartographer/ablate/resample.py`** -- `resample_failure(evaluator,
config, task_seed, n=5, stable_threshold=0.5)` re-runs the SAME
config+seed `n` times and reports `is_stable_failure = pass_rate <=
stable_threshold`, reusing `cartographer.confirm.stats.wilson_interval`
for the CI. This is deliberately the cheapest possible check, and runs
before any attribution, precisely because the "Priya" case above was a
single-trial FAIL prematurely reported as a finding (4/5 resamples came
back correct) -- this gate would have caught it before a single ablation
call was spent.

**`cartographer/ablate/ablations.py`** -- six deterministic prompt
transforms over the SAME latent `World`, each provably preserving
`answer_key` (asserted internally; `AblationUnavailable` raised, never
silently swallowed by the ablation itself, when a world can't yield an
answer-key-preserving variant for that ablation).
- `baseline` -- unmodified render (control).
- `pronoun_suppression` -- resolver-backed, not regex: `render()` already
  records exactly which sentence/entity every pronoun substitution came
  from (`RenderedTask.meta["pronoun_referents"]`), so this inverts those
  exact substitutions back to real names rather than guessing at
  coreference from surface text.
- `distractor_removal` / `filler_removal` -- **judgment call, changed
  approach mid-build**: the obvious route (regenerate from the same seed
  with `distractor_events=0`/`filler_events=0` via `world.generate.DimParams`)
  was tried first and rejected. `World` doesn't retain the `DimParams` it
  was built from, and reconstructing `witness_cutoff`/`coref_pressure`
  well enough to reproduce the same witness assignments purely by
  measuring the world proved unreliable -- confirmed empirically:
  regenerating from a reconstructed `DimParams` changed the belief answer
  key on ordinary worlds with `witness_cutoff > 0` or `coref_pressure > 0`
  (exactly the dimension this whole project's main finding depends on).
  Switched to direct surgery on the already-built `World`: drop the
  target events wholesale, reindex `narration_order` (and an `order`
  query's `Query.pair`, which held stale old-indices and needed
  remapping too -- caught by a 120-world/3-query-kind sweep before
  landing), leave every surviving event's `witnesses`/`actor` fields
  byte-identical. Zero answer-key drift across the sweep once fixed.
- `witness_explicitation` -- rewrites only witness-marker sentences
  (located via `RenderedTask.meta["sentence_is_witness"]`, not text
  matching) to a single canonical "`<Name> was present and saw this`"
  template. **Second judgment call**: originally implemented on top of
  `pronoun_suppression`'s output, which made it indistinguishable from
  `pronoun_suppression` in every test case (both ended up removing every
  pronoun in the story) -- caught by the planted-sensitivity test for
  model A, which attributed to both ablations instead of just one.
  Rewritten to start from the ordinary baseline render (pronouns intact
  in the main narration) and touch only witness sentences, so a model
  sensitive to main-narrative pronoun ambiguity still fails this ablation.
- `temporal_normalization` -- narration re-emitted in true chronological
  order; answer key is provably unchanged since `narration_order` never
  participates in `world/simulate.py::answer`.

**`cartographer/ablate/attribute.py`** -- `attribute_failure(
evaluator_factory, world, name_map, seed, n=5, alpha=0.05)`: resample-gates
first (returns `stable=False` immediately if the failure evaporates,
running zero ablations); otherwise runs every ablation available for that
world `n` times, two-proportion z-tests each against the resample gate's
own baseline arm (no extra baseline calls spent), Holm-Bonferroni corrects
across the family, and attributes any ablation that is both
corrected-significant and has effect size > 0.3. Multiple attributions are
allowed and reported; none attributing yields the honest note "cause not
isolated by current ablation catalog" rather than an error.

**Tests (`tests/test_ablate.py`, 11 new, all passing, zero network)** --
mirrors the synthetic-zoo discipline: plants a known sensitivity and
requires the engine to recover it exactly.
- Fake model A fails iff a story sentence's subject is a bare pronoun
  ("he "/"she ") -- attribution names `pronoun_suppression` only (after
  the witness_explicitation redesign above; before that fix it named
  both, which is what caught the confound).
- Fake model B fails iff more than 2 distractor-object sentences are
  present -- attribution names `distractor_removal` only.
- Fake model C is a deterministic seeded coin biased ~60% pass (mirroring
  the real Priya case's 4/5-passed profile) -- the resample gate correctly
  marks `stable=False` and runs zero ablations. This is the direct
  regression test for the retracted single-trial claim above.
- Fake model D fails everywhere -- no attribution, honest "cause not
  isolated" note, while still confirming every ablation was actually
  tried (distinguishing this from model C's gate short-circuit).
- Every ablation's answer-key invariant checked across 120 generated
  worlds (40 seeds x 3 query kinds), not just 20.
- `attribute_failure` and `resample_failure` both checked for
  byte-identical output across repeated identical-input calls.

**`scripts/attribute_failures.py`** -- loads `results/campaign.sqlite`
READ-ONLY (via `ResultsStore._all()`, the same internal pattern
`export_jsonl` itself uses for a full dump), selects post-fix belief FAILs
newest-first, regenerates each world from its exact stored `latent_params`
+ `task_seed` (exact reconstruction here, unlike the ablation catalog's
post-hoc guess, because the real `DimParams` dict is on the record), and
prints the ablation plan (which ablations are available, estimated call
count) in `--dry-run`-equivalent default mode -- no flag needed, nothing
but local sqlite reads and pure Python happens unless `--live` is passed
explicitly. `--live` wires a real `cartographer.exec.client.Client`
against Nebius and is fully implemented but was never invoked in this
session (verified: ran the dry-run path only, against a `/tmp` copy of
the real store, never opening the tracked file writably).

Verified: `python3 -m pytest -q` -- 254 passed (was previously green
before this work; net +11 from `test_ablate.py`, remainder from a
concurrently-running session's own `cartographer/world/` work). No edits
outside this session's assigned territory
(`cartographer/ablate/`, `tests/test_ablate.py`, `scripts/`).

Files: `cartographer/ablate/__init__.py`, `cartographer/ablate/resample.py`,
`cartographer/ablate/ablations.py`, `cartographer/ablate/attribute.py`,
`tests/test_ablate.py`, `scripts/attribute_failures.py`.

---

## 2026-09-18 — CHANGE — `modelcart` launcher command

Added a launcher script at `~/.local/bin/modelcart` (already on PATH) so
the viewer starts with a single word from any directory: `modelcart` (or
`modelcart <db_path> <port>` to override). It `cd`s into the repo root
and runs `python3 -m cartographer.viewer.serve`.

Two supporting fixes in `cartographer/viewer/serve.py`, both real bugs
independent of the launcher:
- Default db path was `results/pilot.sqlite` (stale dress-rehearsal data)
  — now `results/campaign.sqlite`, the real store every campaign in this
  log has been writing to since the Nebius switch.
- Relative db paths are now resolved against the repo root
  (`Path(__file__).resolve().parents[2]`), not the caller's cwd, and the
  server auto-opens a browser tab on `http://127.0.0.1:<port>/` after a
  short delay. Verified by launching from `/tmp` (unrelated cwd) and
  confirming the real Overview page served correctly.

Not committed to the repo itself (`~/.local/bin/modelcart` lives outside
the tree, machine-local) — noting here so the convenience isn't a
mystery later.

---

## 2026-09-17 — FINDING (confirmed) — adversarial (foil-driven) selection breaks Ultra where magnitude-escalation could not; new failure mode found

First live run of `run_adversarial_belief_campaign` against Nemotron Ultra
(`camp-adv-belief-ultra-001`): 150 calls, $0.4477, 141 PASS / 6 FAIL / 3 ERROR
(94.0%). Compare: the full escalation ray (`camp-breaking_point-ultra-002`)
cranked every scalar knob to the generator's hard ceiling and never dropped
Ultra below 0.857 — no boundary. Trap-selection at *minimal* structural
complexity did better than maximal magnitude.

**Per-dim pass rates:** chain_length 0.96, scramble **0.86**, coref_pressure
1.00, distractor_events 0.96, witness_cutoff 1.00. The weak-signal refinement
pass fired on `scramble` (the machinery built for exactly this diffuse-failure
case working on real data).

**Counterintuitive structural result: all 6 failures occurred at
`chain_length=2` — the SHORTEST chain.** Four of six shared scramble=0.71, and
every one carried the same triple-trap signature (final_true_state +
last_mentioned_location + last_witnessed_by_anyone all diverging). Working
hypothesis: short chains give the model little redundant evidence to
self-correct against, so a highly salient wrong answer dominates; long chains
supply corroborating scaffolding. If it survives replication this inverts the
project's founding assumption that difficulty scales with magnitude.

**Resample discipline applied (5x each, temp=0), per the standing post-Priya
rule — 3 of 6 failures survived:**
- fail#0 (scramble 0.48): 'unknown' 5/5 — STABLE. answer_key='the supply closet'.
- fail#4 (scramble 0.71): 'unknown' 5/5 — STABLE. answer_key='the loading dock'.
- fail#5 (scramble 0.71): 'south stairwell' 4/5 — STABLE. answer_key='the garage'.
- fail#1, #2, #3: not reproducible (MoE variance; one resample hit a token cap).
All three stable prompts reproduce byte-identically from stored seed+params,
and their answer keys are simulation-derived — these are real.

**NEW FAILURE MODE, distinct from the two already catalogued:** two of the three
stable failures are Ultra answering **'unknown'** — declining to attribute any
belief — when the belief IS determinable from witnessed events. This is the
*opposite* of the belief→reality collapse (77% of prior failures) and of the
protagonist-continuity bias. Call it **belief-attribution abstention under trap
pressure**: when foils are dense and narration is scrambled, the model retreats
to "no belief" rather than committing to the inferable one. Needs replication on
fresh seeds before headlining.

---

## 2026-09-17 — BUG (latent, open) — cap-truncation with non-empty reasoning would be mis-scored as FAIL

While resampling the above, one Ultra response came back `finish_reason=length`,
`tokens_out=8000`, `reasoning` field empty, and `text` full of truncated
chain-of-thought — the model never reached an answer. `PipelineEvaluator` only
routes cap-hits to ERROR when the text is EMPTY; a truncated *reasoning dump* is
non-empty, so it would be handed to the verifier, mismatch the answer key, and be
recorded as a capability FAIL.

**Blast radius: ZERO on existing data — verified, not assumed.** Queried all 292
stored FAIL records across every run: none has `finish_reason='length'`. No
historical number is contaminated. (An initial alarm that this affected all
results was checked and retracted within minutes.) The bug is latent: it fires
only when a cap-truncation lands with non-empty text, which happened in an
ad-hoc resample but not in any campaign.

**Fix (not yet applied):** treat `finish_reason == 'length'` as ERROR/cap_hit
regardless of whether text is empty, and/or trigger the existing
`max_tokens_retry` escalation on truncated-but-non-empty responses too. Add a
regression test with a mocked length-capped non-empty response asserting the
verdict is ERROR, not FAIL.

---

## 2026-09-17 — FINDING (confirmed) — escalation mode finds Ultra's ceiling exceeds the generator's current limits

`run_breaking_point_campaign` against `nvidia/Nemotron-3-Ultra-550b-a55b`
(`camp-breaking_point-ultra-002`), belief query, full default ray (all
6 dims from baseline to hard ceiling: chain 25, distractors 40,
coref 8, filler 60, witness_cutoff 0.9 — all raised together at each
step, not one at a time). Cost: $0.18, 56 calls.

**Result: `found=False`, honestly reported.** Pass curve across the
full ramp (t=0 → t=1.0, step 0.15): `[1.0, 0.86, 1.0, 1.0, 1.0, 0.86,
1.0, 0.86]` — never dropped to or below the fail threshold (0.3), so
the engine correctly declined to invent a boundary, bisect, decompose,
or confirm anything. This is the intended honest-failure path, not a
bug: the escalation ray ran its full length and the model simply held.

**What this means:** our current 5-dimension generator, even at its
absolute hardest achievable combined configuration, cannot break
Nemotron Ultra on belief-tracking. Ultra's true capability floor on
this task family sits above our instrument's current difficulty
ceiling. This is a legitimate, well-powered negative result (56 calls
spanning the entire ramp, not a single undersized probe) — not the
same category of thing as the earlier single-trial noise mistake
above. To find where Ultra actually breaks, the next lever is either
extending existing dims further (chain past 25, cutoff past 0.9) or
introducing one of the 5 parked dimensions (tool-planning is the
strongest candidate) — the current family may simply be a capability
this model has already mastered.

---

## 2026-09-17 — FINDING (confirmed) — Ultra pronoun-actor-tracking bug, causally isolated; earlier "3-for-3 tiers" claim partially retracted

Manually re-verified all 3 belief FAILs from `camp-belief-nemotron-3-ultra-550b-a55b-001` by regenerating each world from its stored seed/params and reading the true per-event actor/witness ground truth (not just the rendered prose). Then ran a live ablation directly against Ultra (`nvidia/Nemotron-3-Ultra-550b-a55b`, temp=0.0, same 8000/16000 token budget as the campaign): for each case, replaced ONLY the ambiguous pronoun with the true actor's name (everything else byte-identical) and resampled both the original and disambiguated prompt 3x each for a fair comparison.

**Correction:** the "Priya" failure — which I previously reported as "matching Nano's confirmed continuity-of-observation mode, present in all three tiers" — does NOT reproduce. 4 of 5 total samples of that exact prompt (1 campaign trial + 4 fresh resamples) answered correctly. That claim is retracted as premature; it was very likely MoE routing/sampling noise (Ultra is a 550B mixture-of-experts model — known to have real run-to-run output variance even at temperature 0 due to expert-routing effects), not a stable capability gap. Lesson: a single-trial FAIL should never be reported as a finding without a resample check, even when it superficially matches a pattern seen in other models.

**Real finding, causally isolated — a distinct and more specific mechanism than "continuity of observation":** in the Dexter and Felix cases, a bystander who is EXPLICITLY named as a witness in one sentence becomes the ACTOR of the very next event via a pronoun ("he"/"she"). Per the renderer's own stated resolution rule (unique most-recent same-gender entity), the pronoun correctly and unambiguously binds to that bystander — this is not a rendering bug, the ground truth actor really is the bystander. Ultra gets it wrong anyway, in a way that looks like it defaults to assuming the chain's ESTABLISHED mover (the entity who acted in prior events) keeps acting, rather than updating on the pronoun's correct recency-based binding to the newly-active bystander.

Ablation results (temp=0, 3 samples per condition):
- **Dexter/box: 3/3 wrong with the pronoun, 3/3 right with the name substituted.** Clean 6/6 separation, zero overlap — the strongest single result in the project so far.
- **Felix/basket: 1/3 wrong with the pronoun (vs. 2/3 right), 3/3 right with the name.** Same direction, weaker/noisier signal, consistent with a milder instance of the same bias.

**Naming this failure mode precisely** (distinct from the Nano/Super continuity-of-observation mode, though related): **protagonist-continuity bias in pronoun-driven actor tracking** — the model appears to weight "who has been acting" more heavily than "who was the pronoun's antecedent by the stated recency rule," specifically at the moment a witnessed bystander hands off to become the actor.

**Why this matters for the instrument's credibility:** this is the second time in one day the project caught and corrected its own premature claim (after the witness-rendering bug) — this time by resampling before trusting a single record, which is a cheaper and faster check than the full regenerate-and-verify-ground-truth step, and should become standard practice before any FAIL is reported as a finding. Recommend: extend `confirm_hypothesis`'s fresh-seed re-testing to also resample the SAME seed/config a few times as a first-pass noise filter before spending budget on fresh-seed confirmation.

---

## 2026-09-17 — FIX (fixed) — ultra-model token budgets, and a stop button for /launch

Trigger: a breaking-point campaign against `nvidia/Nemotron-3-Ultra-550b-a55b`
ERRORed out entirely -- every trial cap-hit even after the client's
existing 4000-token auto-retry, because Ultra's reasoning trace alone can
burn through that budget before it ever emits an answer. Separately,
there was no way to stop a runaway campaign from the UI short of killing
the process.

**Token budgets (`cartographer/exec/evaluator.py`, `cartographer/exec/client.py`).**
Added `default_token_budget(model)`: any model id containing `"ultra"`
(case-insensitive) gets `max_tokens=8000` / `max_tokens_retry=16000`;
everything else keeps the prior 1500/4000 defaults. `PipelineEvaluator`
now accepts optional `max_tokens` / `max_tokens_retry` overrides -- an
explicit `max_tokens` with no matching retry override derives one as
`2 * max_tokens` rather than inheriting the model heuristic's retry value,
since that pairing stops making sense once `max_tokens` itself has been
overridden. All four `run_*_campaign` wrappers now take and thread through
`max_tokens`. `/launch`'s step-2 form gained an "Advanced: max tokens"
field (blank = auto); `_campaign_thread` only passes `max_tokens` /
`cancel_event` to a runner that actually declares those parameters
(via `inspect.signature`), so fake runners in tests with the old 5-arg
signature keep working untouched.

To tell a **persistent** cap-hit (exhausted even the bigger retry budget)
from a **transient** one, `ChatResult` gained a `retried: bool` field, set
by `Client.chat` only when its own auto-retry-on-length path actually
fired. `PipelineEvaluator` now appends `;retry_exhausted=1` to
`verifier_detail` alongside `cap_hit=1` exactly when `result.retried` is
True -- i.e. the length-capped result we're looking at is itself the
retried call.

**Stop button (`cartographer/viewer/launch.py`, `cartographer/viewer/serve.py`).**
Campaign threads can't be killed mid-API-call, so cancellation is
cooperative: `handle_start` now creates a `threading.Event` alongside the
campaign thread and stores it in the (module-private) `_CURRENT_RUN` slot.
`PipelineEvaluator` takes an optional `cancel_event` ctor arg and checks it
right where it already checks the cost cap -- if set, it short-circuits
exactly like a cost-cap hit (`verifier_detail` gets `;cancelled=1`, no API
call, `False` returned), so the scheduler drains at the next trial
boundary and the thread exits on its own. `launch.current_run()` never
leaks the event to external callers (popped before returning); new
`request_stop()` / `is_stopping()` are the only ways in from outside the
module. `/live` renders a "Stop campaign" button (form POST to `/stop`)
whenever a campaign is actively registered, swapping to a "stopping…"
notice once the event is set; `/stop` just calls `request_stop()` and
redirects back to `/live`.

Judgment calls: (1) the `"ultra"` substring match is deliberately
case-insensitive and unanchored -- broad enough to catch provider-prefixed
ids like `nvidia/Nemotron-3-Ultra-550b-a55b` without a per-provider
allowlist; (2) `max_tokens_retry`'s "no override -> 2x the override"
fallback was chosen over "no override -> inherit the model heuristic's
retry value" because the latter silently gives an explicitly-small
override (e.g. `max_tokens=500` on an Ultra model) a 16000-token retry
ceiling, which doesn't track the caller's evident intent; (3) `/stop`
doesn't distinguish "nothing was running" from "something was running and
got stopped" in its HTTP response (always 303 -> `/live`) since either
way `/live` is the right place to look, and a silent no-op is harmless.
Did not run any live campaigns as part of this work -- all new tests are
mocked, zero network.

Files: `cartographer/exec/evaluator.py`, `cartographer/exec/client.py`,
`cartographer/viewer/launch.py`, `cartographer/viewer/serve.py`,
`tests/test_evaluator.py`, `tests/test_launch.py`, `tests/test_viewer.py`.

---

## 2026-09-17 — FINDING (confirmed) — order campaigns: Super clean, Nano near-clean

First live runs of `run_order_campaign` (chronology recovery from
scrambled narration), Nebius, post-redesign parallel executor.
`camp-order-super-001`: 105 calls, $0.0284, 0 errors — 100% pass on all
four dims (chain, scramble, distractors, filler). `camp-order-nano-001`:
105 calls, $0.0172, 3 errors — ~96% on chain/distractors/filler, 100% on
scramble; failures diffuse, no boundary triggered. Neither model's
chronology tracking breaks in-range even at full scramble; the earlier
Groq-pilot order degradation (1.0→0.67→0.33) does not replicate on
Nemotron tiers. Order joins state as a saturated query kind; belief
remains the discriminating one. Escalation mode is the tool for pushing
order further if ever needed.

---

## 2026-09-17 — FIX (fixed) — /live active-run bug, ETA overhaul, and a shared shell/design system

User feedback verbatim: "The UI is kind of shitty. Not just the design
but the navigations are a mess and the new launch isn't showing right
away. The ETA is wildly wrong." Two functional bugs fixed first, then a
navigation + visual overhaul. Territory: `cartographer/viewer/*` only.

**1. `/live` didn't show a just-launched run.** `live_html` picked the
run with the newest `created_at` record; a freshly launched campaign has
zero records for its first seconds-to-minutes (world gen + first API call
latency), so the page kept showing the previous run. Fix: `live_html` now
calls `launch_mod.current_run()` first and, if a campaign is actively
registered, renders it even with 0 records ("starting… 0 records", empty
progress bar, no tallies) — only falling back to the newest-record run
when nothing is actively registered. Also: a run still actively
registered is never shown as "complete" even if its newest record happens
to look stale (e.g. mid-stall), since the launcher's bookkeeping is the
source of truth for liveness, not record recency alone. New tests:
`test_live_html_prefers_active_run_with_zero_records`,
`test_live_html_falls_back_to_newest_record_when_no_active_run`.

**2. ETA was wildly wrong.** Old ETA = `remaining / rate` where `rate`
averaged over the *entire* run (first record to last) — a long stall or a
recent fast burst poisoned it badly, and it kept projecting large
"remaining" numbers for runs that legitimately finish early (budget is a
ceiling: explore can find nothing and end the campaign well under
budget). Fix (`_recent_rate_per_min` + `_format_eta` in
`cartographer/viewer/serve.py`): rate is computed over a RECENT window —
records from the last 3 minutes (by record timestamp, not wall clock) —
widened to the last 25 records only when that time-window sample is too
thin to be meaningful. Fewer than 10 usable recent records anywhere ->
"estimating…" instead of a number. A completed run never shows an ETA at
all. Displayed ETA is capped at "at most &gt; 2h at current rate" rather
than printing an absurd total, and both the progress bar and ETA are now
phrased as an upper bound — "n / budget max" and "at most ~X min at
current rate" — rather than a promise, since budget is a ceiling. New
tests: `test_recent_rate_uses_recent_window_not_whole_run_average`,
`test_format_eta_estimating_on_small_sample`,
`test_format_eta_normal_case`, `test_format_eta_caps_huge_projection`,
`test_live_html_completed_run_never_shows_eta`,
`test_live_html_small_sample_shows_estimating`.

**3. Navigation + design overhaul.** Every page (`/`, `/live`, `/atlas`,
`/launch`, `/run/*`, `/record/*`) now renders through one `_shell(title,
active_nav, body)` in `serve.py`: a header with the product name (links
to `/`) and a nav bar (Runs · Live · Atlas · Launch) that highlights the
current page, replacing the old ad-hoc `"← runs | full run detail"`
inline links. Single inline `<style>` block: dark instrument-panel theme
(`#111418` bg, `#E8EAED` text, one teal accent `#2DD4BF` for links/active
nav/progress fill), verdict pills (`PASS #34D399`, `FAIL #F87171`, `ERROR
#FBBF24`) instead of colored text, monospace for run_ids/configs/numbers,
system sans for prose, thin rounded progress bar with a distinct green
"complete" fill, and stat-box cards for /live's tallies instead of a
bullet list. `/` replaced its bare run-id list with a table (run_id,
model, records, pass-rate pill, cost, relative "started" time, live/
complete status), newest-first. `/atlas`'s SVG charts got a matching dark
background/gridline/text palette (accent-family series colors) with
chart-generation logic untouched. No JS added beyond the pre-existing
5s meta-refresh on `/live`. Existing test assertions that checked exact
old-theme HTML (hex colors, old table shape) were updated to check the
same underlying semantics (tallies, ordering, escaping) against the new
markup; `test_key_never_in_any_returned_html`-style guarantees are
unaffected since key handling in `launch.py` wasn't touched beyond
stripping its now-redundant inline `<nav>` fragments (the shell supplies
navigation).

Full suite: 175 passed (`tests/test_viewer.py` 35, full repo 175). Smoke
tested all routes (`/`, `/live`, `/atlas`, `/launch`, `/run/<id>`,
`/record/<id>`) against a copy of `results/campaign.sqlite` on
127.0.0.1 — all 200, no 500s.

Judgment calls: (a) the recent-rate window prefers the time-based slice
whenever it already has >= the min sample size, and only widens to a
record-count window when the time slice is thin — reading the task's
"whichever is larger sample" as being in service of "reflect the recent
phase," not literally always-larger, since always-larger degenerates to
the whole-run average again once a run has few total records. (b) a
registered active run with 0 records shows no tallies/stat-grid (nothing
to tally yet) rather than a zeroed-out grid, to avoid implying data that
doesn't exist. (c) index page's pass-rate pill always renders in the PASS
color regardless of actual rate — pills are used for at-a-glance verdict
identity elsewhere; a rate-dependent color for this one column felt out
of scope for the two-bug-fix mandate and was left as a candidate follow-up.

---

## 2026-09-17 — PHASE (shipped) + FINDING (confirmed) — escalation mode, order/witness_cutoff campaign wiring, and the first verified Super belief failures

**Campaign wiring (exec/schedule territory), three pieces:**

**1. witness_cutoff joins the belief campaign** (`exec/evaluator.py`).
`run_belief_campaign` now sweeps `DimSpec("witness_cutoff", 0.0, 0.9,
False)` alongside the four original dims (baseline 0.0, ceiling 0.9 — the
generator's validated hard ceiling). `PipelineEvaluator`'s config mapping
accepts the key as a float (same path as scramble), and every record's
`latent_params` now carries `witness_cutoff` explicitly (0.0 recorded for
configs that don't set it, so old-vs-new records stay comparable).

**2. Order campaign preset.** `run_order_campaign` (budget 400, cap $0.50,
k_trials 5) sweeps chain_length 2–20, scramble 0–1, distractor_events 2–30,
filler_events 2–40 — the dims that plausibly stress chronology recovery;
witness/coref dims are belief-specific and stay at baseline. Registered in
the viewer's `CAMPAIGN_PRESETS`/`_RUNNERS` (the one-dict exception to this
session's viewer no-touch rule; merged additively against the concurrent
viewer redesign, which had already touched launch.py — no conflicts).

**3. Escalation mode** (`cartographer/schedule/escalate.py`, new — the
centerpiece). Instead of one-dim-at-a-time sweeps, an `EscalationRay` walks
a straight line through the FULL joint difficulty space: t=0 all dims easy,
t=1 all dims at the generator's hard validated ceilings (chain 25, scramble
1.0, coref 8, distractors 40, filler 60, witness_cutoff 0.9 — the
combined-clean config from `tests/test_witness_cutoff.py`).
`find_breaking_point` ramps t by 0.15 (k_trials=7 per probe), bisects the
first pass→fail bracket to width ≤ 0.03, fits a logistic over every
(t, outcome) trial for t* + bootstrap CI (reusing `schedule/estimate.py`),
then hands `config_at(t_fail)` vs `config_at(0)` to decompose (≤ 40% of
budget) and, when a non-unexplained hypothesis emerges, to
`confirm_hypothesis` on fresh seeds (≥ 10_000 convention). If the model
never breaks by t=1.0 the result is an honest `found=False` / "model
exceeds generator limits". `run_breaking_point_campaign` (budget 350, cap
$0.75) wires a PipelineEvaluator to the default 6-dim belief ray; also in
`CAMPAIGN_PRESETS`. Synthetic-zoo tests: a weighted-sum LogisticRule
(s = 0.6a + 0.4b, analytic crossing t* = 0.55) is recovered within 0.1;
always-pass → honest not-found; degenerate baseline; budget hard-respected;
an InteractionRule frontier yields a confirmed `pairwise_interaction`
hypothesis with effect size > 0.2; two runs byte-identical.

Suite: 192 passing (net +17: 9 escalate, 8 campaign-preset/wiring). Zero
network in tests.

**FINDING — the 5 Super belief failures in
`camp-belief-nemotron-3-super-120b-a12b-001` are real (instrument checked
first, per the standing rule).** For each FAIL: the world regenerated from
stored latent_params/task_seed reproduces the stored prompt byte-identically,
the stored answer_key matches `simulate()`'s ground truth, and `verify()`
replays FAIL. These are the first Super-tier belief failures ever recorded.
Two distinct modes:

- **Continuity over-generalization (1/5, the clean case):** config
  chain=4/scramble=0/coref=0/distr=2 — Victor witnesses moves through 8:02
  (break room), does NOT witness the 8:03 move to the loading dock; Super's
  own reasoning trace asserts "he watched … which ended at 8:03" — a false
  premise — and answers the true final location. Exactly Nano's confirmed
  failure mode, now observed (rarely) in Super, at a near-baseline config.
- **Pronoun misresolution (4/5):** the model attaches a rendered
  "she"/"he" to the wrong same-gender antecedent and thereby mis-tracks
  what the query agent did/saw. In three of these the correct antecedent
  is the query agent named in an immediately preceding *witness sentence*
  ("At 8:03, Juno noticed this from across the room. At 8:04, she took…" —
  she=Juno per the renderer's unique-most-recent rule and standard English
  recency preference; Super read Nadia). The fourth (chain=2, scramble=1.0)
  is scramble-order confusion: "he"=Finn (most recent male) read as Gideon.

**Caveat worth carrying into any writeup:** pronoun resolution in all four
cases is deterministic per the renderer's rule and linguistically standard,
so these count as model failures — but witness-sentence insertion raises
same-gender collision pressure exactly where belief attribution matters
(the witness sentence puts the query agent's name adjacent to another
same-gender actor's). Before headlining "Super fails theory-of-mind," an
ablation with pronouns suppressed around witness sentences would cleanly
separate coref failure from belief-tracking failure. Flagged, not done.

**Task 5 status (order campaigns live on Super + Nano): NOT RUN.** The
live-spend instruction reached this agent as relayed approval only; holding
until the user confirms directly. Everything is wired — `run_order_campaign`
against both models is a two-call script away.

---

## 2026-09-17 — DECISION (decided) — parallel wave execution, /live completion state, and a browser /launch page

Three infrastructure changes shipped together (scheduler/exec/viewer
territory, disjoint from the concurrent generator/confirm work):

**1. Parallel wave execution** (`cartographer/schedule/scheduler.py`).
Every evaluator call used to be sequential; campaigns now run waves of
independent trials (explore levels x k_trials, k_trials per bisection
point, weak-signal re-probes) through a
`ThreadPoolExecutor(max_workers=SchedulerConfig.parallelism)` (new field,
default 6). Determinism is preserved exactly: budget is charged and seed
blocks assigned on the coordinating thread in the same order as a
sequential run BEFORE any trial is submitted, results are gathered in
submission order, and `on_record` fires sequentially in that order — so
parallelism changes wall-clock interleaving only, never which probes run,
their seeds, or the record stream. Because charging is up-front, the
budget can never overshoot (tighter than the +max_workers allowance the
design permitted). Points are additionally sorted before every
`fit_logistic`/`boundary_ci` call, since the bootstrap resamples by index
and would otherwise be sensitive to accumulation order. Bisection stays
sequential per-step (inherently), but its k_trials per point parallelize.
Verified by `test_parallel_campaign_identical_to_sequential`:
parallelism=1 vs 6 on clean_threshold with artificial latency produce
byte-identical boundaries, points, tallies, notes, and on_record streams.

**2. Thread safety for persistence.** Chosen fix (of the two candidates):
`ResultsStore` itself is now thread-safe — `check_same_thread=False` plus
an internal write lock serializing `append`/`append_many` — because
appends genuinely happen from executor worker threads, so a lock in
`PipelineEvaluator` alone would still trip sqlite's same-thread check.
`PipelineEvaluator` additionally guards its own mutable state (spend/call/
error counters, cap check) with a lock; chat -> verify runs fully parallel.
The cost cap becomes soft under concurrency: up to parallelism-1 in-flight
calls can each start just under the cap (documented in the class
docstring). Rate limits: new `Provider.suggested_parallelism` (GROQ=2 for
the 30 RPM free tier, NEBIUS=6) is the default when `run_state_campaign`/
`run_belief_campaign` callers don't pass `parallelism` explicitly.

**3. /live completed-run state** (`cartographer/viewer/serve.py`). A
finished run used to look stuck — bar frozen at partial %, bogus ETA. If
the newest record is older than 3 minutes, /live now renders "RUN
COMPLETE — finished at N calls" with a full-width green bar, no ETA,
tallies kept, and no auto-refresh.

**4. /launch page** (`cartographer/viewer/launch.py`, new; wired into
serve.py with a first `do_POST`). Browser flow to start campaigns without
the CLI: provider + optional API key -> server calls the provider's
/models endpoint (new stdlib `list_models` in exec/client.py, filtered to
chat-capable ids) -> model dropdown + campaign type + budget/cost-cap
prefilled from the evaluator presets -> Launch spawns the campaign in a
daemon thread with an auto-generated `camp-{type}-{model-short}-{NNN}`
run_id and redirects to /live. Keys live only in process memory: the
step-2 form carries an opaque one-shot token into an in-memory dict, never
the key; tests assert a sentinel key never appears in any returned HTML.
One campaign at a time via a locked module-level slot; while busy /launch
shows "campaign in progress" linking to /live. Server still binds
127.0.0.1 only — local tool, not a deployment.

Suite: 167 passing at the time of writing (net +28 from this work: 3
scheduler-parallel, 3 list_models, 5 /live-state, 10 launch, plus reworked
live fixtures using now-relative timestamps). Zero network in tests.

---

## 2026-09-17 — PHASE (shipped) — Phase 7 confirmation module (decompose's missing check)

Added `cartographer/confirm/` (new package): decompose finds candidate
factor-set explanations for a failure using probes drawn from a seed range
the search itself controls; nothing previously re-checked those
explanations against seeds the search never touched. `confirm_hypothesis`
does that: given a `FailureHypothesis`, it recovers the failing/baseline
configs from the hypothesis's own evidence (decompose's `evidence[0]` and
`evidence[1]` are always the `verify_failing`/`verify_baseline` probes, in
that fixed order, for every hypothesis kind including "unexplained" --
confirmed by direct inspection, not assumed), re-probes both on fresh
seeds, and reports `effect_size = baseline_pass_rate - failing_pass_rate`
with a Wilson 95% CI per rate (`cartographer/confirm/stats.py`, closed-form,
no scipy) and a pooled two-proportion z-test p-value.
`confirmed = raw_p < 0.05 and effect_size > 0.2` -- both a statistical and a
practical-significance bar, so a technically-significant but tiny effect
doesn't count. `confirm_all` runs a family of hypotheses through
Holm-Bonferroni step-down correction (`stats.holm_bonferroni`) and
substitutes each hypothesis's corrected per-comparison threshold for the
flat 0.05 one.

**Seed convention (documented in `confirmation.py`'s module docstring, not
runtime-enforced since this module can't see what seed range a given
decompose call used):** search-time probing uses `seed_base < 10_000`;
confirmation uses `seed_base >= 10_000` (the default). Each hypothesis in a
`confirm_all` family gets its own disjoint fresh-seed block
(`2 * n_fresh_seeds * k_trials` apart) so no hypothesis's probing can leak
into another's and re-running is byte-identical.

Tests (`tests/test_confirm.py`, 9 new): decompose's own
`pairwise_interaction` hypothesis confirms with effect size >0.9 and
p<0.001 on fresh seeds; a hand-built FALSE hypothesis (claims `distractors`
cause a failure on `clean_threshold`, which only depends on `temporal`) is
correctly *not* confirmed (effect size <0.2); a 3-hypothesis family (1 real
pairwise-interaction result + 2 hand-built false single-factor claims)
confirms exactly the real one under Holm-Bonferroni; determinism for both
`confirm_hypothesis` and `confirm_all`; `to_json_dict()` shape; and direct
unit tests of the three stdlib stats primitives (Wilson interval bounds
and n=0 degenerate case, two-proportion z-test's null/maximally-different
cases, Holm-Bonferroni against a hand-computed 3-p-value example).
`scripts/confirm_demo.py` (main-guarded, not run by tests) runs
decompose-then-confirm across four zoo models plus one explicit
`confirm_all` family call and prints a table -- verified by hand, all four
zoo hypotheses confirm with effect size 1.00 as expected (the zoo's
underlying rules are deterministic given trial_seed, so a correctly
recovered factor set should separate cleanly).

---

## 2026-09-17 — PHASE (shipped) — witness_cutoff: generator hardening targeting the Nano failure mode

Implements item 2 (and the belief-ceiling half of item 1) of the PLANNED
entry directly below, which is why that entry's status now reads
"superseded by this entry" rather than "open."

**New `DimParams.witness_cutoff: float = 0.0`** (`cartographer/world/generate.py`).
At 0.0, behavior is byte-identical to before (belief query agent witnesses
every causal-chain Move except the very last one). As it rises toward 1.0,
the agent's witnessing collapses toward only the chain's earliest moves
(`_witness_cutoff_last_index` linearly interpolates the "last witnessed
index" from `chain_length - 2` down to `0`), while the object keeps moving,
unwitnessed by the agent, for the remainder of the chain. This makes the
correct belief answer an EARLY location -- maximally divergent from the
object's true final location -- which is exactly the trap Nemotron Nano
fell into (see the confirmed finding two entries below: it answered with
the *true final* location, reasoning "he was watching at each move," a
false premise once witnessing has a hard stop).

**Two generator-correctness fixes needed to make the cutoff airtight,**
found by testing against real divergence rates rather than trusting the
mechanism on paper: (1) `pick_entity` gained an `exclude` parameter, and
the chain-actor pick for any post-cutoff chain event now excludes the
query agent -- without this, the agent could still be picked as that
event's *actor* (who is always an implicit witness of their own action),
silently re-extending their witnessed window past the intended cutoff.
(2) added a dedicated small Filler burst (up to 3 events) authored as the
query agent, immediately after their witnessing stops, using the existing
Filler mechanics unchanged -- so their disappearance from the causal
chain's witness sets reads as "still around, just not looking" rather than
"vanished," per the task's requirement. First draft (fix 2 without fix 1)
only reached 72% divergence at witness_cutoff=0.7 over 200 seeds; with both
fixes, 94% -- comfortably over the required 90%.

**`measure()` gains realized dim `witness_gap`**
(`cartographer/world/measure.py`): count of causal-chain Move events after
the query agent's last witnessed chain event that they did NOT witness. 0
for non-belief queries, 0 if the agent never witnessed any chain event
(their belief came from a Tell instead -- a different mechanism, not a
"gap"), and empirically small (<=1) at `witness_cutoff=0.0` since that's
the pre-existing default-witnessing behavior. Verified monotone
non-decreasing in `witness_cutoff` in aggregate across 60-seed populations
at cutoffs 0.0/0.25/0.5/0.75/1.0 (per-seed it isn't perfectly monotone --
actor-exclusion interacts with which chain positions get picked -- so the
test compares population means, not single-seed values).

**Belief-difficulty ceiling verified combined, not swept one dim at a
time:** `chain_length=25, scramble=0.9, coref_pressure=8,
distractor_events=40, filler_events=60, witness_cutoff=0.7` renders and
`validate()`s clean across 10 seeds, with unique entity/location/object
names (no substring-collision hazard triggered -- the existing name pools
in `cartographer/render/naming.py` are sized 30+/34/15\*5 against this
config's needs of ~11 entities / 27 locations / 2 objects, so no pool
enlargement was needed; internal `world/generate.py` name pools are unused
for surface rendering and don't matter here).

No renderer changes were needed beyond what's described above: witnesses
were already rendered per-event via the witness-fix machinery
(`_render_witness_sentence` in `cartographer/render/templates.py`), so the
cutoff's effect is entirely in *which* events end up in each event's
`witnesses` set, not in how those sets get rendered. Re-verified the
surface-completeness invariant (every belief the simulator assigns is
backed by a rendered mention of the believing entity, tied to the correct
event) explicitly under cutoff in `tests/test_witness_cutoff.py`, reusing
the same last-setter-wins reconstruction idiom as
`tests/test_render.py::test_surface_completeness_witnesses_are_rendered`.

Tests: `tests/test_witness_cutoff.py`, 7 new (cutoff worlds validate clean
across 30 seeds x 5 cutoff levels; >=90% belief/true-final-location
divergence at witness_cutoff=0.7 over 200 seeds; witness_gap small at
cutoff=0.0; witness_gap monotone-in-aggregate across cutoff levels;
witness_gap is 0 for non-belief queries; surface-completeness holds under
cutoff; combined-ceiling config renders+validates clean). Full suite:
141/141 in this package's territory passing (up from the prior 132-test
baseline; net +9: 7 in the new file, 2 pre-existing tests unaffected).

---

## 2026-09-17 — PLANNED (superseded by the two entries above) — harden task generator to find Ultra's breaking points

Follow-up to the Nano belief-tracking finding below and the scheduler
sensitivity fix that shipped alongside it (weak-signal refinement path in
`cartographer/schedule/scheduler.py`, plus the new live monitor and atlas
viewer pages). Plan for the next model tier (Ultra):

1. Raise belief difficulty ceilings past what `_belief_scheduler_config`
   currently covers (`chain_length` 2-10, `scramble` 0-1, `coref_pressure`
   0-5, `distractor_events` 2-20) -- chain past 10, and combine higher
   scramble + coref + distractors simultaneously rather than sweeping one
   dim at a time, since Super and Nano's state/belief boundaries were
   already saturating the old ranges (see the two entries below).
2. Add adversarial witness patterns to the generator/renderer that
   systematically test continuity-of-observation -- the exact Nano failure
   mode confirmed below: witness cues that place an entity at a scene and
   then go quiet, so the model must track exactly when witnessing stopped
   rather than over-generalizing that observation continues indefinitely
   past the last explicit cue. Needs new witness-sentence templates (or a
   generator flag) that deliberately end an entity's witnessed presence
   mid-story while further unwitnessed moves happen afterward.
3. Re-run the state + belief campaigns against Ultra with the hardened
   generator once (1) and (2) land.

Status: open -- superseded, see the "witness_cutoff" entry above for what
actually shipped. Item 1's `_belief_scheduler_config` dim-range widening
and item 3's Ultra re-run are still genuinely open -- not touched by this
session, since they live in `cartographer/schedule/`/`cartographer/exec/`
(another agent's territory this session) and require live model calls
respectively.

---

## 2026-09-17 — FINDING (confirmed) — Nemotron Nano shows real belief-tracking failures; Super does not

Ran the same state + belief campaigns on `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`
(`camp-state-nemotron-nano-001`, `camp-belief-nemotron-nano-001`) post
witness-fix. Cost: $0.02, 135 calls, 0 errors.

**State: clean pass, 30/30**, same as Super — no boundary in range.

**Belief: 98/105 (93%) pass — genuine failures, not artifacts.** Explore's
coarse 5-point grid didn't concentrate failures sharply enough at any
single probed level to trigger refine (`boundaries` came back empty
despite 7 real fails) — a scheduler sensitivity gap, not a false
negative on the model. Verified one failure by regenerating the exact
world from its recorded seed/params and checking `simulate()`'s ground
truth directly (not just re-reading the transcript, per the standing
rule after the witness-bug incident): true answer was "the break room"
(the queried entity's last witnessed event placed the object there);
Nano answered "the loading dock," reasoning *"he was watching at each
move"* — a false premise. The object actually moved twice more after the
entity's last witnessed event, unwitnessed by them. **Failure mode:
Nano over-generalizes continuity of observation past the last explicit
witness cue, rather than tracking exactly when witnessing stopped.**

A second failing transcript hinted at a possible distractor-object
naming collision ("worn tablet" vs "worn box" appearing in the same
story) — not chased down yet, flagged for later investigation, may be
a naming-pool issue rather than a model error.

**Why this matters:** confirms the tool can discriminate between tiers
— Super's belief tracking is robust in this range, Nano's genuinely
isn't, with a legible, well-defined error mode. This is the kind of
result the whole project is built to surface. Follow-up: tighten
explore's flip-detection sensitivity (or lower `pass_threshold`/raise
`k_trials` for belief specifically) so a real ~7% failure rate triggers
refine/decompose instead of being absorbed as "no boundary."

---

## 2026-09-17 — FINDING (confirmed, post-fix) — belief campaign re-run on corrected renderer: no boundary found

**Re-ran** `run_belief_campaign` against Nemotron Super (`camp-belief-nemotron-super-002`, fresh run_id, old contaminated `-001` data left untouched for comparison) after the witness-rendering fix (commit `5bd297f`). Cost: $0.03, 105 calls, 0 errors.

**Result: clean reversal of the pre-fix finding.** The literal baseline (chain=2, scramble=0, coref=0, distractors=2) now passes **3/3** (was 3/5 pre-fix). The chain_length explore sweep holds 4–5/5 pass rate all the way to chain_length=10 with no flip. All four dimensions (chain_length, scramble, coref_pressure, distractor_events) returned **no boundary, no hypotheses** — explore found zero flips anywhere in the tested range, so refine/decompose never triggered.

**Interpretation:** once witnesses are actually described in the prose, Nemotron Super's false-belief tracking is robust across this task family's tested difficulty range — the same shape as the state-tracking result (also no boundary through chain 30). This is now a trustworthy negative result, not an artifact: to find where these models actually break on belief tracking, the generator's difficulty range needs to go further (deeper chains, more scramble, more distractors/coref pressure) than what `run_belief_campaign`'s current dim ranges (chain 2–10, scramble 0–1, coref 0–5, distractors 2–20) cover — same lesson as the original state-campaign saturation that motivated widening its range earlier in the build.

**Status of prior numbers:** the four pre-fix boundaries logged in the entry below (chain_length 8.5, scramble 1.04, coref_pressure 1.4, distractor_events 8.75) and the pilot's belief-collapse curve (0.83→0.17→0.00) are **retracted** — both were measuring the witness-rendering bug, not model capability. Kept in the log below for the postmortem record, not as findings.

**Next step, if pursued:** widen belief campaign dim ranges (e.g. chain_length up to 20–25, matching state's widened range) and re-run to find whether a real boundary exists further out.

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
