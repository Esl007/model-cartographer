# Province IV — Code tracing (execution prediction): requirements

*Written 3 Oct 2026 for an implementing agent. Read all of it before writing code. Sections 1–3 are binding; everything else is the design the sections constrain, and may be adjusted where a stated reason is given in the hand-back.*

## 0. Hard rules for whoever builds this

1. `results/campaign.sqlite` is a live, gitignored database holding paid data. **Never** run `git checkout`, `git restore`, `git stash`, `git clean`, `git reset --hard`, or anything else that rewrites the working tree. Stage only your own files, by explicit path. Never `git add -A` / `git add .`.
2. `cartographer/viewer/serve.py` carries another thread's uncommitted work. Edit it only additively (new dict entries, new imports, new routes). Never commit it. Never revert it.
3. **No API spend without an explicit instruction in the task prompt.** Build, test, zoo-validate, stop. If the prompt authorises a smoke test, it is Nano only, `cost_cap=0.03`, one run id, and the result goes in the hand-back. Nothing else runs live.
4. No LLM anywhere in generation, ground truth, foils, mutation, rendering or verification. The whole instrument's credibility rests on this (see `docs/lab-log.md`, the "never an LLM judge" principle).
5. Token budget comes from `default_token_budget(model)` (16000 / 32000 retry). Never lower it. Count `finish_reason == "length"` records separately as ERROR, never as FAIL (the Sep 25 retraction in the lab log is why).
6. Tests before live. The synthetic zoo (§11) is not optional; the project has validated every province against planted fake models before spending a cent.
7. Python 3, no new dependencies. Run the suite with `python3 -m pytest -q` from the repo root (about 5 minutes; current count 726 passing). Everything you add must pass; nothing existing may break.

## 1. What this province measures, and why

**Question.** Given a short program, can the model predict what it does? Not "can it write code", but "can it *read* code correctly": track variable state, follow control flow, know the language's actual semantics where a strong shallow prior says otherwise.

**Why this and not code generation.** Generation needs a sandbox to run model-written code, and attributing a failure to a cause gets fuzzy. Tracing runs only *our* generated programs, so ground truth is deterministic and free, and every failure can be matched against a named shallow-reasoning strategy. It reuses every mechanism the project has: latent structure as source of truth, rendered surface, foils, adversarial seed search, mutators, escalation rays, decomposition, fresh-seed confirmation, `frontier_summary`.

**The two hypotheses this province exists to test.**

- **H1 (cross-province):** every tool-planning failure on every tier was identifier binding (`set_dep_th_flag_4` written for `set_dep-th_flag_4`). Code has the same stressor as variables with look-alike names. If the binding weakness is general, Nano should break on look-alike variable names at a similar program length; if it's specific to tool catalogs, it won't. Either answer is a finding.
- **H2 (structure beats magnitude):** the September lesson was that difficulty comes from traps where a strong prior conflicts with the correct answer, not from bigger numbers. Here the priors are known: lists copy on assignment, closures bind early, `range` is inclusive, `%` truncates toward zero, default arguments are fresh each call. Each is a named foil. The prediction is that tiers separate on trap density, not on program length.

**Prior art to cite, not compete with:** CRUXEval (output prediction on 800 fixed functions), CodeMind, LiveCodeBench's execution split, REval. Ours is parametric, adaptive, and attributes causes.

## 2. Non-negotiable design constraints

| Constraint | Means in practice |
|---|---|
| Latent structure is the source of truth | A `Program` AST (our own dataclasses, not `ast.AST`) is generated first. Source text is a *rendering* of it. |
| Ground truth from simulation | Our own bounded interpreter executes the `Program`. Nothing is hand-authored. CPython `exec` is used **only in tests** as a differential check of the interpreter (§5.3). The live path never executes text. |
| Difficulty is structure | Every trap is a toggle with a matching foil. A trap that does not change the answer is rejected by the generator (§6.4), the way `witness_cutoff` worlds without belief/truth divergence were. |
| Deterministic in `(params, seed)` | Same inputs, byte-identical program and source. Use the `_stable_int/_stable_float` sha256 pattern from `toolworld/generate.py`. No `random` module. |
| Realised dims, not knob settings | `measure(program)` reports what was actually generated (executed steps, live aliases, nesting). Analysis reads these. |
| Fits the existing seams | `ExperimentRecord` with `kind=code_trace`; an `Evaluator` for `find_breaking_point`; a `Spec` row in `report/frontier.py`; a launch preset. No parallel infrastructure. |
| Safe by construction | Generated programs: no imports, no I/O, no attribute access except list/dict/str methods from a whitelist, no `while`, loop bounds are literals ≤ 8, nesting ≤ 3, call depth ≤ 3, interpreter step cap 5,000. |

