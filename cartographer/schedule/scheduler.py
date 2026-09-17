"""Phase 5: the adaptive scheduler -- a wave-based campaign loop.

A campaign explores one or more dims looking for pass/fail boundaries,
refines each found boundary with bisection, then hands each boundary off to
`cartographer.decompose.decompose` to explain *what else* combines with it
to produce failure. The scheduler is model-pipeline-agnostic: it depends
only on a `TaskEvaluator` callable with the same `(config, trial_seed) ->
bool` contract as `decompose.Evaluator`. Real runs wrap
generate -> render -> chat -> verify into one of these; synthetic tests pass
`cartographer.synthetic.ZOO` lambdas directly (see `tests/test_schedule.py`).

Every trial -- explore, refine, or decompose -- draws from one shared budget
counter, so `SchedulerConfig.budget` is a hard cap on total evaluator calls
across the whole campaign, not per-stage. Seeds are always derived
arithmetically from `SchedulerConfig.seed_base`, so a campaign is fully
reproducible given the same evaluator, config, and seed_base.

The scheduler itself never imports `cartographer.store` -- persistence is
the caller's job via the `on_record` callback, keeping this module pure and
therefore trivially testable against the synthetic zoo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from cartographer.decompose import decompose
from cartographer.decompose.hypothesis import FailureHypothesis

from .estimate import boundary_ci

TaskEvaluator = Callable[[dict[str, float], int], bool]

OnRecord = Callable[[dict, bool, int], None]


@dataclass(frozen=True)
class DimSpec:
    """One dimension the campaign explores: its name and probing range."""

    name: str
    lo: float
    hi: float
    is_int: bool = False


@dataclass(frozen=True)
class SchedulerConfig:
    """Full configuration for one adaptive campaign."""

    dims: list[DimSpec]
    baseline: dict[str, float]
    ceiling: dict[str, float]
    explore_points: int = 5
    k_trials: int = 5
    refine_rounds: int = 6
    ci_width_stop: float = 1.0
    budget: int = 400
    decompose_share: float = 0.3
    seed_base: int = 0


@dataclass
class CampaignState:
    """Accumulated state of a campaign, sufficient to resume it later."""

    # dim_name -> list of (dim_value, passed) points gathered (explore+refine).
    points: dict[str, list[tuple[float, bool]]] = field(default_factory=dict)
    budget_spent: int = 0
    # dim_name -> (midpoint, ci_lo, ci_hi), only for dims where a boundary
    # was actually found (excludes "no boundary in range" dims).
    boundaries: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    # dims explicitly marked as having no boundary within [lo, hi].
    no_boundary_dims: set[str] = field(default_factory=set)
    hypotheses: list[FailureHypothesis] = field(default_factory=list)
    waves_run: int = 0
    notes: list[str] = field(default_factory=list)


class _BudgetExhausted(Exception):
    pass


class _SharedBudget:
    """Tracks remaining trial budget and a monotonic seed counter."""

    def __init__(self, budget_total: int, already_spent: int, seed_base: int) -> None:
        self.remaining = budget_total - already_spent
        self.spent = already_spent
        self._next_seed = seed_base + already_spent

    def take_seed_block(self, k: int) -> int:
        start = self._next_seed
        self._next_seed += k
        return start

    def charge(self, k: int) -> None:
        if k > self.remaining:
            raise _BudgetExhausted()
        self.remaining -= k
        self.spent += k


def _round_dim(value: float, spec: DimSpec) -> float:
    return float(round(value)) if spec.is_int else value


def _probe_point(
    evaluator: TaskEvaluator,
    base_config: dict[str, float],
    dim: str,
    x: float,
    k_trials: int,
    budget: _SharedBudget,
    on_record: OnRecord | None,
) -> bool:
    """Run k_trials at `base_config` with `dim` set to `x`; majority vote.

    Every trial is charged against the shared budget individually and, if
    provided, reported via `on_record(config, passed, trial_seed)` -- this
    is the hook a caller uses to persist each trial as an ExperimentRecord.
    """
    budget.charge(k_trials)
    seed_start = budget.take_seed_block(k_trials)
    config = dict(base_config)
    config[dim] = x
    passes = 0
    for i in range(k_trials):
        seed = seed_start + i
        ok = evaluator(config, seed)
        if on_record is not None:
            on_record(dict(config), ok, seed)
        if ok:
            passes += 1
    return passes * 2 > k_trials


def _explore_dim(
    evaluator: TaskEvaluator,
    config: SchedulerConfig,
    spec: DimSpec,
    state: CampaignState,
    budget: _SharedBudget,
    on_record: OnRecord | None,
) -> None:
    """Wave 1: probe `explore_points` evenly spaced levels of `spec`."""
    n = config.explore_points
    if n == 1:
        levels = [(spec.lo + spec.hi) / 2.0]
    else:
        step = (spec.hi - spec.lo) / (n - 1)
        levels = [spec.lo + i * step for i in range(n)]
    levels = sorted({_round_dim(x, spec) for x in levels})

    pts = state.points.setdefault(spec.name, [])
    already = {x for x, _ in pts}
    for x in levels:
        if x in already:
            continue
        passed = _probe_point(
            evaluator, config.baseline, spec.name, x, config.k_trials, budget, on_record
        )
        pts.append((x, passed))

    if all(p for _, p in pts):
        state.no_boundary_dims.add(spec.name)


def _refine_dim(
    evaluator: TaskEvaluator,
    config: SchedulerConfig,
    spec: DimSpec,
    state: CampaignState,
    budget: _SharedBudget,
    on_record: OnRecord | None,
) -> None:
    """Waves 2+: bisect between the last-passing and first-failing explore
    levels found for `spec`, until the boundary CI is narrow enough or
    `refine_rounds` probes have run.
    """
    pts = state.points.setdefault(spec.name, [])
    passing_x = [x for x, p in pts if p]
    failing_x = [x for x, p in pts if not p]
    if not passing_x or not failing_x:
        return  # no flip observed yet (shouldn't happen if caller checked)

    left = max(passing_x)
    right = min(x for x in failing_x if x > left) if any(x > left for x in failing_x) else min(failing_x)
    if right <= left:
        # Fall back to full observed bracket.
        left, right = min(passing_x), max(failing_x)

    for _ in range(config.refine_rounds):
        mid, ci_lo, ci_hi = boundary_ci(pts, seed=config.seed_base + hash(spec.name) % 10_000)
        if (ci_hi - ci_lo) <= config.ci_width_stop:
            break
        probe_x = _round_dim((left + right) / 2.0, spec)
        if probe_x in {x for x, _ in pts}:
            # Degenerate midpoint (integer rounding collapsed); nudge.
            if spec.is_int and right - left <= 1:
                break
            probe_x = (left + right) / 2.0
        passed = _probe_point(
            evaluator, config.baseline, spec.name, probe_x, config.k_trials, budget, on_record
        )
        pts.append((probe_x, passed))
        if passed:
            left = probe_x
        else:
            right = probe_x
        if right <= left:
            break

    mid, ci_lo, ci_hi = boundary_ci(pts, seed=config.seed_base + hash(spec.name) % 10_000)
    state.boundaries[spec.name] = (mid, ci_lo, ci_hi)


def _decompose_dim(
    evaluator: TaskEvaluator,
    config: SchedulerConfig,
    spec: DimSpec,
    failing_side_value: float,
    state: CampaignState,
    budget: _SharedBudget,
    dim_index: int,
    n_dims: int,
    on_record: OnRecord | None,
) -> None:
    """Hand a found (or suspected) boundary off to `decompose`.

    Rationale (per Phase 5 spec): decompose explains what ELSE combines
    with the boundary dim to cause failure -- the scheduler only tells you
    *where* a single-dim boundary sits; decompose tells you whether other
    dims interact with it. The failing config raises every OTHER dim to
    its ceiling value while holding this dim at its failing-side value, so
    any pairwise/higher-order interaction with the boundary dim surfaces.
    """
    if budget.remaining <= 0:
        return
    failing_config = dict(config.baseline)
    failing_config[spec.name] = failing_side_value
    for other in config.dims:
        if other.name == spec.name:
            continue
        failing_config[other.name] = config.ceiling.get(other.name, other.hi)

    per_dim_budget = max(1, int(budget.remaining * config.decompose_share / max(n_dims, 1)))
    per_dim_budget = min(per_dim_budget, budget.remaining)
    seed_base = config.seed_base + 1_000_000 + dim_index * 10_000

    def wrapped_evaluator(cfg: dict[str, float], seed: int) -> bool:
        # Route every decompose trial through the shared budget counter and
        # the same on_record hook explore/refine trials use.
        budget.charge(1)
        ok = evaluator(cfg, seed)
        if on_record is not None:
            on_record(dict(cfg), ok, seed)
        return ok

    try:
        hyp = decompose(
            wrapped_evaluator,
            failing_config,
            config.baseline,
            k_trials=config.k_trials,
            budget=per_dim_budget,
            seed_base=seed_base,
        )
        state.hypotheses.append(hyp)
    except _BudgetExhausted:
        state.notes.append(f"decompose stage for {spec.name}: shared budget exhausted")


def run_campaign(
    evaluator: TaskEvaluator,
    config: SchedulerConfig,
    on_record: OnRecord | None = None,
    progress: Callable[[str], None] = print,
    initial_state: CampaignState | None = None,
) -> CampaignState:
    """Run (or resume) a full adaptive campaign.

    Wave 1 (explore): each dim is swept at `explore_points` evenly spaced
    levels (others held at baseline), `k_trials` per level. A dim that
    passes at every level is marked "no boundary in range" and skips
    refinement.

    Waves 2+ (refine): each dim with an observed pass/fail flip is bisected
    between the last passing and first failing explore level, up to
    `refine_rounds` probes or until the bootstrap CI width is
    <= `ci_width_stop`.

    Decompose stage: for each dim with a found boundary OR a suspected
    interaction (see below), a failing config is built (this dim at its
    failing-side value, every OTHER dim raised to ceiling) and handed to
    `cartographer.decompose.decompose` to find what else combines with it.

    Interaction-suspected path: if a dim's single-dim sweep passes
    everywhere (no boundary found) but the all-ceiling config (every dim at
    its ceiling simultaneously) fails, that is direct evidence of a
    multi-dim interaction the single-dim sweeps are blind to. In that case
    decompose is still triggered, seeded from the all-ceiling failing
    config, for every no-boundary dim -- this is the scientifically
    critical case: a model that's robust to any *one* dim in isolation but
    breaks when several combine.

    Budget: every trial (explore, refine, or decompose) decrements one
    shared counter. On hard exhaustion, the current probe finishes (no
    partial trial is charged) and the campaign returns the best-so-far
    state with a note appended -- it never raises.

    Resumability: pass a `CampaignState` from `resume_from_records` (or a
    prior `run_campaign` call) as `initial_state` to continue a campaign.
    Dims that already have >= `explore_points` points skip re-exploring;
    refinement continues from the existing points. With the same
    `seed_base`, resume + continue produces a superset of a fresh run's
    probes (fresh points are appended, never replaced or re-ordered).
    """
    state = initial_state if initial_state is not None else CampaignState()
    budget = _SharedBudget(config.budget, state.budget_spent, config.seed_base)
    state.waves_run += 1

    try:
        # WAVE 1: EXPLORE.
        for spec in config.dims:
            pts = state.points.get(spec.name, [])
            if len(pts) >= config.explore_points and (
                spec.name in state.no_boundary_dims or any(not p for _, p in pts)
            ):
                progress(f"[explore] {spec.name}: already explored, skipping")
                continue
            progress(f"[explore] {spec.name}")
            _explore_dim(evaluator, config, spec, state, budget, on_record)

        # WAVES 2+: REFINE.
        for spec in config.dims:
            if spec.name in state.no_boundary_dims:
                continue
            pts = state.points.get(spec.name, [])
            has_flip = any(p for _, p in pts) and any(not p for _, p in pts)
            if not has_flip:
                continue
            progress(f"[refine] {spec.name}")
            _refine_dim(evaluator, config, spec, state, budget, on_record)

        # DECOMPOSE STAGE.
        found_boundary_dims = list(state.boundaries.keys())
        n_dims = max(len(config.dims), 1)

        # Detect the interaction-suspected case: every dim's single-dim
        # sweep passes everywhere, but the all-ceiling config fails.
        suspected_dims: list[str] = []
        if not found_boundary_dims and state.no_boundary_dims and budget.remaining > 0:
            all_ceiling_config = dict(config.baseline)
            for d in config.dims:
                all_ceiling_config[d.name] = config.ceiling.get(d.name, d.hi)
            probe_k = min(config.k_trials, budget.remaining)
            if probe_k > 0:
                budget.charge(probe_k)
                seed_start = budget.take_seed_block(probe_k)
                passes = 0
                for i in range(probe_k):
                    seed = seed_start + i
                    ok = evaluator(all_ceiling_config, seed)
                    if on_record is not None:
                        on_record(dict(all_ceiling_config), ok, seed)
                    if ok:
                        passes += 1
                all_ceiling_fails = passes * 2 <= probe_k
                if all_ceiling_fails:
                    suspected_dims = sorted(
                        d for d in state.no_boundary_dims if d in {s.name for s in config.dims}
                    )
                    state.notes.append(
                        "interaction-suspected: all single-dim sweeps passed but the "
                        "all-ceiling config failed"
                    )

        decompose_dims = found_boundary_dims or suspected_dims
        for i, dim_name in enumerate(decompose_dims):
            if budget.remaining <= 0:
                state.notes.append("budget exhausted before decompose stage completed")
                break
            spec = next(s for s in config.dims if s.name == dim_name)
            if dim_name in state.boundaries:
                failing_side_value = config.ceiling.get(dim_name, spec.hi)
            else:
                failing_side_value = config.ceiling.get(dim_name, spec.hi)
            progress(f"[decompose] {dim_name}")
            _decompose_dim(
                evaluator,
                config,
                spec,
                failing_side_value,
                state,
                budget,
                i,
                len(decompose_dims),
                on_record,
            )

    except _BudgetExhausted:
        state.notes.append(f"budget_exhausted: hard stop at {budget.spent}/{config.budget}")

    state.budget_spent = budget.spent
    return state


def resume_from_records(
    records: list[tuple[dict, bool, int]], config: SchedulerConfig
) -> CampaignState:
    """Rebuild a `CampaignState` from prior (config, passed, trial_seed) records.

    A record is treated as an explore/refine probe for dim `d` iff its
    config differs from `config.baseline` in exactly one key, `d`. Records
    that differ in zero or >1 keys (e.g. decompose-stage or all-ceiling
    probes) are not attributable to a single dim's point history and are
    skipped for `points`, but still count against `budget_spent` since they
    consumed real evaluator calls.

    `budget_spent` is simply `len(records)` (each record is exactly one
    trial). Combined with `run_campaign(initial_state=...)`, resuming with
    the same `seed_base` and continuing with a larger `budget` yields a
    strict superset of the probes a single fresh run at that budget would
    have made -- the already-collected points are kept, not re-probed.
    """
    state = CampaignState()
    dim_names = {d.name for d in config.dims}
    for cfg, passed, _seed in records:
        diffs = [
            k
            for k in dim_names
            if cfg.get(k, config.baseline.get(k, 0.0)) != config.baseline.get(k, 0.0)
        ]
        if len(diffs) == 1:
            dim = diffs[0]
            x = cfg[dim]
            pts = state.points.setdefault(dim, [])
            if (x, passed) not in pts:
                pts.append((x, passed))

    for dim, pts in state.points.items():
        if pts and all(p for _, p in pts):
            state.no_boundary_dims.add(dim)

    state.budget_spent = len(records)
    return state
