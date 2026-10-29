# YouTube upload

**File:** `submission/model-cartographer-demo.mp4` (2:10, 1080p)
**Captions:** `submission/model-cartographer-demo.en.srt` (Subtitles → Upload file → With timing)
**Visibility:** Public (the rules require a public YouTube video)
**Audience:** No, it's not made for kids
**Category:** Science & Technology

## Title

Model Cartographer: where does your model break? (NVIDIA Nemotron 3 on Nebius Token Factory)

## Description

Benchmarks give a model one score. Model Cartographer finds where a model breaks, and why.

Every task is generated from a simulated world, so the right answer comes from running that world: no LLM judge. Difficulty goes up along real dials (plan length, look-alike tools, distractors, hidden tests) until the model stops passing, and the result is reported in the task's own units.

Where NVIDIA Nemotron 3 runs, all through Nebius Token Factory:
• Ask the Cartographer (live): Nemotron 3 Nano routes your question with thinking off; Nemotron 3 Super answers from facts in the results store, and a deterministic audit checks every number
• Why did it fail? (live): Nemotron 3 Ultra explains any failed trial from the prompt and the verified answer
• Capability report: Nemotron 3 Super writes it; every figure is audited
• Campaigns: Nemotron 3 Nano, Super and Ultra are the models mapped, 4,565 trials for $5.86

Live demo: https://model-cartographer.vercel.app
Code (MIT): https://github.com/Esl007/model-cartographer

Chapters
0:00 Intro
0:03 The capability map
0:21 Ground truth from simulation, no LLM judge
0:34 Nemotron 3 Nano, Super, Ultra on Nebius Token Factory
0:51 Breaking points
1:06 Live: Nemotron 3 Nano + Super answer questions
1:30 Live: Nemotron 3 Ultra explains a failed trial
1:50 Nemotron 3 Super writes the report
1:59 Open source and live

Built for the Nebius × NVIDIA Global AI Hackathon, Best Apps and Agents track. Narration is synthetic (text to speech).

## Tags

Nebius, Token Factory, NVIDIA, Nemotron, Nemotron 3, LLM evaluation, AI agents, capability evaluation, tool calling, open source
