# Feedback on Nebius and NVIDIA tools

Paste into the feedback field of the submission form. Every point comes from the project's lab log (`docs/lab-log.md`), where the run ids and numbers can be checked.

---

**What we used:** Nebius Token Factory (OpenAI-compatible chat completions) to serve NVIDIA Nemotron 3 Nano 30B, Super 120B and Ultra 550B as the models under test (4,565 trials, $5.86 total), plus Nemotron 3 Super as an analyst that writes an audited capability report, and Nemotron as a renderer and a trap proposer.

**What worked well**

1. **Price and compatibility.** 4,565 Nemotron trials across three model sizes cost $5.86. The API was a drop-in for our existing OpenAI-style client; adding Nebius was one provider entry.
2. **All three tiers in one place.** Having Nano, Super and Ultra on one endpoint let us run the identical task stream against each size and compare breaking points directly. That comparison is the core of our app.
3. **Per-call thinking control.** `chat_template_kwargs: {"enable_thinking": false}` works on Token Factory for Nemotron 3: Nano routed a question in about 1 second with 34 output tokens instead of 3.3 seconds and 323 tokens with thinking on. Our live agent uses Nano without thinking for routing and Super with thinking for the answer. This deserves a prominent place in the docs; we found it by trial.
4. **Ultra is strong.** Nemotron 3 Ultra passed 97% of adversarial belief-tracking tasks (stories built to plant the wrong answer) and only slipped at 25-call tool plans with look-alike tool names, where Super breaks at 24 and Nano at 13.

**Friction, with suggestions**

1. **Reasoning budget is invisible until it fails.** Nemotron 3 Ultra's reasoning trace alone can use 10,000–16,000 tokens before any answer. With our default `max_tokens` of 4,000, every trial of one breaking-point campaign came back `finish_reason=length` with an empty answer, and an 8,000/16,000 retry still truncated. Raising `max_tokens` to 28,000 made all of them finish. *Suggestion:* publish a recommended `max_tokens` per Nemotron model in the model card, and/or expose a reasoning-budget parameter so callers can cap the trace instead of guessing.
2. **Temperature 0 is not deterministic on the MoE models.** Re-running the identical prompt at temperature 0 on Super and Ultra often flipped a failure to a pass (for example, 4 of 5 resamples passed on one Ultra failure), which we attribute to expert routing. Our measured noise on single trials was large enough that we built an automatic resample gate before reporting any weakness. *Suggestion:* document this, and offer a `seed` or deterministic-serving option for evaluation workloads.
3. **Rate limits are per account but not visible.** Three campaigns at 6 concurrent requests each hit 429s; Nano's campaign spent about two-thirds of its 82 minutes in backoff. `Retry-After` was honored and nothing was lost, but we had to infer the shared ceiling. *Suggestion:* return the limit and remaining quota in response headers, and list per-model concurrency limits in the console.
4. **Unsupported parameters are dropped silently.** For `google/gemma-3-27b-it`, Token Factory silently ignored the `tools` field instead of returning an error, which hid a bug in our text-only fallback until we moved the same model to another server that returned a 400. *Suggestion:* reject unsupported parameters with a clear 400, or return a warning header.
5. **Model ids use inconsistent casing.** `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`, `nvidia/nemotron-3-super-120b-a12b` and `nvidia/Nemotron-3-Ultra-550b-a55b` follow three different patterns, which broke simple per-model configuration matching. *Suggestion:* one naming scheme, or case-insensitive ids.

**NVIDIA Nemotron as a worker, not just a subject**

- As the **analyst**, Nemotron 3 Super wrote a useful diagnosis from a structured evidence pack, but quoted some figures that were not in the pack. A deterministic audit catches these, and the report flags them. *Suggestion:* a structured-output or citation mode that ties each number to an input field would make Nemotron much easier to trust in reporting roles.
- As a **trap proposer** (choosing hard configurations for Nano), Super found one confirmed failure at a 75% fail rate, but its edge over random configurations was narrow over 40 trials.
- As a **renderer** (rewriting stories in natural language), Nemotron 3.5 Lightning's output passed our round-trip fidelity gate most of the time (34 passes and 1 failure on scored trials, with 11 renders rejected by the gate).
