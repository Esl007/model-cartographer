# Spec: reproducible, compact breaking points in frontier models

*Written 2026-10-04. Companion files: `docs/research/frontier-failure-modes.md` (the literature pass, with verified citations and effect sizes) and `docs/campaign-catalog.md` (generated from the campaign registry). Sections 1–3 are binding; the rest is the design they constrain.*

## 0. Standing rules (carry into every sub-agent brief)

- Never `git stash` / `checkout` / `restore` / `clean` / `reset` in this repo; `results/campaign.sqlite` is live paid data and is never tracked or touched.
- No LLM anywhere in generation, ground truth, mutation or verification.
- `max_tokens` never below the observed p99 of clean completions; `finish_reason=length` is ERROR, never FAIL; a probe cut short by a cost cap is discarded, not read as a decline.
- No live spend without an explicit instruction. Everything in this spec is buildable and testable offline against planted fake models first.
- The portal (viewer) is being rebuilt in another thread: nothing here edits `cartographer/viewer/*`. Backend changes expose what the portal needs and keep every legacy preset name working.

## 1. The problem, in our own data

Across 6,259 records and five models, the only thing that breaks Super and Ultra is **long plans**: 24-call look-alike plans for Super, degradation from 25 calls for Ultra. Everything *structural* we have (five tool traps, 66-tool catalogs, belief traps, value passing, fault recovery) saturates on them. Long plans are a real limit, but they are the wrong kind of finding for this project: the error compounds over 20–30 model decisions, so the "boundary" is partly an artefact of how many chances the model had to slip. A frontier model will always break eventually if you give it enough steps. That is not a capability map; it is a reliability curve.

**What counts.** A breaking point is *reportable* only if it has a **reproducibility certificate**:

| Condition | Test | Why |
|---|---|---|
| **Compact** | the task at the boundary needs ≤ 12 model decisions (plan calls, turns, or traced statements of a single kind), configurable per province | rules out compounding-error curves |
| **Stable** | every FAIL that enters escalation survived the resample gate (3 re-runs of the byte-identical task; ≤ half pass), and ≥ 60 % of gated FAILs were stable | rules out MoE sampling noise (32 % of narrative single-trial failures were noise) |
| **Confirmed** | fresh-seed confirmation at the boundary configuration (new tasks, seeds ≥ 10,000) agrees | rules out a lucky draw of tasks |
| **Attributed** | decompose names the factor(s) whose relaxation restores passing; for ablations, Holm–Bonferroni across the catalog | rules out "it got harder in general" |

`schedule/certificate.py` computes this from a `BreakingPoint`; `frontier_summary` shows uncertified boundaries with a visible "not yet reproduced" mark, never as a plain BREAKS.

## 2. Where frontier models actually break: the literature, mapped onto our generators

The pattern behind every reproducible frontier failure in the literature is the one we already found twice (Nano's `set_dep_th_flag_4`, Gemma 4's fabricated ticket): **a salient prior overrides something the model was shown.** Length is not the lever; conflict with a prior is. The verified citations and effect sizes are in `docs/research/frontier-failure-modes.md`; the mapping that this spec commits to:

| Family (study) | Mechanism | Our implementation | Province | Compact? | Verifier |
|---|---|---|---|---|---|
| **Trivialized false belief** (Malek et al. 2025, arXiv 2507.07313; SimpleToM, arXiv 2410.13648) | the classic false-belief story with the trap removed (the agent *saw* the last move) is answered as if the trap were still there: GPT-4o 75 → 20 %, Gemini 2.5 Pro 94 → 63 %, o3 88 → 74 % | `full_witness`: the query agent witnesses every move, so the correct belief is the true location; new foil `stale_belief` (the location before the last move). Mirror image of our `final_true_state` foil, so a model failing both is matching a template | I (belief) | yes (one story) | simulation unchanged |
| **Seemingly relevant no-ops** (GSM-NoOp, Mirzadeh et al. 2024, arXiv 2410.05229: GPT-4o 95 → 63 %, o1-preview 93 → 77 % from one clause) | clauses that mention the queried entity but change nothing pull the answer | `noop_distractors`: considered / declined / negated / planned events about the queried object or agent, simulated as no-ops | I | yes (one story) | simulation unchanged |
| **Premise order** (Chen et al. 2024) — *downgraded to a control*: the research pass finds only ≈ 5 points at ≤ 8 premises; the 30 %+ drops need 12+ rules | reordering true premises | `relational_time`: timestamps replaced by true relative cues; with `scramble`, narration order ≠ time order. Kept because it is cheap and is the right control for "order with timestamps" (known null) | I (order, state) | yes | simulation unchanged |
| **Counterfactual conventions** (Wu et al. 2023) | the model follows the default convention instead of the stated one | `dialect`: the program header declares deviations from Python (inclusive `range`, truncating `%`, copy-on-assign, fresh defaults); truth = our interpreter under the declared semantics; Python becomes the foil `python_prior` | IV | yes (6–24 statements) | own interpreter |
| **Prior over observation** (our Gemma 4 finding; same mechanism as pattern completion) | a pattern-shaped value is written in place of an observed opaque one | `ticket_entropy`, `decoy_tickets`, carry/live controls, fabrication classifier (shipped in `ce908b8`) | III | yes (≤ 15-call plans, boundary expected at ≤ 6 carried steps) | ticket check |
| **Reliability, not capability** (τ-bench pass^k) | pass^k collapses while pass^1 looks fine | `X.consistency`: m tasks × k repeats at a mid-difficulty point; report pass^1, pass^k with Wilson CIs | all | yes | repetition |
| **Look-alike identifiers** (ours; F3) | pattern completion over neighbours | already the headline; kept as the control family | II, III | *no at the frontier* (Super 24 calls) | BFS |

Deferred (promising, not in this round; each is one mutator and one ray, none needs new ground truth):
- **Misleading comments / names in code** (CodeCrash, arXiv 2504.14119: −23 % average across 17 models, −14 % with chain of thought): a comment asserting a wrong value on a traced line; foil = the comment's value. Control: a neutral comment of the same length.
- **Goal revealed across 2–4 turns** (Laban et al. 2025, arXiv 2505.06120: −39 % average, mostly unreliability): Province III with the irreversible tool switched on so acting too early is checkable; report pass^k.
- **Harder impossible tasks** (AbstentionBench, arXiv 2506.09038): a near-fit tool replaces the removed one; in III a ticket that is never issued. Keep a solvable twin so wrong refusals are measured.
- Instruction-constraint stacking on plans (constrained BFS); critical-fact position with 2–4k tokens of filler.

The research file's "does not count" list (lost in the middle, RULER, 3k-token padding, Tower of Hanoi, 500-instruction stacks, the reversal curse) matches §8.

**Falsifiability is built in.** Each family has a rival explanation and a control: no-ops vs plain distractors (we already know plain distractors do nothing); relational cues vs `scramble` with timestamps (known null); dialect vs the same programs without the header (known pass rates); ticket entropy vs `ticket_length` (the "it's just long" control); consistency vs single-pass accuracy.

## 3. Engine changes that cut cost without cutting evidence

1. **Early-stopped probes** (`find_breaking_point(early_stop=True)`): a probe stops issuing trials as soon as its bucket (pass / fail / between) cannot change. Seeds stay pre-assigned, so the trial set is a prefix of today's and determinism at any parallelism holds. Measured saving on the deterministic test fakes: 17 % of ramp+bisect trials (10–22 % per fake; less at higher parallelism because a whole wave counts once issued). Off by default; tiers switch it on. The t = 0 baseline always runs in full because it is the reference for degradation.
2. **Scout, then focus** (`scout_rays`): probe every ray at t = 0.5 and 1.0 with k = 3; rank by failure signal; spend the real budget on the rays that moved. For a new model this replaces "run all 12 stages at full budget" with ~70 cheap trials followed by 2–3 full rays.
3. **Certificate in the result**: `BreakingPoint.certificate` is filled by `find_breaking_point` (gate counts added by the gated evaluator), so every runner reports reproducibility for free and `frontier_summary` can read it.
4. **Resample gate everywhere** (interactive done in `ce908b8`; tool and code evaluators next through the same `ResampleGatedEvaluator` pattern, which only needs the `(config, trial_seed, run_suffix)` call shape).
5. **Compactness budget per province**: narrative = one story; II = plan length at the boundary ≤ 12 calls; III = ≤ 12 calls; IV = ≤ 24 statements. A boundary found only beyond that is recorded with `compact=False` and the reason "compounding, not structural".

