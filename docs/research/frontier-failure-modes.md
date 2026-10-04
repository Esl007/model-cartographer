# Frontier-model failure modes that are structural, short, and machine-checkable

Literature scan, 2026-10-03. Web research only. Every arXiv id below was opened and its title and authors checked. Numbers are quoted only when they were read from the abstract or from a results table. Where a paper reports results only in figures, the table says so and gives no number.

Provinces: **I** narrative world-model (object state / belief / event order), **II** single-shot tool planning (BFS ground truth), **III** interactive tool execution with tickets, **IV** code tracing (own interpreter, Python subset).

Caveat on transfer: every effect size below was measured on someone else's task. It tells us where to aim, not what our own generators will show.

---

## 1. Study table

| # | Citation | Failure mode (one sentence) | Effect on the strongest model tested | Compactness | Deterministic verifier? | Province mapping |
|---|---|---|---|---|---|---|
| 1 | Nezhurina, Cipolina-Kun, Cherti, Jitsev 2024, "Alice in Wonderland", arXiv 2406.02061 | A one-step relational question ("Alice has N brothers and M sisters; how many sisters does Alice's brother have?") breaks when the numbers change or the relation is made slightly harder. | Results are in figures only. GPT-4/GPT-4o and Claude 3 Opus score "well above 0.3" on the base problem, with strong swings across 4 number settings. On the harder AIW+ variant they "collapse almost to 0". No reasoning models tested. | 1-2 inferences (count the speaker into her own set). | Yes: integer answer. | I: questions where the queried agent belongs to the set being counted ("how many people are in the kitchen" right after the asker walks in). |
| 2 | Mirzadeh et al. 2024, "GSM-Symbolic", arXiv 2410.05229 (ICLR 2025) | Adding one clause that sounds relevant but changes nothing (GSM-NoOp) causes large drops. Changing only the numbers adds variance. | Table 1, GSM-Symbolic to GSM-NoOp: o1-preview 92.7 to 77.4; o1-mini 94.5 to 66.0; GPT-4o 94.9 to 63.1; GPT-4o-mini 91.7 to 54.1. Abstract: "up to 65%" across models. | One added sentence on grade-school problems. | Yes: numeric answer. | I: an event that sounds like a state change but is not. II: a tool-description sentence that reads like a precondition but is not. IV: dead code that looks load-bearing. |
| 3 | Chen, Chi, Wang, Zhou 2024, "Premise Order Matters", arXiv 2402.08939 (ICML 2024) | Presenting the premises in a different order from the proof order lowers accuracy, even though the logic is unchanged. | GPT-4-turbo, logic with 12 rules: forward 96.5, shuffled 80.8. With 8 rules: 99.0 to 93.5. R-GSM: 94.1 to 85.0. | Proofs need 4-12 rules; R-GSM needs 2-8 steps. **The effect is small at 8 rules or fewer.** | Yes. | II: list the tool catalog in reverse-dependency order. I: narration order vs. causal order. (Our own Province I order queries already hold at ≥99% under scrambling.) |
| 4 | Wu et al. 2023, "Reasoning or Reciting?", arXiv 2307.02477 (NAACL 2024) | The same task under changed rules (base-9 addition, 1-indexed Python, and 11 task families in all) is solved much worse than under the default rules. | GPT-4 numbers are in figures and Appendix C only; not quoted here. Abstract: performance "substantially and consistently degrades". Bases 8 and 16 suffer less than 9 and 11. | 2-digit addition, short programs: a few steps. | Yes. Our interpreter can run a declared dialect exactly. | IV: declare a counterfactual dialect in the prompt (1-indexed lists, inclusive `range`) and trace under it. |
| 5 | Malek, Ge, Lazic, Jin, György, Szepesvári 2025, "Frontier LLMs Still Struggle with Simple Reasoning Tasks", arXiv 2507.07313 | "Unpuzzles": classic puzzles edited to become trivial are answered as if they were the original. | Table 1, puzzle to unpuzzle: o3 87.6 to 74.2; Gemini 2.5 Pro 93.8 to 62.9; Claude 3.7 Sonnet 77.3 to 48.5; GPT-4o 75.3 to 19.6. Moving the unpuzzle to a new setting partly helps (o3 about 75 to 80), which points to memorization. | 1-3 steps by design. | Partly: their set is hand-written. A generated version would be fully deterministic. | I: a Sally-Anne story in which Sally *does* see the move, so her belief equals reality. II: a planning task that looks like a known hard pattern but has a one-step solution. |
| 6 | Sclar et al. 2024, "Explore Theory of Mind", arXiv 2412.12175 | Stories produced by a program-guided search over story actions are adversarial for belief tracking. | Abstract: GPT-4o and Llama-3.1-70B "as low as 9% and 0%" on the hardest generated sets. | Short programmatic stories. Exact per-story length not checked. | Yes: the program tracks every agent's state. | I, directly. This is the closest published analogue to our foil-salience search. |
| 7 | Gu et al. 2024, "SimpleToM", arXiv 2410.13648 (ICLR 2026) | Models infer what a character knows but fail to *use* it to predict the character's action or to judge it. | Two-sentence stories. Mental state / behavior / judgment: GPT-4o 95.6 / 49.5 / 15.3; o1-preview 95.6 / 84.1 / 59.5; Claude 3.5 Sonnet 97.9 / 67.0 / 24.9; Llama-3.1-405B 97.8 / 58.2 / 10.0. | 2 sentences, 1-2 inferences. | Yes: multiple choice. | I: ask "where will Bob look first?" or "which action does Bob take next?" instead of "where does Bob think it is?". |
| 8 | Laban, Hayashi, Zhou, Neville 2025, "LLMs Get Lost in Multi-Turn Conversation", arXiv 2505.06120 | When a fully specified task is split into pieces revealed over several turns, models commit early to wrong assumptions and never recover. | -39% on average over 6 tasks; aptitude -16%, unreliability +112%. Main results table, full vs. sharded: GPT-4.1 90.5 to 59.9; Gemini 2.5 Pro 95.5 to 65.8; Claude 3.7 Sonnet 92.4 to 57.0; o3 88.0 to 52.0; DeepSeek-R1 93.6 to 50.9. | 2-8 shards, i.e. 2-8 user turns. | Yes for Code, Database, Actions and Math, which are checked by execution or exact match. | III: reveal the goal's constraints across 2-4 simulated user turns. |
| 9 | Laban, Murakhovs'ka, Xiong, Wu 2023, "Are You Sure? ... FlipFlop", arXiv 2311.08596 | A content-free challenge ("Are you sure?") makes models abandon correct answers. | 10 LLMs on 7 classification tasks: answers flip about 46% of the time, with a 17% average accuracy drop. Per-model numbers not checked. | 2 turns. | Yes: compare the first answer with the final one. | III/I/IV: after a correct answer or correct tool call, a user turn pushes back with no new evidence. |
| 10 | Gema et al. 2025, "Inverse Scaling in Test-Time Compute", arXiv 2507.14417 (TMLR) | Longer reasoning lowers accuracy on simple counting with distractors and on deduction with constraint tracking. Claude models get distracted; o-series models overfit to the problem framing. | Claude and OpenAI o-series models. Effect sizes not extracted here, so no number is given. | Counting items: 1 step plus distractors. | Yes. | All provinces: a measurement axis (sweep the reasoning budget) rather than a mutator. Pairs with #2. |
| 11 | Levy, Jacoby, Goldberg 2024, "Same Task, More Tokens", arXiv 2402.14848 (ACL 2024) | The same 2-paragraph reasoning question gets worse as irrelevant padding is added, well below the context limit. | Averaged over models, about 0.92 to 0.68 at 3,000 tokens. Different-topic padding hurts more than same-topic padding. Key facts placed last do best. Per-model values read from a figure (GPT-4 about 1.0 to 0.85) are approximate. | Few reasoning steps, but about 3k tokens of padding. Borderline. | Yes: true/false. | I: our filler mutator already covers this, and it saturated (Ultra held with 60 filler events). Low priority. |
| 12 | Lam, Wang, Huang, Lyu 2025, "CodeCrash", arXiv 2504.14119 (NeurIPS 2025); baseline Gu et al. 2024, "CRUXEval", arXiv 2401.03065 | Misleading natural language inside code (comments, names, hints) drags output prediction toward the wrong value, even though execution ignores it. | 17 LLMs: -23.2% on average for output prediction; -13.8% with CoT. Large reasoning models are more robust but use 2-3x the tokens, and QwQ-32B shows "Reasoning Collapse". Baseline (CRUXEval): GPT-4 with CoT reaches only 81% on unperturbed output prediction. | Short CRUXEval-style functions. | Yes: run the code. | IV: wrong comments, misleading variable or function names, and false `# returns X` hints. |
| 13 | Kirichenko, Ibrahim, Chaudhuri, Bell 2025, "AbstentionBench", arXiv 2506.09038; Fan, Li, Sun, Zhou 2025, "Missing Premise exacerbates Overthinking", arXiv 2504.06514 | Reasoning models answer, or think at length, instead of saying the question cannot be answered. | 20 frontier LLMs: reasoning fine-tuning lowers abstention by 24% on average, and scale does not help. Missing-premise paper: reasoning models' response length "drastically increases" on ill-posed questions, while non-reasoning models spot the problem faster. No accuracy number is given in that abstract. | 1 question with one fact removed. | Partly: AbstentionBench uses an LLM judge to detect abstention. In our worlds the BFS proof of unreachability plus `[]` is fully deterministic. | II: `make_unsolvable`, already built. III: a ticket that is never issued. IV: read an undefined name. |
| 14 | Zhang et al. 2024, "ToolBeHonest", arXiv 2406.20015; related: Patil et al., "BFCL", ICML 2025 (PMLR 267) | Tool-augmented models invent tool calls when the toolset cannot solve the task, or when a needed tool is missing or only partly fits. | Gemini-1.5-Pro 45.3/100 and GPT-4o 37.0/100 overall. BFCL v3 adds 1,000 multi-turn cases and tests abstention; its numbers were not extracted here. | Single task, a few calls. | Yes for solvability. ToolBeHonest's free-text parts are not. | II/III: `make_unsolvable` plus the "near-fit tool" variant: a tool whose name matches but whose effect is slightly wrong. |
| 15 | Yao, Shinn, Razavi, Narasimhan 2024, "tau-bench", arXiv 2406.12045 | Agents that follow domain rules across a user dialogue are inconsistent across repeated trials of the same task. | gpt-4o: under 50% task success; pass^8 under 25% (retail). | Long: many turns per episode. **Not compact.** | Yes: the final database state is compared. | III: use **pass^k** as a reporting metric. Do not copy the task length. |
| 16 | Wen et al. 2024, "ComplexBench", arXiv 2407.03978 (NeurIPS 2024 D&B); Jaroslawicz et al. 2025, "How Many Instructions Can LLMs Follow at Once?" (IFScale), arXiv 2507.11538 | Instructions whose constraints are nested, chained or conditional, or very many simultaneous constraints, are followed poorly. | ComplexBench reports "significant deficiencies" but gives no headline number in the abstract. IFScale: the best of 20 frontier models reaches 68% at 500 simultaneous instructions. | ComplexBench composition: a few constraints (compact). IFScale: up to 500 (not compact). | Partly. ComplexBench uses LLM-judged rubrics. IFScale's keyword inclusion is deterministic. | II: conditional preconditions ("if flag X is set, deploy needs Y") — the compact form only. |
| 17 | Dziri et al. 2023, "Faith and Fate", arXiv 2305.18654 (NeurIPS 2023) | Compositional tasks are solved by matching sub-graphs seen in training. Accuracy falls as the computation graph grows. | GPT-4: 59% on 3-digit x 3-digit multiplication (paper intro). | Grows with digits. Already about 9 partial products at 3x3. | Yes. | IV: carry-heavy arithmetic inside traced code. A mild stressor, not a structural trap. |
| 18 | Dasgupta et al. 2022/2024, "Language models show human-like content effects on reasoning tasks", arXiv 2207.07051 (PNAS Nexus) | Logic accuracy depends on whether the conclusion agrees with world knowledge (belief bias). | Qualitative in the abstract. Tested on pre-frontier LMs, so frontier transfer is unverified. | 1 syllogism. | Yes. | I: stories whose final state goes against common sense (an ice cube left in the oven stays "in the oven"; ask location, not state). Low priority. |

