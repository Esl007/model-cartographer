"""Static USD-per-million-token pricing for pilot models.

Prices are (usd_per_1m_input, usd_per_1m_output). Unknown models cost $0.
"""

from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-20b": (0.075, 0.30),
    "qwen/qwen3.8-27b": (0.20, 0.60),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.05, 0.20),
    "nvidia/nemotron-3-super-120b-a12b": (0.08, 0.45),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (0.50, 2.20),
    "nvidia/Nemotron-3_5-Lightning": (0.08, 0.20),
    # Nebius /models pricing, 2026-10-03
    "google/gemma-3-27b-it": (0.10, 0.30),
    # Self-hosted on Modal (scripts/modal_serve_gemma4.py) -- the real cost
    # is L40S time (~$2/h), not tokens. Priced as a gemma-3-27b proxy so
    # campaign cost caps still bind instead of the $0 unknown-model path.
    "gemma-4-31b-it-qat-w4a16-ct": (0.10, 0.30),
    # OpenRouter :free via Google AI Studio -- genuinely $0 per token, but
    # priced at the gemma-3-27b proxy anyway so cost caps bind the same
    # way as the Modal run it is the bf16 contrast for (comparable spend
    # ledgers; the tracked dollars are notional on both sides).
    "google/gemma-4-31b-it:free": (0.10, 0.30),
    # bf16 contrast arm, self-hosted on Modal A100-80GB (~$2.5/h real cost).
    "gemma-4-31b-it-bf16": (0.10, 0.30),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    prices = PRICES.get(model)
    if prices is None:
        return 0.0
    price_in, price_out = prices
    return (tokens_in / 1_000_000.0) * price_in + (tokens_out / 1_000_000.0) * price_out
