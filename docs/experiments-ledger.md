# Experiments ledger: what we ran, what we found, what has been checked

*Compiled 3 Oct 2026, from `results/campaign.sqlite` and `docs/lab-log.md`.*

**Store totals:**
- 4,818 records across 82 run ids, 17 Sep to 3 Oct 2026, **$5.93 tracked spend**.
- Verdicts: 3,850 PASS / 838 FAIL / 119 ERROR / 11 rejected by the fidelity gate.
- *Updated 3 Oct (evening) with the Province III completion runs: E20–E23.*
- 285 of the records are resample-check children (`/verify`).

## How "reproducibility-checked" is defined here

| Code | Check | What it rules out |
|---|---|---|
| **RS** | **Resample**: the byte-identical stored prompt re-run ≥ 3 times; the failure is called STABLE or NOISE | a one-off sampling fluke (the models are mixture-of-experts and vary even at temperature 0) |
| **FS** | **Fresh-seed replication**: same configuration, new worlds the model has never seen | a lucky or unlucky draw of worlds |
| **IM** | **Independent method**: a different experiment design reaches the same conclusion | an artifact of one experiment's design |
| **ST** | **Significance test**: Fisher exact or permutation test, or a Wilson CI that excludes the alternative | small-sample noise |

A finding counts as **reproduced** when it has passed at least one of RS, FS or IM. ST alone does not count, because it tests one run against itself.

---

## 1. Experiments run (24)

### Province I: Narrative world-model

| # | Experiment | Runs | Tiers | Trials | Spend | Verdict |
|---|---|---|---|---|---|---|
| E1 | State tracking (where is the object) | `camp-state-*`, `full_map/state` | N, S, U, gpt-oss | ~270 | $0.07 | **Holds on all tiers** (Nano 90/90, Super 57/60 template, Ultra 30/30). No boundary through chain length 30. |
| E2 | Event order from scrambled narration | `full_map/order` | N, S, U | ~320 | $0.12 | **Holds**: 156/158, 79/80, 80/80. |
| E3 | Belief tracking (what a person thinks) | `camp-belief-*-002`, `full_map/belief` | N, S, U | ~1,100 clean | ~$0.6 | **Tier ordering Ultra > Super > Nano**: 98.9% / 97.3% / 90.1% (Nano 521/578). Two early runs **invalidated** (witness-rendering bug). |
| E4 | Belief in adversarially selected (trap-dense) stories | `full_map/adversarial_belief` | N, S, U | ~600 | ~$0.4 | Nano 88.0%, Super 95.0%, Ultra 99.2%. **Super abstains** ("unknown") under trap pressure. |
| E5 | Narrative breaking point (all dims ramped to ceiling) | `full_map/breaking_point` | N, S, U | ~280 | ~$0.3 | **No frontier on any tier**: holds at the generator ceiling. |
| E6 | Nemotron-written prose instead of templates | `camp-nemrender-showcase-001` | S (subject) | 46 | $0.008 | Super 34/35; fidelity gate rejected 11 of 46 renderings. |
| E7 | Pronoun ablation (manual, Ultra) | none (records lost in the 19 Sep incident) | U | ~20 | — | One clean case: 6/6 separation pronoun vs name. Evidence no longer in the store. |

### Province II: Tool-call planning (single-shot JSON plan)