Considered and not given a row: Shi et al. 2023, "Large Language Models Can Be Easily Distracted by Irrelevant Context" (GSM-IC), arXiv 2302.00093 (ICML 2023). It is the forerunner of #2, tested on pre-frontier models. #2 replaces it for frontier claims. Its mitigation (telling the model to ignore irrelevant text) is a useful control.

---

## 2. Ranked shortlist: most likely to break frontier models within 5 steps on our generators

Ranking criteria: (a) shown on o1/o3/Claude-3.5+/Gemini-2.5-class models; (b) needs 5 steps or fewer; (c) fits a generator we already have, with a verifier that needs no new judgment.

### 1. Province I: the trivialized false-belief story (unpuzzle + SimpleToM)
**Mutation.** Take a world from the belief-query generator in the classic Sally-Anne shape: agent A puts the object in L1 and leaves, B moves it to L2. Then make one change that makes the trap go away. A comes back and watches the move, or B tells A about it, or A makes the move himself. The correct belief is now L2, equal to reality. Ask either the belief question or the behavior question ("Where will A look first?"). The answer key comes from the existing per-agent witnessed-event replay, unchanged. Add a foil `classic_false_belief` (always answer L1). This is the reverse of the `final_true_state` foil. A model that fails both directions is matching a template, not tracking belief.
**Prediction.** Unpuzzles cost 13-31 points on o3 / Gemini 2.5 Pro / Claude 3.7 Sonnet and 56 on GPT-4o (Malek et al. 2025, Table 1). SimpleToM's behavior questions cost 12-46 points against mental-state questions on frontier models (Gu et al. 2024). Expect a drop of 10 points or more on the strong Nemotron tiers, at chain length 3-5.
**Rival explanation.** Witness wording is ambiguous ("A came back as B moved it" may not read as "A saw it"). Controls: run the existing `witness_explicitation` ablation and require that the failure survives it. Add a context-shifted twin (same logic, non-ToM nouns) as Malek et al. did. If failures disappear when the setting changes, the cause is template recall.