## 3. Deliverables and acceptance

**Code** (`cartographer/codeworld/`): `model.py`, `generate.py`, `interpret.py`, `render.py`, `measure.py`, `foils.py`, `mutate.py`, `search.py`, `verify.py`, `__init__.py`.
**Wiring:** `exec/evaluator.py` (evaluator + campaign runner + default rays), `viewer/findings.py` (`CAPABILITY_KINDS`), `viewer/launch.py` (preset + description), `viewer/serve.py` (additive: kind label), `report/frontier.py` (two `Spec` rows).
**Tests:** `tests/test_codeworld.py`, `tests/test_codeworld_mutate.py`, `tests/test_codeworld_zoo.py` (names indicative; ≥ 40 tests expected).
**Docs:** a `docs/build-log.md` entry (what, where, decisions, deviations from this spec with reasons) and a `docs/lab-log.md` entry if any live smoke ran.

**Accepted when all hold:**
1. 5,000 random `(params, seed)` programs across the full dimension ranges: interpreter output equals CPython `exec` output on the rendered source, for every one (differential test, §5.3).
2. Every enabled trap is answer-relevant on every generated program (its foil disagrees with the truth), §6.4.
3. Every mutator in `ANSWER_PRESERVING` leaves the answer byte-identical on 500 programs; every mutator in `ANSWER_CHANGING` changes it on ≥ 95 % (the rest rejected, never silently kept).
4. Zoo (§11): each planted fake model breaks on exactly its planted trap and nothing else; the no-trap control never breaks; `find_breaking_point` on the aliasing-blind fake locates `aliasing_on` as the cause via decompose.
5. Rendered source for the look-alike style parses with `ast.parse` and contains no two identifiers that are byte-identical (confusable, never equal).
6. `python3 scripts/frontier_summary.py` runs and shows the two new rows as `NOT TESTED` for every model (no live data yet) without error.
7. Full suite green.

## 4. Module layout and public names

Mirror `cartographer/toolworld/` so the next person finds things where they expect them.

```
cartographer/codeworld/
  model.py      Program, Stmt variants, Expr variants, Value, Query, CodeDimParams
  generate.py   generate(params, seed) -> Program ; validate(program) -> str | None
  interpret.py  Semantics (dataclass of flags), run(program, semantics=TRUE) -> Trace
                answer(program, semantics=TRUE) -> Value ; STEP_CAP
  render.py     render(program, seed, naming) -> RenderedProgram (source, name_map, query_prompt)
  measure.py    measure(program) -> dict[str, float]
  foils.py      FOILS: dict[name, Semantics] ; foil_answers(program) ; foil_divergence(program) -> CodeFoilProfile
  mutate.py     trap injectors + ablations ; ANSWER_PRESERVING / ANSWER_CHANGING sets
  search.py     search_adversarial_seeds(params, base_seed, n_candidates, top_k, require_kinds)
  verify.py     parse_answer(text) -> Value | None ; verify_answer(text, program, rendered) -> TraceVerification
```

`__init__.py` re-exports the public names (see `toolworld/__init__.py` for the pattern).

## 5. Latent representation and interpreter

### 5.1 Language subset (all of it; nothing else may be generated)