| # | Experiment | Runs | Tiers | Trials | Spend | Verdict |
|---|---|---|---|---|---|---|
| E8 | Baseline tool-planning sweep, plain and adversarial seed selection | `camp-tool_planning*` | N, S, U | 160 | $0.06 | **Saturated** (39/40, 40/40, 40/40, 40/40). The base generator had no traps to find. |
| E9 | Structural-trap ray (5 trap mutators ramped together) | `camp-tool_bp-*` | N, S, U | 392 | $0.35 | **Nano breaks** (t* 0.675 [0.615, 0.758] after the cap fix). Super holds (24/24 at 5 traps). Ultra holds (23/28), but its failures are **deterministic id mis-binding at step 8–9**. |
| E10 | Depth × look-alike names ray, up to 19-call plans | `camp-tool-depth-*` (+ k15 top-up, cap replay, Ultra top-up) | N, S, U | 541 | $0.92 | **Nano breaks at 13-call plans** (5/21). **Super degrades from 14-call plans** (38/60 at ≥ 14 calls vs 28/28 below). Super "break at t* 0.883" **retracted** (token-cap artifact). Ultra dip at 16–18 calls = **noise**. |
| E11 | Deep depth ray, up to 31-call plans, look-alike names on throughout | `camp-tool-deep-*` | N, S, U | 154 | $0.49 | **Super breaks at 24-call plans** (8/28; t* 0.66 [0.56, 0.78]). **Ultra degrades from 25 calls** (11/21 vs 31/35, p = 0.004) **but never collapses** within 31 calls. Nano weak from 7 calls. |
| E12 | Catalog size, 8 → 66 tools (honest, then plausible decoys) | `camp-tool-count/confuse-super-001` | S | 112 | $0.06 | **Null: 112/112.** Catalog size is not a stressor at this scale. |
| E13 | Impossible tasks (a load-bearing tool removed; correct = decline) | `camp-irrelevance-*` | N, S, U | 80 | $0.07 | Writes a plan anyway: **Nano 6/12, Ultra 3/8, Super 2/12**. Mechanism in **11/11** cases: silently skips the missing step. No tier ever declined a possible task. |
| E14 | Nemotron as adversarial proposer of trap configs | `camp-propose-toolworld-001` | N (subject), S (proposer) | 40 | $0.02 | One hit at 75% fail, with the hypothesised mechanism confirmed. Edge over random configs is weak (0.24 vs 0.20). |
| E15 | Hand-built probes on Super (outside the store) | — | S | ~70 | ~$0.02 | Passed all 7 families. Includes **prompt injection in a tool description: resisted 5/5**. |
| E16 | Probe parallelism A/B (infrastructure) | `test-parallel-*` | N | 12 | $0.01 | 5.2× speed-up; identical trial set; 0 rate-limit errors. |

### Province III: Interactive execution (native function calling, one call at a time)

