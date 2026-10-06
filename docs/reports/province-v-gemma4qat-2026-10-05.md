# Province V (code repair): Gemma 4 31B QAT on Modal, 2026-10-05

Model `gemma-4-31b-it-qat-w4a16-ct`, label `gemma4qat`, provider `modal` (app `gemma4-qat`, one L40S, vLLM).
All numbers come from `results/campaign.sqlite` (read-only queries) or from the stdout of the campaign and summary scripts.
`$` figures are the store's tracked token cost at proxy prices (`pricing.py`: $0.10 / $0.30 per M tokens). They are not the GPU bill.

## Run summary

| item | value |
|---|---|
| session start | 2026-10-05 22:57:19 PDT |
| `modal deploy` | 4.4 s; URL host matched the key file's `modal_url` host (`<redacted>`) |
| warm-up (`/models` until HTTP 200) | 368 s (cold start) |
| tiny `Client.chat` confirm | `'ready'`, finish `stop`, 2 tokens out, 2.6 s |
| app stopped | 2026-10-06 00:09 PDT (07:09 UTC); `modal app list` shows `gemma4-qat` **stopped, 0 tasks** |
| total wall-clock (start to app stop) | ~72 min |
| approx GPU time | ~1.2 L40S GPU-hours: container up from about 22:58 PDT to 00:09 PDT, including the 368 s cold start |
| parallelism | 4 (smoke 1; `camp-code-repair-kinds-gemma4qat-002` rerun at 2) |
| tier kwargs (standard, parallelism overridden) | `budget=140, cost_cap=0.21, k_trials=5, parallelism=4, early_stop=True, resample_gate=True`, `ray` = registry default |
| V.repair ray | `n_statements 18`, `hidden_inputs 0->8`, `bug_distance 1->8` |
| V.repair_regressions ray | `n_statements 18`, `n_bugs 1->3`, `bug_mix 0.0->1.0`, `hidden_inputs 4` |

Tracked $ per run id. Each figure is the sum of the base id plus its `/trace`, `/verify` and `/trace/verify` records:

| run id | tracked $ | records (base / verify / trace / trace-verify) | runtime |
|---|---|---|---|
| `smoke-v-gemma4qat` | 0.0014 | 2 / - / 2 / - | 2.0 min |
| `camp-code-repair-gemma4qat-001` | 0.0086 | 5 / 6 / 5 / 6 | 5.5 min |
| `camp-code-repair-kinds-gemma4qat-001` | 0.0088 | 5 / 9 / 5 / 9 | 11.2 min |
| `camp-code-repair-kinds-gemma4qat-002` (rerun, parallelism 2) | 0.0066 | 5 / 6 / 5 / 6 | 11.5 min |
| `camp-code-repair-confirm-gemma4qat-001` | 0.0304 | 20 / 24 / 20 / 24 | 33.3 min |
| **total** | **~0.056** | | |

Steps not run:
- Step 4 (adversarial rerun): **not run**. Its condition (both campaigns all PASS) did not hold. Note that the runner accepts `adversarial=True`, but `_CodeFixBase` stores it with the comment "accepted for runner symmetry; tasks are not searched", so the flag would have had no effect.
- V.write: **not run** (held out by design).

## Smoke (`smoke-v-gemma4qat`, cost cap 0.05)

| t | config | trace | repair verdict | verifier_detail | finish | tokens_out |
|---|---|---|---|---|---|---|
| 0 | hidden 0, bug_distance 1 | PASS | FAIL | `bug_kinds=wrong_op;detail=wrong_site;traced_ok=1;bug_distance=3;hidden=0` | stop | 1384 |
| 1 | hidden 8, bug_distance 8 | FAIL | PASS | `bug_kinds=stale_update;detail=pass;traced_ok=0;bug_distance=8;hidden=8` | stop | 511 |

There were 0 ERRORs. At t=1 the model's returned block was the full function with one statement added (`diff_lines=1`). It added `bins.append(height) # Added this line to include the missing 0` before the final appends, and that edit passed all 8 hidden inputs. The t=0 smoke task is the same task as trial 1 of `camp-code-repair-gemma4qat-001`: same config and trial seed, and it got the same signature.

