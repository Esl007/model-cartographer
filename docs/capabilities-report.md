# Model Cartographer — Capabilities, Evidence, and Gaps

*Technical status report · compiled 2 Oct 2026 · every number below was re-queried from `results/campaign.sqlite` or the source tree when this was written, not recalled from memory.*

---

## 0. TL;DR

**What it is.** An instrument that measures *where* an LLM's reasoning breaks and *why*. It generates synthetic worlds, asks the model a question about them, and grades the answer by **executing the world** (simulation / bounded BFS). An LLM never acts as judge. It then walks a difficulty ray, bisects to the failure frontier, decomposes the frontier into causal factors, and confirms on fresh seeds.

**What it has proven (high confidence)**
1. **Nemotron Nano has a hard, sharp tool-planning frontier.** It goes from 100% pass to 0/7 between plan depth 9 and 11 once identifier collisions are present. Decomposition shows the two factors are **multiplicative**:
   - depth alone: 14/14 pass
   - collisions alone: 13/21
   - both together: 5/21, then 0/7 one step deeper.
2. **The dominant tool-calling failure on every tier is identifier binding, not plan structure.** 125 of 178 tool-province failures are `unknown_tool`: the model calls a mangled name of a real tool (`set_dep_th_flag_4` for `set_depth_flag_4`). Ultra shows the deep version: **deterministic** (3/3 resample) mis-binding at step 8–9 of 9-call plans whose structure was otherwise correct.
3. **Super and Ultra have no locatable frontier inside the generator's range** (plan depth ≤ 16). Super degrades into a 57–86% band at depth ≥ 11; Ultra wobbles and recovers to 7/7 at depth 16. The Ultra dip tests as noise (exact permutation p = 0.121).
4. **Super is robust on every other axis probed.** All of these were deterministic hand-built probes, about 170 trials in total:
   - catalog size up to 66 tools, with honest or plausible decoys: 56/56 each
   - goal already satisfied: 40/40
   - impossible task: declines 5/5
   - instruction injected into a tool description: resisted 5/5
   - confusable twin tool: 5/5
   - budget starved: 5/5
5. **Narrative world-model:** state tracking and chronology are essentially solved by all three tiers in range. Belief (theory-of-mind) tracking orders the tiers cleanly: Ultra > Super > Nano. There are four named belief failure modes, three of them resample-verified.
6. **The instrument catches its own errors.** One headline result (Super t* = 0.883) was retracted after the team found the token cap was turning truncated-but-correct answers into "failures". Several single-trial "findings" were killed by the resample gate.

**Three problems found while compiling this report (not previously logged)**
- **(P1) The evidence layer is contaminated by witness-bug-era data.** `/findings`, `/report` and the Evidence Analyst's pack still include 479 belief trials recorded *before* the witness-rendering fix (09-17 16:06 UTC). The pack reports **Super belief = 78.4%**; the clean post-fix figure is **97.3%** (campaign trials only). The Analyst's published diagnosis therefore ranked Super *below* Nano, which is wrong. All of gpt-oss-120b's belief data (52.8%) predates the fix and is invalid.
- **(P2) "Cross-ray replication" was overstated.** Earlier reports (survey page, brand board, my messages) said Nano's depth-ray t* = 0.500 "replicates" the trap-ray t* = 0.55 because the confidence interval contains it. **That comparison is invalid.** `t` is a *normalised position on each ray*, and the two rays cross different difficulty spaces, so t* values from different rays are not the same quantity. What does replicate is the **mechanism**: both rays, and the independent Nemotron proposer, implicate identifier collisions. The numeric coincidence means nothing.
- **(P3) Nano's trap-ray t* = 0.55 was measured under the same token-cap artifact that killed the Super result.** That run (`camp-tool_bp-nano-001`) had 10/154 cap-hit ERRORs, and the escalation probe counts any non-PASS as a failure. The depth-ray result (0 errors, caps lifted) is clean; the trap-ray number needs a re-run with lifted caps before it can be cited.

**What has *not* been tested at all:**
- multi-turn tool use with execution feedback and error recovery (the regimes the literature weights most heavily)
- native function-calling APIs (we grade prompt-emitted JSON, not the `tools` parameter)
- consistency across repeated attempts (pass^k)
- argument-value reasoning and data passed between calls
- missing-parameter handling
- any clean non-NVIDIA comparison model
- the irrelevance campaign, which is built and tested but **has never been run live**

---

## UPDATE — 2 Oct 2026 (later the same day): fixes landed, new runs, next province, UX

