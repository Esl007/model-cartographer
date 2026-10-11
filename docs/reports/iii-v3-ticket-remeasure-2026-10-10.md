# Province III protocol v3: ticket re-measure and belief power run (Gemma 4 QAT, 2026-10-10)

Spec: `docs/spec-iii-protocol-v3-and-belief-power.md`. Model `gemma-4-31b-it-qat-w4a16-ct` on Modal (one L40S, vLLM), text tool protocol. One session: 43.5 min including a 374 s cold start, about $1.30 of credit, $0 billed. The app was stopped by id and confirmed stopped through the JSON listing; the watchdog confirmed it too. No LLM judged anything.

## The v2 / v3 contrast

Protocol v2 executes every call a text message contains. Protocol v3 (Province VI's turn discipline, now available to Province III, opt-in) stops generation where a second call or a self-written result begins, executes one call per turn, and executes the complete call of a turn that hit the token cap.

| run | protocol | scored episodes | pass | episodes with an invalid ticket | median turns | longest plan tested |
|---|---|---|---|---|---|---|
| `camp-interactive-gemma4qat-001` | v2 | 56 | 25 | 40 | 2 | plan_depth 9 |
| `camp-interactive-gemma4qat-002` (fresh seeds) | v2 | 55 | 26 | 44 | 2 | 9 |
| `camp-interactive-fault-gemma4qat-001` | v2 | 56 | 26 | 43 | 2 | 9 |
| `camp-interactive-fault-gemma4qat-002` (fresh seeds) | v2 | 55 | 24 | 43 | 2 | 9 |
| **`camp-interactive-gemma4qat-v3-001`** | **v3** | **33** | **33** | **0** | **12** | **16 (the ray's end)** |
| **`camp-interactive-fault-gemma4qat-v3-001`** | **v3** | **31** | **31** | **0** | **15** | **16 (the ray's end)** |
| `camp-ef-tickets-gemma4qat-001` (EF A–X) | v2 | 50 | 8 | 50 had a fabricated or invalid ticket | 2 | 8 |
| **`camp-ef-tickets-gemma4qat-002` (EF A–X)** | **v3** | **50** | **50** | **0** | **11** | **8** |

- **Under v2, Gemma wrote the whole plan in its first message**: 11 calls written in the first turn of every EF ticket episode, including the ticket it had not been given yet (`ticket_flag_0_12345`). Those calls then executed blind.
- **Under v3 (one call per turn), it copies every ticket.** Every episode in every condition passes, including EF condition B (opaque ticket, carried 3 calls) and both default rays to their generator limit, where v2 broke at about 8-call plans (t* ≈ 0.22–0.27). The resample gate was on and had nothing to resample.
- **EF condition A clears its ≥ 0.8 gate (10/10),** so the v3 ticket arm is interpretable. It shows no conflict cost at all: A = B = C = D = X = 10/10.

## Decision (pre-registered in the spec, §Task 2.3)

**Fabrication collapses under v3 → F-G4Q-1 is retracted as stated** (X10 in the findings register). "Gemma 4 fabricates a pattern-plausible ticket instead of copying the opaque one returned earlier" was a property of the protocol, not the model. Gemma wrote ticket values before the producer had returned them, because the v2 harness let it write and execute a whole trajectory in one message. The 6/6 `ticket_restatement` rescue falls with it: it was measured under the same artefact.

**What survives is blind-trajectory planning.** When the harness accepts several text calls per message, Gemma 4 QAT writes the entire plan, imagined results included, before seeing one real result. Under v2, 435 of 531 earlier Gemma 4 Province III records have ≤ 2 turns. It is a real behaviour with real consequences for any agent harness that executes multiple text calls per message. But it is a planning or protocol behaviour, not copy-fidelity. Gemma 3's Province III runs (also text protocol, 0 invalid tickets) should get the same turn-count check before any ticket claim about them is reused.

## Belief power run (Task 3): retrieval, not attention, decided

`camp-ef-belief-gemma4qat-001`, now 30 paired seeds (20 new), resample gate on:

| arm | pass | `final_true_state` rate |
|---|---|---|
| A agree (the person saw everything) | 30/30 | 0.00 |
| B conflict | 12/30 (gated; 11/30 ungated) | 0.53 |
| C the belief-forming sentences restated before the question | 27/30 | 0.10 |
| D the same sentences flagged in place | 15/30 | 0.47 |

Pre-registered contrasts (exact McNemar on pass, Wilcoxon on the signature, Holm within the family):

| contrast | pairs | b / c | McNemar p (Holm) | signature Δ | Wilcoxon p (Holm) |
|---|---|---|---|---|---|
| A vs B | 30 | 0 / 19 | 4e-06 (2.4e-05) | −0.53 | 3.1e-05 (1.6e-04) |
| **C vs B** | 30 | 0 / 16 | **3.1e-05 (1.6e-04)** | −0.43 | 9.8e-04 (2.9e-03) |
| **D vs B** | 30 | 3 / 7 | **0.34 (0.69)** | −0.07 | 0.73 (0.73) |

**Restating what the person saw rescues 16 of the 18 conflict failures; flagging the same sentences in place does not help significantly.** For Gemma 4 the belief failure is a retrieval failure, a fact carried across the story, not an attention failure. This is the H-EF2 / H-EF3 pattern, now significant. Every remaining failure in B and D names the object's real location (`final_true_state`).

## Caveats

- **v3 changes two things at once relative to v2:** turn boundaries, and no 2× retry of a capped turn (the retry overflowed the 32k context). The retry rarely fired under v2 (median 2 turns), so the contrast is the turn discipline.
- **Rays reach plan_depth 16 under v3, against 9 under v2:** v2 broke early. "No break to 16" is a generator limit, not a measured ceiling.
- **Serving nondeterminism on this endpoint** (lab log 2026-10-06 E) does not threaten a 0/64-versus-~44/55 contrast.