Cost model (tracked $, Nebius prices, reasoning models): a rigorous axis campaign today ≈ 250 trials ≈ $0.20 (Nano) / $0.45 (Super) / $2.5 (Ultra). Early stop plus scout brings a *full new-model map* from 12 full stages (≈ $3.2 Gemma-class, ≈ $25 Ultra-class) to scout (≈ 15 % of that) plus 3 focused rigorous rays. The point is not the dollars; it is that a frontier model can be mapped in one sitting.

## 4. Evidence tiers (the "Normal / Medium / Harder" question)

The user asked whether a Normal / Medium / Harder switch reduces usage and whether it adds complexity. Recommendation: **yes to one dial, but name it by evidence depth, not by task difficulty.** Escalation already finds the difficulty by itself; a "harder" setting would fight the instrument. What a user actually chooses is how much evidence they pay for:

| Tier | k per probe | budget / cost scale | early stop | resample gate | fresh-seed confirm | decompose | reportable? |
|---|---|---|---|---|---|---|---|
| **Scout** | 3 | 0.35 | on | off | off | off | no: grade C ceiling, "where to look" |
| **Standard** | 5 | 0.7 | on | on | on | singles | yes, grade B ceiling |
| **Rigorous** | 7 | 1.0 | off | on | on | singles + pairs | yes, grade A possible |

Complexity argument: one dial, three rows in a dict, applied by signature inspection (`campaigns/tiers.py`), no new generators, no new verifiers. It *reduces* user-facing complexity because the eleven budget/k/cap/gate knobs collapse into one word, and it reduces spend because most exploratory runs should be Scout. It must never be shown as "Easy / Medium / Hard": a Scout result is not an easier test, it is a cheaper, less certain measurement of the same test.

## 5. Campaign packaging

Today's names mix province, query kind and method (`belief`, `tool_breaking_point`, `full_map`, plus a dozen script-only stages). The fix is a declarative registry (`cartographer/campaigns/registry.py`) with one id per campaign, `<province>.<stressor>`:

- **I.state, I.order, I.belief, I.adversarial_belief, I.breaking_point, I.noop, I.premise_order, I.trivial_belief**
- **II.plan_length, II.plan_length_lookalike, II.traps, II.catalog_size, II.impossible**
- **III.values, III.lookalike, III.fault, III.ticket_mechanism, III.ticket_trap, III.ticket_length**
- **IV.lookalike, IV.traps, IV.length, IV.copy, IV.dialect**
- **X.full_map, X.consistency**

