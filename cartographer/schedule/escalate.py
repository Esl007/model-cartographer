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

Opt-in extras (all off by default; the default path is unchanged):
``early_stop=True`` stops each ramp/bisect probe once its classification is
decided (see `find_breaking_point`); every result carries a
`ReproducibilityCertificate` (`cartographer.schedule.certificate`); and
`scout_rays` spends a few trials per candidate ray to rank where a full
escalation budget should go.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from cartographer.confirm import ConfirmationResult, confirm_hypothesis
from cartographer.decompose import decompose
from cartographer.decompose.hypothesis import FailureHypothesis

from .certificate import ReproducibilityCertificate, certify
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

    DEGRADATION vs BREAK. `found`/`t_star` answer "where does the model
    COLLAPSE" (pass rate <= `fail_threshold`, default 0.30). That is a
    deliberately strict bar, and a model can degrade badly without ever
    reaching it -- e.g. sitting at a 60% pass rate (a 40% failure rate!)
    across the whole upper ray and still reporting `found=False`. Reporting
    only the break therefore throws away the more common result. So
    `degradation_onset` records the FIRST ramp t whose pass rate fell at
    least `degradation_drop` below the t=0 baseline, with
    `config_at_degradation` and `baseline_rate` alongside it. It is
    descriptive (a threshold crossing on k_trials samples), not an
    inferential estimate like `t_star` -- treat it as "difficulty where
    this model started visibly slipping," and widen k_trials before
    leaning on it hard.

    `probe_sizes` runs parallel to `pass_curve`: the number of trials each
    probe actually ran. It is k_trials everywhere unless `early_stop` cut a
    probe short once its classification was decided, in which case that
    probe's rate is over the trials run. `certificate` is the
    `ReproducibilityCertificate` that `find_breaking_point` attaches (gate
    counts 0 -- re-`certify` with real gate counts if a resample gate ran).
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
    degradation_onset: float | None = None
    config_at_degradation: dict[str, float] = field(default_factory=dict)
    baseline_rate: float | None = None
    note: str = ""
    probe_sizes: list[int] = field(default_factory=list)
    certificate: ReproducibilityCertificate | None = None

    def to_json_dict(self) -> dict:
        d = {
            "found": self.found,
            "t_star": self.t_star,
            "t_ci": list(self.t_ci) if self.t_ci is not None else None,
            "config_at_break": dict(self.config_at_break),
            "pass_curve": [[t, r] for t, r in self.pass_curve],
            "hypothesis": self.hypothesis.to_json_dict() if self.hypothesis else None,
            "confirmation": self.confirmation.to_json_dict() if self.confirmation else None,
            "budget_spent": self.budget_spent,
            "ceiling_pass_rate": self.ceiling_pass_rate,
            "degradation_onset": self.degradation_onset,
            "config_at_degradation": dict(self.config_at_degradation),
            "baseline_rate": self.baseline_rate,
            "note": self.note,
            "probe_sizes": list(self.probe_sizes),
        }
        if self.certificate is not None:
            d["certificate"] = self.certificate.to_json_dict()
        return d


# A probe-classification cut: ("ge", c) for `rate >= c`, ("le", c) for
# `rate <= c` -- exactly the comparisons find_breaking_point makes on a rate.
Cut = tuple[str, float]


def _bucket_decided(passes: int, n_run: int, k: int, cuts: list[Cut]) -> bool:
    """True when the k-trial probe's class against every cut is already fixed
    after `n_run` trials with `passes` passes, whatever the k - n_run unrun
    trials would return.

    The full-probe rate can still land anywhere in [passes/k,
    (passes + k - n_run)/k]; each cut is a half-line, so the class is decided
    iff both ends compare the same. The early-stopped rate passes/n_run lies
    inside that interval too (passes/k <= passes/n <= (passes+k-n)/k, and
    float division is monotone), so the rate reported over the trials run
    lands in the same class the full probe would have.
    """
    if n_run <= 0:
        return False
    lo = passes / k
    hi = (passes + k - n_run) / k
    for op, c in cuts:
        if op == "ge":
            if (lo >= c) != (hi >= c):
                return False
        elif (lo <= c) != (hi <= c):
            return False
    return True


