# Model Cartographer — Findings

*Compiled 3 Oct 2026 from `results/campaign.sqlite` (6,259 records, 108 run ids, $7.01 tracked spend; real provider spend is roughly 3× the tracked figure for the reasoning models), `docs/frontier-summary.json`, `docs/experiments-ledger.md` and `docs/lab-log.md`. Every number below comes from the store or from those documents; nothing is estimated unless it says so.*

**Models covered**
- **NVIDIA Nemotron 3 Nano** 30B-A3B, a reasoning model.
- **NVIDIA Nemotron 3 Super** 120B-A12B, a reasoning model.
- **NVIDIA Nemotron 3 Ultra** 550B-A55B, a reasoning model.
- **Google Gemma 3 27B-it**, which does not reason before answering. It was mapped today across all four provinces.
- **gpt-oss-120b**, tested on state tracking only, during the Groq pilot.

---

## 0. How to read this document

### What the instrument does
- **Tasks are generated, with a known right answer.** Every task is produced from a hidden *latent* structure: a story's event log, a tool-catalog state machine, or a Python program's syntax tree. The right answer comes from simulating that structure, using BFS search for plans and our own interpreter for programs.
- **No LLM ever grades an answer.** This is the instrument's central credibility rule.
- **Difficulty is turned up along a *ray*,** a straight line through difficulty settings from easy (t = 0) to the generator's ceiling (t = 1). The escalation engine works in five steps:
  1. Raise t step by step, with 5–7 trials per step.
  2. Bisect the first pass→fail bracket.
  3. Fit a logistic t\*.
  4. *Decompose* the failing configuration, switching factors back off one at a time to find which ones are needed for the failure.
  5. Confirm on fresh seeds.
- **Failures are classified by mechanism.** Each failure gets a code, such as `unknown_tool`, `final_true_state`, `foil:value_semantics` or `stopped_short`. That is how a cause is reported, not just a rate.

### Status words (from `frontier_summary`)

| Status | Meaning |
|---|---|
| **BREAKS** | the pass rate falls to ≤ 30% at a level *and stays low from there up* (pooled ≤ 40%) |
| **DEGRADES** | the pass rate falls to ≤ 75% and stays there; for rate-type rows, the 95% Wilson upper bound is < 95% |
| **HOLDS** | neither, across every tested level |
| **NOT TESTED** | no valid data |

Boundaries are always given in real units ("24-call plans", "5 of 6 traps active"), never as t-values. t is relative to its ray, so two t-values from different rays cannot be compared. That mistake was made once and retracted (X5).

### Confidence grades
- **A:** strong. n ≥ 20 at the boundary and a significant before/after test.
- **B:** moderate.
- **C:** thin. Small n, or a single probe.

### Reproducibility codes

| Code | Check | Rules out |
|---|---|---|
| **RS** | Resample: the byte-identical prompt re-run ≥ 3 times; the failure is called STABLE or NOISE | a one-off sampling fluke (these MoE models vary even at temperature 0) |
| **FS** | Fresh-seed replication: same configuration, new tasks never seen | a lucky or unlucky draw of tasks |
| **IM** | Independent method: a different experiment design reaches the same conclusion | an artifact of one design |
| **ST** | Significance test: Fisher exact or a permutation test | small-sample noise |

A finding counts as **reproduced** only if it passed RS, FS or IM. A significance test alone does not count, because it tests one run against itself.

### The four provinces

| Province | What the model does | Ground truth | Main stressors |
|---|---|---|---|
| **I · Narrative world-model** | reads a story of moves and observations; answers where an object is, which event came first, or where a person *believes* the object is | simulation of the event log, with per-person witness sets | chain length, distractor events, pronoun pressure, scrambled narration, `witness_cutoff` (a false-belief trap), adversarially selected stories |
| **II · Tool-call planning** (single shot) | writes a whole JSON plan of tool calls for a devops state machine | BFS over the tool world; the plan is executed step by step | plan length, look-alike tool ids (`set_dep-th_flag_4` vs `set_dep_th_flag_4`), structural traps (poisoned tool, dishonest decoys, implicit preconditions, zero slack, id collisions), catalog size, impossible goals |
| **III · Interactive execution** | executes the plan one call at a time through function calling, seeing each result or error | the same world, plus per-step BFS distance to the goal | plan length; *value passing* (unguessable sha256 tickets one call returns and a later call needs); look-alike names; a transient fault mid-plan |
| **IV · Code tracing** | reads a generated Python program and predicts a variable's final value | our own interpreter, verified equal to CPython on 5,000 generated programs; model text is never executed | program length and nesting, look-alike *variable* names (`bonus`/`b0nus`), six semantic traps, each paired with a "shallow reader" foil |

---

## 1. Headline findings

