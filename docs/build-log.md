# Build log

Chronological, timestamped record of every commit and every campaign run,
merged into one timeline. All timestamps UTC, derived directly from git
commit metadata and the results stores' `created_at` fields — not
reconstructed from memory. This is the step-by-step complement to
`docs/lab-log.md`, which is organized by topic (findings/bugs/decisions)
rather than by time.

Legend: **C** = git commit, **R** = campaign/pilot run recorded in a
results store.

---

## 2026-09-16

| Time (UTC) | Type | Event |
|---|---|---|
| 19:58:15 | C `b3e64c5` | Phase 0: synthetic validation harness (rules, model zoo, recoverability framework) |

## 2026-09-17

| Time (UTC) | Type | Event |
|---|---|---|
| 07:47:31 | C `7eeab04` | Phase 0.5: canonical experiment schema + append-only results store |
| 08:05:48 | C `bf77e3f` | Phase 1: latent world generator (witnessed event log, ground truth by simulation) |
| 08:36:23 | C `780a05e` | Phase 2: template renderer + deterministic verifier |
| 09:19:52 | C `e7c6d48` | Execution client + pilot runner (Groq/Nebius providers) |
| 09:20:09–09:20:19 | R `pilot-groq-001` | 54 calls, $0.00 — all ERROR (Cloudflare 403, missing User-Agent) |
| 09:21:45–09:22:34 | R `pilot-groq-002` | 54 calls, $0.008 — partial errors (token cap too small for reasoning models) |
| 09:23:05–09:24:10 | R `pilot-groq-003` | 54 calls, $0.0095 — clean run after UA + token fixes |
| 09:24:22 | C `aa0cba1` | Pilot fixes: UA header, reasoning-model token budget |
| 09:40:18 | C `ce4c6ce` | Phase 3: failure decomposition engine (validated on synthetic zoo incl. no-interaction control) |
| 09:52:46 | C `f1d253d` | Widened state difficulty range, capture reasoning/finish_reason, first results viewer |
| 09:52:54–09:54:12 | R `pilot-groq-004` | 54 calls, $0.0106 — first belief-collapse signal observed (later retracted, see witness-bug entry) |
| 09:59:38 | C `672d880` | Phase 4: LLM renderer behind mandatory fidelity gate |
| 10:09:08 | C `97e0d21` | Hardened execution layer from pilot error forensics (429/5xx/timeout retry, cap-hit auto-retry) |
| 10:13:06 | C `0ec7a7f` | Phase 5: adaptive scheduler (explore/refine/decompose waves) |
| 10:18:36 | C `312edee` | Pipeline evaluator glue + Groq campaign script |
| 10:19:09–10:22:10 | R `camp-state-120b-001` (Groq) | 30 calls, $0.0125 — no boundary through chain 30 |
| 10:22:13–13:32:03 | R `camp-belief-120b-001` (Groq) | 214 calls, $0.0595 — killed mid-run: Groq free-tier rate limits projected ~7h to finish |
| — | decision | Switch dev/campaign from Groq to Nebius/Nemotron (66x more TPM headroom, cheap, results count for submission) |
| 13:38:17–13:40:59 | R `camp-state-nemotron-super-001` | 30 calls, $0.0122 — no boundary (Nemotron Super) |
| 13:41:02–14:06:27 | R `camp-belief-nemotron-super-001` | 265 calls, $0.0911 — 4 boundaries found, but baseline itself only passed 3/5 |
| 15:44:05 | C `ae89da7` | Lab log created; witness-rendering bug diagnosed and documented (baseline fails 60% because bystander witnesses never appear in rendered prose) |
| 16:06:56 | C `5bd297f` | **Fix**: witness-rendering bug — non-actor witnesses now rendered into prose; 124 tests green |
| 16:10:34–16:18:45 | R `camp-belief-nemotron-super-002` | 105 calls, $0.0295 — corrected re-run: baseline 3/3, no boundary anywhere (clean reversal) |
| 16:19:42 | C `20abb49` | Logged corrected belief result; retracted pre-fix numbers |
| 16:23:59–16:28:45 | R `camp-state-nemotron-nano-001` | 30 calls, $0.005 — no boundary (Nano) |
| 16:28:54–16:38:58 | R `camp-belief-nemotron-nano-001` | 105 calls, $0.0139 — **93% pass, 7 real failures** — verified against regenerated ground truth: Nano over-generalizes continuity of observation past the last witness cue |
| 16:42:32 | C `c87687e` | Logged Nano finding — first confirmed tier discrimination (Super clean, Nano genuinely fails) |
| 16:55:22 | C `cde7fea` | Live campaign monitor (`/live`), atlas page (`/atlas`), scheduler weak-signal refinement (catches diffuse ~7-10% failure rates that don't trigger a sharp flip) |
| 17:20:02 | C `313e3b2` | `witness_cutoff` generator hardening (94% belief/truth divergence at 0.7 — systematic version of the Nano failure mode) + Phase 7 confirmation module (Wilson intervals, Holm–Bonferroni, fresh-seed convention) |
| 17:21:42 | C `21fada2` | Parallel wave execution (deterministic at any thread count), `/live` completed-run state, browser `/launch` page |
| 17:25:23–17:32:20 | R `camp-belief-nvidia-nemotron-3-nano-30b-a3b-001` | 140 calls, $0.0218 — **run via `/launch`, not yet analyzed** |
| 17:40:51–17:41:30 | R `camp-state-nvidia-nemotron-3-nano-30b-a3b-001` | 30 calls, $0.0046 — **run via `/launch`, not yet analyzed** |
| 19:13:13 | C `0cb65c1` | /live active-run + ETA fixes, shared nav shell, dark theme (viewer redesign track; also carried the new order/breaking_point launch presets) |
| 19:16:54 | C `813ac8e` | Escalation mode (`schedule/escalate.py`: ray ramp → bisect → logistic t* → decompose → confirm), order campaign preset, witness_cutoff in the belief sweep; 5 Super belief failures verified real against regenerated ground truth (1 continuity over-generalization, 4 pronoun misresolution — first recorded Super failures) |
| 19:24:57 | C `7d58049` | Logged first order-campaign results: Super 100%, Nano ~96% — no boundary found on either tier |
| 19:43:25 | C `de733ad` | Ultra-model token budgets (`default_token_budget` heuristic, 8000/16000 for "ultra"-class models) + cooperative stop button for `/launch` |
| 20:29:48 | C `19d4eeb` | Ultra pronoun-ablation done live: isolates a real protagonist-continuity bug (6/6 pronoun-vs-name separation on the Dexter case); retracts the earlier "confirmed in all 3 tiers" claim after the Priya case failed to reproduce on resample (4/5 correct — MoE sampling noise, not a capability gap) |
| 20:48:10 | C `7658a81` | Logged Ultra breaking-point escalation result: the full ray ramped to the generator's hard ceiling and pass rate never dropped below 0.86 — an honest, well-powered no-boundary-found, not a bug |

## 2026-09-18

| Time (UTC) | Type | Event |
|---|---|---|
| 09:36:19 | C `08162f6` | Foil model + adversarial seed search (`world/foils.py`, `world/search.py`): redefines difficulty as trap salience (what a shallow reasoner would answer) instead of knob magnitude, at zero API cost; verified ~1.5x trap concentration vs. unselected seeds |
| 09:36:59 | C `fb7febe` | Automates the manual Ultra pronoun-ablation loop (`cartographer/ablate/`): resample-gate (catches MoE noise before it's reported), answer-key-preserving ablation catalog, Holm–Bonferroni causal attribution — validated against planted sensitivities in fake models |
| 09:51:18 | C `ef6b928` | Logged the first live adversarial-belief campaign on Ultra: trap-selection at *minimal* structural complexity (chain_length=2) broke Ultra (94% pass, 6 failures) where the full magnitude-escalation ray could not; 3 of 6 failures survived resampling, surfacing a new failure mode (belief-attribution abstention under trap pressure) distinct from the belief→reality collapse |
| 13:44:24 | C `d152913` | Viewer IA redesign #1: overview dashboard, `/findings`, `/coverage` pages, self-explaining `/launch` preset cards (the `adversarial_belief` mode gets a NEW badge) |

## 2026-09-19

| Time (UTC) | Type | Event |
|---|---|---|
| 06:13:34 | C `dadd5ac` | `modelcart` launcher command (`~/.local/bin/modelcart`, outside the repo); fixed the viewer's default db path (was pointing at stale `pilot.sqlite`, not the live `campaign.sqlite`) and added auto-open-browser on start |
| 18:43:29 | C `f9ef93a` | One-button `full_map` campaign chaining state → order → belief → adversarial_belief → breaking_point under one budget; plain-English verdict panels computed from stored data (saturation/boundary/failure-mode language, not hand-written) |
| 19:10:49 | C `ea16334` | Adversarial proposer (`cartographer/propose/`): Nemotron proposes trap *configs*, never answers; every proposal passes a deterministic validation gauntlet before any subject-model spend, and a mandatory random-control arm reports whether the proposer actually beats random search (first live run: it didn't — an honest negative result, logged plainly) |
| 19:15:40 | C `6568762` | Viewer redesign #2: research-driven UX/IA pass (Shneiderman "overview → zoom/filter → details on demand", Nielsen heuristics), `docs/design-notes.md` created |
| 19:24:50 | C `271703e` | Automatic FAIL verification: resample-noise gate with explicit auto/manual modes (cost-conscious by design — verification only ever touches the 2–14% of trials that fail), append-only `/verify` child runs, verdict copy reports verified-vs-noise counts instead of raw unfiltered failure counts |
| 19:44:02 | C `21a16a8` | Fixed `/live` showing "0 records" for the entire duration of `full_map` and verify-now runs — the page queried the exact registered run_id, but those campaigns write all real trials under child run_ids (`.../state`, `.../verify`) that can never match; confirmed against a real run with 366 trials + 18 verify resamples that had been fully invisible |
| 20:37:35 | C `a4df298` | Viewer redesign #3: domain-derived signature system (the "claim strip" verdict component) + sidebar restructure, informed by real showcase examples from the `interface-design` Claude Code plugin |

## 2026-09-20

| Time (UTC) | Type | Event |
|---|---|---|
| 00:43:03 | C `642643b` | Viewer pass 4: every table had been a direct child of `<main>` with no enclosing container — the strongest "unstyled document, not an application" tell; enclosed all of them in cards, loosened density (34px→42px rows) past what pass 3 had deliberately chosen as "workbench-tight", loaded Manrope + JetBrains Mono in place of the system font stack |
| 03:01:24 | C `ad9e7fe` | Viewer pass 5: dashboard composition on the overview page (hero KPI row, 2-column grid, card header bars) |
| 03:17:12 | C `1668114` | Viewer pass 6: the pass-5 composition system applied to the remaining eight pages (`/runs`, `/run/<id>`, `/record/<id>`, `/live`, `/atlas`, `/findings`, `/coverage`, `/launch`) |
| 03:21:56 | C `c954e28` | **Incident fix**: untracked `results/campaign.sqlite` after ~1,180 real trial records (~$0.84 of API spend) were destroyed when a `git stash` reverted the tracked live database to its last commit — see the dedicated INCIDENT entry in `docs/lab-log.md` for the full account and recovery attempt |
| 04:53:14 | C `b5ab761` | Rebuilt `/findings` as a live, per-model capability map — the page had taken no store argument at all; every "finding" was a hardcoded dataclass transcribed by hand from this log, which is why it could never switch models, show a strength, or update after a replication |
| 04:58:07 | C `f7b29a8` | Atlas: dropped an inert legend toggle on single-model panes (a "never blank the chart" guard made the only toggle permanently unable to do anything — a control that cannot act) |
| 17:38:26 | C `99dfbc9` | **Second capability province** — `cartographer/toolworld`: TOOL-CALL PLANNING. Deterministic seeded generation of a devops-flavored latent state machine + typed tool catalog (decoys, destructive tools, precondition dependency chains); bounded-BFS `validate()`/`optimal_depth` ground truth; robust-JSON plan verifier (simulate step by step); foil model (`greedy_goal_tool`/`narration_order`/`decoy_bait`) + adversarial seed search; realized-dim measurement. Zoo validation with planted-failure synthetic models recovers ground truth. Same architecture contract as `cartographer/world`. |
| 17:38:36 | C `533ea25` | Wired `run_tool_planning_campaign` (`kind=tool_plan`) through `exec/evaluator.py` (`ToolPlanningEvaluator`, scheduler sweep over plan_depth/decoy_tools/precondition_chain/state_obscurity/parallel_goals) and the `/launch` viewer (NEW preset card); `/findings` + `/atlas` pick the new kind up automatically from `verifier_detail kind=`. |
| 10:39:20 | C `c578c20` | **Phase 9: Evidence Analyst** — `cartographer/analyst/`: deterministic evidence pack (reuses `findings.derive`, one arithmetic contract), analyst-model diagnosis that cites ONLY pack numbers, deterministic contamination audit flagging any figure absent from the pack ("unverified figure"), Tavily literature corroboration with graceful no-key degrade + response caching, `/report` viewer page, `scripts/write_report.py`. Store gains additive `open_read_only()`; keys.py additively loads a Tavily key. Tests +47, zero network. |
| ~10:40 | R analyst call | ONE Nemotron Super analyst call against the live store (READ-ONLY), hard-capped $0.20: diagnosed 3 models; audit caught 2 unverified soft-hedge figures ("low-80%", "mid-90%" ranges); no Tavily key so literature layer rendered its degrade path. Wrote `docs/capability-report.{md,json}`. |

**Total spend to date across all providers: at least ≈$1.7–2** (precise
reconstruction is no longer possible past 2026-09-19 — the underlying
records for ~$0.84 of that spend were destroyed by the git incident
logged above before a final total could be tallied from the store
directly; this figure is a floor, summed only from the dollar amounts
each entry above and in `docs/lab-log.md` states explicitly).

## 2026-09-21

| Time (UTC) | Type | Event |
|---|---|---|
| 12:21:01 | C `7c3cdb9` | Toolworld structural trap mutators (Tasks 1+2): `poison_ladder`, `dishonest_decoys`, `implicit_preconditions`, `zero_slack`, `id_collision`, `interleaved_goals` in new `cartographer/toolworld/mutate.py` (mirrors `world/mutate.py`'s ANSWER_PRESERVING/ANSWER_CHANGING contract); `foils.foil_greedy_lookahead0` (zero-lookahead simulated policy) wired into `FOILS`/`foil_divergence`. 17 new tests, zero live calls. |
| 12:21:06 | C `86fe1c1` | `render.render()` gains `style="implicit"` (strips per-tool "Precondition:" sentences into one general-rules paragraph); `verify.verify_plan()` gains an additive `budget_exceeded_plan` detail code for zero-slack worlds (`budget == optimal_depth`) — ordinary budget-exceeded worlds unaffected. |
| 12:21:12 | C `222a4c7` | `default_tool_ray()` + `run_tool_breaking_point_campaign` (budget=250, cost_cap=0.5) in `exec/evaluator.py`, mirroring `run_breaking_point_campaign`'s shape over five 0/1 structural-trap toggles + existing `precondition_chain`/`parallel_goals`; wired into `/launch` as a NEW preset card. Pure-simulation signal: `greedy_lookahead0` fails 100% (50/50 seeds) at full-ceiling trap-stacked config, up from saturating (Nano 39/40, Super 40/40) under scalar-only escalation. 630/630 tests passing. |
| 13:32:37 | C `1827e06` | Bugfix: `ToolBreakingPointEvaluator` now persists the five trap 0/1 dims (`poison_traps`, `dishonest_decoys_on`, `implicit_preconditions_on`, `zero_slack_on`, `id_collisions_on`) in `latent_params`/`realized_dims`, not just inside `verifier_detail` -- fixes the provenance bug flagged open during the deep-plan id-binding-collapse verification session (see `docs/lab-log.md`). Regression test reconstructs a world from `latent_params` alone and checks byte-identical re-rendered prompt. Additive only; pre-fix records untouched. 631/631 tests passing. |
| 16:34:05 | C `de091d2` | Plumb `renderer_model`/`extractor_model` through `run_state_campaign` so the Phase 4 Nemotron-rendered fidelity-gated pathway is reachable from a campaign entry point. |
| 16:38:42 | C `e4a8d4f` | Bugfix: `llmrender` renderer/extractor no longer crash on a None response text (extractor API error mid-gate) — surfaced by the first-ever live run of the gated pathway. |
| ~16:40 | R | **`camp-nemrender-showcase-001`** — first judged Nemotron-rendered data: Super subject, Lightning renderer, Nano extractor. 46 records all `render_mode=NEMOTRON`: 34 PASS / 1 FAIL (97%) + 11 fidelity-gate rejections, $0.0079 tracked. Template baseline comparison: 60/66 (91%). See lab-log. |
| 16:43:43 | C `1af8bec` | Tool-province adversarial proposer (`cartographer/propose/toolworld.py`): Nemotron proposes toolworld trap configs, offline gauntlet (BFS validate + greedy-foil salience vs random-config median), survivors measured via `ToolBreakingPointEvaluator`; round-0 random-config control arm for `beat_random`. `propose_configs` gains a schema override; breaking-point evaluator accepts all scalar dims. 8 new tests; 322 relevant tests green. |
| ~16:55 | R | **`camp-propose-toolworld-001`** (+`/control`) — Super proposes vs Nano subject, 4 rounds, $0.0195 tracked total. One trap-library hit at 75% fail (implicit_preconditions + id_collisions at depth 6, parallel_goals 2), hypothesis confirmed by verifier detail codes. Supplementary control arm: proposer mean fail 0.2375 vs random-config mean 0.20 → `beat_random=True` (narrow, small n). See lab-log. |
| ~later | C `38efd97` | Bugfix: `plan_depth` no longer saturates at the ladder height (`cartographer/toolworld/generate.py`). Each unit of `plan_depth` past `ladder_height`=4 now appends one strictly-chained `set_depth_flag_j` step (built from existing `_set_flag_tool`) gating the top ladder rung, so realized optimal depth grows ~1:1 (pd=10→13, pd=16→19) instead of plateauing at 7. `plan_depth <= 4` is a strict no-op — byte-identical renders, past records reproduce from `latent_params`. Validated solvable+deterministic pd 1..16 × 30 seeds; all five trap mutators compose solvably at pd=10. 15 new tests; 653/653 passing. See lab-log. |

## 2026-09-25

| Time (UTC) | Type | Event |
|---|---|---|
| ~18:30 | C `899ef28` | Parallelized escalation probes: `find_breaking_point` gains `parallelism` (default 1); each probe's k_trials run concurrently on a thread pool with seeds assigned up front, so trial set / t* / pass curve are provably identical at any worker count. Cuts probe wall clock ~k-fold (a 30-min sequential Nano ramp became ~12 min at parallelism 6). |
| ~18:35–19:40 | R | **`camp-tool-depth-{nano,super,ultra}-002`** — depth-escalation ray (plan_depth 4→16 × id_collisions), $0.41 tracked total. **Nano**: multiplicative collapse (depth 10 alone 88%, collisions alone 64%, both 15% → 0/7 at depth 11). **Super**: FIRST-EVER breaking point, `found=True t*=0.883 CI (0.825, 0.967)` at depth 15 + collisions, gradual frontier. **Ultra**: honest no-break — wobbles 57%/71% at depth 13/15 but 7/7 at 16. Every FAIL on every tier is `unknown_tool` (id binding). Three tiers, one failure mode, ordered thresholds. See lab-log. (A `-001` Nano partial exists: 42 trials, killed pre-parallelization, $0.031.) **Super's t\* here was later RETRACTED — token-truncation artifact; see the `-003` row.** |
| ~20:10 | R | `test-parallel-{sequential,parallel6}` — live A/B of probe parallelization, $0.0086. **5.23× speedup** (146.1s → 27.9s at k=6), **0 rate-limit errors** under 6-wide concurrency, byte-identical trial set (same 6 task_seeds / trial_seeds verified in store). Sole outcome difference was one MoE-noise token-cap hit — which surfaced the artifact below. |
| ~20:25 | R | `camp-tool-depth-super-002/captokens` + `.../ultra-002/topup15` — hardening, $0.053. Replayed Super's 10 cap_hit trials on identical worlds at 16k tokens: **6/10 were actually PASSes**, moving depth-16 from 29% → 71%. Ultra depth-15 fresh top-up 6/7 → combined 11/14 = 79%. |
| ~20:45 | R | **`camp-tool-depth-{nano,super,ultra}-003`** — full re-run with caps lifted to 32000/48000, $0.373, **zero ERRORs in 210 trials** (max tokens_out: Nano 8456, Super 5075, Ultra 4293). **Nano BREAKS**: `t*=0.500 CI (0.450, 0.575)` at depth 10+collisions, 100%→0% cliff, Fisher vs depth 9 p=0.0006 — and the CI contains the trap-ray's independent t*=0.55. **Super: found=False** (t*=0.883 retracted). **Ultra: found=False**, 100% at ceiling. See lab-log. |

---

## Glossary: the three query kinds

Every generated task asks one of three question types about a randomly
built world of entities, objects, locations, and timestamped events
(moves, transfers, told claims):

- **`state`** — "Where is the object *actually* located right now?"
  Answerable purely by replaying the event log forward; no perspective
  involved. Tests raw multi-step state tracking. Both Nemotron tiers
  tested so far pass this cleanly even at long causal chains (30 steps).

- **`belief`** — "Where does *entity X* believe the object is?" The
  correct answer depends on what that specific entity actually witnessed
  or was told, which can diverge from the true state (a false belief) if
  the object moved again after their last exposure to it. This is a
  theory-of-mind task, not a memory task — it's the dimension where real
  capability gaps have shown up (Nano's continuity-of-observation error;
  the `witness_cutoff` knob exists specifically to stress this).

- **`order`** — "Which of these two events happened first?" Tests
  whether a model can recover true chronology from a narration that may
  be presented out of order (the `scramble` dimension shuffles narration
  order while every sentence still carries its real timestamp).

`state` and `order` never depend on witness information, so they were
unaffected by the witness-rendering bug; `belief` is the one query kind
that bug directly contaminated.
