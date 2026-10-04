# Spec: Copy-Fidelity Breaking Point (ticket fabrication under prior pressure)

*Written 2026-10-04. Context: the Gemma 4 QAT full map (lab-log 2026-10-04) found a
clean Province III break — value passing collapses at 8-call episodes (t\*=0.22),
61/61 failing episodes carry invalid tickets, and the spot-checked failure is a
FABRICATED, pattern-plausible ticket (`ticket_depth_flag_1_12345`) written in place
of the opaque sha256 the model received two steps earlier. Hypothesized mechanism,
shared with Nano's `set_dep_th_flag_4` misspelling and the belief→reality collapse:
**a salient prior overrides an observation the model demonstrably received.**
This spec turns that anecdote into a measured boundary with causal attribution.*

## Goal

Map *when* the model substitutes a prior-shaped value for an observed literal, as a
function of controllable world dimensions — and causally attribute which dimension
drives it. Deliverable: a boundary in real units + a Holm-Bonferroni-confirmed
factor attribution + (optional) a cross-province replication in codeworld.

## Decision already taken (do not relitigate)

Latent worlds first, codeworld second. The toolworld/interactive generator gets new
dims and mutators; the code province gets ONE new trap family at most. No raw
length-ceiling extension anywhere — difficulty is structure, not magnitude.

## Part A — new DimParams on the interactive province (`cartographer/interactive/`)

All dims must be answer-key-preserving where used as ablations (the verifier's BFS
ground truth and ticket checking are untouched; only surface/structure of the world
moves). Target model for the campaign: `gemma-4-31b-it-qat-w4a16-ct` via
`--provider modal` (and the bf16 contrast via `--provider openrouter`,
model `google/gemma-4-31b-it:free`, text protocol on both).

1. **`ticket_entropy`** (the headline dim): interpolate the ticket's FORM from fully
   opaque (current sha256 hex) to pattern-suggestive (`ticket_<flag>_<n>`-shaped
   strings that LOOK derivable from context but are not — the true value is still
   random). Prediction: fabrication rate RISES as tickets look more derivable;
   if fabrication is insensitive to this, the mechanism is not prior salience and
   the hypothesis is wrong. This is the dim that makes the claim falsifiable.
2. **`carry_distance`**: steps between the call that returns a ticket and the call
   that consumes it (currently incidental; make it a controlled dim, 1→10).
3. **`n_live_tickets`**: how many tickets must be held simultaneously (1→6).
4. **`ticket_length`**: 8 → 64 hex chars (checks the boring "it's just long" rival
   explanation; run this one BEFORE believing any salience story).
5. **`decoy_tickets`**: tool results additionally contain labelled, plausible but
   wrong ticket-like strings ("previous ticket", "example"). The direct analog of
   foil-salience search — a trap where the prior has a concrete wrong answer to
   grab. Expected to be the strongest single factor.

## Part B — ablation catalog additions (`cartographer/ablate/`)

Answer-key-preserving mutations applied to confirmed FAILs, resample-gated first:

- **`ticket_restatement`**: the consuming step's preceding observation restates the
  needed ticket verbatim (carry_distance → 0 without changing the plan). If this
  rescues, the failure is retrieval-from-history, not copying per se.
- **`copy_instruction`**: one system-prompt line: "ticket values are opaque; always
  copy them exactly, never construct them." Cheap, and if THIS alone rescues, the
  finding downgrades from capability to elicitation — important either way, and it
  doubles as a competition-relevant agent-config fix.
- **`opaque_rename`**: rename tools/flags so no naming pattern suggests a ticket
  format (removes the material the prior builds from).

## Part C — escalation protocol

Use the existing composite-ray machinery (`schedule/escalate.py`): ramp →
bisect → logistic t\* → decompose (singles, then pairs) → fresh-seed confirm
(`cartographer/confirm/`, ≥10k convention). Budget ~$0.75 cap per ray on Modal
pricing proxies. Rays, in order:

1. `ticket_entropy × carry_distance` (the mechanism ray)
2. `decoy_tickets × n_live_tickets` (the trap ray)
3. `ticket_length` alone (the rival-explanation control)

**Gate every reported FAIL through the resample gate (3×, temp 0) before it enters
decomposition** — the interactive province currently bypasses the gate (known gap,
findings §5.4); close that as part of this work.

## Part D — codeworld replication (small, optional, high value)

One new trap family in `cartographer/codeworld/`: an opaque hex literal assigned
early must propagate through assignments/concatenations to the traced final value,
with a pattern-plausible FOIL variant reachable by "completing" the surrounding
naming scheme. Zero API cost to build (interpreter is ground truth). If the same
fabrication appears in code tracing, the mechanism is cross-domain and the paper
claim strengthens; if it doesn't (like look-alike variable names didn't, H1), the
mechanism is specific to tool-result copying — equally publishable, mirrors G8.

## Acceptance criteria

- Boundary stated in real units (e.g. "fabricates above N live tickets at carry
  distance ≥ K when decoys present"), grade A: n ≥ 20 at boundary + significance.
- Factor attribution via decompose + Holm-Bonferroni on fresh seeds.
- bf16-vs-QAT contrast on ray 1 (same seeds both providers): states whether
  quantization amplifies fabrication. OpenRouter free tier is slow (~20 req/min);
  schedule it as the background arm, Modal as the fast arm.
- Every claim either reproduced (RS/FS/IM) or explicitly marked single-run.

## Guardrails (standing rules — copy into any subagent prompt)

- NEVER `git stash` / `git checkout` / `git restore` in this repo; never track or
  touch `results/campaign.sqlite` in git (Sep 20 incident).
- No LLM in the verification path. Ground truth from simulation/BFS only.
- `max_tokens` never below observed p99 of clean completions; audit
  `finish_reason=length` before reporting any escalation (X1).
- Warm the Modal endpoint to `/v1/models` = 200 before any campaign; stop the app
  (`modal app stop gemma4-qat --yes`) when the campaign ends.
- Scope: this spec only. New dims/provinces beyond it need explicit user approval.
