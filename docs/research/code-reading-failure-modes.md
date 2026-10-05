# Code-reading failure modes of strong LLMs: what the literature supports for our generator

Scope: compact (≤ ~25 statements) code-execution and state-tracking failures in strong models, 2023–2026, mapped onto the code-tracing province's Python subset. Web research only, done 2026-10-05 under a 15-minute cap.

**How this was checked.** I opened every citation's arXiv abstract page and confirmed the title, authors and year. Numbers marked "(text)" appear in the paper's text or tables. Numbers marked "(~fig)" are approximate values read from figures, which I saw only through a summarizer, so re-check them before quoting. If a paper reports no number for a claim, the table says "no number found."

**Gemma.** None of the papers I checked tests Gemma on code execution. Our own Gemma 3 27B results (fails on aliasing and negative modulo) and Gemma 4 31B results (100%) are the only Gemma data we have.

---

## 1. Evidence table

| # | Citation | Failure mode (one sentence) | Quantified effect on the strongest model tested | Compactness | In our subset? | General mechanism |
|---|---|---|---|---|---|---|
| 1 | La Malfa, Weinhuber, Torre, Lin, Marro, Cohn, Shadbolt, Wooldridge, 2024, *Code Simulation Challenges for LLMs*, arXiv:2401.09074 | Accuracy falls as the **critical path** (the chain of instructions the answer depends on) gets longer, even when total program length stays at 20–30 lines. | GPT-4: about 90% at critical-path length 5, about 70% at 15, about 40% at 20 (~fig) | 20–30 lines | **Yes.** Assignments and augmented assignment are enough. | Number of dependent state updates on the queried value. Distractor work alone matters less. |
| 2 | Same paper (2401.09074), nested-loop programs | Accuracy falls sharply as loop work grows from O(n) to O(n²) to O(n³). | GPT-4: about 95% at O(n), about 60% at O(n²), about 15–20% at O(n³) (~fig) | Under 10 lines. Loop nesting drives difficulty, not length. | **Partly.** `for` over `range` works. Depth 3 needs one level of nesting beyond what we have, but inline nested `for` is fine. | Total loop-carried updates, i.e. product of trip counts. |
| 3 | Same paper (2401.09074), straight-line programs | Accuracy on add/sub/mov sequences decays with instruction count. | GPT-4: about 95% at 10 instructions, about 45% at 30, about 20% at 50 (~fig) | 10–50 instructions | Yes, but **mostly long**. See section 4. | Raw number of updates (a length effect) |
| 4 | Liu, Chen, Jabbarvand, 2024, *CodeMind*, arXiv:2402.09664 | Output prediction gets worse with nested constructs, complex conditions and loop predicates, mixed arithmetic and logic operators, non-primitive types, and API calls. | GPT-4-Turbo Spearman ρ with accuracy (text): cyclomatic complexity −0.68, nested constructs −0.61, LOC −0.50, loop length −0.35. Accuracy by benchmark: HumanEval 93.2%, CRUXEval 82.6%, Avatar 61.6% | CRUXEval-sized (3–13 lines) and up | Partly. Nesting, conditions and lists/dicts are in. While and recursion are not. | Branching on computed values plus non-primitive state |
| 5 | Liu, Jabbarvand, 2025, *A Tool for In-depth Analysis of Code Execution Reasoning of LLMs* (ExeRScope), arXiv:2501.18482 | Labels failures by construct (if, for, while, nested loop, try) and by **runtime loop-iteration count**. Search snippets report a strong negative correlation between loop length and accuracy. | No number found on the abstract page | Small functions | Partly. While and try are missing. | Iteration count of loop-carried state |
| 6 | Gu, Rozière, Leather, Solar-Lezama, Synnaeve, Wang, 2024, *CRUXEval*, arXiv:2401.03065 | Even on 3–13-line functions, GPT-4 has consistent failures on simple programs, mostly string and list manipulation. | GPT-4 + CoT: 81% output prediction, 75% input prediction (text) | 3–13 lines | Mostly. It uses str/list methods beyond ours. | Built-in method semantics combined with indexing |
| 7 | Abdollahi, Tasnia, Saha, Yang, Wang, Hemmati, 2025, *Demystifying Errors in LLM Reasoning Traces: … Code Execution Simulation*, arXiv:2512.00215 | Nine error categories in reasoning traces. **Computation errors dominate**, followed by control flow and indexing. Examples include misreading `range(-11, 0, -1)`, `split()`, and XOR on large ints. | Accuracy 85–98% across GPT-4o-mini, Gemini 2.5 Flash, DeepSeek-R1 and Claude 4 Sonnet. Category percentages not retrieved. | HumanEval+/LCB snippets | Mostly. Negative-step range, slicing and arithmetic are in. Bitwise ops and floats are not. | An arithmetic slip at one iteration propagates forward. Negative-step and index boundaries. |
| 8 | Chen, Pan, Hu, Li, Li, Xia, 2024 (ICSE'25), *Reasoning Runtime Behavior of a Program with LLM: How Far Are We?* (REval), arXiv:2403.16437 | Models can predict the final output while getting intermediate behaviour wrong (coverage, program state, execution path). They are inconsistent across these levels. | Average runtime-reasoning accuracy 44.4%. Average incremental-consistency score 10.3 (text, across models) | HumanEval/ClassEval functions | **Yes**, if we ask for intermediate state (value at line k, or which branch ran). | Right answer reached through a wrong path. Path questions expose it. |
| 9 | Lam, Wang, Huang, Lyu, 2025, *CodeCrash*, arXiv:2504.14119 | Misleading natural language inside code (comments, **print strings**, hint comments) and structural noise (renaming, reformatted conditionals, garbage code) break output prediction. | GPT-4o: −28.4% structural, −24.3% misleading prints, −26.6% misleading hints. Claude-3.5-Sonnet: −43.4% on misleading hints. DeepSeek-R1 about −2.4% on average. QwQ-32B goes into "reasoning collapse" (2–3× tokens) on plausible wrong hints. o3-mini-low: −19.8% on hints for LCB (text) | CRUXEval / LCB size | **Yes** for misleading `print` strings, renaming and dead code. Comments are not in the subset, but are trivial to add. | A natural-language prior overrides data flow |
| 10 | Spiess, Devanbu, Barr, 2026, *How Robustly do LLMs Understand Execution Semantics?*, arXiv:2604.16320 | Meaning-preserving transforms (OperandSwap, BlockSwap, ForWhileSwap, DeadCodeInsertion) and random identifier renaming cut accuracy for a frontier model. Exception outcomes are predicted badly. | GPT-5.2: 99.2% original, 76.4% after syntax transforms, 75.8% after renaming. Perturbed inputs: 99.1% → 79.6%. Exception prediction 14.8% with the default prompt, 81.3% with a modified prompt. Gemini 3 Pro stays at about 96–98% (text) | CRUXEval size | Mostly. OperandSwap, BlockSwap, dead code and renaming are in. ForWhileSwap and exceptions are not. | Surface-form dependence. Sycophancy toward "no exception." |
| 11 | Orvalho, Kwiatkowska, 2025, *Are LLMs Robust in Understanding Code Against Semantics-Preserving Mutations?*, arXiv:2505.10443 | Renaming, mirrored comparisons, swapped if/else, for→while and loop unrolling change predictions. 10–50% of correct predictions rest on unsound reasoning. | Accuracy drops of up to 70% (text, model not identified on the abstract page) | Small programs | Yes, except for→while | Surface-form dependence. Right answer for the wrong reason. |
| 12 | Haroon, Khan, Humayun, Gill, Amjad, Butt, Khan, Gulzar, 2025, *Assessing the Impact of Code Changes on the Fault Localizability of LLMs*, arXiv:2504.04372 | After semantics-preserving mutations, models lose faults they had localized before. | Fail on 78% of previously localized faults (text; 10 LLMs) | Function-level | Yes (as a transform) | Syntax-pattern reliance |
| 13 | Miceli-Barone, Barez, Konstas, Cohen, 2023 (ACL), *The Larger They Are, the Harder They Fail: LMs do not Recognize Identifier Swaps in Python*, arXiv:2305.15507 | After swapping built-in names (e.g. `print`↔`len`), models continue code as if the original meaning held. **Inverse scaling**: bigger models are more confidently wrong. | Qualitative inverse scaling. No single headline number on the abstract page. | One or two statements plus a short function | **Partly.** Needs rebinding built-in names (`len = print`-style). That is a declared-dialect variant of what we already do. | A prior from identifier meaning overrides the stated binding |
| 14 | Thimmaiah, Zhang, Srinivasa, Li, Gligoric, 2025/26, *LLMs Lean on Priors, Not Programming Language Semantics* (PLSemanticsBench), arXiv:2510.03415 | When familiar operators are given new meanings by supplied rules, models fall back to the standard meaning. Long-horizon traces collapse. | 11 frontier LLMs: about 90% under standard semantics. Drops of 40–60 points under semantic mutation. Best long-horizon result about 35% (text) | Small programs for the mutation result. The long-horizon part is long. | Yes. This is our declared-dialect idea, but per operator and mixed with the standard meaning. | A prior from symbol meaning overrides supplied rules |
| 15 | Wu, Qiu, Ross, Akyürek, Chen, Wang, Kim, Andreas, Kim, 2023 (NAACL'24), *Reasoning or Reciting?*, arXiv:2307.02477 | Under "ThonPy" (Python with 1-based indexing), GPT-4 degrades a lot on evaluating HumanEval programs. | Numbers are in figures and Appendix C only; not retrieved | HumanEval size | Yes. This is our counterfactual-dialect mechanism. | Default semantics leak into a counterfactual |
| 16 | Wei et al., 2025, *EquiBench*, arXiv:2502.12466 | Deciding whether two programs are equivalent mostly follows syntactic similarity. | Best of 19 LLMs: 63.8% and 76.2% on the hardest categories (50% is chance) (text) | Pairs of small to medium programs | Partly. Python pairs, but the task is equivalence rather than tracing. | Syntactic similarity used in place of semantics |
| 17 | Wu et al., 2025 (ICML), *How Do Transformers Learn Variable Binding in Symbolic Programs?*, arXiv:2505.20896 | Trained transformers go through a "shallow heuristic that prioritizes early variable assignments" before learning to follow assignment chains (up to 4 hops, with distractor chains). | Mechanistic; no LLM benchmark number | Under 20 lines | **Yes** (`a = b; b = c` chains plus distractors) | Depth of alias/assignment chains; early-assignment heuristic |

Also seen but not used (no compact quantified claim retrieved): NExT (Ni et al., 2024, arXiv:2404.14662; training on execution traces), Wang et al. 2025 (arXiv:2511.18288; complex execution paths, "stronger reasoning models do not always outperform weaker ones"), ReMind (Gao, Peng, Ren, 2025, arXiv:2511.00488), Tang et al. 2026 (arXiv:2605.30233; LMs "do not incrementally track world states").

---

## 2. Top 8 generalizable mechanisms, ranked

Ranking rule: (literature effect size on strong models) × (fits in ≤ 25 statements and in our subset) × (composes onto one value).

### M1. Critical-path depth on the queried value (dependency-chain length with fixed total size)
- **Dial:** `k`, the number of statements on the queried variable's backward data-dependency slice (5 → 20), with total statement count held at N = 25.
- **Generator rule:** build the slice first. Each step must be non-idempotent and order-sensitive, mixing `+=`, `*=`, `-=` and `%=` with operands taken from *other* slice variables, so skipping or reordering any step changes the answer (check this by deleting each step in the interpreter). Fill the remaining N − k statements with distractor updates to other variables that use the same names and operators.
- **Foil:** *stale-read*: one update on the slice is dropped, or a variable is read at its value before its last update. This is the "early assignment" heuristic in Wu et al. 2505.20896. Implement it as an interpreter that, for one variable, binds reads to the first assignment.
- **Control:** same N, same operator multiset, k = 3 (distractors carry the length). The effect must track k, not N.
- **Predicted effect:** GPT-4 goes from about 90% to about 40% as the critical path grows from 5 to 20 at 20–30 lines (La Malfa 2401.09074, ~fig). Expect Gemma 4 to stay near ceiling until k ≈ 12–15.

### M2. Interacting semantics on one value: alias + mutate-vs-rebind + slice-copy
- **Dial:** number of distinct semantic rules acting on the *same* list object (1 → 5): alias `b = a`, in-place `a += [x]` (mutates, so the alias sees it), rebinding `a = a + [x]` (alias does not see it), copy `c = a[:]`, `.append` / `.pop` / `.extend`, item assignment through the alias, and passing to a function that mutates its argument versus one that rebinds it.
- **Generator rule:** query a variable whose final value differs under each single-rule foil. Search over generated programs until the ground truth disagrees with every foil.
- **Foils:** copy-on-assign (existing). New: **`+=`-rebinds** (treats `a += [x]` like `a = a + [x]`), **`+`-mutates** (the reverse), **slice-aliases** (`a[:]` shares storage), and **param-rebind-visible** (rebinding inside a function leaks to the caller).
- **Control:** the same statements on an int or str, where all of these rules give the same result. That keeps length and syntax identical while removing the semantic interaction.
- **Predicted effect:** CodeMind reports non-primitive types as a failure factor (2402.09664; no per-type number). Gemma 3 already fails aliasing. The literature has **no** quantified aliasing dose-response, so this is a gap we can fill. The `+=` vs `+` asymmetry is the strongest compact candidate because each rule is easy alone and only the interaction is hard.

### M3. Natural-language prior vs data flow (misleading names and `print` strings)
- **Dial:** number of misleading natural-language cues on the critical path (0 → 4). Examples: a function named `total` that returns a max; `is_sorted` that isn't; `print("x is now 7")` when x is 9; a variable called `count` that is decremented.
- **Generator rule:** each cue must name the answer of a specific foil (the "intended" semantics), so a model that follows the cue matches that foil exactly. Cues are only ever names or print-string literals, so the ground-truth interpreter ignores them.
- **Foil:** *name-semantics interpreter*: execute the function or variable as its name claims (sum for `total`, sorted for `is_sorted`), or take the value claimed in the print string.
- **Control:** the identical program with neutral names (`f1`, `v3`) and prints that echo the true value. This differs from our existing look-alike-names test, where names are confusable but not *semantically misleading*.
- **Predicted effect:** CodeCrash misleading prints cost GPT-4o 24.3%; misleading hints cost Claude-3.5-Sonnet 43.4% (2504.14119). Renaming cut GPT-5.2 from 99% to 76% (2604.16320). Reasoning models are more robust (R1 about −2.4%), so expect a smaller effect on thinking models and a larger one when thinking is off.

### M4. Multiplicative loop work with data-dependent trip counts
- **Dial:** total loop-carried updates T = Π trip counts (4 → 60), at fixed statement count. Inner bounds depend on the outer variable or on a list built earlier (`for j in range(i, n)`, `for x in lst[i:]`).
- **Generator rule:** the accumulator update must not be linear in trip count, e.g. `acc = acc * 2 % m + j` or a conditional update, so that one missed or extra iteration changes the answer and a closed form can't be pattern-matched.
- **Foils:** range-inclusive and loop-runs-once (existing). New: **inner-bound-frozen** (inner range is evaluated with the first outer value) and **off-by-one-iteration** (exactly one iteration dropped at the end).
- **Control:** same statement count and nesting, with constant small inner bounds (T ≈ 4).
- **Predicted effect:** GPT-4 goes from about 95% (O(n)) to about 60% (O(n²)) to about 15–20% (O(n³)) (La Malfa, ~fig). CodeMind reports loop length ρ = −0.35 for GPT-4-Turbo. Abdollahi et al. show an arithmetic slip at one iteration propagating forward.

### M5. Data-dependent branching inside loops (path questions)
- **Dial:** number of branch-outcome flips along the executed path (0 → 8). The predicate reads loop-carried state, e.g. `if acc % 3 == 0` where `acc` changes every iteration.
- **Generator rule:** each branch arm updates the queried variable differently, and the sequence of outcomes must be non-periodic. Optionally ask **which branch ran at iteration i** or **the value after iteration i** (REval-style intermediate queries), as well as the final value.
- **Foils:** all-branches-run (existing). New: **predicate-on-initial-value** (the branch decision is computed once, before the loop) and **elif-falls-through** (more than one arm runs).
- **Control:** same code, with the predicate on the loop index only (`if i % 3 == 0`), which is periodic and pattern-matchable.
- **Predicted effect:** cyclomatic complexity has ρ = −0.68 with accuracy for GPT-4-Turbo (CodeMind). REval reports 44.4% average runtime-behaviour accuracy and low consistency (2403.16437). Control-flow errors are the second most common category in reasoning traces (2512.00215).

### M6. Sign-sensitive integer arithmetic composed on one value
- **Dial:** number of operations on the critical value whose result depends on sign or floor semantics (`//` and `%` with negative operands, `-x % m`, mixed with `*` on 3–5 digit magnitudes), plus operand magnitude in digits.
- **Generator rule:** force operands negative at the point of `//` and `%` (verify the operand sign in the interpreter trace), and chain the results so a truncation error at step j changes everything after it.
- **Foils:** %-truncates (existing). New: **//-truncates** (C-style toward-zero division), **carry-drop** (one digit carry missing in a multi-digit product, sampled as ±10^d).
- **Control:** same operator sequence with all operands positive and two digits.
- **Predicted effect:** computation errors are the most common error category even for 85–98%-accuracy models (2512.00215). CodeMind lists non-trivial arithmetic and logic operators. Gemma 3 already fails negative modulo. There is no published magnitude dose-response, so this is our measurement to make.

### M7. Index arithmetic: negative indices, negative steps, computed slice bounds
- **Dial:** number of computed index or slice expressions on the critical value (`s[-k:]`, `s[a:b]` with computed a and b, `s[::-1]`, `range(hi, lo, -1)`, `lst.pop(-2)`), and how many of them use negative values.
- **Generator rule:** bounds come from earlier computed variables (not literals), and each bound sits at an off-by-one boundary where the inclusive and exclusive readings give different answers.
- **Foils:** range-inclusive (existing). New: **slice-end-inclusive**, **negative-step-direction** (`range(-11, 0, -1)` treated as ascending, the exact example in 2512.00215), and **negative-index-from-1** (`s[-1]` read as the second-to-last item).
- **Control:** same statements with literal, positive, non-boundary indices.
- **Predicted effect:** indexing is the third most common error category (2512.00215). Default-indexing priors leak under ThonPy (2307.02477, figures only).

### M8. Metamorphic pairs: semantics-preserving transforms of a solved program
- **Dial:** number of preserving transforms stacked on one program (0 → 4): OperandSwap (`a < b` → `b > a`), BlockSwap (negate the condition and swap the arms), dead-code insertion (an `if False`-equivalent guard that uses a computed predicate that is always false), loop unrolling of the first iteration, and neutral renaming.
- **Generator rule:** apply transforms to programs the model already gets right. Score **consistency** (same answer on P and P′), not just accuracy. Dead code must update the queried variable, so a model that executes it matches a foil.
- **Foils:** all-branches-run (dead code executes). New: **negation-ignored** (after a BlockSwap the model keeps the original arm mapping).
- **Control:** P itself. P′ is the treatment, with the same answer by construction.
- **Predicted effect:** GPT-5.2 drops from 99.2% to 76.4% under OperandSwap, BlockSwap, ForWhileSwap and dead code (2604.16320). Accuracy drops of up to 70% (2505.10443). 78% of previously localized faults are lost (2504.04372). Gemini 3 Pro was robust (about 96%), so the effect varies by model, which suits a map of model differences.

**Composition recommendation:** put M2, M6 and M7 on *one* list variable whose element is then used in an M5 branch predicate inside an M4 loop. Each dial is independent and the foils stay single-belief. Run M3 and M8 as orthogonal wrappers over any generated program.

---

## 3. Mechanisms that need constructs outside our subset (ranked by payoff per construct)

1. **`while` (data-dependent termination).** The trip count is not visible syntactically; it emerges from state, which forces real tracing (M4 without a closed form). ForWhileSwap is one of the transforms behind GPT-5.2's 23-point drop (2604.16320). CodeMind and ExeRScope flag while loops and loop length. It composes with M1, M4, M5 and M6 at no extra cost (e.g. `while x % 7 != 0: x = x * 3 - 5`). Foils: *one-extra-iteration* and *check-after-body* (do-while). **Best payoff per construct.**
2. **`try` / `except` plus implicit exceptions** (e.g. `IndexError` from `pop` on an empty list, `KeyError` from `d[k]`, `ZeroDivisionError` from computed `% 0`). GPT-5.2 predicts exception outcomes at only 14.8% with the default prompt (81.3% when the prompt mentions exceptions). TypeError is the hardest. Part of this is prompt-induced sycophancy, so the probe must state that "raises X" is a valid answer. Foils: *no-exception* (the operation silently returns a default) and *except-catches-nothing*. **Second-best.**
3. **Classes and methods** (shared mutable state through `self`): CodeMind's intra-class dependencies have ρ = −0.68 and ClassEval accuracy is 77.5% for GPT-4-Turbo. These are mostly larger programs, so the payoff per compact program is lower.
4. **Recursion**: listed by CodeMind as a failure factor, with no compact number found.
5. **Floats**: precision, rounding, NaN and Inf errors are documented (2512.00215), but answer matching gets messy. Low priority.
6. **Comprehensions**: no evidence retrieved. Skip.

**Recommendation:** add **`while`** first and **`try/except` with implicit runtime errors** second.

---

## 4. Does not count (long-program or long-context effects)

- Decay on straight-line programs with instruction count (La Malfa: about 95% at 10 down to about 20% at 50 instructions). Beyond about 25 statements this is length. Gemma 4 already passes our 60-statement programs.
- PLSemanticsBench long-horizon semantic adherence (best about 35%). The *operator-mutation* result counts; the long-trace result doesn't.
- CodeMind LOC correlation (ρ = −0.50) and the Avatar/ClassEval accuracy gaps. These mix size with constructs.
- Repository-level execution-path reasoning (Wang et al. 2511.18288) and REval's ClassEval portion.
- Any effect that only appears once total tokens exceed the model's comfortable working length. Use M1's fixed-N control to rule this out every time.
