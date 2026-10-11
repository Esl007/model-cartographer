# Spec: Province III protocol v3 + ticket re-measure, and the belief power run

*2026-10-10. Priority: BLOCKS the Nov 12 paper's ticket section. Source:
`docs/reports/evidence-fidelity-gemma4qat-2026-10-10.md` §4 (the ≤2-turn
artefact) and §7 (what would make the grid informative). Parent rules:
`docs/spec-frontier-breaking-points.md` §0 — plus: never read an empty or
failed `modal app list` as "stopped" (use the fixed `run_vi_live.py` path),
warm to `/v1/models`=200 first, stop by app id in a `finally`.*

## Task 1 — Protocol v3 for Province III (offline first)

Backport Province VI's turn discipline to `interactive/episode.py`'s text
protocol, as a NEW protocol version (never a silent change):

1. **One call per turn**: execute only the first complete ` ```tool_code `
   block in a message; the turn ends where a second call begins (VI commits
   `ca25d44`/`c5b9a21` are the pattern). Extra calls are not executed.
2. **Stop at fabricated tool_output**: a model-written `tool_output` block ends
   the turn before it (the model is imagining results).
3. **Capped turn with a complete call executes that call** (VI `fc5c177`).
4. Stamp every record `protocol_version=3`; v2 and v3 Province III records are
   NON-COMPARABLE and `report_ef.py` / frontier rows must refuse to pool them.
5. Regression: Province III tests stay green; native-protocol path untouched;
   v3 off by default everywhere except the runs below.
6. Add a process measure `turns_total` and `calls_written_first_turn` to
   verifier_detail so blind-trajectory planning is measurable, not inferred.

## Task 2 — Re-measure the ticket findings under v3 (live, Modal, Gemma 4 QAT)

The claims at stake: F-G4Q-1 (fabricates instead of copying; was FS-replicated
under v2) and the 6/6 `ticket_restatement` rescue.

1. Rerun `III.values` and `III.fault` default rays (the F-G4Q-1 configs) under
   v3, fresh run ids `camp-interactive-gemma4qat-v3-001` /
   `camp-interactive-fault-gemma4qat-v3-001`, resample gate ON, standard tier.
2. Rerun the EF ticket arm (A/B/C/D, 10 paired seeds,
   `camp-ef-tickets-gemma4qat-002`) under v3. Condition A must clear the ≥0.8
   gate or the arm is again declared uninterpretable — do not tune past it.
3. Decision rule (pre-registered here):
   - If fabrication persists at B with A ≥ 0.8 → F-G4Q-1 stands, upgraded
     (artifact objection closed); lab-log entry says so.
   - If B's fabrication collapses under v3 → **retract F-G4Q-1 as stated** in
     the X register; the surviving finding is blind-trajectory planning
     (`calls_written_first_turn` ≈ plan length under v2), written up with the
     v2/v3 contrast as evidence. The 6/6 rescue is restated or retracted with it.
   - Either way: one lab-log entry + an addendum section in the EF report and
     the Province V/III findings docs; never edit the original claims in place.

## Task 3 — Belief power run (live, cheap, independent of Task 2's outcome)

The EF belief arm showed the exact H-EF2/H-EF3 signature (C rescues 4/4, D
rescues 1/4) at n=10, p=0.125. Decide it:

1. Extend `camp-ef-belief-gemma4qat` to **30 paired seeds** total across
   A/B/C/D (20 new seeds, same grid, same single-shot evaluator), resample
   gate ON for B-failures.
2. Pre-registered test (unchanged from the EF spec §5): McNemar exact B vs C
   and B vs D, Holm within family; signature = `final_true_state` rate.
3. Expected cost: ≈3–4 min GPU for the new seeds; decide C>D or not at
   p<0.05.

## Budget, order, reporting

Order: Task 1 offline (tests green) → one warm Modal session runs Tasks 2+3
back-to-back → stop by app id. Estimated 1.5–2 L40S GPU-hours ≈ $3–4 of the
~$6.7 October credit remaining; hard cap the session at 2.5 h.
Report: `docs/reports/iii-v3-ticket-remeasure-<date>.md` with the v2/v3
contrast table front and center, plus the belief 30-pair matrix. The paper's
ticket and belief sections are written from that report only.