Each entry carries a plain-English *question* ("Does it keep track of where things are when the story mentions moves that never happened?"), the stressor, the evidence kind (axis / rate / battery), the runner, the run-id template (kept matching `frontier_summary`'s regexes), the frontier rows it feeds, legacy aliases (every old preset and stage name resolves), and the protocol note (native vs text tool calls). `scripts/run_campaign.py --campaign <id> --tier <tier>` is the single entry point; `--list` prints the catalog; `--all --tier scout` is the new-model map. **Hand-off to the portal thread:** read `REGISTRY` and `TIERS`; show province → question → tier; keep `CAMPAIGN_PRESETS` keys working through `resolve()`. The portal is not edited here.

## 6. Work packages (built in parallel today; all offline)

| WP | Scope | Files | Acceptance |
|---|---|---|---|
| A | early-stop probes, certificate, scout | `schedule/escalate.py`, `schedule/certificate.py` | existing escalation tests byte-identical; early stop never changes `found` / bracket on the deterministic fakes; certificate truth table; scout ranks a breaking fake above a non-breaking one |
| B | `noop_distractors`, `relational_time` | `world/*`, `render/templates.py`, narrative keys in `exec/evaluator.py` | answer keys identical with and without each mutation (200 seeds); planted no-op-believer fails iff no-ops target the queried object; planted order-as-time reader fails iff relational cues + scramble |
| B2 | `full_witness` (trivialized false belief) + `stale_belief` foil | `world/generate.py`, `world/foils.py`, `report/verdict.py` cause classification | with `full_witness` the belief answer equals the true location and differs from the stale location on ≥ 95 % of seeds (others re-drawn); planted template-matcher (always answers the stale location) fails iff `full_witness`; `final_true_state` and `stale_belief` are both classified by the cause mapper |
| C | declared `dialect` trap + `python_prior` foil + `default_code_dialect_ray` | `codeworld/*`, code keys in `exec/evaluator.py` | empty dialect byte-identical to today; truth verified against mechanically translated CPython on ≥ 300 programs; header-ignoring fake fails iff dialect on, always `foil:python_prior`; escalation attributes to `dialect_rules` |
| D | registry, tiers, consistency campaign, `run_campaign.py`, catalog | `campaigns/*`, `scripts/run_campaign.py`, `report/frontier.py` consistency row | every legacy name resolves to exactly one id; run-id templates match frontier regexes; tiers monotone; pass^k < pass^1 on a planted 70 % flake and equal on a deterministic fake |
| R | literature pass | `docs/research/frontier-failure-modes.md` | 12–18 verified citations with quantified frontier effects, compactness and verifier feasibility; top-6 ranked with predictions and controls |

Integration (coordinator): full suite green, commit by path, build-log entry, catalog regenerated, `frontier_summary` regenerated.

## 7. Live plan (needs explicit approval; nothing runs today)

Order of runs on Super and Ultra, Standard tier unless stated, text or native protocol as the model requires:

1. `X.consistency` at a mid-difficulty II config (m = 12, k = 5): ≈ 60 trials each. Prediction: Super pass^1 ≈ 0.9, pass^5 noticeably lower; Ultra similar. This is the cheapest reproducible frontier limit we can show.
2. `I.trivial_belief` (`full_witness` on) on Super and Ultra: ≈ 60 trials each at the current belief settings. Prediction from Malek et al.: a visible drop with failures matching `stale_belief`. Both tiers are at 97–99 % on the false-belief version, so any drop here is the cleanest "template over observation" result available.
3. `I.noop` on Super and Ultra: ≈ 90 trials each. Prediction from GSM-NoOp: a drop at 2–4 no-ops about the queried object; the control (`distractor_events`) stays flat.
4. `IV.dialect` on Super and Ultra: ≈ 120 trials each. Prediction from Wu et al.: a drop as `dialect_rules` rises, failures matching `python_prior`. Control: the same programs without the header (known pass rates) and a one-line comprehension probe of the rule.
5. `III.ticket_mechanism` on Super (native) and Gemma 4 (text): the other session owns the Gemma 4 endpoint and the `ce908b8` entrypoint.
6. `I.premise_order` on Super (control, ≈ 90 trials; expected small).
7. Rigorous tier re-run of anything that moved, for the certificate.

Estimated tracked spend for the full list: Super ≈ $1.2, Ultra ≈ $6, Gemma 4 on Modal ≈ GPU time only.

## 8. What does not count

- A boundary reached only past the compactness limit (20–30-step plans, 50+ statement programs): recorded, labelled "compounding", never headlined.
- A drop produced by a token cap, a cold endpoint, a dropped `tools` field or any other protocol artefact (X1, X8, X9 in the findings).
- A single run without the certificate: reported as "observed once", grade C.
- Context-length stress for its own sake: no province extends its length ceiling in this round.
