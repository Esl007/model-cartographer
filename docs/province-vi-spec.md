# Province VI — The repository world: requirements

*2026-10-06. Binding parts: §0–§3. Parent rules: `docs/spec-frontier-breaking-points.md` §0–§1, `docs/province-v-spec.md` §0 (subset, no sandbox, no LLM in ground truth). Evidence this builds on: Province III ticket fabrication (`docs/findings.md`, F23–F27), Province V repair floor (`docs/reports/province-v-gemma4qat-2026-10-05.md`, incl. Resolutions 2026-10-06), Province IV reading null and its scope narrowing (lab log 2026-10-06, A1.2).*

## 0. Hard rules for whoever builds this

1. `results/campaign.sqlite` is live paid data: never tracked, never rewritten; stage only your own files by explicit path; never `git stash` / `checkout` / `restore` / `clean` / `reset`. Do not commit; the coordinator commits.
2. **No LLM anywhere in ground truth.** The call graph, the bug site, the failing test and the verdict are all products of construction and of execution in *our* interpreter.
3. **No sandbox, no `exec`, no `eval` on model output, ever.** Every file the model writes or edits is parsed with `ast` into our `Program` AST through `codefix/parse.py` (extended for the import forms in §4.5). Anything outside the subset is `outside_subset:<construct>`, an ERROR-class protocol outcome. CPython `exec`/pytest run only in tests, on repositories *we* generated (§3.8).
4. Token budget from `default_token_budget(model)`; `finish_reason=length` → episode ERROR `cap_hit`, never FAIL. Per-turn retry at 32k as in `interactive/episode.py`.
5. **No API spend.** Build, test, zoo-validate, export, stop. The live plan (§10) runs only on the user's say-so.
6. Do not edit `cartographer/viewer/*`, `cartographer/exec/{client,keys,pricing}.py`, `scripts/run_all_provinces.py`, `scripts/modal_serve_gemma4.py`, `docs/lab-log.md`.
7. Hypotheses in §8 are **pre-registered**: their statistics and thresholds are fixed here before any run and are not to be edited after the first live record exists. Analysis that departs from §8 is reported as exploratory.

## 1. Why this province, and what it must answer

Provinces IV and V measure a model on one function. Real coding work is navigating a codebase to find where a symptom comes from, then changing the right place without breaking the rest. Province VI asks that question with the same discipline as the rest of the instrument: a **repository whose structure we built**, so the import/call graph, the fault, and the correct fix are known exactly and cost nothing to verify.

The one design decision everything else follows from:

> **A repository is a rendering of a single latent `Program`.** Modules are a partition of the program's function definitions; imports are derived from the call edges that cross the partition; the test file is the visible input/expected pair rendered as an `assert`. Execution flattens the repository back into one `Program` and runs it in our interpreter. Nothing in the interpreter knows what a module is.

Consequences: the call graph *is* the latent (perfect ground truth, free); `n_modules = 1` is exactly Province V, so VI is a strict superset and the cost of navigation is `pass(1 file) − pass(k files)` on the **same bug**; Province V's bug injectors, parser, harness, verifier signatures and the reading pairing all carry over unchanged; the interactive machinery (`run_episode`, text-fallback protocol, ticket/copy-fidelity classifiers, resample gate) carries over by giving `RepoEnv` the same duck-typed surface as `InteractiveEnv`.

Four questions, each a ray, each tied to a finding already on file:

| Ray | What changes | What it isolates | Prior finding |
|---|---|---|---|
| **VI.navigate** | symptom-to-cause distance in the call graph, 1 → 6 hops; files 4 → 12 | can the model follow a symptom to its cause across files | V `bug_distance`, promoted to repo scale |
| **VI.lookalike** | twin module/function names across files (`pricing.compute_total` / `billing.compute_totals`) | does it reference symbols that exist, or ones that *should* exist | III ticket fabrication; II look-alike tools |
| **VI.clone** | a near-miss clone of the buggy function in another module, off the call path, where the obvious edit is wrong | structure over magnitude, repo edition | IV/V `wrong_site` |
| **VI.graph_value** | the same tasks with `get_code_neighbors` on vs off, across tool budgets | the causal value of graph tools: how much exploration they remove, and whether they change the verdict | new; nobody else has deterministic repo ground truth to run this on |

## 2. Non-negotiables