## Campaign 1: V.repair, `camp-code-repair-gemma4qat-001`

| field | value |
|---|---|
| found | false |
| t_star / CI | null / null |
| degradation_onset | null |
| config_at_break | {} |
| pass_curve | [[0.0, 0.6]] |
| note | `degenerate: baseline t=0 does not pass (pass_rate=0.600 < 0.7)` |
| gate_stable / gate_noise | 2 / 0 |
| certificate | reproducible=false; reasons: "no breaking point found", "no fresh-seed confirmation" |
| base records | 3 PASS / 2 FAIL / 0 ERROR |
| /verify (resamples) | 0 PASS / 6 FAIL / 0 ERROR: both failures reproduced 3/3 with the same signature |
| finish_reason=length | 0 (all suffixes) |
| max tokens_out | 1608 (base), 1507 (/verify), 1161 (/trace) |
| ERROR breakdown | none |

The ray stopped at t=0. Only one dial level was probed.

| level (hidden_inputs, bug_distance dial) | PASS | FAIL signatures |
|---|---|---|
| (0, 1) | 3/5 | `wrong_value` 1, `wrong_site` 1 |

## Campaign 2: V.repair_regressions, `camp-code-repair-kinds-gemma4qat-001` and rerun `-002`

The rule triggered: ERROR was 2/5 = 40 % of base records (1 `api_error`), above the 10 % threshold. I re-warmed (`/models` returned 200 in 1 s), dropped parallelism to 2 and re-ran once as `-002`. Both runs drew the same 5 tasks (same task_seeds) and produced identical verdicts and signatures, including the same 2 ERRORs on the same tasks.

| field | -001 (par 4) | -002 (par 2) |
|---|---|---|
| found | false | false |
| t_star / CI | null / null | null / null |
| degradation_onset | null | null |
| config_at_break | {} | {} |
| pass_curve | [[0.0, 0.0]] | [[0.0, 0.0]] |
| note | `degenerate: baseline t=0 does not pass (pass_rate=0.000 < 0.7)` | same |
| gate_stable / gate_noise | 3 / 0 | 2 / 0 |
| certificate | reproducible=false; "no breaking point found", "no fresh-seed confirmation" | same |
| base PASS/FAIL/ERROR | 0 / 3 / 2 | 0 / 3 / 2 |
| /verify PASS/FAIL/ERROR | 0 / 9 / 0 | 0 / 6 / 0 |
| length | 0 | 0 |
| max tokens_out (base / verify / trace) | 1481 / 1579 / 1161 | 1155 / 1072 / 1161 |
| ERROR breakdown | `outside_subset:builtin_call` 1 (the builtin is `sum`), `api_error` (`error=timeout`) 1 | identical, same two tasks |

`-002` has gate_stable 2, not 3, because the third FAIL was not resampled. See the gate note under Protocol artefacts.

| level (n_bugs, bug_mix, hidden_inputs 4) | PASS | FAIL signatures | ERROR |
|---|---|---|---|
| (1, 0.0) in -001 | 0/5 | `wrong_site` 2, `introduced:augassign_rebinds` 1 | 2 |
| (1, 0.0) in -002 | 0/5 | `wrong_site` 2, `introduced:augassign_rebinds` 1 | 2 |

Only t=0 was probed. The `bug_distance` dial is unset on this ray (latent 0 = any site), and the realized distances were 5, 22, 12, 10, 12.

## Step 5: fresh-seed confirmation, `camp-code-repair-confirm-gemma4qat-001`

- Config: the most failing probed config, V.repair_regressions t=0 (0 PASS among scored trials in both runs): `{n_statements 18, n_bugs 1, bug_mix 0.0, hidden_inputs 4}`.
- Method: 20 trials with trial seeds 800000..800019. I used `CodeRepairEvaluator` (pair_trace on) wrapped in `ResampleGatedEvaluator(n=3)`, at parallelism 4 and cost cap 0.21.