class _RayProbe:
    """Bookkeeping for ramp/bisect probes: budget, monotonic seeds, and the
    accumulated (t, outcome) trial points the logistic fit runs over.

    Seeds come from `seed_cursor`, which advances by the FULL k of every
    probe; `spent` advances by the trials actually run. Without early stop
    the two are always equal (so seeds are seed_base + spent, as before);
    with it, trial j of probe i keeps the seed it would have had in a full
    run -- an early-stopped probe's trials are a prefix of the full probe's.
    """

    def __init__(
        self,
        evaluator: Evaluator,
        ray: EscalationRay,
        budget: int,
        seed_base: int,
        parallelism: int = 1,
    ) -> None:
        self.evaluator = evaluator
        self.ray = ray
        self.budget = budget
        self.spent = 0
        self.seed_base = seed_base
        self.seed_cursor = 0
        self.parallelism = max(1, parallelism)
        self.points: list[tuple[float, bool]] = []
        self.pass_curve: list[tuple[float, float]] = []
        self.probe_sizes: list[int] = []

    def can(self, k: int) -> bool:
        return self.spent + k <= self.budget

    def probe_was_cut_short(self, errors_before: int) -> bool:
        """True when the evaluator hit its cost cap (or was cancelled) AND
        recorded new errors during the last probe -- i.e. some of that
        probe's trials were never actually run. Such a probe must not be
        read as a pass rate: unrun trials count as non-passes, which would
        fabricate a decline exactly where the money ran out."""
        stopped = getattr(self.evaluator, "capped", False) or getattr(self.evaluator, "cancelled", False)
        return bool(stopped) and getattr(self.evaluator, "error_count", 0) > errors_before

    def discard_last(self, k: int | None = None) -> None:
        """Drop the last probe's points and curve entry. `k` defaults to the
        number of trials that probe actually ran."""
        if k is None:
            k = self.probe_sizes[-1] if self.probe_sizes else 0
        if k:
            del self.points[-k:]
        if self.pass_curve:
            self.pass_curve.pop()
        if self.probe_sizes:
            self.probe_sizes.pop()

    def _run(self, config: dict[str, float], seeds: list[int]) -> list[bool]:
        if self.parallelism == 1 or len(seeds) <= 1:
            return [self.evaluator(config, seed) for seed in seeds]
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(self.parallelism, len(seeds))) as pool:
            return list(pool.map(lambda s: self.evaluator(config, s), seeds))

    def probe(self, t: float, k: int, cuts: list[Cut] | None = None) -> float:
        """Run k trials at config_at(t); records every (t, outcome) point and
        appends (t, pass_rate) to the pass curve. Caller must check `can(k)`.

        The k trials of one probe are independent (same config, distinct
        deterministic seeds), so with `parallelism > 1` they run concurrently
        on a thread pool. Seeds are assigned up front from the same
        seed_base+counter the sequential path uses, so the trial set is
        identical at any parallelism; `points` keeps seed order so the
        logistic fit and pass rate are unaffected by completion order.

        `cuts` (early stop): issue the pre-assigned seeds in waves of
        `parallelism` and stop once `_bucket_decided` says no remaining
        outcome can change the probe's class against the cuts. The rate is
        then over the trials run (a whole wave counts once it has run, so
        at parallelism p up to p-1 trials past the decision point are kept);
        the class it falls in is the one the full probe would have had.
        """
        config = self.ray.config_at(t)
        seeds = [self.seed_base + self.seed_cursor + i for i in range(k)]
        self.seed_cursor += k
        if cuts is None:
            self.spent += k
            outcomes = self._run(config, seeds)
        else:
            outcomes = []
            passes = 0
            i = 0
            while i < k:
                wave = seeds[i : i + self.parallelism]
                self.spent += len(wave)
                got = self._run(config, wave)
                outcomes.extend(got)
                passes += sum(1 for ok in got if ok)
                i += len(wave)
                if _bucket_decided(passes, i, k, cuts):
                    break
        passes = 0
        for ok in outcomes:
            self.points.append((t, ok))
            passes += ok
        n = len(outcomes)
        rate = passes / n if n else 0.0
        self.pass_curve.append((t, rate))
        self.probe_sizes.append(n)
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
    parallelism: int = 1,
    degradation_drop: float = 0.25,
    early_stop: bool = False,
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

    `parallelism` runs each probe's k_trials concurrently (the evaluators
    are thread-safe and the trial set is seed-deterministic at any worker
    count); the ramp/bisect STAGES stay sequential by nature -- each depends
    on the previous probe's rate. Default 1 preserves the old behavior.

    EARLY STOP (`early_stop=True`, opt-in). A ramp or bisect probe stops
    issuing trials once its bucket is fixed: no outcome of the remaining
    trials could change whether its rate reaches `pass_threshold` or falls
    to `fail_threshold` -- nor, so the search path cannot move, the other
    cut that stage reads (the degradation cut `baseline - degradation_drop`
    on the ramp while no onset is recorded yet; the 0.5 cut on bisect).
    E.g. a k=7 bisect probe, thresholds .7/.3: 5 straight passes decide
    "pass" (5/7 >= .7, >= .5, and it can no longer fall to .3), so the last
    2 trials are never run.
    Seeds stay pre-assigned (an early-stopped probe's trials are a prefix of
    the full probe's), trials are issued in waves of `parallelism`, the
    probe's rate is over the trials run (`probe_sizes` records how many),
    and the logistic fit sees only run trials. Because every decision the
    search makes is a class comparison, the probed t sequence, bracket,
    `found` and `config_at_break` are the same as a full run's whenever the
    budget is not the binding constraint (when it is, the saved trials buy
    extra probes). The t=0 baseline always runs in full: its rate is the
    reference value degradation is measured against, not just a class.
    Saving (measured, ramp+bisect trials, parallelism 1): 10-22% per run on
    the deterministic fakes of tests/test_escalate.py and
    tests/test_degradation_onset.py, ~17% in aggregate. A probe deep in the
    fail regime stops at 7 of 10 (5 of 7); deep in the pass regime at 8 of
    10 (6 of 7) while the degradation cut is live, 7 of 10 (5 of 7) after
    it is recorded or on bisect; a mixed
    probe near the frontier saves little. At parallelism p a decision
    mid-wave keeps the whole wave, so the saving shrinks as p grows. See
    tests/test_escalate_frontier.py.
    `t_star`/`t_ci`/`ceiling_pass_rate` can shift slightly (fewer, and
    differently weighted, points); they are estimates, not classes.

    Every return path attaches `certificate` (`certify(bp)` with gate
    counts 0 and no step count).
    """
    rp = _RayProbe(evaluator, ray, budget, seed_base, parallelism=parallelism)
    base_cuts: list[Cut] = [("ge", pass_threshold), ("le", fail_threshold)]

    # Degradation tracking (see BreakingPoint's docstring): recorded on every
    # exit path, including the found=False ones -- a model that never
    # collapses can still have a well-defined point where it started slipping.
    onset_t: float | None = None
    onset_cfg: dict[str, float] = {}
    baseline_seen: float | None = None

    def result(**kw) -> BreakingPoint:
        kw.setdefault("degradation_onset", onset_t)
        kw.setdefault("config_at_degradation", dict(onset_cfg))
        kw.setdefault("baseline_rate", baseline_seen)
        bp = BreakingPoint(
            pass_curve=list(rp.pass_curve),
            budget_spent=rp.spent,
            probe_sizes=list(rp.probe_sizes),
            **kw,
        )
        bp.certificate = certify(bp)
        return bp

    # --- Stage 1: RAMP -----------------------------------------------------
    if not rp.can(k_trials):
        return result(found=False, note="budget too small for a single probe")
    base_rate = rp.probe(0.0, k_trials)
    baseline_seen = base_rate
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
        errors_before = getattr(evaluator, "error_count", 0)
        ramp_cuts: list[Cut] | None = None
        if early_stop:
            ramp_cuts = list(base_cuts)
            if onset_t is None:
                ramp_cuts.append(("le", base_rate - degradation_drop))
        rate = rp.probe(t, k_trials, cuts=ramp_cuts)
        if rp.probe_was_cut_short(errors_before):
            rp.discard_last()
            return result(
                found=False,
                note=(
                    f"stopped at t={t:.2f}: cost cap or cancellation hit during this probe; "
                    "the incomplete probe was discarded"
                ),
            )
        if t == 1.0:
            ceiling_rate = rate
        if onset_t is None and rate <= base_rate - degradation_drop:
            onset_t = t
            onset_cfg = ray.config_at(t)
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
        errors_before = getattr(evaluator, "error_count", 0)
        bisect_cuts = base_cuts + [("ge", 0.5)] if early_stop else None
        rate = rp.probe(mid, k_trials, cuts=bisect_cuts)
        if rp.probe_was_cut_short(errors_before):
            rp.discard_last()
            break
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

    bp = BreakingPoint(
        found=True,
        t_star=t_star,
        t_ci=(ci_lo, ci_hi),
        config_at_break=failing_config,
        pass_curve=list(rp.pass_curve),
        hypothesis=hypothesis,
        confirmation=confirmation,
        budget_spent=rp.spent,
        ceiling_pass_rate=ceiling_rate,
        degradation_onset=onset_t,
        config_at_degradation=dict(onset_cfg),
        baseline_rate=baseline_seen,
        note="; ".join(notes) if notes else f"frontier bracket ({t_lo:.4f}, {t_hi:.4f})",
        probe_sizes=list(rp.probe_sizes),
    )
    bp.certificate = certify(bp)
    return bp


# ---------------------------------------------------------------------------
# Scout: cheap triage across candidate rays
# ---------------------------------------------------------------------------


@dataclass
class ScoutResult:
    """One ray's scouting outcome. `pass_rates` maps each probed t to its
    pass rate; `signal` is the failure signal 1 - mean(pass_rates) (0.0 when
    nothing could be probed). `note` is non-empty only when a probe was cut
    short by a cost cap / cancellation and discarded."""

    name: str
    pass_rates: dict[float, float] = field(default_factory=dict)
    signal: float = 0.0
    note: str = ""

    def to_json_dict(self) -> dict:
        return {
            "name": self.name,
            "pass_rates": [[t, r] for t, r in self.pass_rates.items()],
            "signal": self.signal,
            "note": self.note,
        }


def scout_rays(
    evaluator: Evaluator,
    rays: dict[str, EscalationRay],
    k: int = 3,
    t_points: tuple[float, ...] = (0.5, 1.0),
    seed_base: int = 0,
    parallelism: int = 1,
) -> list[ScoutResult]:
    """Probe each named ray at `t_points` with `k` trials each and rank the
    rays by failure signal -- lowest mean pass rate first, ties broken by the
    earliest t with any failure, then by name. Costs len(t_points) * k
    trials per ray (2k by default), so a campaign can decide where a full
    `find_breaking_point` budget goes before spending it.

    Seed discipline matches `find_breaking_point`: each ray gets its own
    probe counter starting at `seed_base`, seeds assigned in t_points order
    (seed_base + 0..k-1 for the first t, + k..2k-1 for the second, ...), so
    a ray's result does not depend on which other rays are scouted with it
    and is identical at any `parallelism`. A probe cut short by a cost cap
    is discarded (never read as failures) and scouting of that ray stops.
    """
    results: list[ScoutResult] = []
    for name in sorted(rays):
        ray = rays[name]
        rp = _RayProbe(evaluator, ray, len(t_points) * k, seed_base, parallelism=parallelism)
        rates: dict[float, float] = {}
        note = ""
        for t in t_points:
            errors_before = getattr(evaluator, "error_count", 0)
            rate = rp.probe(float(t), k)
            if rp.probe_was_cut_short(errors_before):
                rp.discard_last()
                note = f"cost cap or cancellation hit at t={t:g}; probe discarded"
                break
            rates[float(t)] = rate
        signal = 1.0 - sum(rates.values()) / len(rates) if rates else 0.0
        results.append(ScoutResult(name=name, pass_rates=rates, signal=signal, note=note))

    def first_failure(r: ScoutResult) -> float:
        fails = [t for t, rate in r.pass_rates.items() if rate < 1.0]
        return min(fails) if fails else float("inf")

    results.sort(key=lambda r: (-r.signal, first_failure(r), r.name))
    return results
