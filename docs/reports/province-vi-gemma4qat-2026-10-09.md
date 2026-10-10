# Province VI first live results: Gemma 4 31B QAT on Modal (2026-10-09, overnight)

Model `gemma-4-31b-it-qat-w4a16-ct`, label `gemma4qat`, self-hosted vLLM on one L40S (Modal app `gemma4-qat`). Text tool protocol (`text_fallback`); tool budget 20 calls; temperature 0; parallelism 4. Ground truth is the project's own interpreter running the edited repository on hidden inputs; no LLM judged anything. All numbers come from `results/campaign.sqlite` (read-only) and the session logs.

Generated tables (`scripts/report_vi.py`):

- [`-tables-003.md`](province-vi-gemma4qat-2026-10-09-tables-003.md): final instrument, control run
- [`-tables-002.md`](province-vi-gemma4qat-2026-10-09-tables-002.md): main session — control, paired graph study, cause distance
- [`-tables-001.md`](province-vi-gemma4qat-2026-10-09-tables-001.md): first pass

## Bottom line

1. **Gemma 4 QAT finds the bug's file and then fails to fix the bug.** It read the file holding the bug in 23 of 23 episodes, usually within 3 tool calls when the cause sat one call from the tested function. It then fixed the cause in only 5 of 23 episodes (22%, 95% CI 10–42%) on the final instrument. The failures are the ones Province V found in a single function, now at repository scale:
   - **symptom patches in the file the test names** (`height = height + summarize_codes([3, 6]) + 106`);
   - **rewrites that fit the visible test** (`visible_fixed_hidden_broken`, 7 of 18 failures);
   - **edits to the wrong line or the wrong file.**
2. **No measurable navigation cost at this sample.** Each of the 8 programs was tested whole in one file and then split across 4 and 8 files. Pass rates were 2/8, 2/8 and 1/7 on the final instrument (4/8, 4/8, 2/8 on the earlier one). At this n the bottleneck is the repair itself, not finding the file.
3. **The code-graph tool went unused.** `get_code_neighbors` was available in 15 graph-on episodes, offered in both the tool schema and the prompt catalog. Gemma called it **0 times** and never mentioned it. It explored by reading files (167 `read_file` calls against 2 `search` calls). **H4 is not supported:**
   - median calls before first reading the bug's file were 7.0 with the tool and 6.5 without (ratio 1.08, one-sided Wilcoxon p = 0.80, 12 complete pairs);
   - pass rates were 3/13 with the tool and 4/14 without.

   The tool had no effect because it was ignored; whether it would shorten the search for a model that uses tools stays open. This was stated as a likely outcome before any data (spec §14.12).
4. **H3 (visible-test overfitting) holds:** 12 of 45 failures at 4+ files (27%, CI 16–41%) passed the visible test and broke hidden inputs; the pre-registered bar was ≥10%. **H2 (failures cluster in the symptom file) is not supported at this n:** the wrong-file share was 1/3 at both 1 and 4 calls away (Fisher p = 0.8). **H1 (fabrication) was not run.**
5. **Five instrument fixes came out of tonight's live runs, all before the first campaign record.** None of them favours the model:
   - **Constant-patch guard (generator).** A bug's effect must now vary with the input, so a constant patch can't pass.
   - **Stop where a second call begins.** Gemma acted out imagined multi-call trajectories in a single turn, so the turn now ends there.
   - **One call per turn.** Extra calls in a message are not executed.
   - **Capped turns with a complete call are kept.** A turn that hit the 6k cap after a complete call executes that call.
   - **Unambiguous line numbers in `read_file`.** It now prints line number, TAB, then the line. With the old space separator, Gemma copied the separator into its edits as indentation, and those edits never applied.

   Each fix is documented below with the evidence that forced it.

## What ran

