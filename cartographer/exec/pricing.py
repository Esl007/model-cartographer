"""Static USD-per-million-token pricing for pilot models.

Prices are (usd_per_1m_input, usd_per_1m_output). Unknown models cost $0.
"""

from __future__ import annotations

PRICE_TODO = "TODO: fill in real Nemotron pricing once available"

PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-20b": (0.075, 0.30),
    "qwen/qwen3.8-27b": (0.20, 0.60),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "nvidia/nemotron-4-340b": (0.0, 0.0),
    "nvidia/nemotron-mini-4b": (0.0, 0.0),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    prices = PRICES.get(model)
    if prices is None:
        return 0.0
    price_in, price_out = prices
    return (tokens_in / 1_000_000.0) * price_in + (tokens_out / 1_000_000.0) * price_out
