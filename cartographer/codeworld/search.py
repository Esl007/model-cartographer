"""Adversarial seed search for the code-tracing province: among many
candidate seeds at fixed `CodeDimParams`, pick the program that plants the
most salient traps for a shallow reader (see `foils.py`). Pure simulation,
deterministic, zero API calls -- mirrors `toolworld/search.py`."""

from __future__ import annotations

import hashlib

from .foils import CodeFoilProfile, foil_divergence
from .generate import GenerationFailed, generate
from .model import CodeDimParams


def _derive_seed(base_seed: int, index: int) -> int:
    payload = f"code_adversarial_seed|{base_seed}|{index}".encode("utf-8")
    return int(hashlib.sha256(payload).hexdigest()[:15], 16)


def search_adversarial_seeds(
    params: CodeDimParams,
    base_seed: int,
    n_candidates: int = 40,
    top_k: int = 1,
    require_kinds: tuple[str, ...] = (),
) -> list[tuple[int, CodeFoilProfile]]:
    """Score `n_candidates` derived seeds by `divergence_score`; return the
    `top_k` best (seed, profile) pairs, descending. With `require_kinds`,
    only programs whose diverging foils include all of them are eligible."""
    scored: list[tuple[int, CodeFoilProfile]] = []
    for i in range(n_candidates):
        seed = _derive_seed(base_seed, i)
        try:
            program = generate(params, seed)
        except GenerationFailed:
            continue
        profile = foil_divergence(program)
        if require_kinds and not set(require_kinds).issubset(profile.trap_kinds):
            continue
        scored.append((seed, profile))
    scored.sort(key=lambda item: item[1].divergence_score, reverse=True)
    return scored[:top_k]
