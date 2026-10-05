# Province V — Code repair and writing: requirements

*2026-10-05. Binding parts: §0–§3. Parent rules: `docs/spec-frontier-breaking-points.md` §0–§1 (standing rules, reproducibility certificate). Evidence: `docs/lab-log.md` 2026-10-05 (Gemma 4 QAT: ~700/700 compact code-reading trials), `docs/research/code-reading-failure-modes.md`.*

## 0. Hard rules for whoever builds this

1. `results/campaign.sqlite` is live paid data: never tracked, never rewritten; stage only your own files by explicit path; never `git stash` / `checkout` / `restore` / `clean` / `reset`. Do not commit; the coordinator commits.
2. **No LLM anywhere in ground truth.** Correctness is decided by running code in *our* interpreter on hidden inputs.
3. **No sandbox, no `exec`, no `eval` on model output, ever.** Model code is parsed with the standard `ast` module into *our* `Program` AST (`codeworld/model.py`). Anything outside the subset (imports, classes, `while` without our bound, attribute access beyond the method whitelist, comprehensions, `try`, `lambda`, recursion, floats, string formatting, `global`) is rejected before any execution and recorded as `outside_subset`, which is an ERROR-class protocol outcome, not a model failure. CPython `exec` is allowed only in tests, as a differential check, on programs *we* generated.
4. Token budget from `default_token_budget(model)`; `finish_reason=length` is ERROR, never FAIL.
5. No API spend. Build, test, zoo-validate, stop. The live plan (§9) runs only on the user's say-so.
6. Do not edit `cartographer/viewer/*`, `cartographer/exec/{client,keys,pricing}.py`, `scripts/run_all_provinces.py`, `docs/lab-log.md`.

## 1. Why this province, and what it must answer

Gemma 4 31B QAT reads every compact program we can generate (384/384 today, ~700 cumulative) while Gemma 3 breaks on list aliasing and negative `%`. Reading is therefore no longer where a frontier-class model's coding boundary lies. Province V asks the next question on the *same programs*: can the model **change** code correctly?

Three questions, in order of difficulty, each a rung:

| Rung | Task shown to the model | What it isolates |
|---|---|---|
| **V-a Repair** | a program with one injected bug, the wrong output it produces, and the expected output; asked for the corrected program | can it locate and fix a fault it can demonstrably *read* |
| **V-b Repair without regressions** | same, but hidden checks cover inputs the shown example does not | does it fix the cause or patch the symptom |
| **V-c Write from spec** | a plain-English description plus 2–3 input/output examples of a function; asked to write it | can it produce code that meets a spec on unseen inputs, including edge cases |

The headline comparison is **read vs write on the same program**: for every repair task the original program is also a tracing task we already know the model solves. A model that traces the buggy program correctly (it can say what the wrong output is) but cannot fix it has a boundary that is specific to code change.

## 2. Non-negotiables

| Constraint | Means |
|---|---|
| Latent structure first | a repair task is `(Program clean, Program buggy, bug_site, visible_input, hidden_inputs)`; a writing task is `(Program reference, spec_text, examples, hidden_inputs)`. Natural language is rendered from these. |
| Ground truth by execution in our interpreter | a submission is correct iff, parsed into our AST, it produces the reference program's output on **every** hidden input (and the visible one). Outputs compared by Python equality after tuple→list normalisation, type-strict (`True ≠ 1`). |
| Functions as the unit | every task is a single function `def solve(...)` over subset types, called by a harness we control; inputs are value tuples. This gives hidden inputs a clean entry point and keeps model output small. Add `Call` of a top-level def with multiple positional args; no `*args`, no keywords in calls. |
| Difficulty is structure | bug *kind* and bug–symptom distance are the levers, not program length (≤ 25 statements; the length ray is the control). |
| Deterministic | same `(params, seed)` ⇒ identical task, bytes included. sha256 pattern, no `random`. |
| Failure named by mechanism | every wrong submission gets a signature (§6). |
| Fits the seams | `ExperimentRecord(kind=code_repair | code_write)`, an `Evaluator` for `find_breaking_point`, `ResampleGatedEvaluator` compatible (`run_suffix`), `Spec` rows in `report/frontier.py`, registry ids `V.*`, cause words. |

## 3. Deliverables and acceptance