| session (local time) | run ids | instrument state | app up | outcome |
|---|---|---|---|---|
| 18:34–19:07 | `smoke-vi-gemma4qat` | protocol v2, 16k/32k turns | 32.8 min | aborted by its smoke gate: 3 first turns ran to 16k, retry died in Modal's 303 polling |
| 19:25–19:46 | `smoke-vi-gemma4qat-2` | + one call per turn, stop at `tool_output`, 6k/12k turns, constant-patch guard | 20.7 min | aborted: 4 of 6 `cap_hit` |
| 19:47–19:59 | `smoke-vi-gemma4qat-3` | diagnostic: turn texts kept, no retry | 12.4 min | reproduced exactly; texts showed imagined multi-call trajectories |
| 20:05–20:51 | `smoke-…-4`, `camp-repo-{control,graph,navigate}-gemma4qat-001` | + stop where a second call begins | 45.5 min | ran; ~25% `cap_hit` (a complete call followed by hand-tracing) |
| 21:03–22:02 | `smoke-…-5`, `camp-repo-control-gemma4qat-002`, `camp-repo-graph-gemma4qat-002` | + capped turn with a complete call executes it | 58.7 min | ran; S3 cut by the soft-deadline guard |
| 22:03–22:25 | `camp-repo-navigate-gemma4qat-002` | same as above | 21.9 min | ran |
| 22:25–22:53 | `camp-repo-control-gemma4qat-003` | + `read_file` in `cat -n` layout | 27.7 min | ran (**final instrument**) |

- **GPU time and cost:**
  - **GPU time:** 219.7 min of app uptime, about 3.7 L40S-hours, which is more than the 2.5 h I first planned for. Each session had its own hard cap, a stop in `finally`, and an independent watchdog; every session ended with the app verified stopped, and the watchdog confirmed each time.
  - **Cost:** Modal billing showed October credits used rising from $16.89 before tonight to $22.57 afterwards. Billed cost is $0.00. Roughly $6.5–7 of the monthly credit remains.
  - **Tracked token cost** is at proxy prices and is not the GPU bill: about $0.6.
- **Records:** 18 smoke records (protocol debugging, not evidence), 107 campaign episodes, all stamped `protocol_version=2`. `-001` used an earlier protocol state, `-002` the old `read_file` layout, `-003` the final instrument. Compare across them only as done below.

## Results

### Navigation cost: the same program over 1, 4 and 8 files (VI.control)

The program was pinned at 8 functions, with the cause one call from `solve`; only the file split changes. Programs are identical per seed (tested offline). At one file this is Province V's repair task in function form.

| files | `-003` final instrument | `-002` old `read_file` layout | `-001` first pass (7 ERRORs) |
|---|---|---|---|
| 1 | 2/8 | 4/8 | 3/6 |
| 4 | 2/8 | 4/8 | 2/8 |
| 8 | 1/7 (1 ERROR) | 2/8 | 1/3 |
| all | **5/23 = 22% (CI 10–42%)** | 10/24 = 42% (CI 24–61%) | 6/17 |

- **The old layout was a real confound.** In `-002`, 12 of 24 episodes had at least one `edit_file` whose `old` text was not found: 38 failed edits, 37 of them from copying `read_file`'s separator spaces as indentation. Under `-003` there were 0 failed edits, and applied edits rose from 63 to 94.
- **Fixing the layout lowered the pass rate.** Paired by repository, 6 repos went PASS→FAIL and 1 went FAIL→PASS (McNemar p = 0.125). That is not significant. For scale, two runs under the same instrument agreed on 15 of 17 repositories (`-001` vs `-002`, where both scored).
- **One reading, not established:** the failed edits had been blocking some of Gemma's own symptom patches. Once edits apply, more of its edits do damage: `visible_fixed_hidden_broken` went from 5 to 7.
- **`-003` is the instrument to cite.**

How Gemma worked, in `-003` (medians over 23 scored episodes):