| Constraint | Means |
|---|---|
| Latent structure first | a task is `RepoTask(latent: Program, partition: module → sids, graph: edges, bug_sites, bug_module, entry_module, visible_input, visible_expected, hidden_inputs, files: path → source, lookalike_pairs, clone_site)`. Files are rendered from the latent; the latent is never recovered from text. |
| Ground truth by execution | after the episode, every file in the repo state is parsed to `FuncDef`s, qualified by module, flattened into one `Program`, and run on the visible and hidden inputs in our interpreter. PASS iff all outputs equal the reference's (type-strict, tuple→list normalised). |
| Functions as the unit | every module holds 1–4 top-level `def`s over subset types; the entry module holds `solve`. No module-level state, no classes. |
| Difficulty is structure | hops, fan-out, look-alikes, clones and tool budget are the levers. Per-file size is pinned (≤ 25 statements per function, ≤ 4 functions per file). |
| Deterministic | same `(RepoParams, seed)` ⇒ identical files, bytes included; same episode transcript given identical model outputs. sha256 pattern, no `random`. |
| Failure named by mechanism | every FAIL carries one primary signature (§6) plus process measures. |
| Paired by construction | `graph_tools` and `restate_symbols` are evaluated on the **same seed** with and without; `n_modules=1` is the single-file control for every repo task. |
| Fits the seams | `ExperimentRecord(kind=repo_fix)`, an `Evaluator` for `find_breaking_point`, `ResampleGatedEvaluator`-compatible (`run_suffix`), `protocol` native/text_fallback, `Spec` rows in `report/frontier.py`, registry ids `VI.*`, cause words. |
| Exportable | `export_repo(task, dir)` writes a real on-disk package that CPython + pytest can run (§3.8). This is the releasable benchmark for the Kaggle "tasks & benchmarks" track. |

## 3. Deliverables and acceptance

**Code** (`cartographer/repo/`): `compose.py` (latent program from composed function-form helpers; partition into modules; graph), `render.py` (files, imports, test file, descriptive naming, look-alikes, clones), `flatten.py` (files → qualified `Program`; import resolution), `env.py` (`RepoEnv`: tools, budget, state, process measures), `verify.py` (end-state verdict + signatures), `evaluator.py` or additions in `exec/evaluator.py` (`RepoFixEvaluator`, rays, runners, paired graph ablation), `export.py`, registry entries, frontier rows, `tests/test_repo*.py`. Extensions to `codefix/parse.py` for §4.5 import forms.

**Accepted when all hold:**
1. **Flatten round-trip:** for 2,000 generated repos across all dials, `flatten(render(task).files)` executes identically to `task.latent` on 8 inputs, and `render(flatten(files))` reproduces `files` byte-for-byte. `n_modules=1` repos render to a single file whose `solve` source equals the Province V `render_function` output for the same latent.
2. **Graph is exact:** for every generated repo, the edge set returned by `get_code_neighbors` over all symbols equals the `Call` edges in the latent, and the shortest path from `solve` to the bug site has length `bug_distance` exactly.
3. **Bug is load-bearing and single-sited:** the visible test fails on the buggy repo and passes on the reference; the bug changes output on ≥ 1 of 8 generated inputs; `bug_module ≠ entry_module` whenever `bug_distance ≥ 1`; exactly one statement differs between buggy and reference latents (for `n_bugs=1`).
4. **Oracles:** the "revert the bug via `edit_file`" oracle passes 100 %; the "edit the clone instead" oracle fails 100 % with `clone_edited`; the "special-case the visible input in `solve`" oracle passes the visible test and fails ≥ 90 % of tasks with `hidden_inputs ≥ 4` as `visible_fixed_hidden_broken`; the "call `utils.compute_total`" oracle is `fabricated_symbol` 100 %.
5. **Zoo** (§7): each planted agent fails exactly on its mechanism with its signature; the perfect agent never fails; `find_breaking_point` on VI.navigate with the symptom-editor fake attributes the break to `bug_distance`; the paired graph ablation on the explorer fake shows the pre-registered exploration reduction.
6. **No execution of model text outside the subset:** feeding `import os`, `__import__`, `open(...)`, `eval`, a module-level loop, and a `from pricing import *` are rejected at parse time with no side effect; `import pricing` followed by `pricing.compute_total(x)` is accepted and resolves to the qualified call (§4.5).
7. `python3 scripts/frontier_summary.py` shows the VI rows as NOT TESTED without error; `python3 -m cartographer.campaigns.registry --write-catalog` lists the VI ids; `scripts/run_campaign.py --list` shows them with tiers.
8. **Export differential:** for 300 exported repos, `pytest` under CPython on the exported package gives the same visible-test outcome as our interpreter (reference passes, buggy fails), and CPython's `solve` outputs on the hidden inputs equal our interpreter's.
9. Full suite green.