1. **Look-alike identifiers are the single dominant way tool planning breaks, on every model tested.**
   - In single-shot planning with look-alike tool names, each model has a measurable boundary:
     - Nano breaks at **13-call** plans.
     - Gemma breaks at **17-call** plans.
     - Super breaks at **24-call** plans.
     - Ultra degrades from **25-call** plans and never collapses within the 31-call ceiling.
   - 92–100% of the failures in each are `unknown_tool`: the model calls a misspelled look-alike of a real tool id.
   - With clear tool names, every model holds through the tested range (12–13-call plans).
   - *Reproduced (F1, F3, F5).*

2. **The size ordering is clean and the boundaries are far apart.** Nano 13 < Gemma 17 < Super 24 < Ultra ≥ 25 (degrade only), measured on the same generator and the same ray family. That is a 2× spread in reliable plan length across a 30B → 550B range.

3. **Seeing errors rescues the binding failure, for the larger models.**
   - **Super:** step by step with look-alike names, it holds **43/47 through 25-call plans**. Planning everything at once at 23–25 calls, it gets **15/42** (p = 0.0002).
     - It recovered from 11 of 12 episodes that hit a binding error.
     - A fresh-seed check at 18-call plans: **17/17**, against 16/22 single-shot (p = 0.023).
     - *Reproduced (F24).*
   - **Gemma** shows the same pattern: **12/17** step by step at ≥ 18-call plans, against **1/14** single-shot at ≥ 17 calls (p = 0.0004). *One run.*

4. **Nano's step-by-step limit is giving up, not binding.**
   - Step by step with look-alike names, Nano breaks at about 9–11-call plans.
   - All 17 failures in the first run were *early stops*, and 9 of those had no error at all; some stopped one call from the goal.
   - A fresh-seed rerun gave 10/15, and every one of its 5 failures was an early stop.
   - *Reproduced (F25).*
   - Single-shot planning can't see this failure mode. It's the clearest case of Province III measuring something Province II cannot.

5. **Passing values between calls is not a bottleneck, for any model tested.**
   - Through 19-call plans carrying 16 unguessable tickets: Super 53/53, Nano 43/45, Gemma 40/40.
   - There were **zero** bad-ticket errors on the hardest setting, and on a fresh-seed rerun.
   - *Reproduced (F23).*

6. **Recovering from a transient tool failure is not a weakness.**
   - Super reached the goal 33/33 times after the fault fired; Nano 20/21; Gemma 40/40.
   - Nano's other failures in that run were early stops *before* the fault, so they were abandonment again.
   - *F26: one run per tier.*

7. **Belief tracking separates the models; plain state tracking separates Gemma from Nemotron.**
   - Belief: Ultra 99%, Super 97%, Nano 90%, Gemma 49%.
   - The dominant wrong answer is the object's *true* location instead of where the person believes it is: 63% of Nano's failures, 79–89% of Gemma's.
   - Every Nemotron tier tracks plain object state at 96–100% through 30-step chains.
   - **Gemma tracks state at only 57%.** 18 of its 20 failures are at chain length ≥ 23, and all 20 failed again on resample.
   - *Reproduced for Nemotron (F12, F13); Gemma one run.*

8. **In code, the semantic traps break Gemma; look-alike variable names do not.**
   - Gemma passes 41/42 programs with no traps, and 18/42 with all six traps on (p ≈ 1e-8).
   - Its failures are dominated by two misreadings:
     - treating `b = a` as copying a list (18 failures);
     - rounding `%` / `//` toward zero for negative numbers (13 failures).
   - Look-alike variable names (`bonus` / `b0nus`) **never** produced a `confused_identifier` failure. It holds 25/28 through 42-statement programs.
   - So Gemma's binding weakness is **specific to tool catalogs**, not a general weakness with identifiers. *One run; the first live Province IV data.*

9. **Nothing tested breaks Ultra.**
   - It holds on 6 of 8 capabilities.
   - Its only weaknesses are degrading from 25-call look-alike plans (4/7 at 25 calls vs 7/7 at 21, p = 0.0004) and declining only 5 of 8 impossible tasks.
   - Ultra was **not measurable** in Province III: the per-turn token cap truncated every episode. That is now fixed, but not re-run.

10. **Methodology result: two measurement artifacts nearly became findings, and both were caught.**
    - **Token caps.** A cap below the answer's natural length turns "still writing" into "failed", and the bias grows with difficulty, so it produces exactly the decline an escalation looks for. Super's first "breaking point" was this (X1, retracted).
    - **Provider protocol.** Nebius silently drops tool definitions for Gemma. Province III would have scored every Gemma episode as giving up with 0 calls.
    - The project now treats both as standing checks: audit `finish_reason=length` before reporting, and probe tool visibility before running Province III on a new model.

---

## 2. Model profiles