### 2. Provinces I, II, IV: one plausible no-op clause (GSM-NoOp)
**Mutation.** Insert exactly one sentence that sounds relevant to the answer but changes no state.
- I: "A planned to move the key to the attic but was called away", "A picked up the key, looked at it, and put it back in the drawer", or a move of a *different* key in a different room.
- II: one tool description gets "Note: deploy usually requires the cache to be warmed", where no such precondition exists in the state machine.
- IV: an assignment to a variable that is never read, or a branch that is never taken whose body changes the returned name.

The answer key must stay byte-identical. Assert it, as the ablation code already does. Add a foil equal to "the answer if the clause had taken effect".
**Prediction.** GPT-4o fell 31.8 points and o1-preview 15.3 points from a single NoOp clause (Mirzadeh et al., Table 1). Expect larger drops on non-reasoning tiers than on reasoning tiers. Expect failures to match the "clause took effect" foil.
**Rival explanation.** (a) The clause really is ambiguous ("planned but was called away" could be read as done). Use a deterministic wording whitelist and the resample gate. (b) Extra length, not content. Add a length-matched neutral sentence as a control. (c) Shi et al.'s control: add "some information may be irrelevant". If that one line recovers most of the loss, report the effect as attention capture rather than a reasoning gap.

### 3. Province IV: misleading natural language in code (CodeCrash)
**Mutation.** Keep the program and the interpreter's answer key unchanged. Then add one of the following:
- a wrong comment (`# returns the list sorted ascending` on code that does not sort);
- a misleading name (`total` holding a max, `is_empty` returning True for non-empty input);
- a false inline hint (`# x == 7 here` where x is 6).