| # | Experiment | Runs | Tiers | Trials | Spend | Verdict |
|---|---|---|---|---|---|---|
| E17 | Plan length × value passing (tickets that must be carried between calls) | `camp-interactive-{nano,super}-001` | N, S | 80 scored | $0.54 | **Value passing is not a bottleneck**: Super 43/43, Nano 36/37 up to 19-call plans carrying 16 values. Zero mishandled tickets. |
| E18 | Interactive with look-alike tool names (model sees each error) | `camp-interactive-lookalike-*` | N, S | 56 scored | $0.36 | **Super 17/20 vs 65% single-shot (p = 0.063)**; recovers from 9 of 12 binding errors. **Nano's interactive frontier is ~9-call plans**, and **all 17 failures are early stops**, 9 with no error at all. |
| E19 | Feasibility probes (outside the store) | — | N, S | ~40 | $0.03 | Native function calling works; error-feedback loop works. **Consistency: Nano solves a world on 63% of single tries but on all 5 tries for only 1 of 6 worlds.** |
| E20 | Error recovery: E17's ray with one call failing transiently mid-plan (call 3 → 9 as plans lengthen) | `camp-interactive-fault-{nano,super}-001` | N, S | 56 scored | $0.31 | **Both tiers recover.** The fault fired in 33 Super episodes: 33 reached the goal. It fired in 21 Nano episodes: 20 reached the goal. Nano's other 2 failures were early stops *before* the fault fired (F25's mechanism). |
| E21 | Super interactive with look-alike names at 23- to 25-call plans | `camp-interactive-lookalike-super-002` | S | 14 scored | $0.25 | **13/14**, where single-shot planning gets **15/42** on the same plan lengths (p = 0.0002). Recovered from 11 of 12 errored episodes. Cost cap stopped the ray at 25-call plans. |
| E22 | Ultra interactive with look-alike names at 25-call plans | `camp-interactive-lookalike-ultra-001` | U | **0 scored** | $0.43 | **No measurement.** All 7 episodes that ran ended on one turn that hit the 16,000-token per-turn cap (`finish_reason=length`); the other 3 never started (cost cap). Graded ERROR, not FAIL. The interactive loop had no 32k retry; fixed afterwards. |
| E23 | Fresh-seed confirmations of F23, F24, F25 (new worlds, trial seeds 500,000+) | `camp-interactive-{nano,super}-002`, `camp-interactive-lookalike-nano-002`, `camp-interactive-lookalike-super-003` | N, S | 48 scored | $0.39 | **All three replicate.** Values at 19-call plans with 16 values: Super 8/8, Nano 7/8, 0 bad tickets. Super look-alike at 18-call plans: 17/17 (single-shot 16/22, p = 0.023). Nano at 9-call plans: 10/15, every failure an early stop. |

N = Nano 30B-A3B, S = Super 120B-A12B, U = Ultra 550B-A55B.

---

### Cross-model

| # | Experiment | Run ids | Tiers | n | Spend | Result |
|---|---|---|---|---|---|---|
| E24 | Gemma 3 27B full map, all four provinces (Province III via the new text tool-call protocol; Nebius drops `tools` for Gemma) | `camp-*-gemma-*` | `google/gemma-3-27b-it` | 1,441 records | $1.08 | Breaks: look-alike tool names at 17-call plans, structural traps, impossible tasks (0/16 declined). Degrades: state (all failures resample-stable), belief (true-location collapse), semantic code traps (aliasing + negative modulo). Holds: order, interactive values and fault recovery, catalog size, look-alike *variable* names. One run per stage: not yet reproduced. |

## 2. Findings and their checks

| # | Finding | Grade | RS | FS | IM | ST | Reproduced? |
|---|---|---|---|---|---|---|---|
| F1 | Nano breaks on long plans with look-alike tool names (~13 calls) | A | — | ✓ deep ray, new seeds | ✓ trap ray + proposer, same mechanism | ✓ p = 0.0006 | **Yes** |
| F2 | For Nano, depth × look-alike names is multiplicative (14/14, 13/21, 5/21, 0/7) | A− | — | — | — | ✓ decompose probes | **No** (fresh-seed confirmation never run) |
| F3 | Look-alike tool-name binding (`unknown_tool`) is the dominant tool failure on every tier | A | — | ✓ | ✓ 6 separate experiments | — | **Yes** |
| F4 | Ultra mis-binds ids deterministically at step 8–9 of 9-call plans | A− | ✓ 3/5 stable, 3/3 each | — | — | — | **Yes** (n = 3) |
| F5 | Super degrades from 14-call plans with look-alike names | A | — | ✓ k = 15 top-up + deep ray | — | ✓ p = 4.7e-05 | **Yes** |
| F6 | Super breaks at ~24-call plans | A | — | partial (28 trials over several bisect probes) | — | ✓ | **Not independently** |
| F7 | Ultra degrades from 25-call plans, no collapse within 31 calls | A | — | — | — | ✓ p = 0.004 | **No** |
| F8 | Ultra's dip at 16–18-call plans is noise | A | — | ✓ fresh top-up 6/7 | — | ✓ permutation p = 0.121 | **Yes** (confirmed as noise) |
| F9 | Super is insensitive to catalog size up to 66 tools | A | — | ✓ two independent runs | — | — | **Yes** |
| F10 | Super resists an instruction injected into a tool description | B | — | — | — | — | **No** (one run, 5/5) |
| F11 | Super judges feasibility correctly (impossible, starved budget, goal already met) | A− | ✓ 4/5 → 40/40 | — | — | — | **Partly** (goal-met only) |
| F12 | Belief capability orders Ultra > Super > Nano | A | ✓ full_map resamples | ✓ several campaigns | — | — | **Yes** |
| F13 | State and order are solved by all tiers in range | A | — | ✓ several campaigns | — | — | **Yes** |
| F14 | Nano belief failures: over-extends "still watching" past the last witness cue | B | ✓ 55 stable resamples in Nano full_map runs | — | ✓ witness_cutoff sweep reproduces the mechanism | — | **Yes** |
| F15 | Super abstains ("unknown") under trap pressure | A− | ✓ 4 of 7 stable | — | — | — | **Yes** |
| F16 | Ultra protagonist-continuity pronoun bias | C | — | — | — | — | **No** (evidence lost) |
| F17 | Short chains are harder for Ultra under traps | C | — | ✗ later run 127/128 | — | — | **No** (did not replicate) |
| F18 | Nemotron-written prose does not degrade Super | C | — | — | — | — | **No** |
| F19 | Nemotron's proposer finds traps better than chance | C | — | — | — | weak | **No** |
| F20 | Nano breaks on the structural-trap ray | A | — | ✓ re-run with caps lifted (location moved 0.55 → 0.675) | — | ✓ | **Yes** (break reproduced; location corrected) |
| F21 | On impossible tasks, models skip the missing step instead of declining (11/11) | B | — | — | ✓ same mechanism on all 3 tiers | — | **No** (one run per tier) |
| F22 | Nano has a large consistency gap (63% single-try vs 17% all-5) | C | — | — | — | — | **No** (probe outside the store) |
| F23 | Value passing is not a bottleneck up to 19-call plans | A− | — | ✓ E23: Super 8/8, Nano 7/8 at the hardest setting, 0 bad tickets | — | — | **Yes** |
| F24 | Super recovers from binding errors when it sees feedback | A− | — | ✓ E23: 17/17 at 18-call plans | ✓ E21 at longer plans: 11 of 12 errored episodes recovered | ✓ p = 0.023 (fresh run alone); p = 0.001 pooled | **Yes** |
| F25 | Nano abandons long interactive tasks early | A− | — | ✓ E23: 10/15, all 5 failures early stops, 3 with no error at all | ✓ also the cause of Nano's 2 pre-fault failures in E20 | — | **Yes** |
| F26 | Both Nano and Super recover from a transient tool failure (retry, then continue) | B | — | — | — | — | **No** (one run per tier; Super 33/33, Nano 20/21 when the fault fired) |
| F27 | With feedback, Super holds past its single-shot frontier: 13/14 at 23–25-call plans vs 15/42 single-shot | B | — | — | — | ✓ p = 0.0002 | **No** (one run; the ray was stopped at 25 calls by the cost cap) |

### Retractions and corrections (all published in the lab log)

| # | Original claim | What happened |
|---|---|---|
| X1 | Super breaks at t* = 0.883 on the depth ray | Token cap truncated valid answers; replay on the same worlds: 6 of 10 were passes |
| X2 | Super has an intrinsic false-belief floor | Witness-rendering bug: the witnesses never reached the prompt |
| X3 | Ultra abstains under trap pressure | Token truncation; the real abstention is Super's (F15) |
| X4 | The "Priya" continuity failure appears on all 3 tiers | 4 of 5 resamples correct: noise |
| X5 | Nano's depth-ray t* "replicates" the trap-ray t* | t is ray-relative; the two numbers are not comparable. Only the mechanism replicates |
| X6 | Nano trap-ray t* = 0.55 | Also cap-biased; re-run gives 0.675 |
| X7 | Nano interactive "drop to 2/5" at its ceiling | 3 trials never ran (cost cap); caught before reporting, and the escalation code fixed |
| X8 | (never claimed) Ultra interactive "0/10" | Every episode was cut off by the per-turn token cap; graded ERROR, reported as *no measurement*. The interactive loop now retries a truncated turn at 32k, matching single-shot |
| X10 | F-G4Q-1 Gemma 4 ticket fabrication (+ the 6/6 restatement rescue) | v2 text protocol executed whole blind trajectories (median 2 turns); under protocol v3 33/33, 31/31, 50/50, zero invalid tickets. Retracted as stated 2026-10-10 |

### Totals
- **27 findings**: **14 reproduced** (F1, F3, F4, F5, F8, F9, F12, F13, F14, F15, F20, F23, F24, F25), **1 partly** (F11), **12 not yet reproduced** (F2, F6, F7, F10, F16, F17, F18, F19, F21, F22, F26, F27). F17 was re-tested and did **not** replicate.
- **8 retractions or corrections**, all caught by the project's own checks.
- **Failure-level resample check:** of the 463 failures counted as evidence, **95 (21%) were resample-checked**. **65 were STABLE (68%), 30 were NOISE (32%)**, so roughly one in three single-trial failures in the narrative province was sampling noise. All 95 came from the narrative `full_map` runs. Tool-province failures were checked by hand where it mattered (Ultra 3/5 stable; Super cap replay 6/10 passes) but **not through the automatic gate**. That is the largest reproducibility gap.

---

## 3. Is Province III complete?

**Yes for Nano and Super. Ultra is not measured.**

| Part | Status |
|---|---|
| Environment: one-call-at-a-time execution, native function calling, unguessable value tickets, transient-fault injection, per-step distance to goal | ✅ built, 17 tests |
| Plugged into escalation (ramp, bisect, t*, decompose, degradation onset) and `frontier_summary` (3 rows: interactive, look-alike, error recovery) | ✅ |
| III-A multi-step execution + III-B value passing, run on Nano and Super | ✅ E17, fresh-seed confirmed (E23) |
| Interactive × look-alike names, run on Nano and Super | ✅ E18, fresh-seed confirmed (E23) |
| III-C error recovery (transient fault mid-plan) on Nano and Super | ✅ E20 |
| Super interactive past its 24-call single-shot frontier | ✅ E21 (to 25-call plans; holds 13/14) |
| Ultra in any Province III experiment | ❌ E22 produced no measurement (per-turn token cap); the loop is fixed, re-run needs approval |
| Native vs prompt-JSON protocol comparison | ❌ not started |
| Launch preset in the viewer's Explore page | ❌ not started |
