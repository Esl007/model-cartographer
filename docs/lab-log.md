# Lab log

Running record of findings, bugs, and decisions — including the ones that
turned out to be wrong. Kept for the writeup's methodology section and so
we don't re-litigate settled questions. Append newest entries at the top.
Status values: `open`, `fixed`, `confirmed`, `retracted`, `decided`.

---

## 2026-09-21 — FINDING (confirmed) — FIRST FOUND BREAKING POINT: Nano breaks at t*=0.55 on the tool-planning trap ray

`camp-tool_bp-nano-001` (154 records incl. bisection, tracked $0.068 ~
$0.26 real): **found=True, t*=0.55** — the first time `find_breaking_point`
has ever returned a boundary in this project (every narrative-province
escalation on every tier, and the tool escalations on Super/Ultra,
returned found=False). The provenance fix (commit 1827e06) is confirmed
working in live data: trap dims present in `latent_params` on every
record.

**The cliff is sharp**: pass rate 100% at t=0.45 and t=0.4875, 43% at
t=0.5063 and t=0.525, 29% at t=0.6. A genuine phase transition, not a
gradual slide — exactly the boundary shape the escalation machinery was
built to localize (ramp -> bisect to <=0.03).

**Failure composition (49 FAILs)**: 28 precondition_violation, 20
unknown_tool, 1 missing_arg. Nano fails BOTH ways: it addresses tools
that don't exist (unknown_tool — the shallow id-binding mode it showed
at step 1 in its very first smoke test) AND calls real tools out of
order (precondition_violation). Contrast the verified Ultra mode
(structure perfect, only deep id-binding slips): the three-tier gradient
on this province is now Nano breaks-at-t*=0.55 with mixed
structural+binding failures / Super grazes (1 FAIL total) / Ultra
degrades to 71-86% only at max traps with pure deep-binding slips.

Caveats, stated plainly: (1) tool_bp campaigns don't auto-verify, and
per the credit-rationing plan no bulk resample was run — the evidence
for this finding is the SHARP REPRODUCIBLE CLIFF (100% -> 43% across
adjacent t values, n=7 per point, replicated at two nearby t's on each
side), which single-trial MoE noise cannot produce, rather than
per-failure resampling. A bounded resample-verification pass (~$0.25
real) is the natural hardening step if this becomes a submission
headline. (2) 10 ERRORs are the known token-cap-under-escalation
mechanism, correctly routed to ERROR.

---

## 2026-09-21 — BUGFIX (fixed) — ToolBreakingPointEvaluator now persists trap dims in latent_params

Fixes the provenance bug flagged open in the 2026-09-20 entry below
("deep-plan id-binding collapse" verification session). `ToolBreakingPointEvaluator`
now writes all five trap 0/1 dims (`poison_traps`, `dishonest_decoys_on`,
`implicit_preconditions_on`, `zero_slack_on`, `id_collisions_on`) into both
`latent_params` and `realized_dims` for every record it persists, in
addition to the existing `verifier_detail` string (kept for back-compat
and human readability). World reconstruction from a record's
`latent_params` alone now round-trips: regression test builds a
`ToolBreakingPointEvaluator` against a fake client + tmp store, runs one
config with all traps on, reads the record back, reconstructs the world
from `latent_params` alone via `ToolDimParams` + `_apply_tool_trap_config`,
and asserts the re-rendered prompt is byte-identical to the record's
stored `prompt_text`. Additive change only; existing records in
`camp-tool_bp-super-001`/`camp-tool_bp-ultra-001` predate the fix and are
left as-is (append-only store) -- they still rely on the `verifier_detail`
fallback for trap provenance. Commit `1827e06`. 631/631 tests passing.

---

## 2026-09-20 — FINDING (confirmed) — the structural traps found tool-planning's first real cross-tier failure mode: deep-plan id-binding collapse on Ultra

The trap-mutation escalation (`run_tool_breaking_point_campaign`) ran on
Super (`camp-tool_bp-super-001`, $0.035) and Ultra
(`camp-tool_bp-ultra-001`, $0.178), 56 trials each, sequential.

**Neither tier hard-breaks (found=False, pass rate never <= 0.3), but the
traps ended the province's total saturation**: Super dipped to 71% at
t=0.9; Ultra to 71% at t=0.6/0.9 and 86% at t=1.0 — the first
tool-planning failures ever recorded on either tier (both were 40/40 on
every previous campaign incl. adversarial selection).

