"""Reproducibility certificate for an escalation `BreakingPoint`.

A breaking point is only worth publishing if someone else could re-run the
search and land on the same frontier. `certify` checks the four things that
decide that, all from data already on hand (no new trials, no LLM):

1. FOUND -- the escalation actually located a pass->fail frontier.
2. FRESH-SEED CONFIRMED -- the decomposed hypothesis was re-tested on seeds
   the search never touched (`cartographer.confirm`, seed_base >= 10_000)
   and held up. No confirmation at all is a failure, not a pass: an
   untested hypothesis is a guess.
3. GATE MOSTLY STABLE -- when a resample gate re-ran FAILs (see
   `cartographer.interactive.campaign.ResampleGatedEvaluator`), at least
   60% of the gated FAILs reproduced. A gate that mostly saw noise means
   the frontier sits on sampling flukes. No gated FAILs (0 + 0) passes
   trivially -- there is nothing to object to.
4. COMPACT -- the boundary is explained in at most `compact_max_steps`
   steps. A boundary that needs many steps is compounding error (each step
   a small chance to slip) rather than a structural limit, and a re-run
   will smear it. `None` means "not measured" and passes.

`reproducible` is the AND of all four; `reasons` names every failing one in
plain words, so a reader sees all the problems at once rather than the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover -- typing only; escalate imports us
    from .escalate import BreakingPoint

# Minimum share of gated FAILs that must reproduce (stable / (stable+noise)).
GATE_STABLE_SHARE = 0.6
# Default step ceiling for a "structural" (compact) boundary.
COMPACT_MAX_STEPS = 12


@dataclass
class ReproducibilityCertificate:
    """Verdict on whether a breaking point would survive an independent re-run.

    `fresh_seed_confirmed` / `confirmation_p` are None when no fresh-seed
    confirmation ran (decompose found nothing, it was "unexplained", or the
    budget ran out). `boundary_steps` is None when not measured.
    """

    found: bool
    gate_stable: int
    gate_noise: int
    fresh_seed_confirmed: bool | None
    confirmation_p: float | None
    boundary_steps: float | None
    compact: bool
    reproducible: bool
    reasons: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "found": self.found,
            "gate_stable": self.gate_stable,
            "gate_noise": self.gate_noise,
            "fresh_seed_confirmed": self.fresh_seed_confirmed,
            "confirmation_p": self.confirmation_p,
            "boundary_steps": self.boundary_steps,
            "compact": self.compact,
            "reproducible": self.reproducible,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_json_dict(cls, d: dict) -> "ReproducibilityCertificate":
        return cls(
            found=bool(d["found"]),
            gate_stable=int(d["gate_stable"]),
            gate_noise=int(d["gate_noise"]),
            fresh_seed_confirmed=d["fresh_seed_confirmed"],
            confirmation_p=d["confirmation_p"],
            boundary_steps=d["boundary_steps"],
            compact=bool(d["compact"]),
            reproducible=bool(d["reproducible"]),
            reasons=list(d.get("reasons", [])),
        )


def _fmt(x: float) -> str:
    return f"{x:g}"


def certify(
    bp: "BreakingPoint",
    *,
    gate_stable: int = 0,
    gate_noise: int = 0,
    boundary_steps: float | None = None,
    compact_max_steps: float = COMPACT_MAX_STEPS,
) -> ReproducibilityCertificate:
    """Certify `bp` (see module docstring for the four rules).

    `gate_stable` / `gate_noise` are the resample gate's counts of FAILs that
    reproduced vs. flipped to a pass on re-run (0/0 when no gate ran).
    `boundary_steps` is how many steps the breaking config takes to explain
    (e.g. plan depth at the frontier); None if unmeasured.
    """
    reasons: list[str] = []

    if not bp.found:
        reasons.append("no breaking point found")

    conf = bp.confirmation
    fresh_seed_confirmed: bool | None = None if conf is None else bool(conf.confirmed)
    confirmation_p: float | None = None if conf is None else float(conf.raw_p)
    if conf is None:
        reasons.append("no fresh-seed confirmation")
    elif not conf.confirmed:
        reasons.append(
            f"fresh-seed confirmation failed (p={conf.raw_p:.3g}, "
            f"effect={conf.effect_size:.2f})"
        )

    gated = gate_stable + gate_noise
    if gated > 0:
        share = gate_stable / gated
        if share < GATE_STABLE_SHARE:
            reasons.append(
                f"resample gate mostly noise: {gate_stable} of {gated} gated FAILs "
                f"reproduced ({share:.0%} < {GATE_STABLE_SHARE:.0%})"
            )

    compact = boundary_steps is None or boundary_steps <= compact_max_steps
    if not compact:
        reasons.append(
            f"boundary needs {_fmt(boundary_steps)} steps, limit {_fmt(compact_max_steps)} "
            "(compounding, not structural)"
        )

    return ReproducibilityCertificate(
        found=bool(bp.found),
        gate_stable=gate_stable,
        gate_noise=gate_noise,
        fresh_seed_confirmed=fresh_seed_confirmed,
        confirmation_p=confirmation_p,
        boundary_steps=boundary_steps,
        compact=compact,
        reproducible=not reasons,
        reasons=reasons,
    )