Use one per program in the first sweep, so each can be attributed. Add a foil equal to "the value the comment claims".
**Prediction.** -23.2% on average across 17 LLMs, and -13.8% even with CoT (Lam et al. 2025). Reasoning models hold up better but spend 2-3x the tokens. We already know Gemma does not break on look-alike variable names (H1 not supported). *Semantically misleading* names are a different trap from *visually confusable* names, so this tests something new.
**Rival explanation.** Trusting comments can be rational, since comments are usually true. Controls: (a) a neutral comment of the same length; (b) an instruction that comments may be wrong. A failure counts only if it matches the comment foil *and* survives control (b).

### 4. Province IV: counterfactual dialect (Reasoning or Reciting)
**Mutation.** Declare one rule change at the top of the prompt and set it in our interpreter: "in this language, lists are 1-indexed", or "`range(a, b)` includes b", or "`//` rounds toward zero". Programs stay short (5-15 statements, no loops longer than 4 iterations), and each must hit the changed rule at least once. The answer key comes from the dialect-aware interpreter. Foil: the CPython answer.
**Prediction.** Wu et al. report "substantial and consistent" degradation for GPT-4 on 1-indexed Python and on base-9 arithmetic (numbers in their figures and appendix only). Our Gemma data already shows weakness on floor division and modulo of negatives. The dialect version tests whether that is a missing rule or a prior that overrides an instruction.
**Rival explanation.** The model never took in the rule, which is an instruction-following failure rather than a tracing failure. Control: ask a one-line comprehension probe first ("with xs = [7, 8, 9], what is xs[1]?"). Score trace failures only on episodes that pass the probe. Also run a default-dialect twin of every program.

