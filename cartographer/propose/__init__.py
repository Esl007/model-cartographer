"""Adversarial proposer package: an LLM (Nemotron or otherwise) proposes
generator CONFIGS (questions) it believes a subject model will fail.
Ground truth always stays simulation-only (`cartographer.world.simulate`) --
the proposer never supplies an answer, only a hypothesis about which corner
of the config space is hard. See `docs/lab-log.md` for the design writeup.
"""