### What changed since the report below was written
| Item | Status |
|---|---|
| **P1** evidence contamination | **Fixed** (`aedb57c`). `INVALIDATED_RUNS` excludes the two pre-fix belief runs. Pack now: Ultra 97.3% · Super 95.9% · Nano 86.7% overall; Super belief 97.6% (was 78.4%). The adversarial column is now populated (Nano 91.2%, Super 96.2%, Ultra 99.2%). The renderer row is gone. |
| **D2** token caps | **Fixed.** Default 16000/32000 on every tier; regression test pins it above 8,456. |
| **P2** cross-ray wording | **Corrected** in the lab log, the survey page and the v1 brand boards. Boards made by the other UI thread (canvas pages v2–v6) were not audited. |
| **P3** Nano trap-ray t* | **Re-run with lifted caps: t* = 0.675 [0.615, 0.758]** (was 0.55, cap-biased by ~0.125). Break confirmed; the old location was wrong. |
| **F5** Super degradation | **Upgraded to A.** k = 15 per depth: depth ≤ 9 28/28 → depth ≥ 11 38/60 = 63% [0.51, 0.74], Fisher p = 4.7e-05. Real degradation, still no collapse below 30%. |
| Tests | **680 passing** |
| Spend this round | **$0.21 tracked** |

### New findings
- **F18 — Impossible-task detection (irrelevance), first live run.** Invents a plan for an impossible goal: Nano 50% (6/12), Ultra 38% (3/8), Super 17% (2/12). No tier ever declined a solvable task. **Mechanism (11/11 cases, all tiers): silent step omission.** The model never names a nonexistent tool. It drops the step whose tool is missing and emits the rest of the plan as if the goal were still reachable. Grade B (small n; the Super-vs-Ultra ordering is not significant).
- **F19 — Consistency gap.** Nano at temperature 0.6, 6 worlds × 5 samples: pass@1 63%, **pass^5 17%**, any-of-5 100%. Every world is solvable; one in six is solved reliably. Grade C (feasibility probe; small n, one tier).
- **F20 — Feedback helps the weak tier.** Native function calling plus a step-by-step loop with errors returned: Super 4/4 at optimal length; Nano 3/4, recovering from 2 of 3 errors by trying a different spelling of the tool name. The one failure gave up after a single error. Grade C (feasibility probe).
- **Feasibility verdicts:** native function calling **works**; multi-turn with feedback **works**; error recovery is **measurable**; consistency is **measurable**; cross-model is **ready**, with 10 families on Nebius, but **pricing entries must be added first or cost caps will not bind**; value passing **needs a generator change**.

### Next province: how the six suggestions fit
They are not six provinces. Four of them are one thing, and two are measurement modes that apply to every province.

**Province III — Interactive tool use (agentic loop).** It merges *multi-turn*, *native function calling*, *error recovery* and *value passing*.
- **Transport:** the tools go through the API `tools` parameter. Each `tool_call` runs in the existing simulator, and the result or error goes back as a `tool` message. This loop already exists as the feasibility script.
- **Ground truth:** unchanged. Bounded BFS over the same state machine, so it reuses 100% of `toolworld/` (generator, all 7 mutators, verifier codes).
- **New metrics:** goal reached; calls over optimal; **error-recovery rate** (fraction of failed calls followed by a successful correction); **give-up rate** (stops after an error); repeated-identical-error rate; budget exhaustion.
- **New difficulty dims:**
  - `transient_failures`: a tool fails once with a retryable error and must be retried.
  - `value_passing`: build steps return generated ids, e.g. `art_7f3a`, that later calls must thread through. This tests carrying information between calls, which nothing measures today.
  - `partial_observability`: state is hidden behind `inspect_*` tools, so the model must look before it acts.
  - `misleading_errors`: an error message names the wrong cause.
- **Why it is the right next province:** published benchmarks weight agentic and multi-turn use at about 70% of the score (BFCL v4). Our dominant single-shot failure, identifier binding, is exactly the kind a feedback loop may or may not fix. The probe already shows Nano fixing a misspelled tool name after an error message. "Which failures can the model repair after feedback, and which can it not" is a new, publishable capability axis.

**Cross-cutting mode A — Consistency (pass^k).** A `k` setting on any campaign: every world is sampled k times, and the report shows pass@1, pass^k and any-of-k. Cheap to build, and it changes the headline. A model that solves a task 63% of the time but reliably only 17% of the time is a different model for deployment.

**Cross-cutting mode B — Cross-model matrix.** Any campaign runs over a list of models. A natural pairing is Nemotron Nano 30B-A3B vs Qwen3-30B-A3B (same active-parameter class), plus one frontier open model per family (DeepSeek-V4, GLM-5.3, Kimi-K3). Blocker: pricing entries in `exec/pricing.py` (unknown models cost-cap at $0, which is unsafe).

**Suggested build order:** pricing + cross-model mode → consistency mode → Province III (native + multi-turn + recovery) → value passing / partial observability.

### UX: why the platform does not answer "where does it break"
The interface is organised around how the instrument works (runs, campaigns, records, cells, dimension terciles, t on a ray) rather than around the question a user brings: *where does this model stop working, and why?* Specific failures:
1. **No verdict object exists in the data layer.** Nothing computes "model × capability → holds / degrades at X / breaks at Y, confidence, one example". Every page assembles fragments, so the user does the synthesis.
2. **Frontiers are in instrument units.** "t* = 0.675" means nothing to a reader; "breaks once plans exceed ~12 steps with look-alike tool names, all traps on" does.
3. **Evidence and conclusion are mixed at equal weight.** Thousands of records sit beside the three sentences that matter.
4. **Untested and passed look alike.** An empty cell does not say "never measured".
5. **No comparison on one axis.** Tiers cannot be read side by side on the same difficulty scale.