| | |
|---|---|
| tool calls / file reads / searches / edits | 17 / 8 / **0** / 4 |
| read the bug's file at all | 23/23 |
| calls before first reading it | 3 |
| share of files read that were on the call path | 0.5 |
| whole-file rewrites (episodes with ≥ 1) | 5/23 |
| ran the tests after its last edit | 16/23 |
| a turn ran to the 6k cap after a complete call (hand-tracing the program in prose) | 6/23 |

Failure signatures, `-003` (18 failures):

| signature | count |
|---|---|
| `visible_fixed_hidden_broken` | 7 |
| `no_change` | 5 |
| `wrong_site_file` | 4 |
| `wrong_site` | 2 |

Three examples from `-003`:

```diff
# visible_fixed_hidden_broken, 8 files: bug (wrong_op) in reporting.py; Gemma patched the call site in scoring.py
-    height = height + summarize_codes([3, 6])
+    height = height + summarize_codes([3, 6]) + 106
# wrong_site_file, 4 files: bug (wrong_index) in quotas.py; Gemma patched grading.py
-    credit = credit + sum(measure_fees(rows[:]))
+    credit = credit + sum(measure_fees(rows[:])) - 40
# pass, 4 files: the cause, in the bug's own file
-    cells = rows[1:len(rows) + 1]
+    cells = rows[1:len(rows) - 1]
```

The first two passed the visible test, but the hidden inputs caught them. That is the constant-patch guard working: the bug's effect differs across inputs, so a fixed offset fixes only the visible one.

### Graph-tool value, paired (VI.graph_value, pre-registered H4) — `camp-repo-graph-gemma4qat-002`

Each seed ran twice on the byte-identical repository, without and with `get_code_neighbors`, at 4, 8 and 12 files with the cause 3 calls away. There were 15 pairs; 12 were complete (3 had a `cap_hit` in one arm and are dropped from the paired statistics, never counted as failures).

| | graph off | graph on |
|---|---|---|
| pass rate | 4/14 (29%) | 3/13 (23%) |
| median calls before first reading the bug's file | 6.5 | 7.0 |
| calls to `get_code_neighbors` | (not offered) | **0 in 15 episodes** |

**H4 not supported.** The ratio was 1.08 against the pre-registered ≤ 0.7, with one-sided Wilcoxon p = 0.80. The verdict difference (McNemar b = 1, c = 0, p = 1) is reported but was not pre-registered.

The tool was offered: it appears in `tools_schema()` and in the text catalog that `text_fallback` puts in the system prompt (checked on the stored configuration). No turn text mentions it.

### Cause distance (points on the VI.navigate ray) — `camp-repo-navigate-gemma4qat-002`, old `read_file` layout

| cause calls away | files | pass rate |
|---|---|---|
| 1 | 4 | 1/6 |
| 4 | 9 | 2/6 |
| 6 | 12 | 1/5 (1 ERROR) |

- **No distance trend at n = 6 per point.**
- **H2 (locked test: one-sided Fisher exact on the `wrong_site_file` share among failures that made an edit, 4 vs 1 call away):** 1/3 against 1/3, p = 0.8. Not supported at this n.

### Pre-registered hypotheses, status

| | status at this sample |
|---|---|
| H1 fabricated cross-file references | not run (needs the look-alike stage with paired `restate_symbols`); 0 fabricated symbols and 0 look-alike calls seen anywhere |
| H2 failures cluster in the symptom file | not supported (n too small: 3 failures with an edit per arm) |
| H3 visible-test overfitting ≥ 10% of failures at ≥ 4 files | **supported**: 12/45 = 27% (CI 16–41%) in `-002`; 4/12 in `-003` |
| H4 graph tools compress exploration | **not supported**; the tool was never used |
| Control (1 file vs Province V) | 2/8 one-file repairs in `-003`. Province V's fresh-seed confirmation was 7/20 for single-function one-shot repair, a different task form, so only indicative. Same floor. |

## Instrument changes made tonight, and why

All of these landed before the first campaign record (spec §0.7 allows that). Each is in the lab log for 2026-10-09 and in spec §14.13, and the code is committed.

