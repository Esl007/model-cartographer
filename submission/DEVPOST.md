# Devpost submission text

**Project name:** Model Cartographer
**Tagline:** Find where your model breaks, and why: tasks from simulated worlds, no LLM judge, and NVIDIA Nemotron explaining the results live.
**Track:** Best Apps and Agents
**Demo URL:** https://model-cartographer.vercel.app
**Repository:** https://github.com/Esl007/model-cartographer
**Video:** https://youtu.be/eTcULrzXMpk
**New or existing:** New. The first commit is dated Sep 16, 2026, inside the submission period.

---

## Inspiration

Choosing a model for an agent comes down to one question that leaderboards don't answer: *where does it stop working?* A single score hides that a model can be excellent at reading code and poor at fixing it, or fine at 12-step tool plans and broken at 24. We wanted a map of each model's limits, measured against ground truth that can't be argued with, and an assistant that explains what the map says.

## What it does

Model Cartographer generates tasks from simulated worlds across six provinces of skill: tracking a story's world state and beliefs, planning tool calls, executing tools turn by turn, tracing code, repairing code, and fixing bugs in multi-file repositories. Because each task comes from a world the app can run, the right answer is computed, never judged by another model. An adaptive scheduler turns one difficulty dial at a time (plan length, look-alike tool names, distractors, hidden tests) until the model stops passing, then reports the breaking point in the task's own units.

In the web app you can:

- **Map:** see one globe per model, sized by its average across provinces, with a flame for each province.
- **Ask the Cartographer (live):** ask a question in plain words. Nemotron 3 Nano routes it, the app pulls the matching facts from 9,472 trials, Nemotron 3 Super answers from those facts only, and a deterministic audit checks every number it used.
- **Breaking points:** read each model's verdict ("breaks at 24-call plans with look-alike tool names").
- **Findings:** open any weakness, see the trials behind it, and open any trial's full prompt and answer.
- **Why did it fail? (live):** on any failed trial, Nemotron 3 Ultra explains the mistake from the prompt and the verified answer, next to the verifier's own classification.
- **Atlas, Runs, Report, Coverage:** compare models on the same difficulty buckets, inspect every run with resample verification, read the audited analyst report, and see what is not tested yet.

## How we built it: NVIDIA Nemotron on Nebius Token Factory

Every Nemotron call goes through Nebius Token Factory's OpenAI-compatible API, and each Nemotron 3 tier does the job it is best at:

- **Nemotron 3 Nano (thinking off, about 1 s)** routes questions in the live agent, choosing models and capabilities only from lists the app supplies.
- **Nemotron 3 Super (thinking on, about 10 s)** writes grounded answers that cite fact ids, and writes the capability report. Both are audited against the results store, and unsupported numbers are flagged on screen.
- **Nemotron 3 Ultra** handles the hard reasoning: explaining why a specific failed trial failed.
- **All three tiers are also the models being mapped:** 4,565 trials across Nano, Super and Ultra for $5.86, on identical tasks, so their breaking points compare directly. Nemotron 3 Super breaks at 24-call plans with look-alike tool names, Nano at 13, and Ultra only slips at 25.

Where Token Factory sped us up: three model sizes behind one key and endpoint made the tier-by-tier comparison a one-line provider entry; the price let us afford adaptive campaigns plus automatic three-times re-runs of every failure; and per-call thinking control (`chat_template_kwargs.enable_thinking`) let the live agent spend reasoning tokens only where they pay off.

The rest is deliberately simple: a Python standard-library engine (task families, adaptive scheduler with logistic frontier fits, resample verification, failure analysis), an append-only SQLite results store, and a stdlib web viewer. The hosted demo runs the same viewer on Vercel's Python runtime with a snapshot of the store, plus the live Nemotron agent (rate-limited per visitor, capped per day). 1,229 tests.

## Challenges we ran into

- **Reasoning budgets.** Nemotron 3 Ultra can spend 10,000–16,000 tokens reasoning before it answers; at our first token limits every trial in one campaign came back truncated and empty. We added per-model budgets and an automatic retry at a larger limit.
- **Noise at temperature 0.** Re-running the same failed prompt on the large mixture-of-experts models often passed. We built an automatic resample gate so a failure only counts as a weakness if it survives re-runs.
- **Our own instrument bugs.** A witness-rendering bug once made belief failures look worse than they were. Every finding now links to transcripts, and several early findings were retracted on the record when a protocol change showed they were artifacts.
- **Honest numbers in the UI.** Different pages once counted trials and pass rates differently. Every page now uses one definition, and the app says when a number covers only some provinces.

## Accomplishments we're proud of

- Ground truth from simulation across six provinces, with no LLM judge anywhere in scoring.
- A live Nemotron agent whose answers are audited number by number against the evidence.
- A real finding: Gemma 4 31B QAT predicts what code will do 97% of the time but fixes injected bugs only 33% of the time, sometimes by fudging a constant so the visible test passes.
- Every number on screen traces down to individual trials and their full transcripts.

## What we learned

Model size is not the whole story: on long tool plans with look-alike names, the three Nemotron 3 tiers break at clearly different points (13, 24, and only a slip at 25). Thinking budgets, routing between tiers, and auditing a model's numbers matter as much in a product as raw accuracy does.

## What's next

- Run campaigns as Nebius Serverless Jobs so anyone can map a new model from the hosted site.
- Add provinces we don't cover yet: instruction following, numeric reasoning, long-context retrieval.
- A "capability diff" in CI that maps a model or prompt change before it ships.

## Built with

Nebius Token Factory, NVIDIA Nemotron 3 (Nano 30B, Super 120B, Ultra 550B), Python standard library, SQLite, Vercel