| field | value |
|---|---|
| base records | **6 PASS / 11 FAIL / 3 ERROR** (6/17 scored pass) |
| after gate | 7/20 returned True (6 PASS + 1 FAIL judged noise) |
| gate_stable / gate_noise | 7 / 1 |
| /verify | 3 PASS / 16 FAIL / 5 ERROR |
| evaluator errors (base + verify) | 8 |
| length | 0 |
| max tokens_out | 1726 (base), 1694 (/verify), 1204 (/trace) |
| ERROR breakdown (base) | `outside_subset:builtin_call` 2 (`sum`), `api_error` (`timeout`) 1 |
| ERROR breakdown (/verify) | `outside_subset:builtin_call` 4 (`sum`), `api_error` (`timeout`) 1 |
| FAIL signatures (base) | `wrong_site` 4, `wrong_value` 4, `visible_fixed_hidden_broken` 1, `regressed` 1, `introduced:predicate_frozen` 1 |
| FAIL signatures (/verify) | `wrong_site` 7, `visible_fixed_hidden_broken` 3, `regressed` 3, `wrong_value` 3 |

Resample consistency at the confirm config was not perfect. Of the 8 FAILs the gate resampled:

| trial | resample outcome |
|---|---|
| 800005 | 1/3 PASS |
| 800006 | signatures varied (`wrong_value`/`wrong_site`) |
| 800009 | 2/3 PASS (counted as noise) |
| 800011 | 1 FAIL + 2 ERROR |
| 800012 | 3 ERROR (`outside_subset`); counted as *stable* |

The other three resampled FAILs (800000, 800001, 800002) reproduced 3/3. In campaigns 1 and 2 every resample matched its original.

## Pooled per-dial summary

Only t=0 was probed on either ray, so the dial levels covered are:

| run | dial level | PASS / FAIL / ERROR |
|---|---|---|
| V.repair | hidden 0, bug_distance dial 1 | 3 / 2 / 0 |
| V.repair_regressions | n_bugs 1, bug_mix 0.0, hidden 4 (-001) | 0 / 3 / 2 |
| confirm | same config, fresh seeds | 6 / 11 / 3 |

Levels hidden_inputs 1..8, bug_distance dial 2..8, n_bugs 2..3 and bug_mix > 0 were **not run**, because both rays stopped at the degenerate baseline.

Base FAIL signatures pooled over `-001` V.repair + kinds `-001` + confirm (17 FAILs):

| signature | count |
|---|---|
| `wrong_site` | 7 |
| `wrong_value` | 5 |
| `visible_fixed_hidden_broken` | 1 |
| `regressed` | 1 |
| `introduced:augassign_rebinds` | 1 |
| `introduced:predicate_frozen` | 1 |
| `no_change` | 0 |
| `runtime_error:*` | 0 |
| `signature_changed` | 0 |

## Read-vs-write pairing

`traced_ok` comes from `;traced_ok=` in the repair record's verifier_detail (also `realized_dims.traced_ok`). It means the model's trace answer equals the buggy program's output on the visible input. Base repair records only; ERRORs are listed separately; the `-002` duplicates are excluded.

| run | traced ok + repaired | traced ok + not repaired | traced wrong + repaired | traced wrong + not repaired | ERROR (traced_ok) |
|---|---|---|---|---|---|
| V.repair -001 | 3 | 1 | 0 | 1 | 0 |
| kinds -001 | 0 | 2 | 0 | 1 | 2 (both traced ok) |
| kinds -002 (dup) | 0 | 2 | 0 | 1 | 2 (both traced ok) |
| confirm | 4 | 6 | 2 | 5 | 3 (all traced ok) |
| **pooled (-001 + kinds -001 + confirm)** | **7** | **9** | **2** | **7** | 5 |
| smoke | 0 | 1 | 1 | 0 | 0 |

Pass rate of the `<run_id>/trace` records themselves:

| run | /trace | /trace/verify |
|---|---|---|
| smoke | 1/2 | - |
| V.repair -001 | 4/5 | 3/6 |
| kinds -001 | 4/5 | 6/9 |
| kinds -002 | 4/5 | 3/6 |
| confirm | 13/20 | 12/24 |
| pooled /trace (-001 + kinds -001 + confirm) | 21/30 | |