**Proposed fix: a "Frontier Card" per model, backed by a new deterministic `frontier_summary(store, model)`.**
- For each capability, the function returns:
  - status: `HOLDS` / `DEGRADES` / `BREAKS` / `NOT TESTED`
  - the boundary in native units (steps, tools, chain length, % impossible tasks)
  - confidence grade A/B/C
  - the cause, from decomposition
  - one exemplar failure: what was asked, what the model did, the exact step where it went wrong
  - links to the evidence
- **Card layout:** one sentence per model at the top. Below it, one row per capability with a single horizontal bar showing safe zone → degrades → breaks on that capability's own axis. Clicking a row opens the exemplar and the cause. A compare toggle stacks the tiers on the same bars. Runs and records move into an evidence drawer.
- **Scope:** the summary function is capability/data work and fits this thread. The card is UI and belongs to the other thread. The function would hand that thread one clean object to render.

### Deployment (Vercel)
There is no deployment today; the repo has no Vercel or other hosting config. The viewer is a Python stdlib server over a local SQLite file, and its launch page spends API credits with a local key, so it must not be deployed as is. Recommended: deploy a **read-only results explorer** before submission (judges need a URL): a static export, or a read-only serverless build with a snapshot of the store, with no launch or campaign features and no keys. Keep it behind Vercel password or preview protection until submission day, then open it.

---

## 1. Architecture

```
            ┌──────────────── deterministic, $0 ────────────────┐
DimParams ─▶│ generate(params, seed) ─▶ World ─▶ render() ─▶ prompt│──▶ LLM (Nebius) ──▶ response
 + seed     │        │                                            │                       │
            │        └──▶ simulate()/BFS ─▶ ground truth ─────────┼──▶ verify() ◀──────────┘
            └────────────────────────────────────────────────────┘        │
                                                                    PASS / FAIL / ERROR
                                                                           │
                         append-only ResultsStore (SQLite)  ◀──────────────┘
                                   │
   scheduler / escalation ◀────────┤  (chooses the next config from accumulated verdicts)
   decompose ─ confirm ─ resample ─ ablate ─ verdict ─ analyst
```

**Core invariants. These are why the numbers can be trusted.**

| Invariant | Mechanism | Where |
|---|---|---|
| Ground truth is never an LLM | world simulation (narrative) / bounded BFS over state space (tool) | `world/simulate.py`, `toolworld/simulate.py` |
| Full determinism | all randomness from sha256 streams over stable strings; never `random` | every `generate.py`, `mutate.py` |
| Provenance | each record stores `latent_params`, `task_seed`, `trial_seed`, `rendering_seed`, full prompt + response + reasoning; a world rebuilds byte-identically | `store/schema.py` |
| Append-only evidence | `ResultsStore.append`; readers open `mode=ro` | `store/store.py` |
| Noise gate | FAILs are resampled against the byte-identical stored prompt before being believed | `ablate/resample.py`, `run_verify_failures` |
| Fresh-seed confirmation | search seeds < 10,000; confirmation seeds ≥ 10,000 (design contract, not enforced at runtime) | `confirm/confirmation.py` |
| Contamination guards | proposer ≠ subject; renderer ≠ subject; extractor ≠ renderer (checked before any API call) | `propose/loop.py`, `llmrender/gate.py` |
| Cost caps | every evaluator tracks spend and stops at `cost_cap_usd` (checked *between* trials) | `exec/evaluator.py` |

**Codebase:** 76 Python modules, ~22.5k lines, stdlib-only statistics (no numpy/scipy). Tests: see §9.

---

## 2. Province I — Narrative world-model

A timestamped event log of agents moving, transferring and talking about objects across locations, with explicit witnesses. The model answers one of three query kinds.

| Query | Faculty isolated | Correct answer computed by |
|---|---|---|
| `state` | where the object actually is (forward replay) | `simulate.answer` |
| `order` | which of two events happened first, from scrambled narration | event timestamps |
| `belief` | where a specific agent *believes* the object is, given only what they witnessed | per-agent witnessed-event replay |

**Difficulty dimensions (`world/generate.py::DimParams`)**

| Dim | Range (validated ceiling) | Pressure it applies |
|---|---|---|
| `chain_length` | 1–30 (escalation ceiling 25) | length of the causal move chain |
| `scramble` | 0.0–1.0 | fraction of narration-order transpositions |
| `coref_pressure` | 0–8 | extra same-gender actors → pronoun ambiguity |
| `distractor_events` | 0–40 | irrelevant moves of other objects |
| `distractor_similarity` | unrelated / same_entities / same_category / near_duplicate | how confusable distractors are |
| `filler_events` | 0–60 | narrative padding |
| `witness_cutoff` | 0.0–0.9 | how early the queried agent stops witnessing (0.7 → 94% belief/truth divergence) |
| `query_kind`, `expect_unknown` | — | task selection |

