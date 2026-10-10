# Spec: the Evidence Fidelity Campaign (cross-cutting; first campaign over the six provinces)

*2026-10-10. Status: ROADMAP — build window opens after the 2026-10-30 hackathon
submission ships; nothing here preempts Phase 9. Parent rules:
`docs/spec-frontier-breaking-points.md` §0–§1 (standing rules, certificate),
plus each province's own spec. Evidence this builds on: F-G4Q-1 and the
restatement rescue (`docs/lab-log.md` 2026-10-04), the dialect/trivial-belief/
no-op/comment nulls (2026-10-04, 2026-10-05), Province V/VI overfitting
(`docs/reports/province-v…`, `province-vi…`), and the X.consistency scope note
(V-E). The external review of 2026-10-10 contributed condition D and the
two-family caution in §2; both are adopted.*

## 0. Hard rules for whoever builds this

1. Standing repo rules apply unchanged (no git stash/checkout/restore; the
   store is never tracked; no LLM in generation, mutation, ground truth or
   verification; `finish_reason=length` is ERROR; warm before campaigns; stop
   the Modal app in a `finally`; builders do not commit — the coordinator does).
2. **No existing evaluator, generator or dim changes meaning.** Every addition
   is a new optional dim or a new ablation entry that defaults to legacy
   behavior, regression-tested byte-identical (the Province VI §14 discipline).
3. **No spend before §7 acceptance holds.** The live plan (§9) runs only on the
   user's explicit go.
4. §5's hypotheses are pre-registered: statistics and thresholds are fixed
   before the first live record and any departure is reported as exploratory.
5. Tool-result rendering is frozen at `protocol_version=2` for the whole
   campaign; a rendering change mid-campaign voids pairing (the X5 lesson).

## 1. What this campaign is