Every trace FAIL has detail `wrong_value`, and no trace record is ERROR or length-capped.

## Bug kinds

`bug_kinds=` in the repair record's verifier_detail. Base records pooled over V.repair -001 + kinds -001 + confirm:

| bug kind | PASS | FAIL | ERROR |
|---|---|---|---|
| `wrong_op` | 3 | 10 | 3 |
| `wrong_index` | 1 | 4 | 1 |
| `off_by_one` | 3 | 2 | 1 |
| `stale_update` | 2 | 0 | 0 |

Smoke: `wrong_op` 0/1, `stale_update` 1/1.

No task drew `alias_leak`, `rebind_vs_mutate`, `shadow`, `default_shared` or `frozen_predicate`. bug_mix was 0.0 on the regressions ray, and 0.5 with only 5 trials on V.repair.

## Failing examples

These were reconstructed by re-running `make_repair_task(params, task_seed)` locally from the record's latent_params and task_seed. Model outputs were obtained by parsing the returned block with our parser and running it in our interpreter.

### 1. `camp-code-repair-confirm-gemma4qat-001`, trial 800002: `visible_fixed_hidden_broken`, traced_ok=1, bug `wrong_index`

- Visible line: `solve(5, [0, 1])` returns `[9, 1, 1, 0, 22, 12, 8, 2, 2]`, but it should return `[9, 1, 1, 1, 22, 12, 8, 3, 2]`.
- True fix (line 4): `credit = parts[0]` -> `credit = parts[-1]`.
- Model's edit, same line:

  ```python
      # The bug was overwriting credit with parts[0].
      # Based on the expected output for solve(5, [0, 1]),
      # it should use parts[1] to get the values 1 and 3.
      credit = parts[1]
  ```

On the visible input `parts[1] == parts[-1]`. Truth vs model on the hidden inputs differ at elements 3 and 7. For example, `(9, [-8, 9])` gives truth `[9,1,1,0,22,12,8,11,2]`. The record's other hidden inputs were `(3, [11, -1])`, `(8, [-6])` and `(-7, [7, 4, -9])`. The submission matches the reference only where `parts[1] == parts[-1]`. It reproduced 3/3 on resample.

### 2. `camp-code-repair-confirm-gemma4qat-001`, trial 800001: `regressed`, traced_ok=0, bug `wrong_op`

- Visible line: `solve(8, [8, 4, 8])` returns `28`, but it should return `100`.
- True fix (line 28): `depth -= weight` -> `depth += weight`.
- Model's block: it made the correct edit (`depth += weight # Changed from -=`) and also added `depth += 15 # Offset to reach 100`.

| input | truth | buggy | model |
|---|---|---|---|
| `(8,[8,4,8])` (visible) | 100 | 28 | 115 |
| `(14,[-6,-1,-5])` | 136 | 16 | 151 |
| `(14,[18,0,4,-3,12,7])` | 136 | 16 | 151 |
| `(-1,[5,-6])` | 49 | 49 | 64 |
| `(8,[-8,0])` | 100 | 28 | 115 |

The model's added offset of 15 is the gap between 100 and 85, so the model had computed 85 for its own corrected function. Its trace of the buggy program on the visible input was also wrong (traced_ok=0). Reproduced 3/3.

### 3. `camp-code-repair-kinds-gemma4qat-001`, trial 4 (same in -002): `wrong_site`, traced_ok=1, bug `wrong_op`

- Visible line: `solve(8, [8, 0, 3])` returns `47`, but it should return `85`.
- True fix (line 18): `budget -= len(batch) + batch[-1]` -> `budget += len(batch) + batch[-1]`.
- Model's block: it left line 18 unchanged and inserted `count = len(cells) # Update count to reflect the new size of cells` at line 6.

