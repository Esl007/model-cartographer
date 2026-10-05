# Spec: finding a compact code-reading breaking point (Province IV, round 2)

*2026-10-05. Evidence base: `docs/research/code-reading-failure-modes.md`. Parent rules: `docs/spec-frontier-breaking-points.md` §0–§1 (standing rules; a boundary counts only if compact, resample-stable, fresh-seed confirmed, attributed).*

## Why
Gemma 4 31B QAT scores 100 % on every Province IV campaign (300+ trials). The ablation engine explains failures; it has none to explain. Three weaknesses in the *search* explain the saturation: traps are independent blocks (each writes its own slot of the answer), programs are sampled at random rather than adversarially, and nothing hunts for a failure and shrinks it.

## Principle: general mechanisms, not puzzles
Every addition is a parametric family with a dial, a generator rule that makes it load-bearing (a named wrong reading must give a different answer, checked by `validate`), a foil (the same interpreter with one belief flipped) and a control that rules out "it is just longer". Program size stays ≤ 25 statements unless the dial *is* size.

## WP-G: generator mechanisms (files: `codeworld/{model,generate,interpret,render,foils,verify,measure}.py`, code section of `exec/evaluator.py`, `campaigns/registry.py`)
1. **`chain_depth`** (5 → 20): number of dependent updates on the path to the queried value, total size held at ~25 by converting live updates into path updates. Foil `stale_read` (one path update skipped). Control: same size, depth 5. (La Malfa et al. 2401.09074.)
2. **`stack_depth`** (0 → 4): semantics stacked on ONE list value that reaches the answer: alias → `+=` (mutates) vs `x = x + [..]` (rebinds) → slice copy → function that mutates vs rebinds its parameter. New foils: `augassign_rebinds`, `concat_mutates`, `slice_aliases`, `param_copies`. Control: the same statements with the alias removed.
3. **`loop_branch`** (0/1) and **`while_on`** (0/1): a data-dependent branch inside a loop over a list; a `while` whose exit depends on computed state (bounded by the generator: must terminate in ≤ 40 iterations under truth and is rejected otherwise; foils that do not terminate count as diverging). New foils: `predicate_frozen` (condition evaluated once), existing `loop_once`.
4. **`sign_chain`** (0 → 3): chained `//`, `%`, `*` on a value that is negative at that point. Existing foil `truncating_modulo`.
5. **`index_math`** (0 → 3): negative indices and computed slice bounds at off-by-one boundaries. New foil `slice_end_inclusive`.
6. **`misleading_comments`** (0 → 3): a comment on a path line asserting a wrong value (the value a named foil would compute, so it is a *plausible* wrong value). New signature `comment_value`. Control: a neutral comment of equal length. (CodeCrash 2504.14119.)
7. **Adversarial selection** in `CodeTraceEvaluator(adversarial=True)`: among N candidate seeds pick the program with the highest foil-divergence score (existing `search.py`).
8. Rays: `default_code_depth_ray` (chain_depth), `default_code_stack_ray` (stack_depth + loop_branch + while_on), `default_code_gotcha_ray` (sign_chain + index_math + misleading_comments). Registry ids `IV.chain_depth`, `IV.stack`, `IV.gotcha`.

Invariants: all dials at 0 ⇒ byte-identical programs to today (hash test); interpreter equals CPython on 3,000 programs across the new grid (including `while`); every enabled mechanism's foil diverges; zoo fakes (one per new foil) fail exactly on their mechanism.

## WP-H: failure hunt and shrink (files: new `cartographer/hunt/`, `codeworld/mutate.py`, `scripts/run_hunt.py`)
1. **Meaning-preserving rewrites** (answer-preserving mutators): operand swap on commutative ops, negate-and-swap if/else, unroll the first loop iteration, insert dead code that reads the path variable, rename one variable. A model that passes the original and fails a rewrite has a *consistency* failure (2604.16320: 99 % → 76 %).
2. **Hunt loop** `hunt(evaluator_factory, seeds, budget)`: (a) sample programs from a config pool, k = 1 each; (b) for each PASS, try rewrites and trap injections (k = 1); (c) every FAIL goes through the resample gate (3 reruns); (d) stable failures are **shrunk**: delta-debugging over top-level statements and trap sites, keeping a candidate only if it is still a valid program (re-`validate`d, answer recomputed by the interpreter) and still fails 2 of 2; (e) output = minimal failing program, its truth, the model's answer, the foil it matches, the statements that were necessary, and a reproducibility check on 3 fresh reruns. Deterministic given (seed, model answers). Hard budget in trials and tracked dollars.
3. Output: records under `camp-code-hunt-<label>-NNN` (+ `/verify`, `/shrink`), and `results/hunt/<run_id>.json` with the minimal programs. `frontier_summary` row `code_hunt` (rate of sampled programs with a stable failure).

Zoo: a planted `value_semantics` reader must yield a shrunk program of ≤ 6 statements that still contains the alias and the mutation; a perfect reader yields no finding; a planted rewrite-sensitive reader (fails iff an if/else is negated) is found only through the rewrite path.

## Live plan (Gemma 4 QAT on Modal, after both WPs verify)
Scout the three new rays (k = 3), Standard tier on any that move, then the hunt with a 300-trial budget. Warm to `/v1/models` = 200 first; stop the app at the end. Then Province V (code repair and writing) is specified from what was found.
