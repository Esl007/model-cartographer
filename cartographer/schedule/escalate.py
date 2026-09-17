"""Escalation mode: find the actual breaking point along a difficulty ray.

Instead of sweeping one dim at a time (the campaign scheduler's job), an
escalation walks a single ray through the FULL joint difficulty space: at
t=0 every dim sits at its easy base value, at t=1 every dim sits at the
generator's hard validated ceiling (chain_length 25, scramble 1.0,
coref_pressure 8, distractor_events 40, filler_events 60, witness_cutoff
0.9 -- verified combined-clean in ``tests/test_witness_cutoff.py``).
`find_breaking_point` ramps t upward until the model breaks, bisects the
frontier tightly, fits a logistic over every gathered trial for a t* with a
bootstrap CI, then hands the frontier to `cartographer.decompose.decompose`
("which dims actually did the breaking?") and, if a non-unexplained
hypothesis emerges, to `cartographer.confirm.confirm_hypothesis` on fresh
seeds (seed_base >= 10_000, per the confirmation module's convention).

If the model never breaks by t=1.0 the result is an honest
``found=False`` / "model exceeds generator limits" -- that is a real
finding, not an error.

Determinism: every trial seed is ``seed_base + <monotonic trial counter>``
assigned in a fixed order; decompose gets a fixed offset block still below
the 10_000 search-seed ceiling; confirmation gets its own >= 10_000 block.
Two runs with identical inputs produce identical results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from cartographer.confirm import ConfirmationResult, confirm_hypothesis
from cartographer.decompose import decompose
from cartographer.decompose.hypothesis import FailureHypothesis

from .estimate import boundary_ci, fit_logistic

Evaluator = Callable[[dict[str, float], int], bool]

# Bisection stops once the (t_lo, t_hi) bracket is at most this wide.
_BISECT_WIDTH = 0.03
# Fraction of the total budget the decompose stage may consume at most.
_DECOMPOSE_SHARE = 0.4
# Confirmation fresh-seed convention floor (see cartographer/confirm).
_CONFIRM_SEED_FLOOR = 10_000


@dataclass(frozen=True)
class RayDim:
    """One dimension's endpoints along an escalation ray."""

    name: str
    base_value: float
    limit_value: float
    is_int: bool = False

    def value_at(self, t: float) -> float:
        v = self.base_value + t * (self.limit_value - self.base_value)
        return float(round(v)) if self.is_int else float(v)


@dataclass(frozen=True)
class EscalationRay:
    """A straight line through joint difficulty space: t=0 maps every dim to
    its base value, t=1 to its limit value, linear in between (ints rounded).
    """

    dims: tuple[RayDim, ...]

    def config_at(self, t: float) -> dict[str, float]:
        return {d.name: d.value_at(t) for d in self.dims}

    def to_json_dict(self) -> dict:
        return {
            "dims": [
                {
                    "name": d.name,
                    "base_value": d.base_value,
                    "limit_value": d.limit_value,
                    "is_int": d.is_int,
                }
                for d in self.dims
            ]
        }


@dataclass
class BreakingPoint:
    """The outcome of one escalation along a ray.

    `found` is True iff a pass->fail frontier was located within t in
    [0, 1]. `t_star` is the logistic-fit crossing (None if not found),
    `t_ci` its bootstrap CI. `pass_curve` is every probed (t, pass_rate)
    in probe order -- the raw ramp+bisect record. `ceiling_pass_rate` is
    the pass rate observed at t=1.0 if it was probed (None otherwise).
    """

    found: bool
    t_star: float | None = None
    t_ci: tuple[float, float] | None = None
    config_at_break: dict[str, float] = field(default_factory=dict)
    pass_curve: list[tuple[float, float]] = field(default_factory=list)
    hypothesis: FailureHypothesis | None = None
    confirmation: ConfirmationResult | None = None
    budget_spent: int = 0
    ceiling_pass_rate: float | None = None
    note: str = ""

    def to_json_dict(self) -> dict:
        return {
            "found": self.found,
            "t_star": self.t_star,
            "t_ci": list(self.t_ci) if self.t_ci is not None else None,
            "config_at_break": dict(self.config_at_break),
            "pass_curve": [[t, r] for t, r in self.pass_curve],
            "hypothesis": self.hypothesis.to_json_dict() if self.hypothesis else None,
            "confirmation": self.confirmation.to_json_dict() if self.confirmation else None,
            "budget_spent": self.budget_spent,
            "ceiling_pass_rate": self.ceiling_pass_rate,
            "note": self.note,
        }


