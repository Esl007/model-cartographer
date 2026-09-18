"""Adversarial seed search: pick the generated world, among many candidate
seeds at a fixed `DimParams`, that plants the most salient trap(s) for a
shallow reasoner (see `foils.py`).

Pure simulation, deterministic, zero API calls -- generating and scoring a
candidate world costs nothing but CPU, so this search is free to run before
ever spending a token on a model call.
"""

from __future__ import annotations

import hashlib

from .foils import FoilProfile, foil_divergence
from .generate import DimParams, generate
from .simulate import validate


def _derive_seed(base_seed: int, index: int) -> int:
    """Deterministic sha256-stream derivation of the `index`-th candidate
    seed from `base_seed` -- same idiom as `world/generate.py`'s
    `_stable_int` (stable string parts hashed, never the `random` module),
    so `search_adversarial_seeds` is a pure function of its inputs."""
    payload = f"adversarial_seed|{base_seed}|{index}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:15], 16)


def search_adversarial_seeds(
    params: DimParams,
    base_seed: int,
    n_candidates: int = 40,
    top_k: int = 1,
    require_kinds: tuple[str, ...] = (),
) -> list[tuple[int, FoilProfile]]:
    """Generate `n_candidates` deterministically-derived seeds from
    `base_seed`, build+validate the world at each, score by
    `FoilProfile.salience_weighted_score`, and return the `top_k` highest
    scoring (seed, profile) pairs, sorted descending by score.

    Worlds that fail `validate()` are skipped entirely (never scored, never
    returned). When `require_kinds` is non-empty, only candidates whose
    `FoilProfile.trap_kinds` is a superset of `require_kinds` are eligible
    -- lets a caller demand e.g. `("final_true_state",)` be among the
    diverging foils, not just any foil.

    Fully deterministic: identical `(params, base_seed, n_candidates,
    top_k, require_kinds)` always yields identical output, since every
    seed is derived by `_derive_seed` (a pure sha256 stream) and `generate`
    /`foil_divergence` are themselves pure functions of `(params, seed)`.
    """
    # Deferred import: `render.naming` imports `world.model`, which runs
    # through this package's `__init__` -- importing it at module scope
    # here would create a circular import at package-load time.
    from ..render.naming import build_name_map

    scored: list[tuple[int, FoilProfile]] = []
    for i in range(n_candidates):
        seed = _derive_seed(base_seed, i)
        world = generate(params, seed)
        if validate(world):
            continue
        name_map = build_name_map(world, seed)
        profile = foil_divergence(world, name_map)
        if require_kinds and not set(require_kinds).issubset(profile.trap_kinds):
            continue
        scored.append((seed, profile))

    scored.sort(key=lambda item: item[1].salience_weighted_score, reverse=True)
    return scored[:top_k]