- **Values:** `int`, `bool`, `str`, `list` (of values), `dict` (str → value), `None`. **No floats** (formatting ambiguity would create verifier noise).
- **Expressions:** literals; names; `+ - * // %` on ints; `+` on str/list; `== != < <= > >=`; `and or not` (short-circuit); `x[i]` on list/str/dict (non-negative and negative indices); `len(x)`; `x[a:b]` slices on list/str; conditional expression `a if c else b`.
- **Statements:** `name = expr`; `name[i] = expr` (list/dict item assignment); `name += expr` (augmented, on int/str/list — the list case is the aliasing trap's teeth); `if / elif / else`; `for name in range(lit)` / `range(lit, lit)` / `for name in <list name>`; `def f(params, with optional default): body; return expr` (defs may be nested one level to create closures); call statements and call expressions; `x.append(v)`, `x.pop()`, `x.extend(y)`, `d.get(k, default)`, `s.upper()`, `s.replace(a, b)`; `print(expr)` (collected into stdout).
- **Forbidden:** `while`, imports, attribute access beyond the method whitelist, comprehensions, `try`, `global`/`nonlocal`, `lambda`, recursion (the generator never emits a call to the enclosing def), string formatting.

### 5.2 Program and Query

```python
@dataclass(frozen=True)
class Program:
    body: tuple[Stmt, ...]
    query: Query            # what we ask about
    params: CodeDimParams   # what was requested
    seed: int

@dataclass(frozen=True)
class Query:
    kind: str               # "final_value" | "stdout"
    target: str | None      # variable name for final_value; None for stdout
```

`final_value` asks for the value of one top-level variable after the program runs. `stdout` asks what the program prints (a list of printed lines, compared as a list of strings). Start with `final_value` as the default and the only kind the rays use; `stdout` is wired but secondary.

### 5.3 Interpreter

`interpret.run(program, semantics)` walks the AST with a `Semantics` object controlling every point where a shallow reasoner's prior might differ from Python:

```python
@dataclass(frozen=True)
class Semantics:
    list_assignment: str = "alias"      # "alias" (Python) | "copy"
    closure_binding: str = "late"       # "late" (Python) | "early"
    range_upper: str = "exclusive"      # "exclusive" (Python) | "inclusive"
    modulo: str = "floor"               # "floor" (Python) | "truncate"
    default_args: str = "shared"        # "shared" (Python) | "fresh"
    scoping: str = "lexical"            # "lexical" (Python) | "flat" (one namespace, inner assignments leak)
    conditionals: str = "evaluate"      # "evaluate" (Python) | "all_branches" (every assignment runs, last wins)
    loops: str = "iterate"              # "iterate" (Python) | "once" (body runs exactly once)
TRUE = Semantics()
```

- `run` returns a `Trace`: final environment (top-level names → values), stdout lines, executed statement count, per-statement `(stmt_id, env_snapshot_of_query_target)` so a failure can be located to the first statement where the model's implied value diverges (parallel to Province III's per-step distance; needed by `verify` only as a nice-to-have in v1).
- Values are deep-copied into snapshots; aliasing must be real inside the environment (two names, one Python list object) so that `alias` semantics are literally Python's.
- Step cap 5,000. Exceeding it is a generator bug: `validate` rejects the program and tests assert it never happens in the generated ranges.
- **Differential test (tests only):** `exec(rendered.source, {"__builtins__": {"range": range, "len": len, "print": <collector>}}, env)` and compare `env[target]` / collected stdout with `run(program, TRUE)`. 5,000 seeds across the dimension grid. This is the acceptance test that the interpreter *is* Python on the subset.

## 6. Generator

### 6.1 Dimensions

```python
@dataclass(frozen=True)
class CodeDimParams:
    n_statements: int = 6            # top-level + nested statements, 4 .. 60
    max_nesting: int = 1             # if/for nesting depth, 0 .. 3
    n_variables: int = 3             # distinct top-level names, 2 .. 12
    distractor_fraction: float = 0.0 # share of statements that cannot affect the answer, 0 .. 0.6
    identifier_confusability: float = 0.0  # 0 = descriptive names, 1 = every name has a look-alike sibling (§7)
    aliasing_on: bool = False
    late_binding_on: bool = False
    shadowing_on: bool = False
    inclusive_range_trap_on: bool = False   # loop bound chosen so exclusive vs inclusive differ in the answer
    negative_modulo_on: bool = False
    mutable_default_on: bool = False
```

Validation in `__post_init__` like `ToolDimParams`. Trap toggles are booleans in the dataclass and 0/1 floats in escalation configs (same 0.5-threshold convention as `_apply_tool_trap_config`).