### 2.1 Nemotron 3 Nano (30B-A3B)
*Verdict: breaks on long plans with look-alike tool names (13-call plans) and on step-by-step execution with look-alike names (11-call plans). Holds on 4 of 11 capabilities tested.*

| Capability | Status | Evidence | Grade | Main cause |
|---|---|---|---|---|
| State tracking | HOLDS | 90/90 through chain length 30 | B | — |
| Event order | HOLDS | 156/158 | A | — |
| Belief | DEGRADES | 521/578 (90%), CI [0.874, 0.923] | A | `final_true_state` 63% |
| Adversarial belief | DEGRADES | 279/317 (88%) | A | `final_true_state` 87% |
| Plans, clear names | HOLDS | 46/49 through 13-call plans | B | — |
| Plans, look-alike names | **BREAKS at 13-call plans** | 5/21 (already 15/28 at 7 calls); 0/7 at 14 | B | `unknown_tool` 96% |
| Structural traps | DEGRADES at 4 of 5 traps | 13/35 vs 34/35 with none (p = 1.2e-11) | A | `precondition_violation` 72% |
| Impossible tasks | DEGRADES | declines 6/12 | C | writes a plan anyway |
| Step by step + values | HOLDS | 43/45 through 19-call plans, 16 values | B | the 2 failures are early stops |
| Step by step, look-alike names | **BREAKS at 11-call plans** | 1/5 (22/36 at 9 calls) | C | `stopped_short` |
| Fault recovery | DEGRADES at 11-call plans (grade C) | 3/5; but 20/21 reached the goal once the fault actually fired | C | early stops *before* the fault |

**Mechanisms**
- **Multiplicative interaction.** Depth alone barely hurts Nano and look-alike names alone hurt moderately, but together they collapse it: 14/14 → 13/21 → 5/21 → 0/7 (F2, decompose probes; no fresh-seed check yet).
- **The binding error is pattern completion.** One example from the store: Nano wrote `set_dep_th_flag_4` where the real id is `set_dep-th_flag_4`. Four of its six visible neighbours used underscores, and it carried that spelling over.
- **The belief error is over-extending observation.** It assumes a person is "still watching" after the last cue that they saw something (F14; 55 resample-stable cases; the `witness_cutoff` sweep reproduces it).
- **Abandonment.** In Province III Nano stops before the goal, often with no error and sometimes after zero calls (F25, reproduced).
- **Consistency gap.** Nano solves a given world on 63% of single tries, but on all 5 tries for only 1 of 6 worlds (F22; a probe outside the store, grade C).

### 2.2 Nemotron 3 Super (120B-A12B)
*Verdict: breaks on long plans with look-alike tool names (24-call plans). Holds on 11 of 12 capabilities tested.*

| Capability | Status | Evidence | Grade | Main cause |
|---|---|---|---|---|
| State tracking | HOLDS | 91/95 | B | — |
| Event order | HOLDS | 79/80 | B | — |
| Belief | HOLDS | 283/291 (97%) | A | — |
| Adversarial belief | HOLDS | 152/160 (95%) | A | **abstains** ("unknown") 88% of failures (F15) |
| Plans, clear names | HOLDS | 28/28 through 12-call plans | B | — |
| Plans, look-alike names | **BREAKS at 24-call plans** | 8/28 (6/7 at 11 calls; CI [0.15, 0.47]; p = 1.5e-7) | A | `unknown_tool` 96% |
| Structural traps | HOLDS | 51/52, including 24/24 with all 5 traps | B | — |
| Catalog size | HOLDS | 112/112 through 66 tools, including dishonest decoys | A | — |
| Impossible tasks | HOLDS (grade C) | declines 10/12 | C | — |
| Step by step + values | HOLDS | 53/53 through 19-call plans, 16 values | A | — |
| Step by step, look-alike names | HOLDS | 43/47 through 25-call plans | B | — |
| Fault recovery | HOLDS | 30/30 (33/33 after the fault fired) | B | — |

**Notes**
- Super *degrades* from 14-call look-alike plans (38/60 at ≥ 14 calls vs 28/28 below; p = 4.7e-5; reproduced, F5), well before it *breaks* at 24.
- **Hand-built probes, 7 families:** goal already met, impossible goal, prompt injection inside a tool description, confusable twin tool, starved budget. Super passed all 7.
  - It resisted the injected instruction 5/5 (F10, one run).
  - Its lone failure did not replicate: 40/40 on 20 seeds × 2 conditions.
- **Prose rendered by Nemotron** (instead of templates) did not degrade Super: 34/35, with 11 renderings rejected by the fidelity gate (F18, grade C).

### 2.3 Nemotron 3 Ultra (550B-A55B)
*Verdict: slips on long plans with look-alike tool names (25-call plans) and on spotting impossible tasks. Holds on 6 of 8 capabilities tested.*

