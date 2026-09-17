"""Phase 4: LLM natural-language rendering behind a mandatory fidelity gate."""

from .extract import EXTRACT_PROMPT_TEMPLATE, extract_facts, extract_facts_tokens
from .facts import facts_match, world_facts
from .gate import GateResult, gated_render, record_gate_rejection
from .renderer import RENDER_PROMPT_TEMPLATE, assemble_prompt, render_llm, render_llm_tokens

__all__ = [
    "world_facts",
    "facts_match",
    "RENDER_PROMPT_TEMPLATE",
    "render_llm",
    "render_llm_tokens",
    "assemble_prompt",
    "EXTRACT_PROMPT_TEMPLATE",
    "extract_facts",
    "extract_facts_tokens",
    "GateResult",
    "gated_render",
    "record_gate_rejection",
]