### 6.2 Growth procedure

Grow the body statement by statement against a live symbol table (name → static type), so every read is of a defined name of a compatible type. Choose statement kinds by stable hash of `(seed, position)`. Honour `max_nesting`. Keep loop iteration counts in 2..6 and nesting so that the executed step count stays well under the cap (check with `run` during generation; if over 2,000 steps, re-draw the statement).

The **query target** is chosen among top-level names assigned at least twice or inside control flow (so there is something to track). `stdout` queries require ≥ 1 `print`.

### 6.3 Trap injection (each is a function in `mutate.py` used by `generate`)

Each trap is injected as a concrete code pattern whose correct reading requires the Python semantics, and whose shallow reading is the matching foil. The generator must make the trap **load-bearing** (§6.4).

| Trap toggle | Injected pattern | Shallow prior (foil) |
|---|---|---|
| `aliasing_on` | `b = a` where `a` is a list, then mutate through one name (`b.append`, `b[0] = …`, `b += […]`), query the other | `list_assignment="copy"` |
| `late_binding_on` | nested `def` reading an enclosing variable; variable reassigned after the def; function called after the reassignment | `closure_binding="early"` |
| `shadowing_on` | a `def` whose body assigns a name that also exists at top level, and the top-level name is the query target or feeds it | `scoping="flat"` |
| `inclusive_range_trap_on` | a `for i in range(n)` whose body accumulates into the target such that off-by-one changes the answer (e.g. `total += i`) | `range_upper="inclusive"` |
| `negative_modulo_on` | `x % k` or `x // k` with `x` negative at that point, feeding the target | `modulo="truncate"` |
| `mutable_default_on` | `def f(acc=[]): acc.append(…); return acc` called ≥ 2 times without the argument, result feeds the target | `default_args="fresh"` |

The two non-trap foils (`conditionals="all_branches"`, `loops="once"`) are **not toggles**; they are always computed and reported as salience (a program where the control-flow-blind foil still gets the right answer has no control flow worth the name).

### 6.4 Answer-relevance invariant

After generation, for every enabled trap, assert `answer(program, FOILS[trap_foil]) != answer(program, TRUE)`. If any enabled trap is not answer-relevant, re-draw with `seed' = stable_hash(seed, "retry", k)` up to 32 times, then raise `GenerationFailed`. The evaluator treats `GenerationFailed` the way the tool evaluator treats `validate()` rejections (record ERROR with `generation_failed=1`, never a FAIL). Tests assert the failure rate is < 2 % over the grid.

### 6.5 Distractors

A distractor is a statement whose removal does not change the answer *and* does not change the answer under any foil (so it cannot accidentally be the trap). Implement `is_distractor(program, stmt_id)` by removal + re-run under all semantics; the generator uses it to label statements, `measure` reports the realised fraction, and the `remove_distractors` ablation deletes them.

## 7. Rendering

`render(program, rendering_seed, naming)` → `RenderedProgram(source: str, name_map: dict, query_prompt: str, answer_format: str)`.

- Deterministic pretty-printer, 4-space indent, one statement per line, no comments by default.
- **Naming styles** (driven by `identifier_confusability`, drawn per name by stable hash):
  - `descriptive` — `total`, `items`, `count`, `names`, `result`, from a fixed pool, type-appropriate.
  - `lookalike` — for a chosen name, a sibling that differs by one of: underscore ↔ hyphen is impossible in Python, so use: `total` / `totl` / `tota1` / `total_` / `totaI` (capital i) / `tot_al`; `items` / `item` / `itens` / `items_`; `count` / `cnt` / `count2` / `c0unt`. Siblings must be valid identifiers and **never equal**. With confusability `c`, round(c × n_variables) names get a live look-alike sibling that is also used in the program (a sibling that is never read is not a trap).
  - Record which names are siblings in `name_map` so the verifier can detect `confused_identifier` (§8).
- Independent top-level statements may be stably permuted by `rendering_seed` only where data-flow allows (optional in v1; if skipped, say so).
- `query_prompt` for `final_value`: *"After this program runs, what is the value of `<name>`? Think it through, then give the final answer on its own last line as `ANSWER: <python literal>`."* For `stdout`: *"…what does it print? Give the printed lines as a Python list of strings on the last line as `ANSWER: [...]`."*
- Round-trip check in tests: `ast.parse(source)` succeeds; identifiers in `source` equal `name_map` values.