The provinces each found a conflict between a **prior** (a plausible,
pattern-shaped answer) and **evidence** (a fact the task explicitly supplies).
Sometimes the evidence wins (declared dialects, misleading comments, no-op
clauses, trap-removed belief). Sometimes the prior wins (carried tickets,
others' beliefs, hidden-test requirements, look-alike identifiers). This
campaign measures, with matched conditions on identical seeds, **when evidence
survives contact with a prior, and which intervention restores it** — using
only the existing engine (escalation, resample gate, ablation attribution,
fresh-seed confirmation, certificates).

It is a campaign, not a province: it generates no new task kind. Every task
comes from an existing generator; the campaign contributes a **condition
grid**, a handful of answer-key-preserving mutators, paired statistics and a
report view.

## 2. The two candidate mechanisms (kept distinct, by design)

The results to date are compatible with **two different failure families**, and
the campaign's job is to test whether they are one, two, or a gradient:

- **Information-fidelity failures**: the model loses, mis-retrieves or
  substitutes a value it was explicitly given. Candidates: ticket fabrication
  (III), look-alike identifier binding (II), `fabricated_symbol` (VI).
- **Causal-model failures**: the evidence is present and even correctly read,
  but the relationship between evidence and required action is wrong.
  Candidates: repair's wrong-site/constant-patch behavior (V, VI), and possibly
  belief (which needs both: carrying another's information state AND reasoning
  over it).

The **carried-vs-in-view hypothesis** (fidelity degrades with retrieval
demand) is the leading explanation for the first family only. It is treated
here as a hypothesis under test (H-EF1/2), not an assumption; H-EF4 is the
explicit two-family discriminator. A result where the families dissociate is
as reportable as one where they unify.

**Operational definitions** (stamped into `realized_dims` on every record):
- `evidence_span`: tokens between the critical fact's last occurrence in the
  prompt/transcript and the decision point; `evidence_turns`: intervening tool
  turns. **In-view** = the fact occurs in the final pre-decision segment
  (`evidence_turns = 0`); **carried** = `evidence_turns ≥ 1` or a file
  boundary. These are measured covariates, not knobs; the knobs below move
  them indirectly and the analysis uses the realized values.

## 3. The condition grid

Four conditions per task family, all on the SAME seed (paired by construction,
run suffixes `/A`, `/B`, `/C`, `/D` on one run id, mirroring the VI
`/graph_on|off` pattern):

| | name | meaning | what a rescue means |
|---|---|---|---|
| **A** | agree | the prior's answer IS the correct answer | baseline sanity; A−B is the conflict cost |
| **B** | conflict | evidence contradicts the prior (today's default traps) | the phenomenon |
| **C** | salience | the critical fact is restated immediately before the decision point | retrieval was the bottleneck |
| **D** | presentation | the fact is emphasized AT ITS ORIGINAL POSITION (formatting only: quoted/upper-cased/flagged), no restatement, no added information | attention, not retrieval, was the bottleneck |

C and D are the diagnostic pair: C moves the fact across the carry gap, D only
makes it louder where it already is. Information identical in B/C/D; every
mutator passes the `_assert_same_answer` discipline already in
`ablate/ablations.py`.

## 4. Per-province instantiation (existing machinery first)

| Province / family | A (agree) | B (conflict) | C (salience) | D (presentation) |
|---|---|---|---|---|
| **I belief** (info+causal) | `full_witness` on (trivial-belief machinery, exists) | `witness_cutoff` classic (exists) | `witness_explicitation` (exists, `ablate/ablations.py`) | NEW `witness_emphasis`: the witnessing/cutoff sentence quoted + prefixed "Note:" in place |
| **II tool binding** (info) | distinct ids (`id_collisions` off, exists) | look-alike ids (`_confusable_id`, exists) | NEW `restate_ids`: the exact callable ids of path-relevant tools appended to the prompt tail | NEW `ids_emphasis`: ids back-ticked in the catalog, nothing appended |
| **III tickets** (info) | NEW dim `ticket_guessable=1`: the true ticket EQUALS the pattern completion (prior agrees) | `ticket_entropy=0`, `carry_distance ≥ 3` (exists) | `restate_tickets` (exists; the 6/6 rescue) | NEW `ticket_emphasis`: ticket wrapped in `<<…>>` at issuance only |
| **IV comments** (in-view anchor) | neutral comments (exists, round-2 control) | `misleading_comments` (exists; Gemma 4 null, other models untested) | restating the true traced value would leak the answer — C is N/A by construction, recorded as such | NEW `comment_emphasis`: the misleading comment upper-cased |
| **V/VI repair** (causal) | bug whose obvious local patch IS the true fix (NEW filter on existing injectors: `bug_local=1`) | standard repair / repo-fix (exists; the floor) | info-C: `restate_symbols` (exists, VI); causal-C NEW `show_data_flow`: append the true value trace from bug site to output (computed by our interpreter, deterministic, answer-key preserving) | NEW `diff_emphasis`: `run_tests` renders expected-vs-actual as an aligned diff block |
| **X consistency** | pass^k with resample CIs runs across all arms (exists) — the stability covariate, not a row | | | |

New mutators: 7 small ones (`witness_emphasis`, `restate_ids`, `ids_emphasis`,
`ticket_guessable`, `ticket_emphasis`, `comment_emphasis`, `show_data_flow`,
`diff_emphasis`, `bug_local`), each in its province's existing module
(`world/`, `toolworld/mutate.py`, `interactive/`, `codeworld/`, `codefix/` or
`repo/render.py`), each an optional dim defaulting off, each with an
answer-key-preservation test and a zoo hook. No new generator, env, verifier
or protocol.

## 5. Pre-registered hypotheses

Response variables are **rates, not only PASS/FAIL** (the ticket-floor lesson):
primary = the mechanism signature rate per scored episode
(`fabricated:*` count, `final_true_state`, `wrong_site*`+`visible_fixed_hidden_broken`,
`foil:comment_value`), secondary = pass rate. Paired tests: McNemar exact on
pass, Wilcoxon signed-rank on rates; families Holm–Bonferroni-corrected;
resample gate (n=3, `unresolved` excluded) on every FAIL; α = 0.05 per family;
fresh-seed confirmation per the certificate for any headline cell.

- **H-EF1 (conflict cost tracks carriage).** Across families, the A→B drop is
  large where the realized evidence state is *carried* (I belief, III tickets,
  II at plan scale) and small-to-absent where it is *in-view* (IV comments).
  Test: per-family A−B paired difference; between-family comparison of those
  differences, carried pooled vs in-view pooled.
- **H-EF2 (salience rescues carried, not in-view).** C improves B
  significantly in III and I (prediction: replicates 6/6-style rescue at n ≥ 20
  pairs) and does not change IV. Null in I would split belief away from the
  information family — informative either way.
- **H-EF3 (presentation rescues neither, or reclassifies).** D does not
  significantly improve B anywhere. A family where D ≈ C reclassifies that
  failure as attention-capture, not retrieval; that outcome is a finding, not a
  miss.
- **H-EF4 (two-family discriminator).** Cross-applied interventions:
  `show_data_flow` (causal-C) on repair improves B-repair but
  `restate_symbols` (info-C) alone does not; conversely `restate_tickets`
  fixes tickets while a causal scaffold (restating the tool-dependency chain)
  does not. If BOTH kinds of C rescue BOTH families, the one-mechanism reading
  survives; if rescues are family-specific, the two-family model wins. Either
  verdict resolves §2.
- **Control (interpretability gate):** condition A pass rate ≥ 0.8 in every
  family, else that family's grid is re-leveled before analysis (a floor in A
  means the conflict isn't the limiting factor there, as in the ticket rays).

## 6. Zoo (before any spend; extends each province's existing zoo)

- `prior_follower`: always emits the pattern/reality/comment answer →
  fails every B, unmoved by C and D, passes A.
- `evidence_follower` (perfect): passes all conditions.
- `attention_limited` (NEW, the D-validator): a scripted policy that uses the
  critical fact iff it is emphasized or restated → fails B, rescued by BOTH C
  and D. Validates that D can detect attention-style failures.
- `retrieval_limited`: uses the fact iff it appears in the final segment →
  fails B, rescued by C only. Validates the C/D asymmetry the analysis rests on.
Acceptance: each fake produces exactly its predicted rescue pattern in every
family where it runs; the existing per-province zoos still pass untouched.

## 7. Deliverables and acceptance

- `cartographer/campaigns/evidence_fidelity.py`: the paired condition runner
  (one seed → up to 4 suffixed records), per-family configs, pooled
  statistics; registry entries `EF.belief`, `EF.tools`, `EF.tickets`,
  `EF.comments`, `EF.repair` (+ `EF.transfer` for H-EF4 arms) as
  `CampaignSpec` rows with `evidence_kind="battery"`, tiers via `apply_tier`,
  legacy-alias-free.
- The 9 mutators with answer-key-preservation tests; `realized_dims` gains
  `evidence_span`, `evidence_turns`, `condition`.
- `report/frontier.py` row family `evidence_fidelity` and a report view: a
  province × condition matrix of signature rates with CIs (the campaign's
  one-figure output; also the paper-2 centerpiece).
- Acceptance: §6 zoo separations; byte-identical legacy behavior with all new
  dims off (regression-tested); `run_campaign.py --list` shows `EF.*`;
  `frontier_summary` renders the rows NOT TESTED without error; full suite
  green.

## 8. What this campaign does NOT do (scope fence)

No new provinces. No "missing-information / clarification" province (roadmap).
No long-context retention sweeps (the filler null stands). No irreversible-
action safety rung. No external retrieval. No protocol/rendering changes. No
V.write. Each of these is its own future spec if ever.

## 9. Live plan (approval-gated, post-2026-10-30)

1. Gemma 4 QAT on Modal (text protocol): full grid at Standard tier,
   ~20 seeds × 4 conditions × 5 families ≈ 400 episodes ≈ 3–4 L40S GPU-hours.
   Short families (I, II, IV are single-shot) dominate the count but not the
   time; III/V episodes dominate the time.
- 2. Nano + Super on Nebius (native protocol): same grid, the cross-model arm
   (~$1.5–2.5 tracked). Ultra joins for I/III only after its deferred
   interactive re-run (the V-E cap fix has still never been exercised on it).
3. Fresh-seed confirmation + certificates on any headline cell.
4. Report: `docs/reports/evidence-fidelity-<label>-<date>.md` + the matrix
   view; lab-log entry; this is also the sequel paper's experimental core.

## 10. Risks, ranked

1. **Condition A construction.** "Prior agrees" must not make tasks trivially
   easier for other reasons (e.g., `ticket_guessable` shortens the effective
   memory demand to zero). Mitigation: A is a sanity anchor and the §5 control,
   never evidence for a mechanism; difficulty covariates (`evidence_span`,
   realized dims) are reported alongside.
2. **IV's missing C cell.** Any restatement of the true value leaks the
   answer; the grid is honestly incomplete there and the analysis treats IV as
   the in-view anchor (A/B/D only).
3. **Serving nondeterminism (V-E).** All pairing on Modal carries resample
   CIs; the cross-model arm on Nebius (deterministic in X.consistency) is the
   primary evidence for paired effects; Modal results are floor-plus-rates.
4. **Belief's dual nature.** If I-belief shows C-rescue but also D-rescue, or
   neither, the family assignment in §2 changes; the analysis plan allows
   re-binning ONLY in the exploratory section.
5. **Multiple comparisons.** 5 families × 3 contrasts × 2 response variables
   is 30 tests; Holm within family, and the paper reports the full matrix, not
   cherry-picked cells.

## 11. Build plan (post-Oct 30; estimates assume the VI build pace)

| WP | scope | est. |
|---|---|---|
| A | 9 mutators + preservation tests + realized covariates | 1.5 d |
| B | paired runner + registry + tiers + report matrix | 1 d |
| C | zoo (4 fakes × families) + acceptance | 1 d |
| D | live grid + confirmation + report | 1 d live-sitting |

Total ≈ 3.5 build days + one live day — a week of the other session's time,
after the hackathon ships.