class _RayProbe:
    """Bookkeeping for ramp/bisect probes: budget, monotonic seeds, and the
    accumulated (t, outcome) trial points the logistic fit runs over.
    """

    def __init__(self, evaluator: Evaluator, ray: EscalationRay, budget: int, seed_base: int) -> None:
        self.evaluator = evaluator
        self.ray = ray
        self.budget = budget
        self.spent = 0
        self.seed_base = seed_base
        self.points: list[tuple[float, bool]] = []
        self.pass_curve: list[tuple[float, float]] = []

    def can(self, k: int) -> bool:
        return self.spent + k <= self.budget

    def probe(self, t: float, k: int) -> float:
        """Run k trials at config_at(t); records every (t, outcome) point and
        appends (t, pass_rate) to the pass curve. Caller must check `can(k)`.
        """
        config = self.ray.config_at(t)
        passes = 0
        for _ in range(k):
            seed = self.seed_base + self.spent
            self.spent += 1
            ok = self.evaluator(config, seed)
            self.points.append((t, ok))
            passes += ok
        rate = passes / k if k else 0.0
        self.pass_curve.append((t, rate))
        return rate


def find_breaking_point(
    evaluator: Evaluator,
    ray: EscalationRay,
    k_trials: int = 7,
    pass_threshold: float = 0.7,
    fail_threshold: float = 0.3,
    budget: int = 350,
    seed_base: int = 0,
    t_step: float = 0.15,
) -> BreakingPoint:
    """Ramp t from 0 to 1 along `ray`, bisect the first pass->fail frontier,
    fit a logistic t*, then decompose + confirm at the frontier.

    Stages (all within one shared `budget` of evaluator trials):

    1. RAMP -- probe t=0 (must reach `pass_threshold`, else a degenerate
       result is returned), then step t upward by `t_step` (clamped to end
       exactly at 1.0), k_trials each, stopping at the first t whose pass
       rate is <= `fail_threshold`.
    2. If t=1.0 is reached without such a failure: `found=False` with note
       "model exceeds generator limits" -- an honest could-not-break-it.
    3. BISECT the (last passing, first failing) bracket to width
       <= 0.03, then fit a logistic over ALL (t, outcome) points and
       bootstrap a CI via `cartographer.schedule.estimate`.
    4. DECOMPOSE at the frontier: config_at(first failing t) vs
       config_at(0), with the remaining budget capped at 40% of the total.
    5. CONFIRM a non-unexplained hypothesis on fresh seeds
       (seed_base >= 10_000 convention).
    """
    rp = _RayProbe(evaluator, ray, budget, seed_base)

    def result(**kw) -> BreakingPoint:
        return BreakingPoint(
            pass_curve=list(rp.pass_curve), budget_spent=rp.spent, **kw
        )

    # --- Stage 1: RAMP -----------------------------------------------------
    if not rp.can(k_trials):
        return result(found=False, note="budget too small for a single probe")
    base_rate = rp.probe(0.0, k_trials)
    if base_rate < pass_threshold:
        return result(
            found=False,
            note=(
                f"degenerate: baseline t=0 does not pass "
                f"(pass_rate={base_rate:.3f} < {pass_threshold})"
            ),
        )

    # Ascending t levels: t_step increments, always ending exactly at 1.0.
    levels: list[float] = []
    t = t_step
    while t < 1.0 - 1e-9:
        levels.append(round(t, 10))
        t += t_step
    levels.append(1.0)

    t_lo = 0.0  # last passing t
    t_hi: float | None = None  # first failing t
    ceiling_rate: float | None = None
    for t in levels:
        if not rp.can(k_trials):
            return result(
                found=False,
                note=f"budget exhausted during ramp before t={t:.2f}",
            )
        rate = rp.probe(t, k_trials)
        if t == 1.0:
            ceiling_rate = rate
        if rate <= fail_threshold:
            t_hi = t
            break
        t_lo = t

    # --- Stage 2: never broke ----------------------------------------------
    if t_hi is None:
        return result(
            found=False,
            ceiling_pass_rate=ceiling_rate,
            note="model exceeds generator limits",
        )

    # --- Stage 3: BISECT + logistic fit ------------------------------------
    while (t_hi - t_lo) > _BISECT_WIDTH and rp.can(k_trials):
        mid = (t_lo + t_hi) / 2.0
        rate = rp.probe(mid, k_trials)
        if rate >= 0.5:
            t_lo = mid
        else:
            t_hi = mid

    sorted_points = sorted(rp.points)
    fit = fit_logistic(sorted_points)
    if fit is not None:
        t_star = min(max(fit.midpoint, 0.0), 1.0)
        _, ci_lo, ci_hi = boundary_ci(sorted_points, seed=seed_base)
    else:  # pragma: no cover -- we always have both outcomes here
        t_star = (t_lo + t_hi) / 2.0
        ci_lo, ci_hi = t_lo, t_hi

    # --- Stage 4: DECOMPOSE at the frontier --------------------------------
    hypothesis: FailureHypothesis | None = None
    confirmation: ConfirmationResult | None = None
    notes: list[str] = []
    decompose_budget = min(budget - rp.spent, int(budget * _DECOMPOSE_SHARE))
    failing_config = ray.config_at(t_hi)
    baseline_config = ray.config_at(0.0)
    if decompose_budget >= 2 * k_trials:

        def charged(cfg: dict[str, float], seed: int) -> bool:
            rp.spent += 1
            return evaluator(cfg, seed)

        hypothesis = decompose(
            charged,
            failing_config,
            baseline_config,
            k_trials=k_trials,
            pass_threshold=pass_threshold,
            fail_threshold=fail_threshold,
            budget=decompose_budget,
            # Fixed offset block, still within the < 10_000 search-seed
            # convention (ramp+bisect counters stay well below 2_000).
            seed_base=seed_base + 2_000,
        )
    else:
        notes.append("decompose skipped: insufficient remaining budget")

    # --- Stage 5: CONFIRM on fresh seeds -----------------------------------
    if hypothesis is not None and hypothesis.kind != "unexplained":
        confirm_k, confirm_blocks = k_trials, 3
        confirm_cost = 2 * confirm_k * confirm_blocks
        if rp.can(confirm_cost):
            confirmation = confirm_hypothesis(
                hypothesis,
                evaluator,
                k_trials=confirm_k,
                n_fresh_seeds=confirm_blocks,
                seed_base=_CONFIRM_SEED_FLOOR + seed_base,
            )
            rp.spent += confirmation.n_trials_spent
        else:
            notes.append("confirmation skipped: insufficient remaining budget")

    return BreakingPoint(
        found=True,
        t_star=t_star,
        t_ci=(ci_lo, ci_hi),
        config_at_break=failing_config,
        pass_curve=list(rp.pass_curve),
        hypothesis=hypothesis,
        confirmation=confirmation,
        budget_spent=rp.spent,
        ceiling_pass_rate=ceiling_rate,
        note="; ".join(notes) if notes else f"frontier bracket ({t_lo:.4f}, {t_hi:.4f})",
    )
