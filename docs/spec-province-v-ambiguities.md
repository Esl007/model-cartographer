# Spec: resolve the five ambiguities in the Province V report

*2026-10-06. Source: `docs/reports/province-v-gemma4qat-2026-10-05.md` (its own
"Protocol artefacts" section names most of these). Parent rules:
`docs/spec-frontier-breaking-points.md` §0 — in particular: no git stash/checkout/
restore, never touch `results/campaign.sqlite` in git, no LLM in verification,
warm to `/v1/models`=200 before any campaign, `modal app stop gemma4-qat --yes`
when done. Everything below is buildable and testable offline first; the live
reruns in A1/B/E are the only API spend, all on the Modal QAT endpoint.*

Priority order: A1 and E protect the headline claims; B–D are instrument
hygiene that must land before anything is graded A.

## A1. The trace-rate tension (21/30 paired vs 384/384 in round 2) — HIGHEST

The read-vs-write dissociation claim is only as good as the paired trace
measurement. 30% trace failure on repair-family programs contradicts the
certified reading null and has three candidate causes. Separate them:

1. **Prompt contamination check (offline first).** Read the pair-trace prompt
   path (`pair_trace` in the repair evaluator). If the trace query's context
   contains the EXPECTED (fixed) output, the repair framing, or the statement
   that the program has a bug, the trace is not the round-2 measurement —
   anchoring on the expected output would produce exactly `wrong_value` fails.
   The trace query must be: buggy program + visible input, nothing else,
   rendered by the same `render` path round 2 used. If it isn't, fix it so it
   is, and declare the old /trace records non-comparable.
2. **Cross-evaluator rerun (live, cheap).** Take the exact 9 programs behind
   "traced wrong" (reconstruct via `make_repair_task(latent_params, task_seed)`;
   the report shows this works) and run the BUGGY program through the round-2
   `CodeTraceEvaluator` verbatim — same prompt format, isolated context,
   run id `camp-code-pairtrace-xcheck-gemma4qat-001`, ~45 trials with the
   resample gate. Outcomes:
   - passes there → the gap is the pairing context (cause 1); the dissociation
     table keeps only trace results measured the round-2 way.
   - fails there too → the repair generator's program family (18-statement,
     `solve(a, xs)` shape, injected-bug sites) sits outside round 2's covered
     distribution; the reading null's scope must be narrowed in the lab log and
     the failure hunt extended to this family.
3. Either way, append the resolution to `docs/lab-log.md` and correct the
   report's read-vs-write table if any cell changes.

Acceptance: the dissociation table's "traced ok" column is backed only by
round-2-equivalent trace measurements; the tension has a one-paragraph
explanation with data.

## B. The subset boundary (`sum` → 8 ERRORs)

Decide ONE of, and implement with tests:
- **Preferred: extend the interpreter** with the deterministic builtins a
  repairing model reasonably reaches for: `sum`, `min`, `max`, `abs` (len
  exists). Differential-check each against CPython on ≥300 generated programs,
  same as the dialect work. No prompt change, no bias introduced.
- Fallback: state the allowed subset in `render_repair_prompt` (and the write
  prompt) explicitly. This changes the task for every future run — if chosen,
  mark pre-change and post-change run ids as non-comparable.

Then rerun only the affected tasks (kinds task + confirm 800012 family,
run id suffix `-postfix-001`) and update the report's ERROR accounting.
Acceptance: zero `outside_subset:builtin_call` on the rerun; interpreter
differential tests green.

## C. The 60-second client timeout (4 api_errors, one hot task)

- Raise the modal-provider read timeout to ≥ 360 s (a 16k-token generation at
  L40S speeds can exceed 60 s; the report measured 318 s attempts), or make the
  timeout a function of `max_tokens`. Do NOT raise it globally for Groq/Nebius.
- Stop the double-spend: on a read timeout, do not retry the identical request
  at the same cap (the report shows 2 × 318 s per trial); one retry only if the
  first attempt errored BEFORE streaming/receiving began.
- Rerun the one hot task (kinds task_seed 919830366599466775, trial 0, and
  confirm 800007/800011) to recover real verdicts.
Acceptance: that task returns a scored verdict, and a regression test pins the
timeout derivation.

## D. Resample-gate accounting (three defects, all offline)

1. The gate detects "this FAIL was actually an ERROR" by diffing the inner
   evaluator's SHARED error counter — racy at parallelism > 1 (confirm run:
   3 of 11 FAILs silently skipped resampling). Fix: the inner evaluator must
   report error-ness per call (return value or thread-local), not via a shared
   counter.
2. A FAIL whose 3 resamples are all ERROR (confirm 800012) is currently counted
   `stable`. Change to a third outcome (`unresolved`) that is excluded from
   both stable and noise counts and surfaced in the note.
3. Resample every FAIL or say why not: the gate must emit
   `gate_skipped=N;reason=...` into the note when budget/caps prevent reruns
   (confirm 800017–800019).
Acceptance: zoo tests with planted concurrent ERRORs and planted all-ERROR
resamples produce the right counts; existing gate tests stay green.

## E. Serving nondeterminism at the confirm config (3 of 8 resamples varied)

This contradicts the X.consistency result (pass^1 = pass^5, zero flaky) and
must be attributed to batching before any pass^k claim ships:

- Take the three varying trials (800005, 800006, 800009). Re-run each 5× at
  **parallelism 1** (no concurrent requests at the server), then 5× at
  parallelism 4, same prompts, temp 0. Compare outputs byte-wise.
  - Serial-deterministic + parallel-varying → vLLM continuous-batching
    numerics; document as a serving property, and make the gate run its
    resamples serially for this provider (one-line change if the gate takes a
    parallelism arg).
  - Varying even serially → true sampling-path nondeterminism; the
    X.consistency "zero flaky" claim gets a scope note (it was measured on a
    different task family), and pass^k reporting must include resample CIs.
- ~30 trials, pennies. Run id `camp-determinism-probe-gemma4qat-001`.
Acceptance: one lab-log paragraph stating which world we live in, with the
byte-diff evidence.

## F. Small loose ends (do if touching the files anyway)

- `adversarial=True` accepted by `run_code_repair_campaign` but unused:
  either wire it to the foil-salience search or remove the parameter — a flag
  that silently no-ops is how X-class retractions start.
- `docs/frontier-summary.{json,md}` are modified-uncommitted from the report's
  step 8: coordinator should commit them with the report.
- V.write (rung 3) remains unrun and is NOT part of this spec; it needs its own
  explicit go-ahead.

## Budget and sequencing

Offline first: A1.1, B (interpreter path), C (timeout logic), D — all testable
against planted fakes and the zoo with zero spend. Then one warm-up and one
combined live session for A1.2 + B rerun + C rerun + E (~100 trials total,
well under 1 GPU-hour; stop the app in a finally block). Report back by
appending to the Province V report (a "Resolutions, 2026-10-0X" section) and
the lab log.