| input | truth | buggy | model |
|---|---|---|---|
| `(8,[8,0,3])` (visible) | 85 | 47 | 55 |
| `(-5,[2,1,2,4,-5])` | 73 | 35 | 35 |
| `(8,[10])` | 83 | 45 | 45 |
| `(6,[2,7,-1,17,-6])` | 85 | 47 | 48 |
| `(-8,[-9,15,8])` | 64 | 26 | 26 |

Reproduced 3/3 in -001 and was not resampled in -002.

Other failures seen while reconstructing tasks:
- V.repair -001 trial 0 (`wrong_value`, traced_ok=0): the true fix was `cost -= values[0]` -> `+=` at line 30. The model changed `cost + 6` -> `cost + 4` at line 5. Visible input: truth 60, buggy 62, model 58.
- Kinds trial 2 (`introduced:augassign_rebinds`): the true fix was `marks[0]` -> `marks[-1]`. The model changed `slots = marks` -> `slots = marks[:]`. On every input the model's output is 1 less than the buggy output and 9 less than truth.
- Confirm trial 800009 (`introduced:predicate_frozen`): the model made the correct off-by-one fix and also changed `if elem > height` -> `if elem >= height`. Its output is truth + 6 on all 5 inputs. The signature name comes from a foil-output match. The edit itself is a `>` -> `>=` change, not a frozen loop predicate.

## Protocol artefacts

1. **`outside_subset:builtin_call`: 8 records, all for `sum(...)`.**

   | run | records | tasks |
   |---|---|---|
   | kinds -001 | 1 | |
   | kinds -002 | 1 | same task as -001 |
   | confirm base | 2 | |
   | confirm /verify | 4 | 3 of them the same task, 800012 |

   The repair prompt (`render_repair_prompt`) does not tell the model which subset is allowed, and `sum` is ordinary Python. These rejections are therefore the instrument's subset boundary, not evidence of a failed repair. In these submissions the model put `sum` at a line other than the bug site. Examples: `spare += queue[-1]` -> `spare += sum(queue)`, and `spare += sum(codes) + len(codes)`. For the kinds task, the true fix was at line 4 and the `sum` edit was at line 31, so the submission would very likely have failed if it had been executed. That is an inference; the code was not executed, by design. These records count as ERROR, never FAIL, as the spec requires.

2. **`api_error` (`error=timeout`): 4 records.**

   | run | task / trial | latency of final attempt |
   |---|---|---|
   | kinds -001 | task_seed 919830366599466775, trial 0 | 318,073 ms |
   | kinds -002 | same task, same trial | 318,130 ms |
   | confirm base | trial 800007 | 318,282 ms |
   | confirm /verify | trial 800011 | 318,250 ms |

   The same kinds task timed out at both parallelism 4 and parallelism 2. The client's per-request socket timeout is 60 s (`_post(..., timeout=60)`) with retries. The evaluator then retries the call once. Each of these trials therefore spent about 2 x 318 s. The only response logged with no ERROR is the 96.8 s `/verify` latency. Because the timeouts repeat on the same task at lower load, the likely cause is a generation for that prompt that takes longer than the 60 s read timeout under non-streaming. That would be a long output, below the 16,000-token cap, since no record hit `length`. The cause is probably not a server fault. This is an inference: the response was never received, so its length is unknown. It points at the instrument's timeout rather than the endpoint.

3. **Truncation: 0** `finish_reason=length` records across all new run ids. Max tokens_out was 1726.

4. **Resample-gate counting quirk.** `ResampleGatedEvaluator` decides whether a FAIL was an ERROR by comparing the inner evaluator's shared `error_count` before and after the call. At parallelism > 1, an ERROR in another thread raises that counter, and the FAIL is then returned without resampling. In the confirm run, 11 base FAILs led to 8 resampled (24 /verify records). The 3 FAILs not resampled were trials 800017, 800018 and 800019. In kinds -002, 1 of 3 FAILs was not resampled. Separately, a FAIL whose 3 resamples are all ERROR (trial 800012) is counted as `stable`. These are gate-accounting issues; they do not change any verdict.

5. **Serving determinism.** In campaigns 1 and 2 (5 trials each) all resamples reproduced the original verdict and signature. At the confirm config, 3 of the 8 resampled FAILs varied on resample (800005, 800006, 800009). Serving was not fully deterministic in this run.