| Capability | Status | Evidence | Grade |
|---|---|---|---|
| State / order | HOLDS | 30/30, 80/80 | B |
| Belief / adversarial belief | HOLDS | 181/183 (99%), 127/128 | A |
| Plans, clear names | HOLDS | 28/28 through 12-call plans | B |
| Plans, look-alike names | **DEGRADES at 25-call plans** | 4/7 at 25 calls vs 7/7 at 21 (p = 0.0004); 4/7 at 29, 3/7 at 31; never ≤ 30% | A |
| Structural traps | HOLDS | 51/56; 23/28 with all 5 traps | B |
| Impossible tasks | DEGRADES (grade C) | declines 5/8 | C |
| Province III | **not measured** | every episode hit the 16k per-turn cap (E22); the loop now retries at 32k | — |
| Province IV | not run | — | — |

**Notes**
- **Deterministic mis-binding.** Ultra's trap-ray failures are mis-bound ids at step 8–9 of 9-call plans; 3 of 5 were stable on resample, 3/3 each (F4).
- **A dip that was noise.** Ultra's dip at 16–18-call plans was confirmed as noise (fresh top-up 6/7; permutation p = 0.121; F8).
- **Ultra's real limit is beyond the generator.** Finding where it collapses needs plans longer than 31 calls. BFS ground truth costs about 8 s per task at 31 calls.

### 2.4 Gemma 3 27B-it (Google, non-reasoning; mapped 3 Oct, all four provinces, $1.08)
*Verdict: breaks on look-alike tool names (17-call plans), on trap-laden catalogs (4 of 5 traps active) and on spotting impossible tasks. Slips on state tracking, belief, misleading stories, step-by-step execution with look-alike names (11-call plans) and semantic traps in code. Holds on 6 of 14 capabilities tested.*

| Province | Capability | Status | Evidence | Grade | Main cause |
|---|---|---|---|---|---|
| I | State tracking | **DEGRADES** | 26/46 (57%), CI [0.42, 0.70]; 18 of 20 failures at chain length ≥ 23; **all 20 STABLE on resample** | B | wrong location |
| I | Event order | HOLDS | 79/80 | B | — |
| I | Belief | **DEGRADES** | 80/162 (49%) | A | `final_true_state` 79% |
| I | Adversarial belief | **DEGRADES** | 62/145 (43%) | A | `final_true_state` 89% |
| II | Plans, clear names | HOLDS | 34/42 through 13-call plans | B | `precondition_violation` (8 failures) |
| II | Plans, look-alike names | **BREAKS at 17-call plans** | 0/7; 1/7 at 18 (already 7/14 at 7 calls) | B | `unknown_tool` 92% |
| II | Structural traps | **BREAKS at 4 of 5 traps** | 4/35 (20/21 at 1 trap; p = 7e-16) | A | `precondition_violation` 77% |
| II | Catalog size | HOLDS | 46/56 through 66 tools (noisy: 4/7 at 34 and 43 tools, 7/7 at 52 and 60) | B | — |
| II | Impossible tasks | **BREAKS** | declines **0/16**; correct on 22/24 possible tasks; balanced accuracy 0.46 | C | writes a plan for every impossible goal |
| III | Step by step + values (text protocol) | HOLDS | 40/40 through 19-call plans, 16 values | A | — |
| III | Fault recovery (text protocol) | HOLDS | 40/40 | A | — |
| III | Step by step, look-alike names (text protocol) | DEGRADES at 11-call plans | 3/5 (5/5 at 7); 60–80% through 25 calls | C | `unknown_tool` 62% |
| IV | Look-alike variable names | HOLDS | 25/28 through 42-statement programs; **0** `confused_identifier` failures | B | `wrong_value` (3) |
| IV | Semantic traps | **DEGRADES** | 26/49 at 5 of 6 traps, 18/42 at 6 of 6, 41/42 at 0 (p = 2e-9) | A | copied-list misreading 38% |
| IV | Length-only control | slips, never breaks | 7/7 → 5/7 → 3/7 → 5/7 from about 38 statements | — | — |

**Province IV trap decomposition for Gemma**

Each probe below switches *one* trap off with the other five still on:

| Trap switched off | Pass |
|---|---|
| aliasing | **5/7** |
| mutable default | 5/7 |
| negative modulo | 5/7 |
| shadowing | 8/14 |
| late binding | 2/7 |
| inclusive range | 1/7 |
| (all six on) | 18/42 |

Failure signatures across the run:
- `foil:value_semantics` (believes `b = a` copies a list): **18**
- `foil:truncating_modulo` (believes `-7 % 3 == -1`): **13**
- `wrong_value` (matches no known misreading): 8
- `foil:early_binding`: 3
- `foil:fresh_default`: 3
- `foil:flat_scope`: 2
- `foil:loop_once`: 1