### 5. Province III: sharded goal across turns (Lost in Multi-Turn)
**Mutation.** In the interactive loop, the simulated user reveals the goal in 2-4 shards. Turn 1: "get service S healthy". Turn 2: "it has to use artifact version B, not A". Turn 3: "and don't touch flag F". Each shard narrows the goal, and the BFS ground truth is computed on the full goal. Shards arrive when the agent yields or asks. Score only the final state and any irreversible calls made before the last shard. The existing `irreversibility` tool, which is built but has never been swept, fits here: committing early becomes checkable. Report pass^k (tau-bench), not only pass@1.
**Prediction.** Frontier models lost 30-43 points in sharded mode (GPT-4.1 -30.6, o3 -36.0, DeepSeek-R1 -42.7). Most of the loss is unreliability (+112%), not aptitude (-16%) (Laban et al. 2025). Expect a rise in variance across resamples bigger than the fall in mean.
**Rival explanation.** Acting early may be reasonable agent behavior rather than a world-model failure. Controls: (a) a *concat* condition, with all shards in turn 1 as a bullet list, to separate "multi-turn" from "split wording"; (b) an explicit instruction to wait until the user says "go". The finding counts only if the loss holds against (a) and comes mostly from early commitment, read off the irreversible-call log.

### 6. Provinces II and III: unanswerable tasks made harder (AbstentionBench / missing premise / ToolBeHonest)
**Mutation.** This extends `make_unsolvable` in two compact ways. First, *near-fit tool*: replace the removed load-bearing tool with one whose name and description almost match but whose effect moves a different service, so BFS still proves the goal unreachable. Second, in Province III, *ticket never issued*: the call that should return the ticket returns a permanent, non-transient error, so no valid plan exists after step 2. Correct answer: decline or stop. Keep a solvable twin of every world so false declines are measured too.
**Prediction.** Reasoning fine-tuning lowers abstention by 24% on average across 20 frontier models (Kirichenko et al. 2025). GPT-4o scores 37.0/100 on ToolBeHonest. Reasoning models think *longer* on missing-premise inputs (Fan et al. 2025). Our own data already shows Gemma declining 0/16, Ultra 5/8, and every fabricated plan silently skipping the missing step (11/11). The near-fit tool should push even Super (10/12 now) toward fabrication.
**Rival explanation.** The prompt may not make declining feel allowed. Keep the explicit decline instruction and confirm, on the solvable twins, that the model does decline when it should *not* (calibration). A second rival: the model finds a real alternative path. That is ruled out by the BFS proof, so check that every fabricated plan fails in the verifier with `fabricated_plan_on_unsolvable` rather than `goal_reached`.