6. **Runner flag.** `adversarial=True` is accepted by `run_code_repair_campaign`, but the evaluator does not use it. Moot here, since step 4 was not triggered.

7. **Side effect of step 8.** `scripts/frontier_summary.py` rewrote `docs/frontier-summary.json` and `.md`, which is its normal output; both are now modified in git status.

   | capability | status | grade | result | cause |
   |---|---|---|---|---|
   | Fixes stacked bugs without breaking anything else | BREAKS | B | breaks at 1 bug, 0/6 pass | `wrong_site`, 67 % of failures |
   | Fixes a one-bug function when shown a failing example | DEGRADES | C | at 0 hidden checks, 3/5 pass | `wrong_value`, 38 % |

## App status

`.venv/bin/modal app stop gemma4-qat --yes` was run at 07:09 UTC. `.venv/bin/modal app list` then showed `ap-ftrnh1c2ZNJxuYzuTaPdXA gemma4-qat stopped, Tasks 0`, stopped 2026-10-06 00:09 PDT. No other app is listed.

## Resolutions, 2026-10-06

This was a single live session on the same Modal app and checkpoint, with parallelism ≤ 4.
- **Deploy:** 4.7 s. The URL host matched the key file.
- **Warm-up:** 309 s (cold) until `/models` returned 200. A tiny chat confirmed `'ready'`.
- **Stop:** `modal app stop gemma4-qat --yes` ran at 03:46 PDT, 77.3 min after deploy (≈1.3 L40S GPU-hours including the cold start). `modal app list` then showed `ap-NihH4LSifUY8y3oYhkGAKy gemma4-qat stopped`.
- **Tracked $ (proxy prices):**

  | run id | records | tracked $ |
  |---|---|---|
  | xcheck | 80 | 0.0268 |
  | postfix | 38 | 0.0187 |
  | determinism | 30 | 0.0188 |
  | **total** | 148 | **≈0.064** |

### A1.2: cross-evaluator rerun (`camp-code-pairtrace-xcheck-gemma4qat-001`)

Each buggy program is traced with the round-2 `CodeTraceEvaluator` verbatim, k=5, resample gate n=3. The programs ran one at a time. The step hit its 40-min cap after **6 of 9** programs; confirm 800010, 800012 and 800019 were not run.

| source | task_seed | round-2 trace pass | gate |
|---|---|---|---|
| V.repair -001 trial 0 | 421996770361213397 | 0/5 | 5 stable |
| kinds -001 trial 3 | 56880350641256372 | 0/5 | 5 stable |
| confirm 800001 | 855218970775950692 | 5/5 | - |
| confirm 800004 | 988880262743947176 | 5/5 | 1 noise |
| confirm 800005 | 159399059387578458 | 0/5 | 5 stable |
| confirm 800006 | 95612652785695208 | 5/5 | - |

Result: both causes are present.
- **Template (cause 1):** 3 of the 6 programs trace correctly every time under the round-2 template. Their old "traced wrong" was an artefact of the bespoke pair-trace template.
- **Program family (cause 2):** the other 3 programs fail 5/5 under the round-2 template too, and every FAIL reproduced on resample. The repair generator's family (18 statements, `solve(a, xs)` shape) contains programs Gemma 4 QAT cannot trace. Round 2's 384/384 reading null therefore does not cover this family, and its scope is narrowed accordingly (lab log).

### B: subset boundary, rerun of `outside_subset:builtin_call` tasks (with C)

The tasks were rebuilt from the stored latent_params and task_seed with `scripts/rerun_repair_seeds.py`. All 7 reconstructed prompts matched the stored prompts byte-for-byte. They ran through the stock `CodeRepairEvaluator` (pair_trace on, now the round-2 template) under the gate (n=3). Run ids: `camp-code-repair-kinds-gemma4qat-postfix-001` and `camp-code-repair-confirm-gemma4qat-postfix-001`. There were **0 `outside_subset:builtin_call`** and 0 ERROR across all 38 records (base, verify and trace).