## 8. Verifier

`verify_answer(text, program, rendered) -> TraceVerification(verdict, detail, parsed, truth, foil_matches, failed_stmt)`.

1. Extract the answer: the **last** line matching `^\s*ANSWER:\s*(.+)$`; else the last fenced block; else the last line. `ast.literal_eval` it. Failure → `malformed_answer`; empty response → `empty_answer`.
2. Compare to `answer(program, TRUE)` with Python equality. (Tuples vs lists: normalise tuples to lists before comparing; note it in detail as `normalised=tuple`.)
3. On mismatch, compute the **failure signature**, in this priority:
   - `foil_match=<name>[,<name>…]` — the parsed answer equals one or more foil answers. This is the analogue of `unknown_tool`: it names the shallow strategy the model used.
   - `confused_identifier=<name>` — the answer equals the final value of a look-alike sibling of the target (requires `name_map`). Test for this **before** `foil_match` when both apply, and report both.
   - `wrong_value` — neither.
4. `verifier_detail` string, same shape as the other provinces: `kind=code_trace;campaign=1;query=final_value;detail=<signature>;n_stmts=..;trace_len=..;traps=aliasing_on=1;late_binding_on=0;…;confusability=0.5` . Keep `kind=code_trace` first; `_query_kind_of` in `findings.py` keys on it.

`Verdict.PASS` on equality; `Verdict.FAIL` on any mismatch including malformed (the model was asked for a literal and did not give one — same policy as `malformed_plan` in Province II); `Verdict.ERROR` only for infrastructure (`cap_hit`, `api_error`, `cost_capped`, `generation_failed`).

## 9. Measure, foils, search, mutators

**`measure(program)`** → realised dims (floats): `n_statements`, `max_nesting`, `n_variables`, `trace_length` (executed statements under TRUE), `distractor_fraction` (via §6.5), `n_live_aliases` (pairs of names bound to one list object at any point), `n_closures`, `traps_on` (count), `identifier_confusability` (share of names with a live sibling), `answer_size` (len of repr).

**`foils.py`**: `FOILS = {"value_semantics": Semantics(list_assignment="copy"), "early_binding": …, "flat_scope": …, "inclusive_range": …, "truncating_modulo": …, "fresh_default": …, "all_branches": …, "loop_once": …}`. `foil_divergence(program) -> CodeFoilProfile(n_diverging, divergence_score, trap_kinds, foil_answers)` where `divergence_score` counts diverging foils, weighted by how early (statement index / n_statements) the foil's trace first departs from the truth, mirroring `ToolFoilProfile`.

**`search.py`**: copy `toolworld/search.py`, swap types. Used by the evaluator when `adversarial=True`.

**`mutate.py`**:

```python
ANSWER_PRESERVING = {"rename_descriptive", "remove_distractors", "annotate_aliasing", "annotate_closures"}
ANSWER_CHANGING   = {"inject_aliasing", "inject_late_binding", "inject_shadowing",
                     "inject_inclusive_range_trap", "inject_negative_modulo", "inject_mutable_default"}
```

- `rename_descriptive(program, seed)`: look-alike names → descriptive. Answer-preserving by construction (names are not values). This is the ablation that tests H1 causally, exactly as `pronoun_suppression` did for belief.
- `remove_distractors`: delete statements flagged by §6.5.
- `annotate_aliasing` / `annotate_closures`: add a comment on the trap line (`# b and a are the same list`, `# f reads x at call time`). Answer-preserving; separates "didn't notice" from "noticed, still wrong". Rendering-level, so it lives in `render` with a flag; expose through `mutate` for the catalog.
- `inject_*`: the six §6.3 injectors, re-usable on an existing program (they are what `generate` calls).

Every mutator asserts its own answer invariant and raises `MutationUnavailable` when it cannot hold on this program (the `AblationUnavailable` pattern in `ablate/ablations.py`).

## 10. Evaluator, rays, campaign, wiring

