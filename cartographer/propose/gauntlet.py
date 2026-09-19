"""Hard validation gauntlet: deterministic, zero-API filtering of proposer
`Proposal`s before any subject-model budget is spent on them.

Steps, per proposal:
  1. Clamp/reject out-of-range config values against the real `DimParams`
     legal ranges (same ranges advertised in `proposer.PROPOSAL_PROMPT`'s
     schema text).
  2. Build `DimParams`, generate + `validate()` the resulting world at a
     handful of candidate seeds.
  3. Run adversarial seed search (`cartographer.world.search
     .search_adversarial_seeds`) with `require_kinds=require_traps` to pick
     the single best seed for this config.
  4. Reject if that best world's `salience_weighted_score` is below the
     MEDIAN score of a same-size batch of RANDOM (non-adversarially-picked)
     seeds at the same config -- the proposer must beat random chance to
     earn a subject-model trial.

Every rejection is recorded with a human-readable reason so a caller can
report gauntlet throughput (proposed vs. rejected vs. survived).
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field

from ..render.naming import build_name_map
from ..world.foils import FoilProfile, foil_divergence
from ..world.generate import DimParams, generate
from ..world.model import World
from ..world.search import search_adversarial_seeds
from ..world.simulate import validate
from .proposer import Proposal

# (base_default, min, max, is_int) per DimParams field the proposer is
# allowed to set. base_default is used to fill in any field the proposal
# omits. Ranges mirror `_GENERATOR_LIMITS` in cartographer/exec/evaluator.py
# and the schema text in proposer.py.
_FIELD_RANGES: dict[str, tuple[object, float, float, bool]] = {
    "chain_length": (2, 1, 30, True),
    "scramble": (0.0, 0.0, 1.0, False),
    "coref_pressure": (0, 0, 8, True),
    "distractor_events": (2, 0, 40, True),
    "filler_events": (2, 0, 60, True),
    "witness_cutoff": (0.0, 0.0, 0.9, False),
}
_VALID_DISTRACTOR_SIMILARITY = {"unrelated", "same_entities", "same_category", "near_duplicate"}
_VALID_QUERY_KIND = {"state", "belief", "order"}

_VALID_TRAP_NAMES = {
    "final_true_state",
    "last_mentioned_location",
    "last_witnessed_by_anyone",
    "query_agent_own_last_move",
    "most_frequent_location",
}


@dataclass(frozen=True)
class GauntletResult:
    """Outcome of running one `Proposal` through the gauntlet.

    `accepted=True` iff the proposal survived every stage; `seed` and
    `profile` are then the winning adversarial seed and its `FoilProfile`.
    `reason` is always set to a short machine-and-human-readable string
    (e.g. "invalid_query_kind", "world_invalid", "below_random_median")
    identifying why a rejected proposal was rejected, or "accepted".
    """

    proposal: Proposal
    accepted: bool
    reason: str
    params: DimParams | None = None
    seed: int | None = None
    profile: FoilProfile | None = None
    random_median_score: float = 0.0


def _clamp_config(raw_config: dict) -> tuple[dict, list[str]] | None:
    """Clamp `raw_config`'s numeric fields into their legal ranges and
    validate its categorical fields. Returns (clamped_config, notes) on
    success (notes records any clamping that occurred, may be empty), or
    None if the config is unsalvageable (e.g. an invalid query_kind or
    distractor_similarity enum value, or a non-numeric value for a numeric
    field)."""
    clamped: dict = {}
    notes: list[str] = []

    for key, value in raw_config.items():
        if key in _FIELD_RANGES:
            _, lo, hi, is_int = _FIELD_RANGES[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None
            v = float(value)
            if v < lo or v > hi:
                notes.append(f"{key} clamped from {v} to [{lo},{hi}]")
            v = max(lo, min(hi, v))
            clamped[key] = int(round(v)) if is_int else v
        elif key == "distractor_similarity":
            if value not in _VALID_DISTRACTOR_SIMILARITY:
                return None
            clamped[key] = value
        elif key == "query_kind":
            if value not in _VALID_QUERY_KIND:
                return None
            clamped[key] = value
        # Unknown keys are silently dropped (not an error -- the proposer
        # may echo back fields outside the schema; DimParams construction
        # would reject them anyway, so drop rather than reject the whole
        # proposal over one extra key).

    return clamped, notes


def _build_params(clamped_config: dict) -> DimParams:
    base = dict(
        chain_length=2,
        scramble=0.0,
        coref_pressure=0,
        distractor_events=2,
        distractor_similarity="same_category",
        filler_events=2,
        query_kind="belief",
        witness_cutoff=0.0,
    )
    base.update(clamped_config)
    return DimParams(**base)


def _random_seed_scores(
    params: DimParams, base_seed: int, n: int, require_kinds: tuple[str, ...]
) -> list[float]:
    """Scores of `n` deterministically-derived seeds NOT chosen by the
    adversarial search (a plain "first n generated, no ranking" control),
    for the random-median comparison. Uses a distinct hash namespace from
    `world.search._derive_seed` so it never accidentally reuses the exact
    same seeds the adversarial search already tried."""
    scores: list[float] = []
    for i in range(n):
        payload = f"gauntlet_random_seed|{base_seed}|{i}".encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        seed = int(digest[:15], 16)
        world = generate(params, seed)
        if validate(world):
            continue
        name_map = build_name_map(world, seed)
        profile = foil_divergence(world, name_map)
        if require_kinds and not set(require_kinds).issubset(profile.trap_kinds):
            continue
        scores.append(profile.salience_weighted_score)
    return scores


def run_gauntlet(
    proposal: Proposal,
    base_seed: int,
    n_candidates: int = 40,
    n_random_control: int = 20,
) -> GauntletResult:
    """Run one `Proposal` through the full gauntlet. Zero API calls."""
    require_traps = tuple(t for t in proposal.require_traps if t in _VALID_TRAP_NAMES)
    if len(require_traps) != len(proposal.require_traps):
        # Silently drop unknown trap names rather than rejecting outright --
        # same "tolerate extra junk from the LLM" philosophy as unknown
        # config keys. If ALL requested traps were bogus, require_traps
        # becomes () and the proposal is simply unconstrained.
        pass

    clamp_result = _clamp_config(proposal.config)
    if clamp_result is None:
        return GauntletResult(proposal=proposal, accepted=False, reason="invalid_config")
    clamped_config, _clamp_notes = clamp_result

    try:
        params = _build_params(clamped_config)
    except TypeError:
        return GauntletResult(proposal=proposal, accepted=False, reason="invalid_config")

    winners = search_adversarial_seeds(
        params, base_seed, n_candidates=n_candidates, top_k=1, require_kinds=require_traps
    )
    if not winners:
        return GauntletResult(
            proposal=proposal, accepted=False, reason="no_valid_world_with_required_traps", params=params
        )
    seed, profile = winners[0]

    random_scores = _random_seed_scores(params, base_seed, n_random_control, require_traps)
    if not random_scores:
        # No random control candidate could even satisfy require_traps --
        # the adversarial winner is the only evidence we have; accept it
        # rather than penalizing the proposal for a control-arm shortfall.
        median = 0.0
    else:
        median = statistics.median(random_scores)

    if profile.salience_weighted_score < median:
        return GauntletResult(
            proposal=proposal,
            accepted=False,
            reason="below_random_median",
            params=params,
            seed=seed,
            profile=profile,
            random_median_score=median,
        )

    return GauntletResult(
        proposal=proposal,
        accepted=True,
        reason="accepted",
        params=params,
        seed=seed,
        profile=profile,
        random_median_score=median,
    )


def run_gauntlet_batch(
    proposals: list[Proposal],
    base_seed: int,
    n_candidates: int = 40,
    n_random_control: int = 20,
) -> list[GauntletResult]:
    """Run `run_gauntlet` over every proposal, deriving a distinct
    `base_seed` per-proposal index so different proposals in the same round
    don't collide on identical candidate-seed batches."""
    results = []
    for i, proposal in enumerate(proposals):
        payload = f"gauntlet_proposal_seed|{base_seed}|{i}".encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        proposal_seed = int(digest[:15], 16)
        results.append(
            run_gauntlet(
                proposal,
                base_seed=proposal_seed,
                n_candidates=n_candidates,
                n_random_control=n_random_control,
            )
        )
    return results
