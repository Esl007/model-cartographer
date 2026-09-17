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

**Total spend to date across all providers: ≈ $0.31.**

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