**`CodeTraceEvaluator`** in `exec/evaluator.py`, modelled line-for-line on `ToolBreakingPointEvaluator`: config keys = `CodeDimParams` scalars (ints/floats) + the six trap toggles as 0/1; `_task_seed(config, trial_seed)` for the world seed; cost cap and cancel under the lock; `default_token_budget(model)` and the length retry; `ExperimentRecord(kind=code_trace)` with `latent_params` = config incl. toggles, `realized_dims` = `measure(program)` + `n_foil_matches`, `prompt_text` = full prompt, `response_text`, `verifier_detail` from §8. Add `_CODE_KNOWN_KEYS` next to `_TOOL_KNOWN_KEYS`.

**Default rays** (functions beside `default_tool_ray`):

```python
def default_code_lookalike_ray():   # H1 — the cross-province test
    return EscalationRay(dims=(RayDim("n_statements", 6, 40, True),
                               RayDim("identifier_confusability", 0.0, 1.0, False)))

def default_code_trap_ray():        # H2 — structure
    return EscalationRay(dims=(RayDim("n_statements", 6, 30, True),
                               RayDim("aliasing_on", 0, 1, True), RayDim("late_binding_on", 0, 1, True),
                               RayDim("shadowing_on", 0, 1, True), RayDim("inclusive_range_trap_on", 0, 1, True),
                               RayDim("negative_modulo_on", 0, 1, True), RayDim("mutable_default_on", 0, 1, True)))

def default_code_length_ray():      # control: magnitude only, no traps, descriptive names
    return EscalationRay(dims=(RayDim("n_statements", 6, 60, True), RayDim("max_nesting", 0, 3, True)))
```

Trap toggles as `RayDim(name, 0, 1, True)` switch on at t ≥ 0.5, matching `_apply_tool_trap_config`'s convention; check that file and reuse its helper rather than re-implementing.

**`run_code_breaking_point_campaign(client, store, run_id, model, ray=None, budget=200, cost_cap=0.5, k_trials=7, parallelism=4, max_tokens=None, cancel_event=None, verify="auto")`**: same shape as `run_tool_breaking_point_campaign` so `/launch`'s generic kwarg forwarding works.

**Wiring checklist** (each is small; do all):
- `viewer/findings.py`: add `"code_trace"` to `CAPABILITY_KINDS`.
- `viewer/serve.py` (additive only): `_KIND_ORDER.append("code_trace")`, `_KIND_LABEL["code_trace"] = "Code tracing"`, `_KIND_SHORT["code_trace"] = "Code trace"`.
- `viewer/launch.py`: preset `"code_tracing": {"budget": 200, "cost_cap": 0.5}` plus a `CAMPAIGN_DESCRIPTIONS` entry with `query_kind: "code_trace"`, `new: True`; dispatch to the campaign runner where `tool_breaking_point` is dispatched (grep for it).
- `report/frontier.py`: two `Spec` rows in `SPECS`, before `consistency`:
  - `code_lookalike`: label "Traces a program when variable names look alike", short "code with look-alike variable names", province "IV · Code tracing", kind `code_trace`, mode `axis`, runs `(r"^camp-code-lookalike-",)`, `where=lambda r: r.latent_params.get("identifier_confusability", 0) >= 0.5`, axis `_latent("n_statements")`, axis_label → "`N`-statement programs".
  - `code_traps`: label "Traces a program through semantic traps (aliasing, closures, off-by-one, modulo, defaults)", short "semantic traps in code", runs `(r"^camp-code-traps-",)`, axis = count of trap toggles on (write `_code_traps_on` like `_traps_on`), axis_label "`k` of 6 traps active".
  - Run `python3 scripts/frontier_summary.py` and confirm both rows render as NOT TESTED without exceptions.

Run-id convention for the future live runs: `camp-code-lookalike-{nano|super|ultra}-001`, `camp-code-traps-…`, `camp-code-length-…`.

## 11. Synthetic zoo (phase zero; blocks everything live)

Add fake models to the existing zoo pattern (see `tests/test_synthetic.py` and the planted-sensitivity tests in `test_ablate.py`). Each fake is `(program, rendered) -> str` returning an `ANSWER:` line computed by our interpreter under a chosen `Semantics`, so it is a *perfect reader with one wrong belief*:

| Fake | Returns | Must break on | Must not break on |
|---|---|---|---|
| `perfect` | `answer(TRUE)` | nothing | everything |
| `copy_believer` | `answer(value_semantics)` | `aliasing_on` | all other traps, look-alikes |
| `early_binder` | `answer(early_binding)` | `late_binding_on` | others |
| `flat_scoper` | `answer(flat_scope)` | `shadowing_on` | others |
| `inclusive_ranger` | `answer(inclusive_range)` | `inclusive_range_trap_on` | others |
| `truncator` | `answer(truncating_modulo)` | `negative_modulo_on` | others |
| `fresh_defaulter` | `answer(fresh_default)` | `mutable_default_on` | others |
| `sibling_confuser` | truth, except returns the value of a look-alike sibling when the target has one (prob. 1) | `identifier_confusability > 0` | descriptive names |
| `length_limited(L)` | truth if `trace_length ≤ L` else `all_branches` answer | long programs | short ones |

Tests:
- Each fake's FAIL rate is 100 % on programs with its trap on and 0 % with it off (exact, because the generator guarantees answer-relevance).
- `verify_answer` on each fake's output yields `foil_match=<its foil>` (or `confused_identifier`) on every FAIL, never `wrong_value`.
- `find_breaking_point(copy_believer_evaluator, default_code_trap_ray())` returns `found=True` and the decompose step attributes the break to `aliasing_on` alone (use the existing decompose output shape; see `test_escalate.py` / `test_decompose.py`).
- `find_breaking_point(perfect, any ray)` → `found=False`, `degradation_onset=None`.
- `find_breaking_point(sibling_confuser, default_code_lookalike_ray())` → `found=True`, boundary at the first t where `identifier_confusability ≥` the smallest value that gives the target a sibling. `rename_descriptive` ablation on its failures → PASS (the planted-sensitivity test, same as `pronoun_suppression`).
- `length_limited(40)` on `default_code_length_ray()` → break located near the config whose realised `trace_length` crosses 40 (within one bisect step).

## 12. Live plan (for the user to approve later; not for the agent to run)

Prompts are 200–1,500 input tokens; Nano reasons at 2–8k output tokens on tracing. Estimates per 200-trial ray: Nano ≈ $0.20, Super ≈ $0.45, Ultra ≈ $2.5.

1. Smoke: Nano, `default_code_length_ray`, budget 20, cap $0.03. Checks parsing, the answer format, and token demand (`finish_reason`).
2. H1: `default_code_lookalike_ray` on Nano and Super (k = 7), ≈ $0.70. Compare with `camp-tool-depth-*`: does Nano's break land at a similar length? Is the signature `confused_identifier`?
3. H2: `default_code_trap_ray` on Nano and Super, ≈ $0.70. Decompose at the frontier: which trap carries the break?
4. Control: `default_code_length_ray` on Nano and Super, ≈ $0.50. If this holds while 2–3 break, the structure-not-magnitude claim replicates in a fourth province.
5. Ultra: one ray, k = 5, only after 2–4, ≈ $1.5–2.5.
6. Fresh-seed confirmation of anything that becomes a finding, trial seeds ≥ 500,000.

Report pass rates with Wilson CIs; use the ledger's RS/FS/IM/ST codes.

## 13. Out of scope for v1 (do not build)

Code generation; any sandbox; Nemotron-rendered source (the LLM renderer + fidelity gate); the adversarial proposer for codeworld; `while` loops; floats; exceptions as a query kind ("which line raises"); multi-function programs beyond one nesting level; the atlas chart changes (the atlas picks up `kind=code_trace` through `CAPABILITY_KINDS` already — verify, don't redesign).

## 14. Hand-back format

In the final message: files touched (new vs edited); test count before/after and the full-suite result line; the 7 acceptance items each marked met / not met with the number; deviations from this spec with the reason; the generation-failure rate on the grid; two rendered example programs (one look-alike, one all-traps) with their truth and every foil's answer; anything you noticed that the spec got wrong. Commit your files by explicit path with a message that starts `Province IV: code tracing —`. Leave `serve.py` uncommitted.