**Mutators (`world/mutate.py`):** `add_distractors`, `add_filler`, `scramble_narration` (answer-preserving); `extend_chain`, `add_coref_pressure` (answer-changing).

**Foils (`world/foils.py`)** — what a shallow reasoner would answer: `final_true_state`, `last_mentioned_location`, `last_witnessed_by_anyone`, `query_agent_own_last_move`, `most_frequent_location`. `foil_divergence` scores a world by how many foils diverge from the truth, weighted by salience. `world/search.py` picks the most trap-dense seed out of N candidates at zero API cost. This is the "adversarial selection" mode, which redefines difficulty as *trap salience* rather than knob magnitude.

**Ablations (`ablate/ablations.py`)** — answer-key-preserving prompt transforms over the same world, used for causal attribution: `pronoun_suppression`, `distractor_removal`, `filler_removal`, `witness_explicitation`, `temporal_normalization`. Each one asserts that the answer key is byte-identical to baseline. `attribute.py` resample-gates, runs each ablation n times, applies two-proportion z-tests with Holm–Bonferroni correction, and names the factor(s) that flip a failure to a pass.

**LLM rendering pathway (`llmrender/`)** — Nemotron narrates the world as natural prose. A *different* Nemotron extracts the facts back out, and `gate.py` deterministically diffs the extracted facts against the world. Any dropped or altered fact rejects the rendering before a subject token is spent.

---

## 3. Province II — Tool-call planning

A devops state machine: services climb a ladder (`down → provisioned → configured → deployed → healthy`), artifacts are built then published, flags gate rungs. Typed tools have preconditions and effects. The model emits a JSON array of `{tool, args}`; `toolworld/verify.py` executes it step by step.

**Verifier detail codes:** `goal_reached`, `malformed_plan`, `precondition_violation:step=N:tool`, `unknown_tool:step=N:id`, `bad_arg`, `missing_arg`, `goal_not_reached`, `budget_exceeded`, `budget_exceeded_plan` (zero-slack), `correctly_declined`, `fabricated_plan_on_unsolvable:N`, `incorrectly_declined`.

**Difficulty dimensions (`toolworld/generate.py::ToolDimParams`)**

| Dim | Range | Notes |
|---|---|---|
| `plan_depth` | 1–16 (realised optimal depth = pd + 3) | fixed 09-21: values > 4 used to be silent no-ops |
| `decoy_tools` | 0–60 (default sweeps capped at 5) | default decoys **self-describe as no-ops**, see §6 |
| `precondition_chain` | 0–2 | extra flag/artifact dependency layers |
| `state_obscurity` | 0.0–1.0 | indirectness of state narration |
| `irreversibility` | bool | adds a destructive tool. **Built, never swept.** |
| `parallel_goals` | 1–2 | two goals in one plan (enables `interleaved_goals`) |

**Structural trap mutators (`toolworld/mutate.py`)**

| Mutator | Class | What it does |
|---|---|---|
| `poison_ladder` | answer-changing | plausible trap tool that permanently forecloses the goal |
| `dishonest_decoys` | answer-preserving | renames decoys to sound load-bearing, strips the "no effect" confession |
| `implicit_preconditions` | answer-preserving (render-level) | strips per-tool "Precondition:" sentences; states the rules once |
| `zero_slack` | answer-changing | budget = optimal depth exactly |
| `id_collision` | answer-preserving | near-collision ids (`advance_o1`, `adva-nce_1`) |
| `interleaved_goals` | answer-changing | two goals that must interleave via a shared window flag |
| `make_unsolvable` | **answer-eliminating** | removes one load-bearing tool; BFS proves the goal unreachable; correct answer = `[]` |

**Foil:** `foil_greedy_lookahead0` (calls the first currently-valid tool, with no lookahead). It passes 20/20 un-mutated worlds (the base generator is greedy-solvable) and fails 200/200 at the full trap stack.

---

## 4. The search & inference machinery

| Component | What it computes | Statistics |
|---|---|---|
| `schedule/scheduler.py` | adaptive explore → refine (bisection) → decompose waves, one shared budget, parallel waves | — |
| `schedule/escalate.py` `find_breaking_point` | ramp t in 0.15 steps (k = 7) → first rate ≤ 0.30 → bisect to width ≤ 0.03 → logistic t* + bootstrap CI → decompose at frontier → confirm | logistic MLE by grid search; sha256-seeded bootstrap |
| `degradation_onset` (new 09-25) | first ramp t whose pass rate falls ≥ 0.25 below baseline; reported even when `found=False` | descriptive threshold, not inferential |
| `decompose/engine.py` | minimal factor set: single factors relaxed/raised, then pairs, then triples | multi-trial probes |
| `confirm/confirmation.py` | re-tests a hypothesis on fresh seeds | Wilson intervals, two-proportion z-test, Holm–Bonferroni |
| `confirm/replicate.py` | fresh-seed replication of any `/findings` cell, with cost estimate | same |
| `ablate/resample.py` | n× resample of the exact stored prompt → STABLE / NOISE | majority rule |
| `synthetic/` | validates all of the above against rule-based fake models with *known* boundaries before any real model is used | recovery error per strategy |
| probe parallelism (09-25) | k trials per probe run concurrently; seeds pre-assigned so the trial set is identical at any worker count | verified: 5.23× wall-clock speedup, 0 rate-limit errors, byte-identical trial set |

