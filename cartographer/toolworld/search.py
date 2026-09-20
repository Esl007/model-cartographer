"""Adversarial seed search for the tool-planning province.

Pick the generated tool-world, among many candidate seeds at a fixed
`ToolDimParams`, that plants the most salient trap(s) for a shallow planner
(see `foils.py`). Pure simulation, deterministic, zero API calls -- mirrors
`world/search.py`.
"""

from __future__ import annotations

import hashlib

from .foils import ToolFoilProfile, foil_divergence
from .generate import ToolDimParams, generate
from .simulate import validate


def _derive_seed(base_seed: int, index: int) -> int:
    payload = f"tool_adversarial_seed|{base_seed}|{index}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:15], 16)


def search_adversarial_seeds(
    params: ToolDimParams,
    base_seed: int,
    n_candidates: int = 40,
    top_k: int = 1,
    require_kinds: tuple[str, ...] = (),
) -> list[tuple[int, ToolFoilProfile]]:
    """Generate `n_candidates` deterministically-derived seeds from
    `base_seed`, build+validate the tool-world at each, score by
    `ToolFoilProfile.divergence_score`, and return the `top_k` highest scoring
    (seed, profile) pairs, sorted descending by score.

    Worlds failing `validate()` are skipped. When `require_kinds` is non-empty,
    only candidates whose `trap_kinds` is a superset of `require_kinds` are
    eligible. Fully deterministic in its inputs."""
    scored: list[tuple[int, ToolFoilProfile]] = []
    for i in range(n_candidates):
        seed = _derive_seed(base_seed, i)
        world = generate(params, seed)
        if validate(world):
            continue
        profile = foil_divergence(world)
        if require_kinds and not set(require_kinds).issubset(profile.trap_kinds):
            continue
        scored.append((seed, profile))

    scored.sort(key=lambda item: item[1].divergence_score, reverse=True)
    return scored[:top_k]