No single trap is *necessary* for Gemma to fail. Aliasing and negative modulo carry most of it, and switching off late binding or the inclusive-range trap changes almost nothing.

---

## 3. Cross-model matrix

| Capability | Nano | Super | Ultra | Gemma 3 27B |
|---|---|---|---|---|
| State tracking | holds 100% | holds 96% | holds 100% | **degrades 57%** |
| Event order | holds 99% | holds 99% | holds 100% | holds 99% |
| Belief | degrades 90% | holds 97% | holds 99% | **degrades 49%** |
| Adversarial belief | degrades 88% | holds 95% | holds 99% | **degrades 43%** |
| Plans, clear names | holds to 13 | holds to 12 | holds to 12 | holds to 13 |
| Plans, look-alike names | **breaks at 13** | **breaks at 24** | degrades from 25 | **breaks at 17** |
| Structural traps | degrades (4/5 traps) | holds (24/24 at 5) | holds (23/28 at 5) | **breaks (4/5 traps)** |
| Catalog size (66 tools) | — | holds 112/112 | — | holds 46/56 |
| Impossible: declines | 6/12 | 10/12 | 5/8 | **0/16** |
| Step by step + values | holds 43/45 | holds 53/53 | not measured | holds 40/40 |
| Step by step, look-alike names | **breaks at 11** (gives up) | holds to 25 | not measured | degrades from 11 |
| Fault recovery | 20/21 after the fault | 33/33 | not measured | 40/40 |
| Code: look-alike variables | — | — | — | holds to 42 statements |
| Code: semantic traps | — | — | — | **degrades (18/42 all on)** |

Two patterns stand out:
- **Belief tracks model size cleanly** (Ultra > Super > Nano), and Gemma sits well below Nano.
- **Look-alike names in a tool catalog** are the stressor every model breaks on eventually. The order of the boundaries follows model size within the Nemotron family, and Gemma sits between Nano and Super.

---

## 4. By province: mechanisms in detail

### 4.1 Province I — narrative world-model
- **Difficulty is structure, not size.** Raising scalar knobs (longer chains, more distractors, more pronouns) saturated every Nemotron tier. Ultra held ≥ 86% even with every knob at its ceiling: chain 25, 40 distractors, coreference 8, 60 filler events, witness cutoff 0.9.
  - Real failures came from *traps*, where a strong prior conflicts with the right answer.
  - 77% of the post-fix belief failures that could be reconstructed exactly were one error: answering where the object *really* is.
  - That led to foil-salience search, which selects stories that maximise the gap between a shallow reasoner's answer and the truth.
- **Belief→reality collapse** (`final_true_state`) is the dominant belief error on every model:
  - Nano: 63% of belief failures, 87% of adversarial failures.
  - Gemma: 79% and 89%.
  - Ultra and Super rarely fail; Super's failures under trap pressure are mostly *abstentions* rather than wrong locations (F15).
- **Event order holds on every model** (≥ 99%), even with fully scrambled narration.
- **About 1 in 3 single-trial narrative failures is sampling noise.** 95 failures were resample-checked; 65 were STABLE (68%) and 30 were NOISE (32%). That's why the resample check runs before any narrative failure is reported. Gemma's 20 state failures were 20/20 STABLE.

### 4.2 Province II — single-shot tool planning
- **Look-alike tool ids are the stressor.**
  - With them, the model calls a tool that doesn't exist, almost always a one-character variant of a real id (`set_dep_th_flag_6` for `set_dep-th_flag_6`). 92–100% of failures on every model.
  - Without them, every model holds through the tested range.
- **Structural traps**
  - Five traps are switched on together: a poisoned tool, decoys rewritten to sound real, preconditions hidden in one general paragraph, zero slack, and id collisions.
  - Nano and Gemma degrade or break; Super and Ultra hold.
  - The failure code changes to `precondition_violation` (72–77%): the model calls a real tool before the step it depends on.
- **Catalog size** up to 66 tools, including decoys that look real, does **not** stress Super (112/112) and stresses Gemma only noisily.
  - The first catalog sweep was an invalid test: the decoys described themselves as having "no effect". It was redone with dishonest decoys, with the same result.
- **Impossible tasks** (one load-bearing tool removed):
  - Only Super declines reliably (10/12). Ultra declines 5/8, Nano 6/12, Gemma **0/16**.
  - Where checked, every fabricated plan **silently skipped the missing step** (11/11, F21).
  - No model ever declined a *possible* task. Gemma solved 22/24 possible tasks, so its failure is specific: it plans without checking feasibility.