---

## 5. Nemotron's roles beyond test subject

| Role | Module | Status / evidence |
|---|---|---|
| **Subject** (Nano 30B-A3B, Super 120B-A12B, Ultra 550B-A55B) | evaluators | 3,880 trials |
| **Evidence Analyst** — writes the diagnosis from a deterministic pack; `audit_diagnosis` flags every number not in the pack | `analyst/` | ran once live; audit flagged 2 unsupported figures. **Its input is contaminated, see P1** |
| **Literature corroborator** (Tavily) | `analyst/corroborate.py` | built; never run (no key) |
| **Adversarial proposer** (narrative) — proposes configs, never answers; zero-API gauntlet; random control arm | `propose/loop.py` | smoke test: 4 trap-library entries; round-0 hits 2 vs control hits 2, so **does not beat random** |
| **Adversarial proposer** (tool) | `propose/toolworld.py` | 1 hit at 75% fail on Nano (implicit preconditions + id collisions, depth 6, 2 goals) with the hypothesised mechanism confirmed by verifier codes. Rounds 0–2: total gauntlet wipe-out. `beat_random` 0.2375 vs 0.20 from a *supplementary* control arm, small n: **weak evidence** |
| **Renderer** (Lightning) + **fidelity extractor** (Nano) | `llmrender/` | 46 trials: 34 PASS / 1 FAIL / 11 rejected by the gate |

---

## 6. Evidence base

**Store:** 4,133 records, 61 run ids, 17 Sep – 26 Sep 2026, **$2.96 tracked API spend**. Verdicts: 3,374 PASS / 664 FAIL / 84 ERROR / 11 REJECTED_BY_FIDELITY_GATE. Render modes: 4,087 template / 46 Nemotron-prose. 285 records are resample-gate children (`/verify`).

**Real spend is higher than tracked.** Earlier in the project the account moved $30 → $22.50 while the store showed $1.99, so roughly 3–4× under-tracking. Ad-hoc `client.chat` calls (manual resamples, probes, ablations) never write `cost_usd`, and ~$0.84 of records were destroyed in the 09-19 data-loss incident.

**Clean vs contaminated data.** Two runs predate the witness-rendering fix and must be excluded from any belief statistic: `camp-belief-nemotron-super-001` (265) and `camp-belief-120b-001` (214, gpt-oss). About 1,180 records were also destroyed on 09-19, including the original Ultra adversarial-belief and pronoun-ablation evidence.

### 6.1 Narrative province — clean (post-fix, campaign trials, resamples excluded)

| Tier | state | order | belief (standard) | belief (adversarial selection) | narrative escalation |
|---|---|---|---|---|---|
| Nano | 90/90 | 156/158 | **521/578 = 90.1%** | **279/317 = 88.0%** | no frontier (both full_map runs) |
| Super | 57/60 template (3 FAILs; their 6 resamples split 3/3) + 34/35 prose-rendered | 79/80 | **283/291 = 97.3%** | **152/160 = 95.0%** | no frontier |
| Ultra | 30/30 | 80/80 | **181/183 = 98.9%** | **127/128 = 99.2%** | no frontier (54/56) |
| gpt-oss-120b | 30/30 | — | *invalid (pre-fix)* | — | — |

Cap-hit ERRORs (excluded above): 38 belief, 2 order. The narrative campaigns still run Nano/Super at the default **1500/4000** token budget.

### 6.2 Tool province — every run

| Run | Tier | P / F / E | What it was |
|---|---|---|---|
| tool_planning-…-nano-001 | Nano | 39 / 1 / 0 | baseline sweep, no traps |
| tool_planning-super-001 | Super | 40 / 0 / 0 | baseline |
| tool_planning-adv-{super,ultra}-001 | S, U | 40/0/0 each | adversarial seed selection: nothing to select, generator had no traps |
| tool_bp-super-001 | Super | 51 / 1 / 4 | trap ray: no break (min 71%) |
| tool_bp-ultra-001 | Ultra | 51 / 5 / 0 | trap ray: no break; **3/5 failures deterministic id mis-binding at step 8–9** |
| tool_bp-nano-001 | Nano | 95 / 49 / 10 | trap ray: **found t* = 0.55** (*cap-biased, see P3*) |
| propose-toolworld-001 (+control) | Nano | 26 / 7 / 7 | Nemotron proposer |
| tool-depth-*-002 | all | — | depth ray at 4k caps. **Super t* = 0.883, RETRACTED** |
| tool-depth-super-002/captokens | Super | 6 / 4 / 0 | replay of 10 capped trials at 16k: 6 were passes |
| tool-depth-nano-003 | Nano | 64 / 34 / 0 | depth ray, caps lifted: **t* = 0.500 [0.450, 0.575]**, 100% → 0/7 |
| tool-depth-super-003 | Super | 48 / 8 / 0 | no frontier; 57–86% band at depth ≥ 11 |
| tool-depth-ultra-003 | Ultra | 53 / 3 / 0 | no frontier; 7/7 at depth 16 |
| tool-count-super-001 | Super | 56 / 0 / 0 | 8 → 66 tools, honest decoys |
| tool-confuse-super-001 | Super | 56 / 0 / 0 | 8 → 66 tools, all decoys plausible |
| test-parallel-* | Nano | 11 / 0 / 1 | parallelism A/B |

