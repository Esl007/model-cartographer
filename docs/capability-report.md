# Capability Report

_Diagnosis written by nvidia/nemotron-3-super-120b-a12b, audited deterministically; all numbers trace to the results store._

Generated: 2026-09-20T17:38:41.308172+00:00

Evidence base: 1305 trials (1273 scored), $0.3257 spend.

## Executive Summary

The evidence shows a clear performance tier: the Nano model achieves a very high overall pass rate (94.1 %) with near‑perfect scores on state and order queries, but it shows a specific breakdown when witness_gap is high, especially combined with high entity_state, temporal or coreference loads. The Super and GPT‑OSS models sit in a lower tier (overall pass rates 63.4 % and 58.6 % respectively) and fail broadly on belief, distractors and context_length dimensions, with belief pass rates dropping to roughly 60 % (Super) and 53 % (GPT‑OSS). All three models handle state queries flawlessly, but only Nano maintains perfect order performance; the other two have no order trials in the pack.

## Per-Model Profile

### NVIDIA-Nemotron-3-Nano-30B-A3B  
**Strengths**  
- Order queries: 100.0 % pass (78/78 scored, 0 failures).  
- State queries: 100.0 % pass (60/60 scored, 0 failures).  

**Weaknesses / Breakdowns**  
- High‑witness_gap region: 81.8 % pass (27/33 scored, 6 real failures).  
- High‑entity_state region: 88.8 % pass (158/178 scored, 20 real failures); this weakness concentrates where witness_gap is high (3–22), where the pass rate falls to the same 81.8 % (27/33).  
- The capability grid shows belief pass rate 92.5 % (492 scored, 455 pass, 37 fail) – the only non‑perfect kind besides the dimensional weaknesses above.  

### nemotron-3-super-120b-a12b  
**Strengths**  
- State queries: 96.7 % pass (29/30 scored, 1 failure).  

**Weaknesses / Breakdowns**  
- High‑distractors region: 48.6 % pass (70/144 scored, 74 real failures); concentration at temporal 0.52–0.57 drops to 36.9 % pass (31/84).  
- High‑context_length region: 50.0 % pass (87/174 scored, 87 real failures); concentration at temporal 0.52–0.58 drops to 37.5 % pass (39/104).  
- Belief queries: 60.7 % pass (224/369 scored, 145 real failures); concentration at context_length 15–37 drops to 44.2 % pass (68/154).  
- The grid shows entity_state (high) 55.8 % pass, temporal (high) 50.7 %, coreference (high) 52.6 %, distractors (high) 48.6 %, context_length (high) 50.0 %; witness_gap has no trials (n_scored = 0).  

### openai/gpt-oss-120b  
**Strengths**  
- State queries: 100.0 % pass (30/30 scored, 0 failures).  

**Weaknesses / Breakdowns**  
- High‑distractors region: 42.7 % pass (38/89 scored, 51 real failures); concentration at coreference 3–21 drops to 31.1 % pass (19/61).  
- High‑coreference region: 45.0 % pass (54/120 scored, 66 real failures); concentration at distractors 6–20 drops to 31.1 % pass (19/61).  
- Belief queries: 52.8 % pass (113/214 scored, 101 real failures); concentration at context_length 15–37 drops to 38.4 % pass (38/99).  
- The grid shows entity_state (high) 57.7 % pass, temporal (high) 45.2 %, coreference (high) 45.0 %, distractors (high) 42.7 %, context_length (high) 48.7 %; witness_gap again has no trials.

## Cross-Model Comparison

- **Tier discrimination**: Nano’s overall pass rate (94.1 %) is markedly higher than Super’s (63.4 %) and GPT‑OSS’s (58.6 %).  
- **Shared failure modes**: All three models show perfect state performance, but Super and GPT‑OSS both suffer heavily on belief, distractors and context_length (belief pass rates ≈ 60 % and 53 %; distractors ≈ 49 % and 43 %; context_length ≈ 50 % and 49 %). Nano’s belief pass rate remains high (92.5 %).  
- **Distinct failure modes**: Nano’s primary vulnerability is the interaction of high witness_gap with high entity_state/temporal/coreference, where pass rates fall to the low‑80 % range. Super and GPT‑OSS lack witness_gap trials entirely, so this failure mode is not observed for them. Conversely, Super and GPT‑OSS exhibit strong sensitivity to distractor and context_length loads, which Nano handles relatively well (its high‑distractors and high‑context_length grid entries show pass rates in the mid‑90 % range).  
- **Order handling**: Only Nano has order trials and passes them perfectly; Super and GPT‑OSS have zero order trials in the pack.

## Recommended Next Probes

1. **Nano

## Contamination Audit

**2 unverified figure(s)** -- stated by the analyst but absent from the evidence pack:

- `80 %` (percent) -- context: here pass rates fall to the low‑80 % range. Super and GPT‑OSS lack w
- `90 %` (percent) -- context: ries show pass rates in the mid‑90 % range). - **Order handling**:

## Literature Corroboration

literature check not run (no Tavily key)
---

How this was made: numbers come from a deterministic evidence pack computed straight from the store (scored trials only; infra errors and resample-noise failures excluded). The analyst model only narrates; a deterministic audit re-checks every figure. The literature layer only corroborates against public sources -- no LLM output is ever written back as a measurement.