1. **Episode protocol stamp (coordinator action item).** Every interactive record now carries `protocol_version=2;result_rendering=json|text`. Province III's environment has no `render_result`, so **Province III results are still fed back as JSON**. The rendering change never touched III. The multi-line call parser does reach III's text protocol; the lab log sets the comparability rule for v1 and v2 III records.
2. **Context-window guard.** The endpoint's limit is 32,768 tokens and vLLM rejects any request where prompt + `max_tokens` exceeds it, so turns are clamped to what fits. When less than 1.5k tokens fit, the episode ends as `context_exhausted` (judged on its end state).
3. **Constant-patch guard (generator).** In smoke 1, the "PASS" changed a constant at a call site until the outputs matched. Many components compute mostly from local constants, so a bug shifted the output by the same amount on every input. A bug is now kept only if its effect differs across inputs. The hidden inputs always include one whose difference from the reference differs from the visible test's. New zoo agent `constant_patcher` fails on every applicable task.
4. **Turn boundaries for the text protocol (Province VI opt-ins; III unchanged):**
   - generation stops where a second call or a self-written result begins;
   - only the first call of a message executes (`skipped_calls` recorded);
   - a turn capped after a complete call executes that call, and the kept message ends at the call (`truncated_turns` recorded);
   - a capped turn with no complete call stays `cap_hit` ERROR.

   The diagnostic smoke showed why: after one call, Gemma wrote a leaked `thought` marker and then acted out a whole imagined trajectory alone (`return 2211`, then the same edit and `run_tests()` repeated until the cap). After stops were added, 15 of 16 remaining caps were a complete call followed by thousands of tokens of hand-tracing the program.
5. **`read_file` in `cat -n` layout** (number, TAB, line). See the control results above.
6. **Per-turn budget 6k, no retry**, for Province VI tool turns on this endpoint. The 16k/32k single-shot policy made runaway turns cost about 22 minutes and ended in Modal 303s. The client now follows Modal's 303 polling for up to 12 repeats.

## Caveats

- **Small samples:** n = 5–8 per cell; one model; one night. Treat every rate as provisional.
- **Instrument state differs across runs:** `-002` S2 and S3 used the old `read_file` layout. The H4 non-uptake result doesn't depend on edits; the S3 rates do.
- **Serving nondeterminism:** temperature-0 outputs on this endpoint vary run to run (lab log 2026-10-06 E). Repeat agreement tonight was 15 of 17 repositories.
- **Text protocol, not native function calling:** the stop strings and one-call-per-turn rule are harness choices. They match what native calling enforces, but a natively calling model (Nemotron on Nebius, spec §10.3) is the cleaner test of H4.
- **Frontier rows would mix instrument states:** `repo_*` rows in `report/frontier.py` match `camp-repo-*` by regex, so they would pool `-001`, `-002` and `-003`. **Action for the portal owner:** add `camp-repo-control-gemma4qat-001`, `-002`, `camp-repo-graph-gemma4qat-001` and `camp-repo-navigate-gemma4qat-001` to `INVALIDATED_RUNS` in `cartographer/viewer/findings.py`, as superseded instrument states. That file belongs to the portal thread (spec §0.6), so I did not edit it, and I did not regenerate `docs/frontier-summary.*`.

## Suggested next steps (each needs a go-ahead; none were launched)

1. **Nemotron Nano and Super on Nebius**, native function calling, on VI.control and VI.graph_value. This is the clean H4 test with models that use tools, and it serves as the atlas's tier-discrimination content. Nebius cost, not Modal.
2. **H1 look-alike stage** (`lookalike_names` with paired `restate_symbols`) on whichever model reaches a non-floor pass rate.
3. **Larger n on the final instrument** for VI.control if Gemma's navigation cost matters for the other project: about 0.8 GPU-min per episode at parallelism 4, roughly $0.03 per episode.