**Tool failure codes (all tool runs, 178 FAILs):** `unknown_tool` 125 · `precondition_violation` 46 · `bad_arg` 5 · `missing_arg` 2.

---

## 7. Findings ledger, graded

**Grade A** = replicated, resample-verified or significance-tested, clean data. **B** = deterministic and verified, but small n or one campaign. **C** = suggestive, unreplicated, or evidence partly destroyed. **R** = retracted.

| # | Finding | Grade | Evidence |
|---|---|---|---|
| F1 | Nano tool-planning frontier at depth 10–11 under id collisions; collapse 100% → 0/7 | **A** | depth-nano-003, 0 errors; Fisher depth-9 vs depth-11 p = 0.0006 |
| F2 | Depth × collisions is multiplicative for Nano (14/14, 13/21, 5/21, 0/7) | **A−** | decompose stage of the same run; not separately confirmed on fresh seeds |
| F3 | Identifier binding (`unknown_tool`) is the dominant tool failure across tiers | **A** | 125/178; independently implicated by trap ray, depth ray and proposer |
| F4 | Ultra deep-plan id mis-binding at step 8–9 of 9-call plans is deterministic | **A−** | 3/5 failures 3/3 stable on byte-identical rebuilt worlds; n = 3 |
| F5 | Super: no frontier at depth ≤ 16; degrades to 57–86% | **B** | depth-super-003, k = 7 per point, wide CIs; k = 15 needed to separate real degradation from noise |
| F6 | Ultra: no frontier at depth ≤ 16; depth 13–15 dip is noise | **A** | permutation p = 0.121; Fisher d15 vs d16 p = 0.462; fresh top-up 11/14 |
| F7 | Super insensitive to catalog size up to 66 tools, honest or plausible decoys | **A** | 112/112 across two rays, 0 errors |
| F8 | Super resists instruction injection in tool metadata | **B** | 5/5, one injection template |
| F9 | Super judges feasibility correctly (impossible task, starved budget, goal already met) | **A−** | 5/5, 5/5, 40/40 (after a 4/5 that did not replicate) |
| F10 | Belief capability orders Ultra > Super > Nano | **A** | §6.1 clean data, multiple campaigns |
| F11 | State and order are essentially solved by all tiers in range (chain ≤ 30) | **A** | ~99% across ~500 trials |
| F12 | Nano belief failure: over-generalises continuity of observation past the last witness cue | **B** | ground truth regenerated; `witness_cutoff` sweep reproduces the mechanism |
| F13 | Super trap-pressure **abstention** ("unknown" when belief is inferable) | **A−** | 4 stable of 7 candidates, finish_reason = stop, well under cap |
| F14 | Ultra protagonist-continuity bias in pronoun actor tracking | **C** | 6/6 ablation separation on one case (Dexter), weaker on a second; records destroyed 09-19 |
| F15 | Adversarial selection broke Ultra at chain_length = 2 ("short chains harder") | **C** | original records destroyed; later Ultra adversarial 127/128 does not reproduce |
| F16 | Nemotron prose rendering does not degrade Super vs templates | **C** | 34/35 vs 283/291; small n, not a controlled A/B |
| F17 | Nemotron proposer finds traps better than random | **C** | narrative: does not beat random; tool: 0.2375 vs 0.20, supplementary arm |
| R1 | Super tool frontier at t* = 0.883 | **R** | token-cap artifact (6/10 capped trials were passes) |
| R2 | Super intrinsic false-belief floor | **R** | witness-rendering bug |
| R3 | Ultra trap-pressure abstention | **R** | token truncation; phenomenon re-attributed to Super (F13) |
| R4 | "Priya" continuity failure present in all 3 tiers | **R** | 4/5 correct on resample (MoE noise) |
| R5 | Nano t* "replicated across rays" (0.500 vs 0.55) | **R (new)** | t is ray-relative; see P2. The mechanism replicates; the number does not |

---

## 8. Known defects, ranked by impact on what we can claim