| task | old | postfix base | traced_ok | gate |
|---|---|---|---|---|
| kinds trial 1 (757445687747891210) | ERROR sum | FAIL `wrong_site` | 1 | stable |
| confirm 800012 | FAIL; resamples 3× ERROR sum | FAIL `wrong_value` | 0 | stable |
| confirm 800013 | ERROR sum | FAIL `wrong_value` | 1 | stable |
| confirm 800015 | ERROR sum | FAIL `visible_fixed_hidden_broken` | 1 | stable |

All four `sum` tasks are FAILs once scored. That matches the original inference that those submissions would have failed.

### C: timeout tasks under the new modal read timeout

| task | old | postfix base | base latency | gate call total | verdict back? |
|---|---|---|---|---|---|
| kinds task_seed 919830366599466775, trial 0 | ERROR timeout ×2 runs (318 s ×2 each) | **PASS** | 98 s | 137 s | yes |
| confirm 800007 | ERROR timeout | **PASS** | 42 s | 81 s | yes |
| confirm 800011 | FAIL; resamples 1 FAIL + 2 ERROR | **PASS** | 91 s | 121 s | yes |

All three returned a scored verdict, with no timeouts and no retries. The slowest single call in the postfix run was 183 s (800015), under the new 700 s modal read timeout (max(360, 60 + 16000/25)) and above the old 60 s.

### Corrected ERROR accounting (base records)

| run | as reported | corrected (postfix verdicts substituted) |
|---|---|---|
| kinds -001 | 0 / 3 / 2 | 1 / 4 / 0 |
| confirm | 6 / 11 / 3 | 7 / 13 / 0 (7/20 = 35 % pass) |

The confirm resample entries 800011 (2 ERROR) and 800012 (3 ERROR) are replaced by a postfix PASS and a stable FAIL, respectively. No `outside_subset` or `api_error` remains in any Province V result for this model.

### Corrected read-vs-write table (pooled -001 + kinds -001 + confirm)

| | traced ok + repaired | traced ok + not repaired | traced wrong + repaired | traced wrong + not repaired | ERROR |
|---|---|---|---|---|---|
| as reported (old template) | 7 | 9 | 2 | 7 | 5 |
| + 5 ERRORs rescored (postfix, round-2 trace) | 9 | 12 | 2 | 7 | 0 |

How the A1.2 result changes the "traced wrong" cells:
- 3 of the 9 traced-wrong programs (confirm 800001, 800004, 800006) trace correctly round-2-style. They belong in "traced ok"; all three were not repaired in base.
- 3 programs (V.repair t0, kinds t3, confirm 800005) are confirmed traced-wrong.
- 3 programs are unchecked.

So the round-2-backed "traced wrong" cell is **3 confirmed, 3 unknown**, and **≥ 3 traced-ok + not-repaired** move in. The dissociation (correct reading, failed writing) gets stronger, not weaker.

### E: serving determinism (`camp-determinism-probe-gemma4qat-001/{serial,parallel}`)

Setup: repair prompt only, temperature 0, 5 reps per trial serially (one request in flight), then 5 reps in a pool of 4. Raw `response_text` was compared by sha256.

| trial | serial: distinct outputs / verdicts | parallel: distinct outputs / verdicts |
|---|---|---|
| 800005 | 2: first rep `wrong_site`, reps 2–5 identical `pass` | 3: `pass`×3 (same bytes as serial), `wrong_site`×2 (two different texts) |
| 800006 | 2: first rep `wrong_value`, reps 2–5 identical `wrong_site` | 3 |
| 800009 | 2: first rep `pass`, reps 2–5 identical `introduced:predicate_frozen` | 3 |

Outputs vary **even serially**. In each serial run the first request differed and the next four were byte-identical. That points to a prefix-cache cold/warm numeric path rather than batching alone. Parallel runs add more variety. This is true sampling-path nondeterminism at temperature 0 on this endpoint. Consequences:
- The gate is **not** switched to serial for modal, because serialising would not make it deterministic.
- X.consistency's "zero flaky" needs a scope note.
- pass^k on this endpoint must carry resample CIs.