- **Nemotron as an adversarial proposer** of trap configurations found one 75%-fail configuration on Nano, with the hypothesised mechanism confirmed. Its edge over random configurations is weak (0.24 vs 0.20; F19, grade C).

### 4.3 Province III — interactive execution
- **Value passing** (an unguessable ticket returned by one call and needed by a later one) never broke anything. Across 136 scored episodes on three models there were zero invalid tickets at the hardest setting. Nano carried a ticket returned at step 7 and first used at step 11.
- **Feedback rescues binding for the larger models:**
  - **Super:** 43/47 through 25 calls step by step, against 15/42 at 23–25 calls single-shot. It recovered from 9 of 12, then 11 of 12, errored episodes.
  - **Gemma:** 12/17 at ≥ 18 calls step by step, against 1/14 single-shot.
- **Nano's interactive limit is persistence.** It stops early; 9 of 17 early stops had no error, and 3 stopped before making a single call. Its binding *does* improve with feedback (it recovered from 7 of 15 errored episodes), but it gives up first.
- **Transient faults** are retried correctly by every model measured.
- **Measurement notes**
  - Gemma was measured through the **text tool-call protocol**. Nebius drops the `tools` field for Gemma, so the catalog is written into the prompt and calls are parsed from ```` ```tool_code ```` blocks. Nemotron used native function calling. The two are the same task under different protocols and are labelled per record (`protocol=text|native`).
  - Ultra produced no measurement: the per-turn 16k cap truncated all 7 started episodes. The loop now retries at 32k.

### 4.4 Province IV — code tracing (first live data: Gemma only)
- **The interpreter matches CPython** on 5,000 generated programs (0 mismatches). A synthetic set of fake models, each with one planted misunderstanding, is attributed to the right trap. That validation came first, and the instrument has only been run live on Gemma.
- **H1** (the identifier-binding weakness generalises from tool names to variable names): **not supported for Gemma.** There were zero `confused_identifier` failures with look-alike variable names, while the same model breaks at 17-call plans on look-alike tool names.
- **H2** (traps break models, length does not): **supported for Gemma.** 41/42 with no traps vs 18/42 with all six (p ≈ 1e-8). The length-only control slips from about 38 statements (3/7 to 5/7) but never breaks.
- **Which semantics Gemma gets wrong:** list aliasing (`b = a` read as a copy) and floor division/modulo on negative numbers. It mostly gets right closures binding late, `range` excluding its end, default arguments being shared, and function locals shadowing globals.

---

## 5. Findings register

### 5.1 Nemotron findings (F1–F27, from the ledger)

| # | Finding | Grade | RS | FS | IM | ST | Reproduced? |
|---|---|---|---|---|---|---|---|
| F1 | Nano breaks on long plans with look-alike tool names (~13 calls) | A | — | ✓ deep ray, new seeds | ✓ trap ray + proposer, same mechanism | ✓ p = 0.0006 | **Yes** |
| F2 | For Nano, depth × look-alike names is multiplicative (14/14, 13/21, 5/21, 0/7) | A− | — | — | — | ✓ decompose probes | No |
| F3 | Look-alike tool-name binding (`unknown_tool`) is the dominant tool failure on every tier | A | — | ✓ | ✓ 6 experiments | — | **Yes** (now also Gemma) |
| F4 | Ultra mis-binds ids deterministically at step 8–9 of 9-call plans | A− | ✓ 3/5 stable | — | — | — | **Yes** (n = 3) |
| F5 | Super degrades from 14-call plans with look-alike names | A | — | ✓ k = 15 top-up + deep ray | — | ✓ p = 4.7e-5 | **Yes** |
| F6 | Super breaks at about 24-call plans | A | — | partial | — | ✓ | Not independently |
| F7 | Ultra degrades from 25-call plans, no collapse within 31 calls | A | — | — | — | ✓ p = 0.004 | No |
| F8 | Ultra's dip at 16–18-call plans is noise | A | — | ✓ 6/7 | — | ✓ p = 0.121 | **Yes** (as noise) |
| F9 | Super is insensitive to catalog size up to 66 tools | A | — | ✓ two runs | — | — | **Yes** |
| F10 | Super resists an instruction injected into a tool description | B | — | — | — | — | No (5/5, one run) |
| F11 | Super judges feasibility correctly (impossible, starved budget, goal already met) | A− | ✓ 4/5 → 40/40 | — | — | — | Partly |
| F12 | Belief orders Ultra > Super > Nano | A | ✓ | ✓ | — | — | **Yes** |
| F13 | State and order are solved by all Nemotron tiers in range | A | — | ✓ | — | — | **Yes** |
| F14 | Nano's belief failures over-extend "still watching" past the last witness cue | B | ✓ 55 stable | — | ✓ witness_cutoff sweep | — | **Yes** |
| F15 | Super abstains ("unknown") under trap pressure | A− | ✓ 4 of 7 stable | — | — | — | **Yes** |
| F16 | Ultra protagonist-continuity pronoun bias | C | — | — | — | — | No (evidence lost) |
| F17 | Short chains are harder for Ultra under traps | C | — | ✗ 127/128 | — | — | **Did not replicate** |
| F18 | Nemotron-written prose does not degrade Super | C | — | — | — | — | No |
| F19 | Nemotron's proposer finds traps better than chance | C | — | — | — | weak | No |
| F20 | Nano breaks on the structural-trap ray | A | — | ✓ re-run, location corrected 0.55 → 0.675 | — | ✓ | **Yes** |
| F21 | On impossible tasks, models skip the missing step instead of declining (11/11) | B | — | — | ✓ all 3 tiers | — | No |
| F22 | Nano consistency gap (63% single-try vs 17% on all 5 tries) | C | — | — | — | — | No |
| F23 | Value passing is not a bottleneck up to 19-call plans | A− | — | ✓ 8/8, 7/8, 0 bad tickets | — | — | **Yes** (Gemma 40/40 agrees) |
| F24 | Super recovers from binding errors when it sees feedback | A− | — | ✓ 17/17 | ✓ 11 of 12 recovered at longer plans | ✓ p = 0.023 / 0.001 pooled | **Yes** |
| F25 | Nano abandons long interactive tasks early | A− | — | ✓ 10/15, all failures early stops | ✓ also in the fault run | — | **Yes** |
| F26 | Nano and Super recover from transient tool failures | B | — | — | — | — | No (Gemma 40/40 agrees) |
| F27 | With feedback, Super holds past its single-shot frontier (13/14 vs 15/42 at 23–25 calls) | B | — | — | — | ✓ p = 0.0002 | No |

### 5.2 Gemma 3 27B findings (G1–G9, all from one run each, E24)

| # | Finding | Evidence | Grade | Reproduced? |
|---|---|---|---|---|
| G1 | Breaks on long plans with look-alike tool names at 17-call plans | 0/7 at 17, 1/7 at 18; `unknown_tool` 92% | B | No |
| G2 | Breaks on structural traps at 4 of 5 traps | 4/35 vs 20/21 at 1 trap, p = 7e-16 | A | No |
| G3 | Never declines an impossible task (0/16), while solving 22/24 possible ones | balanced accuracy 0.46 | C | No |
| G4 | State tracking degrades at long chains (57% overall; 18 of 20 failures at chain ≥ 23) | all 20 failures STABLE on resample | B | **Yes, RS only** |
| G5 | Belief collapses toward the true location (49%; 79–89% of failures are `final_true_state`) | 80/162, 62/145 | A | No |
| G6 | Value passing and fault recovery hold step by step (text protocol) | 40/40 each | A | No (agrees with F23/F26) |
| G7 | Feedback rescues look-alike binding: 12/17 step by step at ≥ 18 calls vs 1/14 single-shot at ≥ 17 | p = 0.0004 | B | No (agrees with F24) |
| G8 | Look-alike *variable* names do not confuse it in code (0 `confused_identifier` failures, 25/28 to 42 statements) | — | B | No |
| G9 | Semantic code traps break it, mostly via list aliasing and negative modulo | 18/42 all on vs 41/42 none; 31 of 48 failures | A | No |

### 5.3 Retractions and corrections (all published in the lab log)

| # | Original claim | What actually happened |
|---|---|---|
| X1 | Super breaks at t\* = 0.883 on the depth ray | The token cap truncated valid answers; replaying on the same tasks, 6 of 10 were passes. Retracted; caps raised to 16k/32k |
| X2 | Super has an intrinsic false-belief floor | Witness-rendering bug: witnesses never reached the prompt. All pre-fix belief results invalidated |
| X3 | Ultra abstains under trap pressure | Token truncation; the real abstention is Super's (F15) |
| X4 | The "Priya" continuity failure appears on all 3 tiers | 4 of 5 resamples were correct: noise |
| X5 | Nano's depth-ray t\* "replicates" its trap-ray t\* | t is relative to its ray, so the numbers are not comparable; only the mechanism replicates |
| X6 | Nano trap-ray t\* = 0.55 | Also biased by the cap; re-run gives 0.675 |
| X7 | Nano interactive "drop to 2/5" at its ceiling | 3 trials never ran (cost cap); caught before reporting, and the escalation code now discards cut-short probes |
| X8 | (never claimed) Ultra interactive "0/10" | Every episode was truncated by the per-turn cap; recorded as no measurement |
| X9 | (never claimed) Gemma Province III "0 calls, gives up" | Nebius drops `tools` for Gemma; the text tool-call protocol was built before measuring |
| X10 | F-G4Q-1: Gemma 4 QAT fabricates a pattern-plausible ticket instead of copying the opaque one (fresh-seed replicated), and the 6/6 restatement rescue | Protocol artefact. The v2 text protocol executed every call in a message, and Gemma wrote whole trajectories, tickets included, before any result (median 2 turns). Under protocol v3 (one call per turn): 33/33, 31/31 and 50/50, zero invalid tickets. Retracted as stated (2026-10-10); the surviving finding is blind-trajectory planning. `docs/reports/iii-v3-ticket-remeasure-2026-10-10.md` |

### 5.4 Totals
- **Nemotron: 27 findings.**
  - 14 reproduced: F1, F3, F4, F5, F8, F9, F12, F13, F14, F15, F20, F23, F24, F25.
  - 1 partly reproduced: F11.
  - 12 not yet reproduced; one of those (F17) was re-tested and did **not** replicate.
- **Gemma: 9 findings.** Only G4 has a reproducibility check (RS). The rest are single runs that agree in mechanism with reproduced Nemotron findings.
- **9 retractions or corrections**, all caught by the project's own checks before or shortly after reporting.
- **Resample coverage.** Of the failures counted as evidence before today, 21% were resample-checked: 68% STABLE, 32% NOISE. Tool-province and code failures are not yet routed through the automatic resample gate. That is the largest reproducibility gap.

---

## 6. Methodology lessons the data forced

1. **Never set `max_tokens` below the answer's natural length.**
   - A truncated answer becomes a failure, and the truncation rate rises with difficulty, so it fakes a breaking point.
   - Defaults are now 16k with a 32k retry.
   - The interactive loop retries a truncated turn too.
   - `finish_reason=length` is always ERROR, never FAIL.
2. **Resample before reporting a narrative failure.** Roughly a third are noise on these MoE models, even at temperature 0.
3. **Discard cut-short probes.** A probe partly blocked by a cost cap scores the trials that never ran as failures. `find_breaking_point` now discards such probes and says so in its note.
4. **Boundaries must be sustained.** One noisy dip is not a frontier. The level and everything above it must stay low.
5. **Compare like with like.**
   - A t-value belongs to its ray.
   - Interactive vs single-shot comparisons are made at matched plan lengths.
   - Text-protocol vs native results are labelled per record.
6. **Check what the model actually sees.** A provider can silently drop request fields. Gemma never saw its tools until the catalog was written into the prompt.
7. **Validate on planted fake models before spending.** Each province's fake models must break on exactly their planted weakness, be attributed correctly, and leave the perfect fake unbroken. Province IV ran zero live trials until this passed.

---

## 7. What is not known yet

- **Ultra in Provinces III and IV.** III is fixed but not re-run, at roughly $3–5 for 10 episodes. IV has never been run.
- **Nemotron in Province IV.** Do the H1/H2 results for Gemma hold for Nano and Super? Planned at about $0.70 for H1 and $0.70 for H2 on Nano and Super.
- **Where Ultra and Super actually collapse.**
  - Ultra never collapses within 31-call plans single-shot.
  - Super step by step holds through 25 calls, where the run stopped on its cost cap.
  - Both need a longer-plan generator.
- **Fresh-seed replication** of G1–G9, F2, F6, F7, F21, F26 and F27.
- **Consistency (pass^k)** has no campaign yet; it was measured once outside the store.
- **Native vs text-protocol comparison** on a model that supports both, to measure how much the protocol itself changes Province III results.
- **The resample gate for tool and code failures**, which so far exist only as single trials unless checked by hand.

---

## 8. Data and code pointers

**Run ids**
- Nemotron: `camp-tool-depth-*-003`, `camp-tool-deep-*-001`, `camp-tool_bp-*`, `camp-interactive-*`, `camp-full_map-*`, `camp-belief-*-002`, `camp-irrelevance-*`, `camp-tool-count|confuse-super-001`.
- Gemma: `camp-*-gemma-*` (12 stages; see E24 in the ledger).

**Living summaries**
- `docs/frontier-summary.md` and `.json`: per model × capability. Regenerate with `python3 scripts/frontier_summary.py`.
- `docs/experiments-ledger.md`: E1–E24, F1–F27, X1–X8.
- `docs/lab-log.md`: the dated narrative, including every retraction.

**Code**
- **Generators:** `cartographer/world` (I), `toolworld` (II), `interactive` (III), `codeworld` (IV).
- **Escalation and decompose:** `cartographer/schedule/escalate.py`, `cartographer/decompose`.
- **Province IV spec:** `docs/province-iv-spec.md`.
- **One-command profile of any Nebius model:** `scripts/run_all_provinces.py --model <id> --label <tag> [--smoke] [--text-tools]`.

**Viewer:** run `modelcart`, then open http://127.0.0.1:8788/. It shows the capability map and a discovery page per finding.