| # | Defect | Impact | Fix |
|---|---|---|---|
| **D1** | `/findings`, `/report` and the Evidence Analyst pack include the two pre-witness-fix belief runs (P1) | **Critical.** The Analyst's diagnosis misranks Super below Nano; Super belief shown 78.4% vs clean 97.3%; gpt-oss belief invalid | Exclude runs created before `2026-09-17T16:06:56Z` from belief aggregates (or an explicit `INVALID_RUNS` list), then re-run the Analyst |
| **D2** | Escalation probes count ERROR (token cap) as failure; Nano/Super default caps 1500/4000 are below observed need (Nano max clean completion 8,456 tok) | **High.** Biases every escalation toward false breaks; already produced R1; contaminates Nano trap-ray t* (P3) | Raise `default_token_budget` to ≥ p99 of clean completions per model (≈ 16k/24k), and/or have `_RayProbe` exclude ERRORs and report them separately |
| **D3** | Cross-ray t* comparison presented as replication (P2) | **High** (credibility) | Correct survey page, brand board and lab log; replication claims must compare *configs* or *mechanisms*, not t |
| **D4** | `adversarial_belief` capability cell is always empty: adversarial trials are stored as `kind=belief` + `adversarial=1` and the grid keys on `kind` | Medium. Adversarial results invisible in `/findings` and the pack | Split on the `adversarial=1` flag in `findings.derive` |
| **D5** | Renderer model (Lightning) appears as a tested model with 0.0% (gate rejections stored under the renderer) | Low. Misleading row | Filter `REJECTED_BY_FIDELITY_GATE` out of model rollups |
| **D6** | Default `decoy_tools` decoys self-confess ("no effect on state") | Medium. Any decoy sweep without `dishonest_decoys` is a no-op | Keep the mutator route (comparability); document it |
| **D7** | `irreversibility` built but never swept; no campaign tests destructive-action avoidance | Medium. Untested capability | Add it to a ray |
| **D8** | Pre-09-21 `tool_bp` records lack trap dims in `latent_params` | Low. Needs the `verifier_detail` fallback to rebuild worlds | Documented; append-only store, no migration |
| **D9** | No shared rate limiter; concurrent campaigns contend on the account limit | Medium. "Parallel" multi-model runs take ~1 h | Shared token bucket across `Client`s, or keep sequential |
| **D10** | Cost tracking misses ad-hoc calls | Medium. Budget planning blind spot | Route every chat through a cost-recording wrapper |
| **D11** | `FullMapResult`/`StageSummary` printed counts don't sum with verify children | Low. Cosmetic; always use SQL | Fix the summariser |
| **D12** | Generator ceiling (depth 16) is below Super/Ultra's frontier | High for the headline: cannot locate their breaking points | Extend the depth chain; see §10 |
| **D13** | 3 Super `state` FAILs (57/60) never investigated — the only state failures on any tier; resamples split 3 PASS / 3 FAIL, so partly real | Low–medium. Unexplained anomaly | Rebuild worlds and resample |

---

## 9. Test & verification status

**677 passed, 0 failed** (full suite, re-run 2 Oct 2026, 3 min 48 s), on the current working tree. That tree includes the other thread's uncommitted `viewer/serve.py` edits.

What the suite covers:
- synthetic-zoo recovery of known boundaries (escalation recovers an analytic t* = 0.55 within 0.1)
- determinism and byte-identical world rebuild
- answer-preserving invariants for every ablation and answer-preserving mutator
- BFS solvability for every generated and mutated world
- unsolvability proofs (`make_unsolvable`, 20/20 seeds)
- verifier grading for every detail code
- the parallel-probe determinism guarantee
- the Analyst audit (catches a planted invented number)
- read-only store enforcement
- viewer pages

All of it runs with zero network.

What the suite does **not** cover: anything about live model behaviour (by design), and the data-hygiene rules behind D1/D4/D5. Those are exactly the class of bug that tests did not catch.

---

## 10. What's left out (capability gaps versus the tool-calling literature)

Ranked by how much the published benchmarks weight them and how badly they are missing here.

| Gap | Why it matters | Current state |
|---|---|---|
| **Multi-turn execution with feedback** | BFCL v4 weights agentic 40% + multi-turn 30%; τ-bench shows pass rates roughly halving across repeated trials; models use error signals poorly (median 32.9% adaptation in SciAgentGym) | **Absent.** Every plan is graded as one emission; the model never sees an observation or an error |
| **Error recovery** | Recovery rate after a failed call is a headline agent metric | Absent (needs the above) |
| **Native function-calling API** | Real deployments use the `tools` parameter / `tool_calls`; the literature reports structured FC modes have a *different* error profile from prompt-JSON | **Absent.** We parse JSON from free text |
| **Consistency (pass^k)** | τ-bench's core reliability metric; a model that passes 1 of 3 runs is not deployable | Absent. Everything runs at temp 0 with single draws (resamples are used only as a noise gate) |
| **Data passing between calls** | Composition: one call's output id feeds the next call's args | Absent. All args are pre-declared entity ids |
| **Argument-value reasoning** (numbers, units, enums, ranges) | `bad_arg` is a common real-world failure | Barely exercised (5 `bad_arg` total) |
| **Missing-parameter handling** | Models fabricate plausible values instead of asking | Not built |
| **Irrelevance / unanswerable** | BFCL category; fabrication on impossible goals | **Built and unit-tested, never run live** (`run_tool_irrelevance_campaign`) |
| **Destructive-action avoidance** | Safety-relevant; irreversible tools | `irreversibility` exists, never swept |
| **Long context / large catalogs (hundreds+)** | Published collapse sits in the hundreds-to-thousands range | Tested to 66 tools only (null) |
| **Cross-family comparison** | Judges will ask "compared to what?" | Only gpt-oss, and its belief data is invalid |
| **Reasoning-trace analysis** | `reasoning_text` is stored for every trial | Never mined. Could localise *where* binding slips happen |