## 4. Tasks

### 4.1 Composition, not extraction (the generator)

Two ways to get a multi-function latent were considered. **Extraction** (take a Province V program and factor blocks out into helpers) needs liveness analysis and multi-value returns on our AST; `helperize` does the single-statement case and no more. **Composition** needs neither and gives depth and fan-out by construction. Build composition; extraction is out of scope.

1. Draw `n_funcs` function-form programs with `codeworld.generate` via `codefix.tasks.reference_program` (each is `def f_i(params) -> value`, param kinds ∈ {int, list, str}, return kind known from the query target). Pin each to ≤ 25 statements.
2. Draw a call DAG over them with `solve` as the root: depth = `graph_depth`, fan-out ≤ `fanout`. An edge `g → f` means `g`'s body contains `t = f(<args>)` where the args are `g`'s own variables of matching kinds (or literals when no variable fits). Kind matching is a filter on the draw, not a search.
3. Choose the bug carrier: a function at depth exactly `bug_distance` from `solve` whose result reaches `solve`'s return (checked by executing with and without the bug on 8 inputs, as Province V does). Inject with `codefix.bugs.inject(kind, seed)` on that function's body; `bug_kind`, `n_bugs`, `bug_mix` keep Province V's meaning.
4. **Partial inlining** for naturalness: with probability `inline_p` per edge, inline a callee's body into its caller (rename locals to fresh ids; answer-preserving by construction, asserted). Inlining is the easy direction of refactoring and gives call bodies that look written rather than generated. Never inline an edge on the path to the bug (that would change `bug_distance`).
5. Partition the functions into `n_modules` modules: the path from `solve` to the bug crosses module boundaries at every hop (so the failing test is never in the cause's file when `bug_distance ≥ 1`); remaining functions are placed to make fan-in realistic (a `utils`-like module with 2–4 small helpers). `distractor_modules` adds modules unreachable from `solve` (noise with no causal role; their functions must not share names with reachable ones unless `lookalike_names` is on).
6. Inputs: the visible input and `hidden_inputs` are drawn as in Province V (`input_pool`, `edge_inputs`) on the composed `solve`; filtered so the reference runs without error under truth; the visible input is one on which the bug shows.

### 4.2 Rendering

- One file per module, `<name>.py`; descriptive module names from a fixed vocabulary by role (`pricing`, `inventory`, `ledger`, `report`, `utils`, `parsing`, `rules`, …) chosen by seed; function names via `render(..., naming="descriptive")` so they read like code. Entry module holds `solve`; `tests/test_<entry>.py` holds `from <entry> import solve` and `assert solve(<visible>) == <expected>` plus 0–2 extra asserts on other inputs (those pass on the buggy repo too; they are not hints).
- Imports: `from <module> import <name>` for every crossing edge, one line per imported name, sorted; no unused imports.
- **Look-alikes** (`lookalike_names=1`): for `k` crossing edges, a *twin* function with a name at edit distance 1–2 from the callee's (`compute_totals`, `compute_total_v2`, `computeTotal`) is placed in a *different* module, with a different body and signature-compatible params. It is never called. The model-facing catalog of symbols (via `search`/`list_files`/`get_code_neighbors`) contains both; only one is on the path.
- **Clone trap** (`clone_trap=1`): a copy of the buggy function's *reference* body is placed in another module under a related name, not on the call path. Editing it is `clone_edited`; because its body is the fixed one, the model that "finds the right code" by pattern-matching the bug's shape finds the clone first.
- **Misleading comment** (`misleading_comments`, from IV round 2): a comment on the buggy line asserting it is correct. Off by default; a dial.
- `opaque_names`: as in III, replaces module and function names with opaque ids; strips the naming scheme as an ablation (tests whether look-alike failures are about *names* or about *references*).

### 4.3 Tools (mirrors of this harness's own; `RepoEnv.tools_schema()`)

| tool | args | returns | notes |
|---|---|---|---|
| `list_files` | — | paths | always available |
| `read_file` | `path` | numbered source | counts a read of that module |
| `search` | `pattern` | `path:line: text` matches (literal substring, case-sensitive, ≤ 40 hits) | the manual route to the graph |
| `edit_file` | `path, old, new` | ok / `no_match` / `not_unique` | exact-string replace, trailing whitespace stripped before matching; the error is shown to the model and counted (`edit_nomatch`, `edit_nonunique`) — copy-fidelity at repo scale |
| `write_file` | `path, content` | ok | whole-file rewrite; counted as `whole_file_rewrite` |
| `run_tests` | — | per-assert pass/fail with actual vs expected, or the parse/runtime error | runs the **visible** tests only, via flatten + harness; never the hidden inputs |
| `get_code_neighbors` | `symbol` (`module.name` or `name`) | callers and callees with `path:line`, from the latent graph | present only when `graph_tools=1`; ambiguous bare names return all candidates (look-alikes included) |

Termination: the episode ends when the model stops calling tools, says it is done, exhausts `tool_budget`, or hits a protocol error. Verdict is computed on the end state. `restate_symbols=1` appends `defined_symbols: {module: [names]}` to every tool result (the III `restate_tickets` rescue, applied to symbols); `restate_imports=1` appends the entry module's import block to `run_tests` output.

### 4.4 Dials (`RepoParams`)

| dial | range | default | role |
|---|---|---|---|
| `n_modules` | 1–12 | 4 | 1 = Province V control |
| `bug_distance` | 0–6 | 1 | hops from `solve` to the bug carrier |
| `fanout` | 1–4 | 2 | branching of the call DAG |
| `n_funcs` | derived | — | ≥ `bug_distance + 1`, ≤ 4 × `n_modules` |
| `inline_p` | 0–0.5 | 0.25 | naturalness; never on the bug path |
| `distractor_modules` | 0–4 | 0 | unreachable modules |
| `lookalike_names` | 0/1 | 0 | twins across modules |
| `clone_trap` | 0/1 | 0 | fixed-body clone off the path |
| `misleading_comments` | 0/1 | 0 | IV round-2 dial |
| `bug_kind`, `n_bugs`, `bug_mix`, `hidden_inputs` | as V | `None`, 1, 0.0, 4 | the fault itself |
| `tool_budget` | 4–40 | 20 | calls per episode |
| `graph_tools` | 0/1 | 0 | `get_code_neighbors` available |
| `restate_symbols`, `restate_imports`, `opaque_names` | 0/1 | 0 | ablations |

### 4.5 Subset extension (`codefix/parse.py`)

Accept, at module top level only: `from <m> import <n>[, <n>...]` and `import <m>`; and in expressions `<m>.<n>(args)` as a qualified `Call`. `<m>` must be a module in the repo; `<n>` must be a top-level def in that module **at verification time** (so a model that adds a helper and imports it is fine). Reject: `import *`, relative imports, aliasing (`as`), importing anything that is not a repo module (`outside_subset:foreign_import`), module-level statements other than defs and imports, attribute access that is not `module.func(` (existing `method_whitelist` rule stands). A call to a name that resolves nowhere is **not** a parse error: it is recorded and scored as `fabricated_symbol` (§6), because that is the mechanism under test.

Flatten qualifies every def as `<module>__<name>` and rewrites calls accordingly, so two modules may define the same bare name (needed for look-alikes). The harness entry is `<entry>__solve`.

## 5. Prompt

System: the role (fix the failing test in this repository), the tool catalog, the constraint that edits must stay within the subset (listed as in V §5, now with the import forms), and `tool_budget`. User turn: the output of `run_tests` on the initial repo (the failing assert with actual vs expected) and the file list. Nothing else: no file contents, no hint about which module. Text-fallback protocol identical to III (` ```tool_code ` blocks, `format_tool_results`).

## 6. Verifier and signatures (`repo/verify.py`)

End state → parse all files → flatten → run visible + hidden. Then classify, first match wins:

| signature | condition |
|---|---|
| `outside_subset:<construct>` | any edited/written file fails to parse into the subset (ERROR class) |
| `cap_hit`, `api_error`, `budget_exhausted_no_edit` | protocol/infrastructure; `budget_exhausted_no_edit` is ERROR only if `tool_budget < 8`, otherwise FAIL `no_edit` |
| `no_change` | no file differs from the initial repo |
| `fabricated_symbol[:lookalike\|plausible\|wrong_module]` | a call or import names a symbol that does not exist (`lookalike`: within edit distance 2 of an existing name in another module; `wrong_module`: the name exists but in a module other than the one imported from; `plausible`: otherwise) |
| `clone_edited` | the clone function's body changed and the bug carrier's did not |
| `wrong_site_file` | all edits are in files other than `bug_module` and the output is still wrong (editing the symptom file — the repo-scale `wrong_site`) |
| `wrong_site` | an edit in `bug_module` but not at a bug site, output still wrong |
| `visible_fixed_hidden_broken` | visible tests pass, ≥ 1 hidden input wrong |
| `regressed` | a previously correct hidden input now wrong while the visible stays wrong |
| `introduced:<foil>` | as V (the diff introduces a construct from a foil family) |
| `wrong_value` | right site, output still wrong |

Process measures on every record (PASS or FAIL): `n_calls`, `n_reads`, `n_searches`, `n_neighbors`, `n_edits`, `n_test_runs`, `edit_nomatch`, `edit_nonunique`, `whole_file_rewrite`, `calls_to_first_cause_read` (calls before the first `read_file` of `bug_module`; ∞ if never), `files_read_on_path / files_read` (navigation precision), `ran_tests_after_last_edit` (0/1), `tokens_in_total`.

## 7. Zoo (planted agents; must pass before any spend)

Each is a scripted policy over `RepoEnv`, run through the same `verify`:
- **perfect**: `run_tests` → `get_code_neighbors`/`search` down the path → `read_file(bug_module)` → `edit_file` revert → `run_tests`. PASS always; `calls_to_first_cause_read = bug_distance + 2` with graph tools, more without.
- **symptom_editor**: edits `solve` to special-case the visible input → `visible_fixed_hidden_broken`; its pass rate must fall to 0 as `hidden_inputs` rises (break attributed to `hidden_inputs`).
- **symptom_file_editor**: edits a plausible line in the entry module → `wrong_site_file`, rate rising with `bug_distance`.
- **fabricator**: imports `from utils import compute_total` (non-existent) → `fabricated_symbol:plausible`; with look-alikes on, calls the twin → `fabricated_symbol:lookalike`.
- **clone_editor**: edits the function whose body matches the bug shape → `clone_edited` whenever `clone_trap=1`.
- **explorer_blind / explorer_graph**: breadth-first reads without / with `get_code_neighbors`, then the perfect edit. Same verdicts; `calls_to_first_cause_read` differs by the pre-registered amount (§8 H4). Under a tight `tool_budget` the blind explorer runs out → `budget_exhausted_no_edit`.
- **copier**: correct intent, `old` string off by indentation → `edit_nomatch` twice, then gives up → `no_change` with `edit_nomatch=2`.

## 8. Pre-registered hypotheses (fixed before the first live record)

All tests on paired or per-seed records with the resample gate (`ResampleGatedEvaluator`, n=3; `unresolved` excluded). Paired binary comparisons use McNemar's exact test; rate comparisons across dial levels use Fisher with Holm–Bonferroni over the family (reuse `ablate/interactive.attribute_ticket_failure` machinery). α = 0.05 per family.

- **H1 — cross-file references get fabricated.** At `n_modules ≥ 8`, `lookalike_names=1`, `tool_budget=20`: `fabricated_symbol` is the primary signature in ≥ 25 % of FAILs, and `restate_symbols=1` (paired, same seeds) reduces its count by ≥ 50 %. Null: fabrication < 10 % of FAILs or no restatement effect.
- **H2 — failures cluster in the symptom file.** Among FAILs with an edit, `wrong_site_file` outnumbers `wrong_site`, and the `wrong_site_file` share increases monotonically with `bug_distance` over 1 → 4 (Spearman ρ > 0.5 on per-level shares).
- **H3 — visible-test overfitting reappears.** `visible_fixed_hidden_broken` rate at `hidden_inputs=4` exceeds its rate at `hidden_inputs=0` (where it is 0 by definition) and is ≥ 10 % of FAILs at `n_modules ≥ 4`; same mechanism as V-b, now with the test file as the visible artefact.
- **H4 — graph tools compress exploration.** Paired on seeds at `n_modules ∈ {4, 8, 12}`, `tool_budget=20`: median `calls_to_first_cause_read` with graph tools is ≤ 0.7 × without (Wilcoxon signed-rank, one-sided). Secondary: pass-rate difference (McNemar) is reported with its CI; a verdict effect is **not** pre-registered — the pre-registered claim is about exploration length, and the budget curve (`tool_budget` 6 → 30, both arms) shows where exploration savings turn into verdict changes, if anywhere.
- **Control (must hold or VI results are not interpretable):** at `n_modules=1` the pass rate equals Province V's on the same `(bug_kind, hidden_inputs)` within the resample CI.

## 9. Wiring

- `RepoFixEvaluator(client, model, store, run_id, cost_cap, protocol, run_suffix, pair_trace=False)`: `evaluate(params, seed, trial_seed)` builds `RepoTask`, runs `run_episode` with `RepoEnv`, verifies, stores `ExperimentRecord(kind="repo_fix")` with realized dims (`n_modules`, `bug_distance`, `graph_tools`, …) and the process measures in `detail`.
- Paired graph ablation: `run_graph_value_campaign(seeds, budgets)` runs each seed twice (`graph_tools` 0/1) under run suffixes `/graph_off` and `/graph_on`, then writes the paired statistics to the campaign note; `report/frontier.py` row `repo_graph_value` renders them.
- Rays: `default_repo_navigate_ray` (`bug_distance` 1→6, `n_modules` 4→12, budget 20), `default_repo_lookalike_ray` (`lookalike_names` 1, `n_modules` 4→12), `default_repo_clone_ray`, `default_repo_budget_ray` (`tool_budget` 30→6 at `bug_distance` 3), `default_repo_control_ray` (`n_modules` 1→8 at fixed bug). Registry ids `VI.navigate`, `VI.lookalike`, `VI.clone`, `VI.budget`, `VI.graph_value`, `VI.control`; tiers via `apply_tier`; `scripts/run_all_provinces.py` is left alone (coordinator wires a stage).
- Frontier rows: `repo_navigate`, `repo_lookalike`, `repo_clone`, `repo_budget`, `repo_graph_value`, `repo_control`; cause words: `fabricated_symbol`, `wrong_site_file`, `clone_edited`, `edit_nomatch`, `budget_exhausted`.
- `export_repo(task, dir)` writes the package plus `tests/`, a `README` naming the seed and params, and `expected_hidden.json` (hidden inputs and outputs) so the benchmark is checkable offline by anyone with CPython.

## 10. Live plan (approval needed; no spend before §3 holds)

1. Smoke, Gemma 4 QAT on Modal (`protocol=text_fallback`): `VI.control` at `n_modules ∈ {1, 4}`, 3 seeds, budget 20 (≈ 6 episodes). Checks the control (§8) and the text-protocol parsing of 7 tools.
2. `VI.navigate` standard tier + paired `VI.graph_value` at `n_modules ∈ {4, 8}`, 10 seeds each arm (≈ 40 episodes), on Gemma 4 QAT. Because V already shows a repair floor on this model, the VI question for Gemma is "how much does navigation add on top of the floor", read off `VI.control` and the process measures (navigation precision, `calls_to_first_cause_read`), not off pass rate alone.
3. Nemotron Nano / Super on Nebius (native tools): `VI.navigate` + `VI.lookalike` standard tier, then `VI.graph_value` on the tier that is not at floor or ceiling. This is the tier-discrimination content for the atlas.
4. Fresh-seed confirmation of any breaking point per `docs/spec-frontier-breaking-points.md`; certificate requires `compact` (≤ 12 decisions — here, ≤ 12 tool calls on the perfect path) and `fresh_seed`.
5. Cost: an episode is 10–30 turns with a growing context (≈ 2–6k tokens per turn at `n_modules=8`), so 30–120k input tokens per episode; budget ~$0.05–0.20 per episode on hosted Nemotron, GPU-time-bound on Modal (≈ 2–4 min per episode at parallelism 4). Stage 2 ≈ 1.5–2.5 GPU-hours; set `parallelism ≤ 4`, stop the app in a `finally`.

## 11. Bottlenecks, ranked by risk to the Oct 30 / Nov 12 dates

1. **Composition coherence (generator).** Kind-matched call DAGs over independently generated function-form programs will often fail the "bug reaches `solve`'s output" filter, or produce `solve` bodies that are mostly plumbing. Mitigations are in §4.1 (filter-by-execution, partial inlining), but the draw count per usable task is unknown until built; budget two generator iterations. The `n_modules=1` control makes any residual unnaturalness measurable rather than confounding.
2. **Subset extension for imports.** Models write `import pricing` + `pricing.f(x)` as often as `from pricing import f`; rejecting either form inflates ERRORs exactly as `sum()` did in V. §4.5 accepts both; `import x as y`, relative imports and `*` remain rejected and must be reported as protocol artefacts, not model failures, in every report.
3. **Multi-turn cost and the serving nondeterminism from V-E.** A single varying token early in an episode changes the whole trajectory, so the resample gate will see more `noise` on Modal than it did on single-shot tasks, and `pass^k` needs CIs (already required). Prefer hosted Nemotron for the paired graph-value study (deterministic enough at temp 0 in X.consistency), and treat Modal/Gemma results as floor-plus-process-measure results.
4. **Exact-match `edit_file` is strict by design.** `edit_nomatch` is a real copy-fidelity mechanism (III), but a model that fails only on indentation would be misread as "cannot fix". The verdict is on the end state, so repeated `edit_nomatch` followed by a correct `write_file` is still PASS; the count is a process measure. Report it next to pass rate, never fold it into FAIL.
5. **Tool-catalog confound in the graph ablation.** `graph_tools=1` adds one tool to the system prompt; the arms differ by that text. Accept and state it; the paired design and the `search`-only route (which can reconstruct the graph manually) make the comparison about *exploration cost*, which is what H4 claims.
6. **Protocol difference across models.** Nemotron native function calling vs Gemma text fallback (III, X9) is a confound on cross-model comparisons; within-model paired comparisons (graph on/off, restate on/off) are unaffected. State it in the atlas rows.
7. **Scope creep.** Not in VI: extraction refactoring, multi-file edits as the *correct* fix, tuple returns / unpacking in the subset, classes, module-level state, exceptions, a `write` rung (new function from spec), hidden-test discovery. Each would be its own spec.

## 12. Build plan (estimates for Opus 5.5 agents, disjoint files)

| WP | files | depends on | est. |
|---|---|---|---|
| A compose + flatten + parse imports | `repo/compose.py`, `repo/flatten.py`, `codefix/parse.py` | — | 1.5 d |
| B render + export + differential | `repo/render.py`, `repo/export.py` | A | 1 d |
| C `RepoEnv` + tools + episode wiring + text protocol | `repo/env.py` | A (interfaces) | 1 d |
| D verify + signatures + zoo | `repo/verify.py`, `tests/test_repo_zoo.py` | A, C | 1 d |
| E evaluator, paired campaign, rays, registry, frontier, catalog | `exec/evaluator.py` additions, `campaigns/`, `report/frontier.py` | C, D | 0.5 d |

A and C can start in parallel against the `RepoTask` dataclass fixed on day 0. Realistic wall-clock with review: 5–6 days, leaving one live week before Oct 30.

## 13. Coordinator review, 2026-10-09 (pre-build; §0.7 allows edits until the first live record)

Checked against the design conversation of 2026-10-06. All ten designed elements present; four additions (latent-rendering unification with the n_modules=1 control, export_repo, restate_symbols, the import-form lesson from V-B) are improvements. Two nits and one deferral:

1. **H2's statistic is underpowered as written.** Spearman ρ over four levels (bug_distance 1→4) has almost no resolution; ρ > 0.5 can arise from noise. Suggest: keep the monotonicity claim descriptive, and pre-register the TEST as Fisher exact on `wrong_site_file` share between the extreme levels (1 vs 4), Holm-corrected within the H2 family. Builder may adopt this wording before the first live record.
2. **Naming: `budget_exhausted_no_edit` (ERROR path) vs FAIL `no_edit` vs `no_change` (§6).** Three near-synonyms for adjacent outcomes invite mis-tabulation in frontier rows. Suggest collapsing to `no_change` (FAIL) and `budget_exhausted` (ERROR when tool_budget < 8), with the edit-less-ness visible in process measures (`n_edits=0`).
3. **Deferred, explicitly out of scope (add to §11.7's list):** a corrupted-graph arm — `get_code_neighbors` returns one wrong edge, measuring over-trust of tools vs. verification by reading. It is the graph-value study's natural falsification twin, but it is a new mutator + zoo agent and must not cost a day before Oct 30. Park it with the other roadmap items.