**Code** (`cartographer/codefix/`): `parse.py` (source → `Program` or `SubsetViolation`), `harness.py` (call `solve` on inputs under our interpreter; output capture), `bugs.py` (bug injectors = answer-*changing* mutators with a site), `tasks.py` (repair/write task generation, hidden-input generation, spec rendering), `verify.py` (judge a submission; signatures), `evaluator.py` or additions in `exec/evaluator.py` (`CodeRepairEvaluator`, `CodeWriteEvaluator`, rays, runners), registry entries, frontier rows, `tests/test_codefix*.py`.

**Accepted when all hold:**
1. **Parser round-trip:** for 3,000 generated programs (incl. `while`, defs with defaults, all dials), `parse(render(p).source)` yields a `Program` whose execution equals `p`'s on 5 random inputs, and whose re-rendered source equals the original source. Every construct outside the subset in a hand-written list of 25 snippets (imports, classes, lambda, comprehension, try, f-string, `while True`, recursion, `global`, attribute access, float literal, `**`, `is`, chained comparison, tuple unpacking, `del`, `assert`, star-args, nested def > 1, `print(x, y)`, `input()`, `open`, walrus, `match`, `async`) is rejected with `SubsetViolation` naming the construct.
2. **Bugs are load-bearing and single:** every injected bug changes the output on ≥ 1 of 8 generated inputs (else re-drawn), the visible example is always one where it does, and `bug_site` names exactly one statement.
3. **Oracles:** the reference solution passes its own task 100 %; a "revert the bug" oracle passes every repair task; a "patch the visible example only" oracle (hard-codes the visible input's expected output with an `if`) passes V-a and **fails ≥ 90 % of V-b** tasks (hidden inputs must be designed to catch it).
4. **Zoo** (§7): each planted writer fails exactly on its mechanism with its signature; the perfect writer never fails; `find_breaking_point` on the repair ray with the symptom-patcher fake attributes the break to `hidden_inputs`.
5. **Submissions outside the subset are never executed**: a test feeds `import os; os.system("echo x")` and `__import__`, `open(...)`, and a 10⁹-iteration `while`; all four are rejected at parse time with no side effect, no timeout.
6. `python3 scripts/frontier_summary.py` shows the V rows as NOT TESTED without error; `python3 -m cartographer.campaigns.registry --write-catalog` lists the V ids.
7. Full suite green (currently 954).

## 4. Tasks

### 4.1 Function form
Generation reuses `codeworld.generate` with a new wrapper: the generated statements become the body of `def solve(<params>)` where 1–3 of the initial assignments are turned into parameters (int, list, str), the query target becomes `return <target>`, and the body may call the program's own helper defs. `Program.entry = "solve"`, `Program.param_kinds`. Hidden inputs are drawn per parameter kind (ints −9..20 incl. 0 and negatives; lists of 0–6 ints incl. empty and single; strs of 0–8 chars) and filtered so the reference runs without error under truth; at least one empty-list / zero / negative input is kept when the parameter kind allows it.

### 4.2 Bug injectors (`bugs.py`) — one per existing foil family, so a fix requires the exact semantics the tracing province measured
| Bug | Edit | Reading-province sibling |
|---|---|---|
| `off_by_one` | `range(a, b)` → `range(a, b-1)` or `+1`; slice bound ±1 | inclusive_range / slice_end |
| `alias_leak` | `c = a[:]` → `c = a` (or add an alias then mutate through it) | value_semantics |
| `rebind_vs_mutate` | `a += [x]` ↔ `a = a + [x]` where an alias exists | augassign_rebinds |
| `wrong_op` | `//`↔`/`-free swap: `//`↔`%`, `+`↔`-`, `<`↔`<=`, `==`↔`!=` | truncating_modulo, predicate |
| `stale_update` | drop one update on the dependency path | stale_read |
| `shadow` | a helper assigns to a name instead of returning it (expects a global effect) | flat_scope |
| `default_shared` | `q=None`-style fresh list replaced by `q=[]` shared default across calls | fresh_default |
| `frozen_predicate` | a loop condition reads a copy taken before the loop | predicate_frozen |
| `wrong_index` | `L[0]`↔`L[-1]`, `L[i]`↔`L[i+1]` | index_math |

`n_bugs` (1 → 3) stacks independent injectors on different statements. `bug_distance` (1 → 8): statements between the bug and the point where its effect reaches the returned value (controlled by choosing the injection site along the dependency path).

### 4.3 Shown example and hidden inputs
V-a: one visible input, its wrong output (buggy program under truth) and expected output (reference). V-b: the same plus hidden inputs chosen so that the minimal symptom patch fails (at least one hidden input where the buggy and reference outputs differ *differently*). V-c: no code; a spec sentence rendered from the reference program's structure ("returns the sum of the elements greater than `limit`, counting each at most once…") — rendering is templated per generated structure family; **the spec is checked by executing the reference on the examples**, never by reading it.

### 4.4 Dials (`CodeFixParams`)
`n_statements` (8 → 25), `bug_kind` (categorical, or `bug_mix` 0..1 share of semantic vs syntactic bugs), `n_bugs` (1 → 3), `bug_distance` (1 → 8), `hidden_inputs` (0 → 8; 0 = V-a), `edge_cases` (0 → 3: empty list, zero, negative, single element), `misleading_comment` (0/1: a comment that describes the buggy line as correct), `spec_examples` (1 → 3, V-c), `identifier_confusability` (reuse).

## 5. Prompt and submission format
Repair: "Here is a Python function and one example where it returns the wrong value… Return the corrected function, complete, in one ```python block. Keep the same name and parameters." Write: "Write `def solve(…)` so that … Examples: … Return only the function in one ```python block." The last fenced block is parsed; prose outside it is ignored.

## 6. Verifier and signatures (`verify.py`)
Order of checks: `empty_answer` → `no_code_block` → `outside_subset:<construct>` (ERROR-class) → `parse_error` → `signature_changed` (name/arity) → run on visible + hidden inputs → PASS if all equal; else signature:
- `visible_fixed_hidden_broken` (passes the shown example, fails ≥ 1 hidden) — the symptom patch;
- `introduced:<foil>` (the submission, read under truth, equals the *buggy* program's output on all inputs: nothing fixed) → `no_change`; equals a foil reading of the reference → `introduced:<foil>` (e.g. fixed the loop bound but created an alias leak);
- `wrong_site` (the diff touches statements other than `bug_site` and hidden inputs fail);
- `regressed` (fails an input the buggy program got right);
- `runtime_error:<kind>` (our interpreter raised on some input);
- `wrong_value` otherwise.
Also record `diff_lines` (statements changed vs. the buggy program) in `realized_dims`, and `traced_ok` = whether the same model solved the buggy program as a *tracing* task in the same campaign (the read-vs-write pairing: the evaluator runs the tracing question first on the buggy program under `kind=code_trace`, run id `<run_id>/trace`).

## 7. Zoo (planted writers; must pass before any spend)
`perfect` (returns the reference), `reverter` (reverses the bug edit), `symptom_patcher` (adds `if <params> == <visible input>: return <expected>` to the buggy function), `no_op` (returns the buggy program), `python_habit` (fixes the bug but rewrites `a = a + [x]` as `a += [x]` everywhere, introducing `augassign_rebinds` when an alias exists), `wrong_site_fixer` (changes a different statement), `escaper` (emits `import os`), `blocked_prose` (no code block). Expected: `symptom_patcher` fails V-b only, signature `visible_fixed_hidden_broken`; `python_habit` fails iff an alias exists with `introduced:augassign_rebinds`; `escaper` → `outside_subset:import`, never executed; `find_breaking_point` on `default_repair_ray` (hidden_inputs 0 → 8) with `symptom_patcher` → break attributed to `hidden_inputs`.

## 8. Wiring
`CodeRepairEvaluator` / `CodeWriteEvaluator` (same shape as `CodeTraceEvaluator`: cost cap, cancel, `run_suffix`, token budget, adversarial optional); rays `default_repair_ray` (hidden_inputs 0 → 8 × bug_distance 1 → 8, n_statements pinned 18), `default_repair_kind_ray` (n_bugs 1 → 3 with `bug_mix`), `default_write_ray` (edge_cases 0 → 3 × spec_examples 3 → 1); runners `run_code_repair_campaign`, `run_code_write_campaign` with `early_stop` / `resample_gate` / `adversarial`; registry `V.repair`, `V.repair_regressions`, `V.write` with plain-English questions; frontier rows (axis) with `CAUSE_WORDS` for every signature; `findings.CAPABILITY_KINDS` += `code_repair`, `code_write`.

## 9. Live plan (approval needed)
Gemma 4 QAT on Modal, then Gemma 3 (Nebius) for the contrast: Scout the three rays (≈ 100 trials), Standard on anything that moves, the read-vs-write pairing on every repair trial (doubles calls, still ≈ 1,500 output tokens each). Expected ≈ 1 GPU-hour. Then the certificate: fresh seeds at the boundary, resample gate, attribution.

## 10. Out of scope
Multi-file or repository tasks; libraries/imports; performance; style; floats; agentic tool-using coding loops (Province III covers tool use); anything that would require executing model code outside our interpreter.