---

## 11. What we can do next — ranked plan with cost

Costs are tracked API spend; multiply by ~3 for real account burn until D10 is fixed.

### Tier 0 — correctness before any new claim ($0, code only)
1. **Fix D1** (exclude pre-fix belief runs from findings/pack). Re-run the Evidence Analyst (≈ $0.02) so its published diagnosis is correct.
2. **Fix D2.** Raise default token budgets to ≥ p99 and make escalation probes treat ERROR separately. Add a test: a cap-hit must never lower a probe's pass rate.
3. **Fix D3.** Correct the "cross-ray replication" wording everywhere it appeared.
4. **Fix D4/D5** (adversarial cell, renderer row).

### Tier 1 — harden what we have (≈ $0.30)
5. Re-run the **Nano trap ray with lifted caps** (≈ $0.08). This gives an un-biased trap-ray t* and checks whether the frontier configuration it reports matches the depth ray's mechanism.
6. **k = 15 at depths 11–16 for Super** (≈ $0.10). Settles whether the 57–86% band is genuine degradation (would be a reportable onset) or small-n noise.
7. **Fresh-seed confirmation of F2** (multiplicative interaction) through `confirm_hypothesis` at seeds ≥ 10,000 (≈ $0.05).
8. **Run the irrelevance campaign** on all three tiers, 40 trials each, 40% unsolvable (≈ $0.10–0.25). Measures fabrication rate and balanced accuracy, a brand-new capability axis with zero build cost.

### Tier 2 — new capability axes (build + ≈ $0.50 each)
9. **Native function-calling mode.** Same worlds, tools sent as JSON schema in the API `tools` parameter, `tool_calls` parsed and fed to the same verifier. Direct A/B against prompt-JSON. High real-world relevance; modest build (client + parser).
10. **Interactive province (multi-turn with feedback).** The model emits one call, receives the simulated observation or error, and continues. New metrics: recovery rate, steps-to-goal, repeated-error rate, goal drift. Ground truth stays BFS over states. The biggest build and the highest literature weight; reuses all of `toolworld/`.
11. **Consistency (pass^k).** Same world, k = 5 at temperature > 0. Report pass^k alongside pass@1. Cheap build.
12. **Value-passing tools.** Build steps return generated artifact ids that later calls must thread through. Tests composition without leaving deterministic ground truth.
13. **Missing-parameter worlds** (omit a needed fact; correct response = ask/decline). Shares the `allow_decline` machinery.
14. **Irreversibility ray.** Destructive shortcuts; measure avoidance.

### Tier 3 — locate Super/Ultra frontiers (build + ≈ $0.60)
15. **Extend the depth generator past 16** (the chained-flag mechanism already scales). Re-run the depth ray on Super/Ultra. This is currently the only axis that moves them.
16. **Combine the axes that hurt:** depth × collisions × implicit preconditions × interleaved goals, as one composite ray.

### Tier 4 — breadth & insight
17. **Clean cross-family baseline:** at least one non-Nemotron model on Nebius, full_map plus the tool rays.
18. **Reasoning-trace mining:** align `reasoning_text` with the failing step to see whether binding slips appear in the reasoning or only in the emitted JSON.
19. **Tavily corroboration** (needs a key): links each confirmed finding to published work.

**Recommended order:** Tier 0 → items 5, 6, 8 → item 9 (native FC) → item 10 (interactive). Tier 0 is mandatory before the submission cites any numbers.

---

## 12. Glossary

- **t / ray** — an escalation ray maps t ∈ [0, 1] linearly from an easy baseline config to a hard ceiling config. t* is the logistic midpoint (P(pass) = 0.5) on *that* ray. t values from different rays are not comparable.
- **found** — the ramp hit a pass rate ≤ 0.30 (collapse). `found = False` means "no collapse in range", not "no failures".
- **degradation_onset** — the first t with a pass rate ≥ 0.25 below baseline.
- **Scored rate** — PASS / (PASS + FAIL). ERRORs (token caps, API failures) and resample-noise FAILs are excluded.
- **STABLE / NOISE** — a resample-gate classification of a FAIL against its byte-identical stored prompt.
- **Foil** — a plausible wrong answer a shallow reasoner would give; used to pick trap-dense worlds.
- **Answer-preserving / changing / eliminating** — whether a mutation keeps, changes, or removes the correct answer.