**Ultra's failures resample-verified (3x each, exact stored prompts,
against byte-identically reconstructed trap worlds): 3 of 5 stable, and
the stable three are DETERMINISTIC — 3/3 identical failure at the
identical step on the identical tool.** The mode: at step 8-9 of a
9-call optimal plan (zero-slack, all traps on), Ultra calls a
near-collision id (`advance_o1`, `adva-nce_1` — the id_collision
mutator's confusables) instead of the intended tool. The confusable
EXISTS in the catalog (so it isn't `unknown_tool`) but its precondition
doesn't hold at that point -> `precondition_violation`. Deep into a long
plan, under id-confusability pressure, Ultra's binding of intent to
interface identifier degrades -- the plan STRUCTURE is right (n_calls=9,
overhead=0, correct step ordering) and the failure is purely in
addressing the right tool id. The other 2 of 5 were MoE sampling noise
(0/3 and 1/3 on resample) -- the noise-gate discipline catching
single-trial phantoms yet again.

This is a genuinely distinct failure class from anything the narrative
province can measure, and distinct from "can't plan": call it
**deep-plan identifier-binding collapse**. Nano's earlier name-for-id
failure was the shallow version (step 1 of a 1-step plan); Ultra survives
shallow binding but degrades at depth 8-9. Discriminates by DEPTH x
CONFUSABILITY, not by tier alone.

**Provenance bug found during verification (fixed 2026-09-21, see entry above):**
`ToolBreakingPointEvaluator` records only the scalar dims in
`latent_params`; the five trap 0/1 dims live only inside the
`verifier_detail` string (`traps=...`). World reconstruction from a
record therefore FAILS silently if you use `latent_params` alone -- my
first resample round verified against un-mutated worlds and produced 15
invalid verdicts (all spurious unknown_tool; caught because the failure
codes didn't match the originals, ~$0.15 of Ultra calls wasted, redone
correctly). Trap dims belong in `latent_params`/`realized_dims` proper
so the standard reconstruction path works. Flagged for fix, not patched
here.

---

## 2026-09-20 — FINDING (confirmed) — adversarial selection alone cannot break tool-planning: the generator has no traps to select

Follow-up to the tool-planning saturation diagnosis (Nano 39/40, Super
40/40). Ran the free probe first: `run_tool_planning_campaign` with
`adversarial=True` (foil-driven seed search ON) on both Super
(`camp-tool_planning-adv-super-001`) and Ultra
(`camp-tool_planning-adv-ultra-001`), 40 trials each, sequential (not
parallel -- see the rate-limit diagnosis entry).

**Result: 40/40 PASS on both. $0.0434 total.** Adversarial selection
moved nothing -- in sharp contrast to the narrative province, where
switching on foil-driven selection broke Ultra at chain_length=2.

**Why this is expected and what it proves:** seed search can only pick
the most trap-dense world the generator can PRODUCE. The current
toolworld generator produces no real traps: decoys self-identify ("Has
no effect on deployment state"), every precondition is spelled out in
the tool's own description, and every world is greedy-solvable (no
locally-valid call is ever globally wrong). Selection over trap-free
worlds is selection over nothing. This isolates the bottleneck to the
GENERATOR's structure, not the selection machinery -- the exact
motivating evidence for the trap-mutation build now in progress
(toolworld/mutate.py: poison_ladder, dishonest_decoys,
implicit_preconditions, zero_slack, id_collision, interleaved_goals).

---

## 2026-09-20 — FEATURE (shipped) — the Evidence Analyst: Nemotron writes the diagnosis, a deterministic audit keeps it honest

**What we built:** `cartographer/analyst/` — the Phase 9 centerpiece that
turns the model from a test subject into a worker. Three layers under one
credibility rule: *all numbers come from the deterministic store; the LLM
only narrates and interprets; the literature layer only corroborates.*

1. **Evidence pack** (`evidence.py`) — a JSON-able digest computed straight
   from the store, reusing `viewer/findings.derive` so per-model /
   per-kind pass rates, weak regions, trap fire rates, verification stats,
   spend/trials, and the curated findings all obey the SAME
   scored-only/noise-excluded denominator as every other surface (no new
   arithmetic). Curated findings carry `evidence_resolves` so a diagnosis
   can't lean on run_ids the 2026-09-19 incident destroyed.
2. **Analyst call + audit** (`analyst.py`) — prompts the analyst model
   (Nemotron Super by default) with strict "cite only pack numbers"
   instructions, parses the reply into sections (robust to fences /
   preamble). Then the signature move: `audit_diagnosis` extracts every
   numeric token the model wrote (percents / counts / dollars), diffs each
   against the pack at the pack's own rounding precision (0.5pt on
   percents, exact on counts), and returns every unsupported figure as an
   "unverified figure" flag. The narrator can say anything; only the
   store's numbers are trusted.
3. **Tavily corroboration** (`corroborate.py`) — for each confirmed /
   needs-replication finding, forms focused literature queries, searches
   Tavily (stdlib urllib), and has the analyst model classify the relation
   (corroborates / contradicts / no_clear_signal) with snippets retained
   for human checking. NO key → whole layer degrades to "literature check
   not run (no Tavily key)". Responses cached to scratch JSON so re-renders
   don't re-bill.

**Surfaces:** `/report` viewer page (diagnosis, PROMINENT audit flags,
linked corroborations, provenance footer, empty state) under the RESULTS
nav group; `scripts/write_report.py` writes `docs/capability-report.{md,json}`.

**Safety:** every store access is `ResultsStore.open_read_only()` (new
additive `mode=ro` URI opener; append disabled on a read-only store) —
the live gitignored `campaign.sqlite` is never write-locked or schema-
altered by a reader. `keys.py` additively loads a Tavily key; existing key
tests untouched.

**Live run (authorized, READ-ONLY, one analyst call, hard-capped $0.20):**
diagnosed 3 models. Executive summary opened: *"The evidence shows a clear
performance tier: the Nano model achieves a very high overall pass rate
(94.1%)… the Super and GPT-OSS models sit in a lower tier (overall pass
rates 63.4% and 58.6%)… belief pass rates dropping to roughly 60% (Super)
and 53% (GPT-OSS)."* The audit flagged **2 unverified figures** — both the
model's own soft-hedge phrasing ("low-80% range", "mid-90% range") that
maps to no exact pack value, caught deterministically and rendered as
flags rather than passing silently. No Tavily key present, so the degrade
path is what actually rendered (as expected — the user hasn't signed up).

**Tests:** +47, zero network (fake client + mocked Tavily transport):
evidence-pack rates match SQL by hand, audit catches a planted invented
number AND passes a faithful diagnosis, rounding tolerance, Tavily parse /
classify / degrade / cache-hit, read-only store refuses append, `/report`
renders all sections + empty state, keys extension additive.

---

## 2026-09-20 — FINDING (retraction risk) — "Ultra abstention under trap pressure" replication attempt found a token-budget truncation artifact instead

User asked to replicate the "needs-replication" abstention finding
(`docs/lab-log.md`, 2026-09-17: Ultra sometimes answers "unknown" under
adversarial trap pressure instead of committing to a wrong-but-plausible
foil, 2 stable cases observed, never replicated). The original finding's
run_ids were destroyed in the 2026-09-19/20 data-loss incident, so the
exact original cases can't be re-tested directly. Instead: ran a fresh
`full_map` campaign on Ultra (`camp-full_map-ultra-002`, $1.0014, 426
records) and its `adversarial_belief` stage surfaced one fresh candidate
carrying the SAME trap signature as the original finding
(`final_true_state,last_mentioned_location,last_witnessed_by_anyone`),
record with answer `'unknown'` against answer_key `'the photo studio'`.

**Auto-verify's 3 resamples (default 8000/16000 "ultra"-class token
budget) all hit `finish_reason=length` and correctly routed to ERROR, not
FAIL** — inconclusive by infrastructure, not by capability. Rather than
report that as evidence either way, resampled the exact same stored
prompt 5x more directly at `max_tokens=28000`. Every one of the 5
completed cleanly (`finish_reason=stop`, 10k-16k tokens used — genuinely
needing more than the "ultra" ceiling, not stalling):

- 2/5 correct ("photo studio")
- 3/5 wrong, but NOT abstaining — every wrong answer was `'archive room'`,
  a specific, confidently-stated foil location
- 0/5 answered "unknown"

**Conclusion: this candidate does not replicate as abstention.** At
adequate token budget, Ultra never abstains on this world — it either
gets it right or commits confidently to a specific wrong foil, which is
the ALREADY-confirmed belief-to-reality/foil-capture failure mode, not a
distinct abstention mode. The apparent "unknown" in both the original
campaign trial and the auto-verify resamples was very plausibly a
truncation artifact: the naive answer-extraction logic reads the last
line of a length-capped response, and a reasoning chain cut off
mid-deliberation can spuriously contain or resemble "unknown" without the
model having actually concluded that.

**This casts real doubt on the original 2-case finding too** — it used
the same 8000/16000 budget infrastructure, and the mechanism just
demonstrated here (truncation masquerading as abstention) is a live,
confirmed alternative explanation. The original cases can't be re-tested
(evidence destroyed), so this can't be fully confirmed OR refuted — but
"abstention under trap pressure" should not be presented as a standing
capability finding without this caveat. Recommending status change from
`needs-replication` to `retracted` (or at minimum a heavy caveat) in
`cartographer/viewer/findings.py`'s curated set, pending a decision.

**Actionable infra finding:** the highest-trap-density adversarial belief
worlds on Ultra can need reasoning chains beyond the current 16000-token
retry ceiling; 28000 resolved every case tried here. `default_token_budget`
in `cartographer/exec/evaluator.py` may be worth raising for adversarial
campaigns specifically (regular belief/state/order trials haven't shown
this need). Not yet changed — flagging for a decision, since raising the
ceiling raises per-trial cost too.

Minor tooling note: `run_full_map_campaign`'s returned `FullMapResult`
stage summaries (`n_pass`/`n_fail`/`n_error`) do not sum to `n_records`
for stages with verify children (observed on this exact run: belief stage
reported n_records=130 but n_pass+n_fail+n_error=27) — the printed
summary is not reliable; direct-store SQL was used for every number in
this entry instead. Worth a fix, not chased here.

---

## 2026-09-20 — FINDING (confirmed) — trap-pressure abstention replicates on Super, resample-verified; the phenomenon is real, just mis-attributed to the wrong tier

Continuing the abstention-replication work above: Super's fresh `full_map`
run (`camp-full_map-super-002`, $0.1731, 495 records) turned up 8 FAILs on
`adversarial_belief`, 7 of them answering `'unknown'`, ALL under the exact
same trap combination
(`final_true_state,last_mentioned_location,last_witnessed_by_anyone`) that
produced both the original Ultra claim and the Ultra candidate just
debunked above as a token-truncation artifact. Given that debunking,
these were not taken at face value -- checked `finish_reason` first
(all `stop`, 500-1900 tokens, nowhere near Super's 4000-token budget --
not a truncation case), then ran the same resample-stability classification
this project applies everywhere:

| original record | resamples (3x) | pass rate | verdict |
|---|---|---|---|
| 6186c0cf0863 (unknown) | unknown, unknown, unknown | 0.00 | **STABLE** |
| fefd0c534495 (unknown) | unknown, unknown, unknown | 0.00 | **STABLE** |
| 31e06e4beb39 (unknown) | unknown, unknown, unknown | 0.00 | **STABLE** |
| ed9d3a74ae48 (unknown) | east wing, unknown, unknown | 0.33 | **STABLE** |
| 3e9839b5185c (loading dock -- not abstention) | FAIL, FAIL, FAIL | 0.00 | STABLE, but a foil-capture case, not abstention |
| 132bf126e7e0 (unknown) | unknown, PASS, PASS | 0.67 | NOISE |
| e835c376654a (unknown) | PASS, PASS, PASS | 1.00 | NOISE |
| 1c28851f8c76 (unknown) | PASS, PASS, PASS | 1.00 | NOISE |

**3 of 7 apparent abstentions were noise** (majority or unanimous PASS on
resample) -- exactly the Priya-case pattern this whole project's resample
discipline exists to catch. But **4 survive**, 3 of them answering
`'unknown'` on literally every one of 4 independent draws (original + 3
resamples), the 4th on 3 of 4. Verified one case's ground truth directly:
prompt reproduces byte-identical from the stored seed, answer_key = `'the
workshop'` -- a normal, fully determinable belief, not an edge case where
abstaining could be defended as correct.

**Conclusion:** trap-pressure abstention is real -- it just wasn't Ultra's
phenomenon. The original 2026-09-17 finding attributed it to Ultra using
infrastructure (8000/16000 token budget) that this session showed can
mask truncation as abstention; today's Ultra candidate evaporated under
that exact mechanism. Super, on a normal (non-"ultra") 1500/4000 budget
with no truncation risk, shows the pattern cleanly and reproducibly. This
is the SAME failure mode the earlier session named ("belief-attribution
abstention under trap pressure, distinct from belief-to-reality collapse
and pronoun bias") -- just correctly attributed now, and with a much
stronger evidence base (4 stable cases, resample-verified, vs. the
original's 2 unreplicated ones).

**Action:** `cartographer/viewer/findings.py`'s curated "Ultra abstention
under trap pressure" entry updated to attribute the confirmed mechanism to
Super instead, with the Ultra investigation folded in as a documented
false lead and the reasoning (token-truncation masking) kept visible
rather than deleted -- the retraction is itself part of the finding's
credibility.

---

## 2026-09-20 — FINDING (confirmed) — the full_map rebuild: three-tier summary, and token-cap exhaustion confirmed on a third tier

User-approved rebuild after the data-loss incident: `full_map` run on
all three Nemotron tiers, $1.31 total spend, 1572 real trials written to
the live store.

| tier | records | spend | state | order | belief | adversarial_belief | breaking_point |
|---|---|---|---|---|---|---|---|
| Nano | 651 | $0.1377 | 100% (30/30) | 97.5% (78/80) | 89.2% (166/186 scored) | 85.6% (137/160) | 91.3% (42/46 scored) |
| Super | 495 | $0.1731 | 93.3% (28/30) | 98.75% (79/80) | 98.5% (128/130) | 95.0% (152/160) | 96.4% (54/56) |
| Ultra | 426 | $1.0014 | 100% (30/30) | 100% (80/80) | 97.7% (127/130 scored) | 97.7% (127/130 scored) | 98.2% (54/55 scored) |

Clean tier ordering on belief/adversarial_belief (Ultra > Super > Nano),
consistent with every prior campaign. Super's state result (28/30, 2
FAILs) is the first time state tracking has failed at all on ANY tier in
this project's history -- worth a look in its own right, not investigated
here (not the ask this session).

**Nano's breaking_point stage hit 10/56 (~18%) ERROR, all
`cap_hit=1;retry_exhausted=1`, `finish_reason=length`** -- the escalation
ray pushes every dim toward the generator ceiling (long chains, high
scramble), and at that difficulty even Nano's reasoning can exceed the
default 1500/4000 token budget. Correctly routed to ERROR, not
mis-scored as FAIL -- the 2026-09-17 fix holding as designed. This is
the SAME mechanism just characterized on Ultra earlier today (see the
retraction entry above), now confirmed on a non-"ultra" tier too: token
exhaustion under escalation/adversarial difficulty is not "ultra"-model-
specific, it's a property of the difficulty regime. `default_token_budget`
in `cartographer/exec/evaluator.py` likely needs a distinct, higher floor
for `breaking_point` and `adversarial_belief` campaigns specifically
(regular state/order/belief trials have not shown this need on any
tier) -- flagged for a decision, not changed here.

---

## 2026-09-20 — DECISION (decided) — tool_planning stays a standalone campaign, not a full_map stage

User call, with reasoning: tool-call planning measures a genuinely
different province (goal-directed action sequencing under preconditions)
from the five existing `full_map` stages (all narrative
state/order/belief tracking over one world-model generator). Bundling it
into `full_map` would blur "what capability did this number measure" the
moment a judge or a future reader looks at a profile report -- a single
aggregate pass rate spanning two unrelated provinces is less useful than
two clearly separated ones. `tool_planning` stays a standalone `/launch`
preset (already wired: `cartographer/exec/evaluator.py`'s
`run_tool_planning_campaign`, `kind=tool_plan` picked up automatically by
`/findings`' capability grid and `/atlas`). Revisit if/when a second
tool-planning-flavored family exists and a real "full battery" concept
spans provinces cleanly.

---

## 2026-09-20 — BUG (fixed... partially) — the three "parallel" full_map campaigns took ~an hour instead of minutes: shared-account rate-limit contention, not a parallelism failure

User noticed the three tier-rebuild campaigns (Nano/Super/Ultra,
2026-09-20 -- see the "full_map rebuild" entry above) took far longer in
total than expected for genuinely parallel execution and asked for a
diagnosis.

**They DID launch in true OS-level parallel** -- three separate `python3`
processes, each with its own `Client`/`ResultsStore` connection, confirmed
by their start timestamps landing within 17 seconds of each other
(18:28:19 / 18:28:30 / 18:28:36 UTC). That part worked as designed.

**But wall-clock duration was wildly uneven and none of the three
finished in the "minutes" a truly independent parallel run should take:**

| campaign | records | duration | gaps >15s (count / total secs) | max single gap |
|---|---|---|---|---|
| Ultra | 432 | ~10.5 min | 4 / 81.6s | 20.5s |
| Super | 495 | ~22 min | 28 / 539.7s | 24.8s |
| Nano | 651 | ~82.5 min | 94 / 3172.6s | 124.7s |

Nano alone spent ~53 of its ~82.5 minutes (roughly two-thirds) sitting in
gaps between record writes far larger than any observed single-call
latency (avg call latency across all three campaigns: 3.7-9.4s) --
the unmistakable signature of exponential-backoff retry delay on 429s,
not generation time.

**Root cause:** `cartographer.exec.client.NEBIUS` has no explicit
`suggested_parallelism` override, so it uses the `Provider` dataclass
default of **6** -- a figure calibrated assuming ONE campaign runs against
the account at a time, which is exactly what `/launch`'s module-level
one-campaign-at-a-time guard (`cartographer/viewer/launch.py`) exists to
enforce. This rebuild was launched by invoking
`run_full_map_campaign`/`run_tool_planning_campaign` directly via script,
bypassing that guard entirely (deliberately, for wall-clock speed) -- so
three campaigns each independently issuing up to 6 concurrent requests
put up to **18 concurrent requests** against one Nebius account's shared
rate-limit pool. `RetryPolicy`'s exponential backoff (honoring
`Retry-After`) absorbed the resulting 429s correctly -- no record was
corrupted or mis-scored -- but the aggregate throughput was capped by the
shared ceiling, not additive per-campaign capacity, so "parallel" bought
far less speedup than the process-level concurrency alone would suggest.

**Why Nano specifically absorbed the most delay:** it simply issued the
most total requests of the three (651 records vs. 495 and 432,
compounded by ~14+ length-cap retry round-trips from the
`cap_hit=1;retry_exhausted=1` errors already logged in the full_map-rebuild
entry above) -- more requests in the same contested window meant more
collisions with the shared throttle, not a Nano-specific rate limit.

**Not fixed here** (diagnosis only, per the ask): the real fix is either
(a) never bypass `/launch`'s one-at-a-time guard for genuinely concurrent
multi-model campaigns -- accept sequential wall-clock time as the safe
default, or (b) if concurrent cross-model campaigns are wanted regularly,
add a *shared* rate limiter/semaphore across `Client` instances hitting
the same provider+account, since per-campaign `suggested_parallelism` has
no visibility into sibling campaigns. Flagging for a decision rather than
changing unilaterally, since (b) is a real architecture change to the
execution layer.

---

## 2026-09-19 — DESIGN (pass 6) — the pass-5 composition system applied to the remaining eight pages

Pass 5 proved the composition fix on the overview only and closed with a
note that the other eight pages still read as stacked documents. This
entry closes that: `/runs`, `/run/<id>`, `/record/<id>`, `/live`,
`/atlas`, `/findings`, `/coverage`, `/launch` now all use the same four
primitives (`.page-head`, `.stat-grid` KPI row, `.grid-2`, `.card` +
`.card-head`). No new primitives were invented; four small additions were
needed to cover cases the overview didn't have:

- `.page-head-main` / `.page-head-actions` — the overview's page-head
  only had to hold ONE action. A run detail page has three (watch live,
  compare across models, verify N failures), and /run and /record put a
  title PILL beside the title, so each half of the header row needed its
  own flex wrapper.
- `.card-title-name` — uppercase 10px mono is right for "PROMPT" /
  "ALL RUNS"; it is unreadable for a finding's actual claim
  ("Belief-to-reality collapse (witness-rendering bug, corrected)").
  Same header bar, sentence-case sans title.
- `.card-gap` — /coverage's "not yet covered" roadmap and /findings'
  retractions are a different KIND of content from the capability cards
  around them (honest gaps and withdrawn claims, not features). Dashed
  edge, transparent fill. Rendering them identically to the capability
  cards flattens self-skepticism into just another feature list.
- Containment rules for content that carries its own enclosure:
  `.card-body > details > table` goes flat (the card is already the
  border), `.card-body pre.block` loses its margin, `.card-body .card`
  sits on surface-2, and preset radio-cards get a raised surface, a hover
  state and an inset checked ring so they read as selectable inside a
  card body.

**A real bug the KPI row surfaced.** Putting "pass rate" in /run's hero
row exposed that total-trials is the wrong denominator: on
`camp-adversarial_belief-minimax-m3-001` (115 of 140 trials ERRORed on
infra) a naive `n_pass / n_trials` renders **16%** directly above a
verdict panel headline saying **92% pass rate**. The KPI now mirrors
`report/verdict.py`'s `_verified_only` + `_pass_rate` exactly — scored
trials only (PASS+FAIL, minus failures already classified as noise) — and
carries the error count as the Trials box's inline delta. Measured after
the fix: minimax `92%` / `115 errors` / `23/25 scored` against panel
"92% pass rate"; `camp-belief-nemotron-super-001` `47%` / `123/264
scored` against panel "47%".

**Measured rendered, not asserted from the markup.** A parser walked
`<main>`'s direct-child sequence on every page against the real store
(1,959 records, real run_ids and record_ids). Per page: page-head is the
first child of `<main>` (index 1 where a breadcrumb `.page-strip`
legitimately precedes it, on /run and /record); the KPI row is the child
immediately after it and above the first card on every page that has one;
**zero** `h2`-then-table patterns anywhere (the defining pattern of the
rejected passes); **zero** tables, `<pre>` blocks or bare lists left as
direct children of `<main>`. Cards/grid-2 per page: overview 3/1, runs
1/0, run 2/0, record 5/1, live 2/0, atlas 20/3, findings 8/0, coverage
5/2, launch 2/0 — and card-head, card-title and card-meta counts equal
the card count on every page (no card shipped without a real header bar).
Every page 200; every page's tag tree balanced with nothing unclosed at
EOF; verdict-panel-before-first-table ordering re-confirmed on three real
runs including a full_map parent.

The three surviving `h2`s (atlas's per-query-kind group headings, the
verdict panel's own headline, the overview's "Where to go") are real
hierarchy sitting above a GRID of cards, not the rejected
title-floating-above-one-block pattern.

320/320 green.

---

## 2026-09-19 — DESIGN (pass 5) — composition, not surfaces: the actual "doesn't look like SaaS" cause

User on pass 4: "it just changed the font and font color no structural
changes." Correct, and the sharpest note of the four passes. Passes 3-4
fixed SURFACES (sidebar, tokens, card borders, density, typeface) but
never touched COMPOSITION. Every page was still a single stacked column:
h1 -> panel -> h2 -> table -> h2 -> list -> h2 -> grid.

No reference composes that way. Measured against them, the overview had
three specific faults:
- **KPI stats buried mid-page.** admin-dashboard and Numra both OPEN
  with a KPI row; ours sat below the models table, and carried only 2
  stats where references carry 4.
- **Nothing two-column.** Every reference pairs a list beside a chart
  (Services|Requests, Onboarding Funnel|Active Signals). Ours was 100%
  full-width stacked, which reads as a document regardless of how good
  the borders are.
- **Section titles as h2 ABOVE content** instead of inside a card header
  bar (admin-dashboard's "Tenants", test-factory's "REQUEST"/
  "ASSERTIONS" -- title left, contextual meta right, hairline beneath).

Fixed on the overview: `.page-head` (title left / primary action right,
per test-factory's top-right "Run Test"), a 4-up KPI row moved to the
top (added Models mapped + Overall pass rate), a real `.grid-2` two-column
row pairing "What we've found" against "What recent runs revealed", and
the models table moved inside a `.card` with its title in the header bar
and a live "N models" meta on the right.

Verified rendered at 1400px: 4 KPI cards at y=167 (top of content), two
482px columns, CTA at x=1110/y=32, 3 card header bars, no dead gutter.
320/320 green.

Remaining pages (/runs, /run, /record, /live, /atlas, /findings,
/coverage, /launch) still need the same primitives applied -- the
primitives now exist and are proven on the overview.

---

## 2026-09-19 — DESIGN (pass 4, by Fable directly) — the three measured defects behind "still doesn't look like a SaaS app"

Pass 3 delivered the sidebar, the claim-strip signature, and a
domain-derived token system, and the user's verdict was "better now but
not looking like a SaaS." Rather than re-reading the design skill a
fourth time, this pass MEASURED the rendered DOM against the reference
screenshots. Three concrete defects, none of them taste:

1. **Content had no container.** `document.querySelector('table')
   .parentElement` was `MAIN.shell-main` -- the data table sat directly
   on the page canvas. In every reference (test-factory's REQUEST /
   RESPONSE / ASSERTIONS, admin-dashboard's Services / Requests /
   Tenants, bug-tracker's list) nothing ever floats on the canvas; each
   block is enclosed in a bordered card. This enclosure is most of what
   separates "application" from "styled document," and it was simply
   absent. Fixed by making every table self-enclosing (own surface,
   border, radius) rather than hand-wrapping 11 emission sites across 3
   modules and churning their markup assertions; `.card` + `.card-head`
   exist for the cases that want an explicit label-left/meta-right
   header bar.

2. **Density was tighter than any reference.** Pass 3 explicitly chose
   "workbench-tight, not SaaS-airy" and landed at 0.44rem cell padding
   (34px rows) and 13.6px card padding. Measured, every reference sits
   at ~44-52px rows and ~20-24px card padding. The intent was
   "precision instrument"; the result read as cramped. Spacing scale
   moved onto a clean 4px grid, cells to 0.7rem/1rem (42px rows),
   cards to 20px.

3. **Default system typeface.** `-apple-system, BlinkMacSystemFont...`
   -- named explicitly in the design skill's own Avoid list ("default
   typography ... where a direction was set"). Now loads Manrope +
   JetBrains Mono, the exact pairing the reference site itself ships,
   with the full system stack retained as fallback so an offline run
   degrades rather than breaks.

Verified in a real browser after the change: Manrope resolves
(`document.fonts.check` true), tables carry their own surface
(rgb(19,20,21)) and border, rows are 42px, card padding 20px, sidebar
248px + main 887px at a 1150px viewport with no dead gutter.

**One self-inflicted regression, caught and fixed:** the new CSS comment
contained a literal table tag, and
`test_run_detail_page_contains_verdict_panel_marker` does a naive
`page.index()` over the whole document including the inline style block
-- so it found that comment before the real markup and failed. Confirmed
against a stash that the test passed pre-change (i.e. mine to fix, not a
pre-existing flake) and reworded the comment rather than weakening the
assertion. 320/320 green.

---

## 2026-09-19 — INCIDENT (fixed) — ~1,180 real trial records destroyed by a git operation; results/campaign.sqlite untracked (commit c954e28)

`results/campaign.sqlite` was a *tracked* git file that every campaign
appends to while running. It went from 1,959 records back to the 779
last committed on 2026-09-17 between two points in this session where
nothing should have touched it. Root cause: a mutable, live-written
database was under version control at all, and *any* `git checkout`,
`git restore`, `git stash`, or `git reset` on the repo silently reverts
it to whatever was last committed — which is exactly what a `git
stash`/`stash pop` (used mid-session to check whether a failing test was
a real regression) did to it.

**Lost, unrecoverably:** the `camp-full_map-*` run (366 trials — the run
the one-button `full_map` campaign was built to produce), the
`camp-adversarial_belief-*` runs including the Ultra run behind the
abstention-under-trap-pressure finding, `propose-super-vs-nano-001` (the
live Nemotron-proposer-loop result), every `/verify` resample trial, and
the MiniMax-M3/GLM runs. Roughly **$0.84** of real API spend. Checked
before declaring it unrecoverable: `git stash list` (empty), `git
reflog` (no dangling commit holds it — the working file was reverted in
place, never committed over), every `.sqlite` under `/tmp` and this
repo's temp dirs (none carry the lost run_ids), and the working file's
raw bytes (byte-identical to the committed blob, zero string traces of
the lost run names — overwritten, not merely deleted).

**What survives:** every finding's write-up in this log (numbers,
run_ids, conclusions) — only the underlying trial rows and their
evidence links are gone. `docs/findings` citations to the destroyed
run_ids are honest about it now (see the entries below): a citation
whose records are gone renders as "evidence no longer in store" instead
of a dead link pretending to work.

**Fix:** `results/*.sqlite{,-wal,-shm}` added to `.gitignore`;
`git rm --cached` on `campaign.sqlite` and `pilot.sqlite`. The last
committed snapshot (779 records) stays in git history as a fallback; the
live file now sits entirely outside git's reach, with a dated local copy
(`results/campaign.sqlite.bak-20260919`) as a second line of defense.
Every subagent prompt touching this repo since has carried an explicit
"never run git checkout/restore/stash/clean/reset --hard here" rule.

**Process gap, not just a tooling gap:** I own this regardless of which
exact command triggered it — I ran `git stash` against a repo I knew had
a live database in it, without first checking whether that file was
tracked. The `.gitignore` fix prevents recurrence; it doesn't undo the
loss.

---

## 2026-09-19 — BUILD (confirmed) — viewer: third redesign pass — domain-derived signature system, then a sidebar/showcase-evidence correction, same session

**Why a third pass.** Two earlier redesign passes on the same day had each
reskinned the SAME inherited page skeleton (a research-cited type
scale/spacing/pill system, then a nav-IA reorganization) without either
touching visual identity in a way that stopped reading as generic. The
user's verbatim reaction to pass 2: "It looks like AI Slop and the UX
sucks... search online for some of the best UI skills." They pointed at a
specific design methodology (github.com/Dammyjay93/interface-design)
whose own thesis names the failure directly: generic research into "what
good dashboards look like" (Nielsen, Linear, Grafana — pass 2's actual
citations) produces the median output any AI would generate from the same
prompt, which is still slop by the skill's own definition even when
competently executed.

**What this pass did differently.** Domain exploration FIRST, from this
project's own source (`cartographer/world/foils.py`, `cartographer/
report/verdict.py`, `cartographer/confirm/confirmation.py`, `cartographer/
schedule/escalate.py`, `cartographer/ablate/attribute.py`) rather than
generic UI research — mined real vocabulary (witness/belief/foil/
salience/verdict/stable-vs-noise/boundary/ablation/fresh-seed) and wrote
it up as this product's actual design brief in `docs/design-notes.md`
(replacing the prior research-citations version) BEFORE writing any CSS.
Full brief — Intent / Domain vocabulary / Color world / Signature /
Rejecting / Direction — is in that file verbatim; summary:

- **Color world**: achromatic ground truth (simulated fact is never
  tinted), amber/phosphor for belief-query CLAIMS (provisional, can be
  wrong in a specific way), a trap-sprung FAIL split from an infra ERROR
  by hue (one's a finding, one's noise — the single highest-value color
  decision), a real salience color ramp.
- **Signature**: the "claim strip" — `[mark] VERDICT  evidence clause
  (trap names, salience swatch)  stability tag` — replacing the old
  dot-plus-label pill everywhere a trial verdict renders (`/run/<id>`'s
  per-trial table, `/live`'s recent-records feed, `/record/<id>`'s
  verdict block). Degrades gracefully to bare mark+word when there's no
  evidence to show (state/order records). Two other candidates
  (a literal chain-of-custody diagram, a bigger always-on salience heat
  strip) were considered and rejected in the design-notes writeup with
  reasons, not just listed.
- **Token system rebuilt from scratch**, not recolored: `--verdict-fail-
  trap` / `--verdict-error-infra` / `--salience-low/mid/high` / `--claim-
  belief` / `--ground-truth` / `--surface-instrument-0/1/2` replace
  `--accent` / `--pass` / `--fail` / `--error` / `--bg` / `--surface-1/2`.

**Real bug found by actually rendering the signature component against
`results/campaign.sqlite` before building the rest of the app** (the
task's own highest-risk-first process step, not skipped): the claim
strip's evidence clause naively sliced the first N characters of
`response_text` to show what a model answered. `MiniMaxAI/MiniMax-M3`'s
adversarial_belief records write their FULL chain-of-thought into that
field with the stated answer only at the very end ("Looking at this
problem, I need to track what Hakim has p..." instead of "**Answer:
lobby**") — a real, misleading defect, not a cosmetic one. Fixed with
`_short_answer()`: take the LAST non-empty line (mirrors `verdict.py::
_notable_failures`'s own existing pattern for extracting a QUESTION from
a multi-line prompt), which is a no-op for terse single-line answers and
correctly lands on the stated answer for verbose ones. Applied at all
four `response_text`-rendering call sites in `cartographer/viewer/
serve.py` (this pass's territory) — `verdict.py`'s own `NotableFailure.
model_answer` field itself was NOT touched (`cartographer/report/**` is
off-limits territory this pass), the fix is a display-time transform
only where this pass renders that field.

**Mid-pass correction 1 — top nav bar was itself never questioned.**
After the signature/token work landed, direct feedback backed by the
interface-design skill's own showcase gallery (Synchro, Numra Analytics,
Apex Capital — all left-sidebar layouts): "no saas has it like that
except for some decades-old government websites." Correct: all three
passes today, including this one, had inherited a horizontal
`header`+`nav` top bar and only ever reskinned its tokens, never asked
whether it was the right CONTAINER for 7 nav items grouped into 3
sections. Restructured `_shell()` to a fixed 248px left sidebar (brand
identity block + live trial counter at top, the same `_NAV_GROUPS` data
grouped as vertical rows below) beside a `<main>` content area, with
global nav and page-level context (breadcrumb) now in two visually
distinct regions instead of stacked in one header row.

**Mid-pass correction 2 — verify against actual shipped pixels, not a
description of them.** Further feedback: "it's not just the left side
menu bar.. IT's everything." The user pulled full-resolution preview PNGs
from the interface-design skill's OWN gallery and extracted concrete
facts this pass's generic instincts had gotten wrong on their own:
primary buttons are neutral inverted-contrast (light fill/dark text), not
accent-filled — this pass had every button amber-filled, which was
exactly the "decorative accent" failure mode design-notes.md's Rejecting
section had already argued against once, just recommitted with a
different hex value; a verdict renders as a small pill directly beside
the h1, not only inside a big card below; an "assertions" pattern
(expected/actual side by side, only the mismatch colored) is a near-exact
structural fit for this project's own "what entity X believed" vs "what
the model answered" and was applied to the verdict panel's notable-
failures list (this is also where the `_short_answer` bug above resurfaced
a second time, via `verdict.py`'s full-length `model_answer` field, and
got the same fix applied at the display layer); hero KPI stat cards (big
bold number, muted label, near-invisible border); focal-point coloring
(color the ONE thing that's the point) — adapted, not copied verbatim,
for `/atlas`'s multi-model comparison chart: a model/dimension line
renders neutral by default and switches to amber exactly when a REAL
fitted boundary was found for it, verified post-fix against real data
(43 neutral lines vs. 15 amber, amber clustering on `belief`-kind charts
where this project's actual findings live, `state`-kind charts rendering
entirely neutral since they're uniformly clean per this log). Full
addendum with per-item before/after reasoning is in `docs/design-notes.md`
("Addendum: two mid-pass structural corrections").

**Verification.** `/tmp/mc_venv/bin/python -m pytest -q` — 320/320 green
throughout every step of this pass (signature build, sidebar restructure,
showcase corrections) — no markup-structure test broke because content
tests assert on plain-English substrings (`_NAV_LABELS`, `data-testid=
"verdict-panel"`, literal preset-card strings) rather than DOM shape.
Served `results/campaign.sqlite` on a live port and curled all 9 routes
(`/`, `/findings`, `/coverage`, `/runs`, `/live`, `/atlas`, `/launch`, one
real `/run/<id>`, one real `/record/<id>`) after the FINAL state — all
200, every `_NAV_LABELS` string and the verdict-panel testid confirmed
present via grep on the raw curl output, not just pytest. Also visually
inspected every page live via the preview tool against real adversarial-
belief data with actual trap names, salience scores, and verified-stable/
unverified FAILs rendering correctly end-to-end, not just in isolated
component mockups.

Files: `cartographer/viewer/serve.py`, `cartographer/viewer/atlas.py`,
`cartographer/viewer/findings.py`, `docs/design-notes.md`.

---

## 2026-09-19 — BUG (fixed) — /live showed "starting... 0 records" for the entire duration of full_map and verify-now runs

User report, verbatim: "The launch screen is stuck and even though the runs
are finished. it's still showing that some run is there but no way to access
it without going to runs. And verifying also shows the same screen but the
actual process are working. The UI is not."

**Root cause:** `live_html()` looked up records with `store.by_run(run_id)`
-- an EXACT match. Two real campaign shapes never write under the exact
run_id they're registered under as the active run:
- `full_map` campaigns write every trial under child run_ids
  (`f"{run_id}/{stage}"` for state/order/belief/adversarial_belief/
  breaking_point) -- the parent run_id itself never gets a single direct
  record.
- A "Verify failures now" pass writes its resample trials under
  `f"{run_id}/verify"` (or per-stage `f"{run_id}/{stage}/verify"` for a
  full_map parent) -- never under the original run_id, whose own records
  are already complete and static.

So `/live`, watching the exact registered run_id, saw zero matching rows
for the campaign's ENTIRE real duration in both cases -- not a rendering
glitch, a query that structurally could never match. Confirmed against the
live store: `camp-full_map-nemotron-3-super-120b-a12b-001` has 366 real
trial records and 18 verify resamples, all invisible to the old `/live`
query the whole time it ran.

The run detail page (`/run/<id>`) already had the correct fallback logic
(probe `f"{run_id}/{stage}"` children when the exact id has none) -- `/live`
just never used it, which is why "going to runs" worked as a manual
workaround and matches the user's exact description of the bug.

**Fix** (`cartographer/viewer/serve.py`): new `_collect_run_records(store,
run_id)` shared by `/live`, mirroring `run_html`'s aggregation and adding
verify-record collection on top. `/live` now shows the real stage tracker
and tallies for an in-progress full_map run, and a new "Verifying failures"
panel surfaces resample counts (incl. how many flipped to PASS, i.e. were
noise) when a verify-now pass is running against an otherwise-static run.
`busy_html()` (`cartographer/viewer/launch.py`) also now links directly to
`/run/<id>`, not just `/live`, closing the "no way to access it without
going to runs" gap even independent of the live-page fix.

2 new regression tests in `tests/test_viewer.py` reconstruct both exact
scenarios; `test_launch.py`'s existing busy-page test extended to assert
the direct run link. 320/320 passing.

---

## 2026-09-19 — BUILD (confirmed) — viewer: ground-up UX/IA redesign (research-driven)

**What we saw:** direct user feedback after two prior presentation passes
-- the viewer still read as generic dark-dashboard "AI slop," and the
nav/IA "totally sucks." Two earlier passes had added pages and copy but
never done a real design pass on hierarchy, IA, or visual restraint.

**What we did:** research first (Nielsen's 10 heuristics, Shneiderman's
"overview first, zoom/filter, details on demand" mantra, and the
restraint-driven visual language of Linear/Vercel/Grafana/Datadog-class
dev tools), written up as 25 sourced principles in `docs/design-notes.md`
-- then applied them:

- New design system in `cartographer/viewer/serve.py`'s `_CSS`: a real
  4-step type scale, a spacing scale, 3 surface-elevation steps
  (replacing "border around every box"), ONE accent color reserved for
  links/primary-CTA/active-nav, semantic color (pass/fail/error) reserved
  exclusively for verdict pills (now a dot+label pattern, not a solid
  chip), tabular numerals + right-aligned numeric table columns
  throughout.
- Nav regrouped into Results (Overview, Findings, Runs) / Explore (Atlas,
  Coverage) / Act (Launch, Live) instead of a flat list in build order.
- `/` rewritten task-first: what-is-this -> what-did-we-find -> one
  primary CTA ("Map a model" -> `/launch`), above the fold; raw stat
  counts and the quick-links grid demoted below.
- `/run/<id>`: verdict panel promoted above an now-collapsed
  (`<details>`) raw per-trial table; breadcrumb added.
- `/record/<id>`: verdict/provenance panel first, prompt/response/
  reasoning blocks below (reasoning already collapsed pre-existing);
  breadcrumb (`Runs -> run -> record`) and prev/next-trial lateral nav
  added so the page is never a dead end.
- `/live`: added a `full_map` stage tracker (state -> order -> belief ->
  adversarial_belief -> breaking_point) so progress reads at a glance;
  empty state now points at `/launch`.
- `/launch`: presets rendered as radio-cards, `full_map` shown
  prominently with a RECOMMENDED badge, the other five presets collapsed
  under "Advanced"; added a cost/duration expectation line above submit;
  submit copy changed to "Start mapping."
- `/runs`, `/atlas`: empty states now point at `/launch` instead of a
  bare "no data" table; numeric columns right-aligned.
- `/findings`, `/coverage`: cards switched from `.stat-box` to the new
  `.panel` elevation pattern for consistency with the rest of the site.

**Verification:** `/tmp/mc_venv/bin/python -m pytest -q` -- 298/298
green throughout (no markup-structure test broke; content-only tests
already asserted on plain-English substrings, not styling). Also
served `results/campaign.sqlite` locally, curled every route (200 on
`/`, `/findings`, `/coverage`, `/runs`, `/live`, `/atlas`, `/launch`,
one real `/run/<id>`, and one real `/record/<id>`), and checked open/
close tag balance (`div`/`details`/`table`/`form`) on every rendered
page -- all balanced.

**Note:** a concurrent agent made content-only edits to evaluator/
verdict/run-detail copy and to `launch.py` (added a `verify_failures`
checkbox + cost note) during this same window; both edits merged
additively with no conflicts, confirmed by rereading the diff and
rerunning the full suite after the merge.

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

## 2026-09-19 — FEATURE (shipped) — automatic FAIL verification (resample-noise gate)

**What we saw:** the Priya case (Ultra, 4/5 resamples passed) and 3 of 6
adversarial-belief FAILs earlier this project both evaporated on manual
resample — single-trial temp=0 FAILs against big MoE models (Nemotron
Ultra/Super especially) are frequently pure expert-routing noise, not a
real capability gap. `cartographer.ablate.resample.resample_failure`
already did the resample+classify arithmetic but was only ever invoked by
hand. The user's ask: make it automatic for FAILs, never for PASSes (cost
discipline — FAILs are 2–14% of trials), and NOT force a blanket
API-credit spend without an explicit off switch.

**What shipped:**

- `cartographer.exec.evaluator.run_verify_failures(client, model, store,
  run_id, n=3, stable_threshold=0.5, cost_cap_slice, max_verified=30,
  cancel_event)` — re-executes each stored FAIL's EXACT `prompt_text`
  (zero regeneration drift) `n` more times, classifies `stable`
  (pass_rate ≤ threshold, including the original FAIL) vs. `noise`
  (mostly passes on resample). Every resample trial is a NEW append-only
  `ExperimentRecord` under `run_id=f"{run_id}/verify"` with
  `verifier_detail` carrying `verify_of=<original_record_id>;sample=<i>`
  — the original FAIL record is never mutated. Classification is DERIVED
  AT READ TIME (`classify_verified_failure` /
  `classify_all_verified_failures`) by reading that run's verify trials,
  not cached in a separate summary record — cheap enough (small n) that a
  stale cache would be strictly worse, and it means a different
  `stable_threshold` needs no new records. Capped at 30 FAILs verified per
  run (most-recent-first), logged if truncated.

- **Mode, not a blanket default-on bool** — mid-flight requirement change
  from the user: `run_state_campaign` / `run_order_campaign` /
  `run_belief_campaign` / `run_adversarial_belief_campaign` /
  `run_full_map_campaign` all take `verify: str = "auto"` ("manual" spends
  zero verification calls, recording FAILs as usual). "auto" reserves 15%
  of the campaign's own `cost_cap` for verification (`_VERIFY_COST_CAP_SHARE`
  in `evaluator.py`) so total spend (main loop + verification) can never
  exceed the caller's original cap. `breaking_point` is deliberately never
  auto-verified (it's driven by `find_breaking_point`, not `run_campaign`
  — wiring it in would mean re-deriving "which escalation-ray trials are
  FAILs" outside the `CampaignState` idiom this helper relies on; skipped
  with a note rather than contorted in). For `full_map`, verification runs
  PER STAGE immediately after that stage's own main loop, inside that
  stage's own cost-cap slice (not once at the end across the whole
  battery) — simpler to reason about per-stage and keeps each stage's
  spend self-contained.

- **/launch**: a "Verify failures" checkbox (default checked) with the
  cost note "re-tests each failure 3x — typically adds a few percent to
  cost; uncheck to skip and verify later by hand". Standard HTML checkbox
  semantics: checked → `verify="auto"`, unchecked (simply absent from the
  POST body, as real browsers do) → `verify="manual"`.

- **Manual path is first-class**: a run's detail page (`/run/<id>`) shows
  each FAIL row's status (`verified-stable` / `noise` / `unverified`) and,
  if any FAILs are unverified, a "Verify N failures now" action (provider
  + key + model form → POST `/verify`) that reuses the EXACT SAME
  one-campaign-at-a-time thread/cancel_event machinery as `/launch`
  (`cartographer.viewer.launch.handle_verify_now` / `_verify_now_thread`).
  `scripts/verify_past_failures.py --live --run <run_id>` calls
  `run_verify_failures` directly — same underlying call, no second
  implementation. `--dry-run` (also the default with no `--live`) lists
  every run with unverified FAILs and an estimated call/cost, zero network
  calls.

- **Verdicts** (`cartographer.report.verdict`): a run's pass_rate,
  dim-region rates, and belief-collapse classification now all read
  `_verified_only()` — noise-classified FAILs are excluded from findings
  entirely, `notable_failures` never surfaces one. Exact copy for a mixed
  9-FAIL run (6 stable, 3 noise): *"9 failures recorded; 6 survived
  verification (3 were sampling noise and are excluded from findings)."*
  A run with zero verification data (old runs, or `verify="manual"` never
  followed up): *"N failure(s) recorded; failures not yet verified — use
  \"Verify failures\" on this page (or scripts/verify_past_failures.py)."*

**Bug caught building this**: `cartographer.report.verdict._is_full_map_parent`
used a bare `LIKE f"{run_id}/%"` to detect full_map parents — a plain run
with a `f"{run_id}/verify"` child (which every run can now have) was being
misdetected as a full_map parent and its real records hidden. Fixed by
excluding the verify-suffix run_id explicitly from that query
(`tests/test_verdict.py::test_verdict_mixed_stable_and_noise_reports_verified_counts`
caught this immediately). Also caught in the same pass: the original
pass-rate arithmetic in `run_verify_failures` double-counted the original
FAIL as a pass (`n_pass_incl_original` initialized to 1 instead of 0),
which would have silently misclassified every boundary-threshold case as
noise — fixed before any real API traffic touched it.

Full suite: 318/318 passing (up from the 298-test baseline before this
feature; net +20 tests across `test_evaluator.py`, `test_verdict.py`,
`test_launch.py`, `test_viewer.py`). Zero live API calls anywhere in
tests — all resample/verify behavior exercised via scripted fake clients.

---

## 2026-09-19 — BUG (critical, fixed) + FEATURE (shipped) — /atlas was plotting almost none of its data; rebuilt as an interactive capability map

**What we saw:** two rounds of "the Atlas panel has no interactive
features, just raw projections" and "it just looks like a generic
outdated government platform". Four prior viewer passes had treated this
as a styling problem and restyled around it.

**Root cause:** it was never mainly a styling problem — `/atlas` was
plotting almost no data. `atlas._one_dim_varied_points` admitted only
records that varied *exactly one* dim away from a per-model baseline (the
`resume_from_records` idiom, correct for a grid sweep). The adaptive
campaign scheduler moves several dims at once, so on the real 779-record
store that filter left **3–8 records per (model, dim), binned into a
single x value**. Measured across all 30 (kind, dim, model) series: 21 of
them collapsed to exactly **one** bin. A `<polyline>` with one point
renders nothing. The page was six panels of empty axes, correctly styled.

The secondary cause is real and was the actual "government platform"
tell: every quantity in the app was a **digit in a table cell**. The
shipped references this design is derived from (the bug-tracker's
left-edge severity bars, the admin-dashboard's in-cell progress meters)
encode magnitude and criticality *visually* and keep digits for the exact
value. Absence of visual encoding — not fonts, borders or spacing — is
what read as unfinished.

**Fix:**

- **Bucketing replaces the baseline filter.** Quantile buckets over the
  value *population* (not its range) for every record of a query kind, so
  columns carry comparable trial counts rather than comparable axis
  slices — which matters precisely because the scheduler samples
  difficulty so unevenly. Dims with ≤6 distinct values bucket by exact
  value instead. All 779 records now land in a cell; cells carry n=5..250.

- **Primary visualization is a models × difficulty heatmap**, not
  parallel polylines. Defensible on four counts: (1) "where does each
  model break" is a lookup on a model/difficulty grid, and a heatmap *is*
  that grid, so the answer is a position rather than an inference from
  overlapping lines; (2) a line implies interpolation between points that
  these unevenly-sampled adaptive x values do not support, while a cell
  claims only what it measured; (3) it scales — six models on shared axes
  is spaghetti, six heatmap rows is six rows; (4) it is the same object
  as the capability-map mental model the rest of the app uses. The
  degradation **curve is kept as the secondary view**, driven by the same
  buckets (so the two can never disagree), because reading two models'
  slopes against each other is the one job lines beat a heatmap at.

- **Interaction** (inline vanilla JS, ~70 lines, no library, no CDN):
  hover/focus readout on all 30 data marks; per-model toggles that hide a
  series (with a guard that refuses to blank the chart); query-kind and
  dimension selectors that are **real `<a href>` links**, so every view
  state has a shareable URL and the page works fully with JS off; and
  click-through from any cell or point to a new `/trials?kind&dim&b&model`
  page listing the individual trials behind it, re-deriving the same
  buckets so the cell and the list can never disagree.

- **Focal-point discipline kept, not regressed.** One hue on the map:
  cells are an achromatic luminance ramp down to the 60% break threshold
  and trap-red below it. On the curve, hue goes to exactly one series —
  the model with the *lowest fitted frontier*, i.e. the one that breaks
  first, which is the finding; the rest are neutral, separated by stroke
  weight/dash and direct end-of-line labels rather than a rainbow legend.

- **Visual encoding across the app:** inline pass-rate bars (`_rate_bar`,
  neutral by default, colored only past the threshold, with a
  `threshold=None` escape hatch for bars measuring a share rather than a
  verdict — without it every bar on the page ends up colored, which is
  the failure the threshold exists to prevent); sparklines of pass rate
  over a run's trial sequence on `/runs` and the run KPI (`_sparkline`,
  which returns "" for <2 points rather than implying a trend from a
  dot); left-edge row severity on FAIL/ERROR rows only — deliberately not
  on PASS rows, since an edge on every row is an edge that means nothing;
  and chips for categorical metadata (query kind, render mode, trap
  kinds) that used to be comma-joined prose.

**Verified against the real store, not asserted:** 779 records, 7 runs,
3 models, served on :8792 and measured in the rendered DOM. 15 heatmap
cells and 15 curve points, 30 elements with a hover readout; hovering
cell #8 opens a tooltip reading "nemotron-3-super-120b-a12b /
context_length 11–19 / pass 58.5% (38/65)", opacity 0→1 over the 120ms
transition and back on mouseleave. Clicking a legend toggle takes the
curve from 3 visible series to 2 (dots 15→10) with the heatmap
unaffected; re-clicking restores; clicking every toggle leaves one on.
All 15 drill-down hrefs and all 7 selector links return 200. Three cells
cross-checked against direct SQL on the live store and matched exactly:
gpt-oss-120b @29–37 = 16/44 = 36.4%, nemotron-super @11–19 = 38/65 =
58.5%, Nano @6–8 = 51/55 = 92.7%; the `/trials` page for the first shows
44 rows, 16/44, 28 severity-edged FAIL rows, 44 transcript links. Bar
widths measured in pixels track their rates (93.3% → 54.11px of a 58px
track; 52.8% → 30.61px) and only the sub-60% bars are colored. With the
`js` class removed (the no-JS case) the legend hides rather than showing
dead buttons, while all 15 cells remain real links with their 32 native
`<title>` readouts intact.

**Bug caught building this:** the first default-pane heuristic ranked by
record count, which is a five-way tie in this store (every record carries
all five dims) and landed on `belief/entity_state` — six buckets, but
four of them holding five trials each. Ranking now counts cells clearing
n≥10 first, which opens on `belief/context_length`: the clean monotone
story where Nano holds ~90–100% while gpt-oss-120b falls 67%→36% and
nemotron-super 76%→38%. That finding was in the database the whole time
and the old page could not show it.

Full suite: 359/359 passing (+15 viewer tests). One existing assertion
updated rather than weakened: `test_record_html_escapes_script_in_response_text`
used a blunt `"<script>" not in page`, which now also catches the shell's
own trusted inline script; it strips `<script>…</script>` blocks first
and additionally asserts `"<script>alert(" not in page`, so it still
tests that attacker-controlled text cannot introduce a tag instead of
silently degrading into "the app contains no JavaScript".

**Follow-up (same day):** the atlas legend rendered a toggle even on a
single-model pane, where the handler's "never blank the chart" guard makes
it permanently inert — a control that cannot act, which is the exact
unfinished-interface tell this pass set out to remove. `_legend_html` now
returns "" below two models; a single-model chart is already narrowed,
there is no comparison left to narrow. 360/360 passing.

**Note on history:** the bulk of the work described above was swept into
commit b5ab761 ("Rebuild /findings as a live per-model capability map")
by a concurrent agent committing the shared worktree, so the atlas
rebuild is not described by the message of the commit that carries it.
This entry is its record.

---

## 2026-09-19 — BUG (critical, fixed) + FEATURE (shipped) — /findings could not see the store at all; rebuilt as a live per-model capability map

**What we saw:** the user's verdict on the page that is supposed to be the
product's headline: *"The findings are the weakest. Viewers wanna know
where it failed. It has differentiation for confirmed and needs
replication only. Also, add verify or do replication in the findings so
that it gets updated as well. I want to see what are the positives and
negatives. It is good to have model vs model comparison. But I'd like it
to have a capability map panned out in the findings. No way to switch
inbetween models findings. It's just showing the latest findings."*

**Root cause:** not styling. `cartographer/viewer/findings.py`'s
`findings_html()` **took no store argument**. Every "finding" on the page
was a hardcoded `@dataclass` literal transcribed by hand from this log.
That single fact explains every complaint at once: a page with no store
cannot switch models (there are no models), cannot show a strength (nobody
hand-writes "it passed"), cannot say where a failure lives (the realized
dims were never read), and cannot update after a replication (there was
nothing to update). Four prior passes restyled it; none of them could have
fixed it.

**Fix:** `findings_html(store, model=None)` — the page is now derived.

- **Capability map.** Models × capabilities, rendered as a heat grid:
  4 query kinds (state / order / belief / adversarial_belief) + the 6
  measured difficulty dims, each dim column reporting its HIGH tercile.
  On the current store that is **3 models × 10 capabilities = 30 cells,
  21 populated and 9 honestly empty** (nothing in this store has ever run
  `order`, `adversarial_belief`, or produced a `witness_gap`). Every
  populated cell is an `<a>` into `/trials?model=…&axis=…&key=…`, which
  calls the same `cell_trials()` the grid called, so a cell and the list
  it opens are one computation run twice rather than two that agree today.
  The 5-step ramp is built only from tokens already in the palette
  (verdict-pass → claim-belief → salience → verdict-fail-trap); no new hue.
  Tercile cuts are GLOBAL, not per-model — a column whose value range
  differed per row would be incomparable numbers under one header. The
  header tooltip reports the smallest value that actually lands in the
  high bucket, not the raw cut: `distractors` is 2 for two thirds of the
  store, so its terciles collapse to (2, 2) and "distractors >= 2" would
  have been a lie about the column's contents.

- **Strengths, which were invisible before.** A cell at ≥95% over ≥20
  scored trials is a finding. Verbatim from the rendered page:
  *"NVIDIA-Nemotron-3-Nano-30B-A3B passes state queries cleanly: 30/30, no
  failures at all."*

- **Weaknesses that answer WHERE.** A cell under 90% over ≥20 scored
  trials, reported with the dimension region the failures concentrate in,
  the trap kinds parsed from `traps=`, and three linked example trials.
  Verbatim: *"nemotron-3-super-120b-a12b fails belief queries: 61% pass
  (224/369 scored, 145 real failures) — concentrated at context_length
  20–37, where it drops to 43% (53/124)."* Dim-region weaknesses are
  pruned to the two that genuinely LOCALIZE the failure (≥5 points below
  the model's own overall rate, max 2): without that rule a model that is
  simply bad at belief emits six near-identical cards restating the same
  50% and buries the one card that says something. Pruned cells are not
  hidden — they are still squares in the map, still clickable.

- **One denominator.** Scored trials only (PASS+FAIL), ERRORs excluded,
  resample-`noise` FAILs excluded, via the existing
  `classify_all_verified_failures` — the same definition
  `report/verdict.py` uses. `*/verify` and `repl-*` run_ids are excluded
  from the map entirely: a verified-noisy failure must not get three more
  votes, and a replication's deliberately-easy baseline arm must not lift
  a capability rate.

- **Model switcher.** `/findings?model=<name>`, real links. The server
  renders every model's section and hides all but the selected one
  server-side; a small vanilla script toggles visibility and
  `history.pushState`es the URL. Chosen over pure server-side tabs because
  switching then costs zero round-trips and the capability map above the
  sections never repaints (it doesn't change); chosen over a pure
  client-side widget because the URLs stay shareable and the page is
  correct with JS off. An unknown `?model=` falls back to All models.

- **Replicate, so a finding updates.** Each weakness gets a Replicate
  action that re-runs its worst config plus the generator baseline on
  fresh seeds (`seed_base >= 10_000`) and scores them with the Phase 7
  `confirm_all` — Wilson intervals + Holm-Bonferroni, not reimplemented.
  New `cartographer/confirm/replicate.py` is glue only. The cost estimate
  is derived from that model's own recorded per-trial cost, never a token
  guess: *"~$0.0066 (20 fresh-seed trials × $0.000332 mean observed
  cost/trial over 400 recorded trials)"*. POST `/replicate` reuses
  `/launch`'s one-campaign-at-a-time slot and cancel_event.

- **Curated findings kept, and separated.** The pronoun ablation, the
  witness-bug retraction and the escalation-vs-trap-selection result are
  causal claims no aggregate over verdict columns can reproduce; they stay
  verbatim in a "Documented findings" section labelled *curated · not
  auto-derived*, with their run_ids checked against the store at render
  time. A citation whose records are gone renders struck through as
  "evidence no longer in store" rather than as a live link. (On the
  current store all three cited run_ids still resolve; the ~1,180 records
  lost on 2026-09-19 happened not to include them. The check and its test
  stay, because the next loss will not be so lucky.)

**Bug caught by actually clicking the button:** the replicate POST was
tested live, found a key in `env/`, and started a real replication; it was
cancelled two API calls in (total spend **$0.0015**, against a COPY of the
store in a scratch dir — the live `results/campaign.sqlite` was never
opened by the verification server and still holds exactly 779 records).
The cancelled job left the baseline arm with zero SCORED trials, and the
page reported that as *"replication disagrees"* — a cancelled job rendered
as evidence against the finding. Added a fourth state,
`replication-incomplete`, which says so in plain English. Also renamed the
zero-failure verification state from `verified-stable` to `no-failures`:
a 30/30 cell had nothing resampled, and claiming an audit that never ran
is the exact overstatement this page exists to avoid.

**Measured on the rendered DOM (port 8791), not asserted from source:**
`/findings` and three per-model views all 200; grid 3×10 with 30 `<td>`s;
all 21 cell hrefs 200 and landing on their Trials page with a matching
rate and n; all 6 example `/record/` links 200; three cells cross-checked
against direct read-only SQL over the live store and matching to the
digit — super/belief 224/369 = 61%, nano/high-coreference 44/48 = 92%
(global tercile cut 3.0), gpt-oss-120b/high-distractors 38/89 = 43%.

Full suite: 360/360 passing (+25 in the new `tests/test_findings.py`).
Two existing assertions updated rather than weakened:
`test_nav_present_on_every_page` and `test_findings_page_lists_the_four_modes`
now pass a store (the signature change is the fix) and the latter asserts
the curated section is present AND labelled as curated, which is the new
truth — the page it used to describe no longer exists.

---

## 2026-09-20 — FEATURE (shipped) — second capability province: TOOL-CALL PLANNING (`cartographer/toolworld`)

**What we built:** a whole second task family alongside the narrative
state/belief/order province, following the same architecture contract
(deterministic seeded generation, latent representation as ground truth,
`validate()` before use, foil model + adversarial seed search, campaign
preset + `/launch` wiring, realized-dim measurement).

The latent world is a small devops-flavored state machine: services on a
fixed ladder (`down → provisioned → configured → deployed → healthy`),
artifacts with an `absent → built → published` lifecycle, and boolean
flags. A tool catalog of 3–11 typed tools each carries preconditions over
that state and a deterministic effect. Tools come in three
adversarially-relevant flavors: **decoys** (no-op housekeeping tools that
never change goal-relevant state), **destructive** (`teardown_service`
knocks a service all the way back to `down`, so a wrong call under a tight
budget dead-ends the goal), and **gated** (a rung's advance tool requires a
flag set / an artifact published by *another* tool first — real dependency
chains). The goal is a conjunction of clauses; `parallel_goals=2` adds a
second subgoal sharing the same tool pool.

**Ground truth is bounded BFS, never an LLM judge.** `optimal_depth`
exhaustively searches the enumerated state space (finite ladders + booleans,
total precondition/effect functions) for the shortest valid plan;
`validate()` proves a generated world is solvable within its budget and that
the recorded optimal depth is correct. The verifier parses the model's JSON
plan robustly (strips markdown fences and reasoning preamble; unparseable →
FAIL `malformed_plan`), then **simulates it step by step**: the first call
whose preconditions aren't met → FAIL `precondition_violation` with the
step/tool; goal predicate satisfied at the end → PASS. Inefficient-but-
correct plans PASS (we measure capability, not style) and the overhead
(plan length − optimal) is logged as a realized dim.

**Difficulty knobs** (`ToolDimParams`): `plan_depth`, `decoy_tools`,
`precondition_chain`, `state_obscurity` (fraction of the initial state
phrased indirectly as narrated history the reader must fold forward, plus
scrambled presentation order), `irreversibility`, `parallel_goals`. Verified
across the full grid that these raise measured `optimal_depth` monotonically
where structurally expected — note `plan_depth` saturates at the ladder
height (4 advancement rungs), so beyond that, depth is driven by
`precondition_chain` and `parallel_goals`.

**Foil model** (`greedy_goal_tool` = jump straight to the goal-producing
advance tool ignoring preconditions; `narration_order` = call every offered
tool once in catalog order; `decoy_bait` = lead with the highest-surface
decoy then the greedy tool). A foil "diverges" when its plan fails to reach
the goal; adversarial seed search picks trap-dense worlds at zero API cost.
The zoo-validation test proves the foil bookkeeping (`n_diverging`) matches
independent verification of each foil plan on 30 seeds.

**Campaign:** `run_tool_planning_campaign` (`kind=tool_plan`) sweeps
plan_depth/decoy_tools/precondition_chain/state_obscurity/parallel_goals via
`ToolPlanningEvaluator` (same cost-cap/cancel/thread-safety as
`PipelineEvaluator`). It defaults `verify="manual"` — unlike belief, a
tool-plan FAIL is a hard deterministic verifier outcome (an unmet
precondition, an unreached goal), not the soft MoE expert-routing noise the
resample check was built for, so auto-resampling would add cost without the
payoff. `/launch` gets a NEW preset card; `/findings` and `/atlas` pick the
kind up automatically because they already derive it from
`verifier_detail kind=`.

**Zoo validation** (this repo's discipline): synthetic "models" with planted
failure rules — a perfect planner (BFS solve) scores 100%; a
precondition-ignoring greedy planner FAILs exactly the gated worlds and
PASSes only the trivial 1-step ones; a decoy-only planner FAILs everything
(`goal_not_reached`); a prose-only planner always FAILs `malformed_plan`.
All recover ground truth.

**Which dims look most likely to discriminate tiers (a priori, pre-live):**
`precondition_chain` and `parallel_goals` — they force genuine ordering
under dependencies and the longest optimal plans (up to depth 10 in the
sweep), where a shallow planner's shortcut is provably wrong; the foils
diverge hardest there. `state_obscurity` is the sleeper: it doesn't change
the optimal plan at all, only how much of the initial state must be inferred
from indirect narration — so it isolates *reading/inference* from *planning*,
the tool-planning analog of the belief province's continuity-of-observation
trap. `decoy_tools` mostly scales search-space breadth (easy to ignore for a
capable model); `plan_depth` alone saturates at the ladder height. `budget_
exceeded` and `malformed_plan` FAIL details will separate "can't plan" from
"can't format," which the narrative province can't distinguish.

Full suite: 613/613 passing (+253 in the new `tests/test_toolworld.py` plus
`tests/test_launch.py` preset tests). Zero changes to the narrative province.

---

## 2026-09-21 — FEATURE (shipped) — tool-planning province: structural trap mutators (`cartographer/toolworld/mutate.py`)

**What we saw:** the tool-planning province's scalar knobs saturate too
easily — reading a rendered max-difficulty prompt showed three concrete
tells: (a) decoy tools literally announce "(Has no effect on deployment
state.)" in their own description; (b) every precondition is spelled out in
prose per-tool, so nothing has to be inferred from general rules; (c) the
world is greedy-solvable at every reachable state — calling any currently-
valid advance tool is always globally safe, so "call whatever's valid" never
loses. The narrative province already proved magnitude-escalation (bigger
numbers) fails to trap capable models while structural traps (plausible
wrong shortcuts) succeed; this mirrors that lesson into the tool-planning
province.

**Fix / decision:** six deterministic mutators in
`cartographer/toolworld/mutate.py`, mirroring `world/mutate.py`'s
ANSWER_PRESERVING / ANSWER_CHANGING contract:

- `poison_ladder` (ANSWER_CHANGING) — plants a goal-sounding trap tool
  (`sync_release_state`, always fireable) whose effect clears a flag a real
  advance step needs, with no re-setter in the catalog. World stays solvable
  (trap is avoidable) — the greedy no-lookahead foil walks into it on 24/30
  seeds; a lookahead planner never does.
- `dishonest_decoys` (ANSWER_PRESERVING) — rewrites every decoy's name +
  description to reference the goal service by name and deletes the
  "(Has no effect...)" confession; effects stay genuine no-ops, so
  `optimal_depth` is provably unchanged (asserted across 20+ seeds).
- `implicit_preconditions` (ANSWER_PRESERVING, render-level) — implemented
  as `render.render(world, seed, style="implicit")` rather than a world
  mutation, to keep the world/render split clean per the build spec: every
  tool's own "Precondition: ..." sentence is stripped and replaced by ONE
  "environment mechanics" paragraph up top stating the general rules; the
  model must map the general rule to the specific call.
- `zero_slack` (ANSWER_CHANGING) — tightens `budget` to exactly
  `optimal_depth`; `verify.py` gained an additive branch emitting the new
  `budget_exceeded_plan` detail code specifically when budget==optimal_depth
  (existing worlds with normal slack are provably untouched — same detail
  string as before).
- `id_collision` (ANSWER_PRESERVING) — re-ids every tool to a deterministic
  near-collision confusable (`advance_0` → `advance_o0`-style) while names
  stay distinct; targets the `unknown_tool` interface-binding failure class
  Nano already showed once live.
- `interleaved_goals` (ANSWER_CHANGING, `parallel_goals==2` only) — adds a
  shared window flag required by BOTH subgoals' final rung, one-way closed
  by subgoal A's own completion step. "Finish all of A, then all of B" (the
  natural union-of-two-independent-plans a no-lookahead planner produces)
  dead-ends B every time; a real interleaved plan (open window → B's final
  rung → THEN close via A's final rung) exists and is asserted solvable.

**Task 2 — foil:** `foils.foil_greedy_lookahead0` simulates the literal
zero-lookahead policy (first currently-valid non-decoy tool, catalog order,
repeat until stuck or goal) and is wired into `FOILS`/`foil_divergence`.
PASSes every un-mutated world (20/20 seeds — confirms the base generator's
"any valid tool is safe" property the diagnosis flagged). On
`interleaved_goals` worlds it fails 100% of the time (30/30 applicable
seeds); on `poison_ladder` it falls into the trap on the large majority
(24/30) but not all — the trap's catalog position is seeded, so on some
seeds the greedy scan happens to reach a real advance tool first.

**Task 3 — escalation:** `default_tool_ray()` in `exec/evaluator.py` ramps
five 0/1 trap toggles (`poison_traps`, `dishonest_decoys_on`,
`implicit_preconditions_on`, `zero_slack_on`, `id_collisions_on`) plus the
existing `precondition_chain`/`parallel_goals` scalars from off/easy to
all-on/ceiling. `interleaved_goals` did NOT map onto its own RayDim — it
only ever applies when `parallel_goals==2` (see
`mutate.interleaved_goals_applicable`), so it rides the existing
`parallel_goals` dim for free rather than needing a synthetic toggle with
nothing to represent. `run_tool_breaking_point_campaign` (budget=250,
cost_cap=0.5) mirrors `run_breaking_point_campaign`'s shape exactly
(`find_breaking_point` + decompose + confirm), wired into `/launch` as a
new NEW-badged preset card.

**Pure-simulation signal (free, zero API calls):** greedy-lookahead0 fails
50/50 (100%) at full ceiling config (all five traps stacked +
precondition_chain=2 + parallel_goals=2) across 50 seeds — up from 0%
divergence on the un-mutated province's greedy foil at the same scalar
settings. That's the trap-density signal this build set out to produce:
worth spending live budget on `tool_breaking_point` once escalated.

**Verification:** 630/630 tests passing (613 prior + 17 new in
`tests/test_toolworld_mutate.py`) — zoo validation with `GreedyPlanner`
(lookahead 0, PASSes normal/FAILs poison_ladder+interleaved_goals),
`PerfectPlanner` (bounded BFS, PASSes every mutated-but-solvable world
including zero_slack), `VerbosePlanner` (perfect + 2 redundant decoy calls,
PASSes normal/FAILs zero_slack with `budget_exceeded_plan`), `NameCaller`
(uses tool names not ids, FAILs id_collision with `unknown_tool`).
Determinism + ANSWER_PRESERVING invariants asserted across 20+ seeds each.
Zero live API calls anywhere in this build — the live escalation run
happens later, separately, after human review.

---

## 2026-09-21 — FEATURE (shipped) + FINDING (confirmed) — first judged data through the Nemotron-rendered prose pathway; fidelity gate rejects 11/46

**What we saw:** The Phase 4 LLM-rendered pathway (Nemotron narrates the
world as prose behind the mandatory fidelity gate) existed in code but had
never produced a single judged record. `camp-nemrender-showcase-001` fixes
that: a 36-budget `state` campaign on Super with renderer =
Nemotron-3.5-Lightning and extractor = Nano (contamination guard satisfied:
renderer != subject, extractor != renderer). SQL-verified results: 46
records, all `render_mode=NEMOTRON` — **34 PASS / 1 FAIL** on scored trials
(97%) plus **11 REJECTED_BY_FIDELITY_GATE**, tracked cost $0.0079 of a
$0.15 cap. Accepted renders averaged 1.34 gate attempts.

**Root cause / reasoning:** Two latent bugs surfaced the moment the pathway
ran for real. (1) `run_state_campaign` had no way to pass
`renderer_model`/`extractor_model` down — plumbed through in `de091d2`.
(2) `llmrender/extract.py` crashed with `AttributeError: 'NoneType' object
has no attribute 'find'` whenever the extractor's API call errored (text is
None) — guarded in `e4a8d4f`. Both fixes came out of the first live run's
stack trace, i.e. the pathway was untestable-in-principle until someone
paid for one real campaign.

**Fix / decision / next step:** Comparison against the template pathway:
Super's template-rendered `state` baseline across the store is 60/66 (91%),
vs 34/35 (97%) prose-rendered — natural-language wording does not degrade
Super on state tracking (small n; not a confirmed wording-sensitivity
finding, just an absence-of-effect observation). The headline is the gate
itself doing real work: 11 of 46 rendering attempts dropped a fact and were
rejected *deterministically, before any subject tokens were spent on a
corrupted world*. Nemotron now has a judged **renderer** role in the
narrative province.

## 2026-09-21 — FEATURE (shipped) + FINDING (confirmed) — Nemotron proposes toolworld traps; one hypothesis-confirmed hit at 75% fail on Nano; beats random configs narrowly

**What we saw:** New `cartographer/propose/toolworld.py` (commit `1af8bec`)
points the adversarial proposer loop at the tool province: Super proposes
configs over the five structural trap dims + scalar `ToolDimParams` knobs,
an offline gauntlet (generate → mutate → BFS `validate` → greedy-foil
salience vs a random-config median) filters them for free, survivors are
measured with real Nano trials through `ToolBreakingPointEvaluator`.
`camp-propose-toolworld-001` (4 rounds, $0.15 cap): rounds 0–2 produced
0/6/6 proposals with 100% gauntlet rejection; round 3 got 4 of 6 through,
19 measured trials (SQL: 15 PASS / 4 FAIL / 1 ERROR, $0.0090). **One
library hit**: `{plan_depth: 6, parallel_goals: 2, state_obscurity: 0.5,
implicit_preconditions_on: 1, id_collisions_on: 1}` → **75% fail rate
(3/4)**, and the proposer's stated hypothesis ("model must infer
dependencies; likely misses a hidden precondition") is exactly what the
verifier shows: all three FAILs are `precondition_violation` at confusable
ids (`adva-nce_1`, `advance_o1`, `adva-nce_0`) at steps 5–6 of 7-call
plans.

**Root cause / reasoning:** The round-0 control arm never ran (round 0
yielded no parseable proposals, and the control triggers only in round 0),
leaving `beat_random=None`. A supplementary control arm was run under
`camp-propose-toolworld-001/control` — 4 sha256-derived random configs × 5
Nano trials, $0.0105: control mean failure rate **0.20** (one random config
also cleared the 30% hit threshold) vs proposer mean **0.2375** →
`beat_random=True`, but narrowly and at small n. Honest read: the proposer
found a *sharper, hypothesis-backed* trap than any random config did (75%
vs the best random 40%), but its average edge over random configs is thin —
mostly because the gauntlet rejected everything it proposed for two
straight rounds.

**Fix / decision / next step:** Nemotron now has a **proposer** role in the
tool province, closing the "zero Nemotron involvement beyond test subject"
gap. The proposed trap config independently corroborates the Nano t*=0.55
escalation finding from the same day: both point at implicit preconditions
× id collisions as Nano's soft spot. Two loose ends logged, not chased:
(a) rounds 0–2's total gauntlet wipeout suggests the digest gives the
proposer too little tool-province context to aim well until FAIL evidence
accumulates; (b) the control config that hit 0/0 measured trials burned its
5 attempts as ERRORs (excluded from rates by design).

## Template for new entries

```
## YYYY-MM-DD — TYPE (status) — one-line summary

**What we saw:** ...
**Root cause / reasoning:** ...
**Fix / decision / next step:** ...
```