**Next in line (not top 6).** AIW-style self-inclusion counting in Province I (#1). Premise order in Province II (#3), where the effect is only about 5 points at 8 rules or fewer for GPT-4-turbo. FlipFlop pushback in Province III (#9): cheap to add as a second turn, but closer to sycophancy than world-modelling.

---

## 3. What does NOT count (avoid: needs long context or many compounding steps)

- **Lost in the Middle**, Liu et al. 2023, arXiv 2307.03172 (TACL). A position effect over many retrieved documents spanning thousands of tokens. Our contexts are short; our filler mutator is the short form, and it saturated.
- **RULER** (including variable tracking and multi-key needles), Hsieh et al. 2024, arXiv 2404.06654 (COLM 2024). Defined at 4K-128K tokens; "only half" of 17 models hold at 32K. A pure length effect.
- **Same Task, More Tokens**, Levy et al. 2024. Real, but it needs about 3k tokens of padding. Our `add_filler` already measured this and Ultra held. Keep it as a control, not a mutator.
- **Faith and Fate** at large sizes (Dziri et al. 2023). The collapse grows with multiplication size and puzzle grid size. Compounding, not structural.
- **The Illusion of Thinking**, Shojaee et al. 2025, arXiv 2506.06941 (NeurIPS 2025). Reasoning models collapse past a complexity threshold on puzzles such as Tower of Hanoi, where move count grows exponentially. These are long-horizon failures.
- **tau-bench task length** (Yao et al. 2024). Episodes run many turns. Borrow pass^k, not the episodes.
- **IFScale at high instruction density** (Jaroslawicz et al. 2025). The headline effect is at hundreds of simultaneous instructions.
- **Premise order with 12 rules** (Chen et al. 2024). The large drop needs long proofs plus distracting rules. At 8 rules or fewer the frontier effect is about 5 points.
- **Reversal curse**, Berglund et al. 2023, arXiv 2309.12288 (ICLR 2024). A training-time effect (GPT-4: 79% forward vs 33% reverse on celebrity parents). The abstract states that models *can* reverse a relation given in context. Our tasks are all in-context, so this does not apply.
- **GSM-IC** (Shi et al. 2023) and **content effects** (Dasgupta et al.). The magnitudes were measured on pre-frontier models. Use GSM-NoOp (#2) for frontier claims.

## Sources

- https://arxiv.org/abs/2406.02061 — Alice in Wonderland
- https://arxiv.org/abs/2410.05229 — GSM-Symbolic
- https://arxiv.org/abs/2402.08939 — Premise Order Matters
- https://arxiv.org/abs/2307.02477 — Reasoning or Reciting
- https://arxiv.org/abs/2507.07313 — Frontier LLMs Still Struggle with Simple Reasoning Tasks
- https://arxiv.org/abs/2412.12175 — Explore Theory of Mind
- https://arxiv.org/abs/2410.13648 — SimpleToM
- https://arxiv.org/abs/2505.06120 — LLMs Get Lost in Multi-Turn Conversation
- https://arxiv.org/abs/2311.08596 — FlipFlop
- https://arxiv.org/abs/2507.14417 — Inverse Scaling in Test-Time Compute
- https://arxiv.org/abs/2402.14848 — Same Task, More Tokens
- https://arxiv.org/abs/2504.14119 — CodeCrash
- https://arxiv.org/abs/2401.03065 — CRUXEval
- https://arxiv.org/abs/2506.09038 — AbstentionBench
- https://arxiv.org/abs/2504.06514 — Missing Premise exacerbates Overthinking
- https://arxiv.org/abs/2406.20015 — ToolBeHonest
- https://proceedings.mlr.press/v267/patil25a.html — BFCL (ICML 2025)
- https://arxiv.org/abs/2406.12045 — tau-bench
- https://arxiv.org/abs/2407.03978 — ComplexBench
- https://arxiv.org/abs/2507.11538 — IFScale
- https://arxiv.org/abs/2305.18654 — Faith and Fate
- https://arxiv.org/abs/2207.07051 — Content effects
- https://arxiv.org/abs/2302.00093 — GSM-IC
- https://arxiv.org/abs/2307.03172 — Lost in the Middle
- https://arxiv.org/abs/2404.06654 — RULER
- https://arxiv.org/abs/2506.06941 — The Illusion of Thinking
- https://arxiv.org/abs/2309.12288 — Reversal Curse
