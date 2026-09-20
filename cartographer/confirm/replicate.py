"""Fresh-seed replication of one derived finding from `/findings`.

A derived finding (see `cartographer.viewer.findings`) is an observation
about already-recorded trials: "this model fails belief queries 37% of the
time in this region." That is evidence, not a confirmed result -- the
trials it summarizes were all drawn by one campaign, from one seed range,
and a depressed pass rate over a self-selected region is exactly the shape
of finding that regresses to the mean when you look again.

This module re-tests one finding's implicated configuration (and its
baseline) on seeds that campaign never touched, then hands the two arms to
`confirm_all` -- the SAME Wilson-interval + Holm-Bonferroni machinery
Phase 7 already uses for decompose hypotheses (see
`cartographer/confirm/confirmation.py`). Nothing statistical is
reimplemented here; this module is purely the glue that turns "a cell in
the capability map" into "a `FailureHypothesis`-shaped pair of configs plus
a live evaluator", and that names the run_ids the fresh trials land under.

Seed contract: `confirm_hypothesis` requires `seed_base >= 10_000` so the
fresh seeds are provably disjoint from search-time probing (`< 10_000`).
Campaign trials go through `cartographer.schedule.scheduler`, whose trial
seeds are small block indices, so the default `seed_base=10_000` inherited
from `confirm_all` keeps that guarantee here too.

Run-id convention: each replication writes TWO sibling top-level run_ids,
`repl-{model}-{kind}-{NNN}-failing` and `...-baseline`. Sibling (rather
than `parent/child`) because `cartographer.report.verdict._is_full_map_parent`
treats ANY `f"{run_id}/..."` child as a full_map stage, and a replication
is not a full_map battery. Both prefixes are excluded from the capability
map's own cells (a replication deliberately over-samples one hard config
and one easy one, so folding it into the campaign's pass rate would bias
the map in both directions at once) and read back ONLY as that finding's
replication status.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from ..decompose.hypothesis import FailureHypothesis, ProbeOutcome
from ..exec.client import Client
from ..exec.evaluator import PipelineEvaluator
from ..store.schema import Verdict
from ..store.store import ResultsStore
from .confirmation import confirm_all

REPLICATION_RUN_PREFIX = "repl-"
FAILING_ARM_SUFFIX = "-failing"
BASELINE_ARM_SUFFIX = "-baseline"

# Deliberately small: a replication is an on-demand action a cost-conscious
# human clicks, not a campaign. 5 trials x 2 fresh seed blocks per arm = 20
# API calls total, which at observed per-trial costs is a fraction of a US
# cent for every model in the store today.
REPLICATION_K_TRIALS = 5
REPLICATION_N_FRESH_SEEDS = 2

# Generator defaults (`cartographer.exec.evaluator._default_base`) expressed
# as a config dict -- the "easy" arm every finding is replicated against.
BASELINE_CONFIG: dict[str, float] = {
    "chain_length": 2.0,
    "scramble": 0.0,
    "coref_pressure": 0.0,
    "distractor_events": 2.0,
    "filler_events": 2.0,
}


def arm_run_ids(base_run_id: str) -> tuple[str, str]:
    """(failing_run_id, baseline_run_id) for a replication's base run_id."""
    return base_run_id + FAILING_ARM_SUFFIX, base_run_id + BASELINE_ARM_SUFFIX


def is_replication_run(run_id: str) -> bool:
    return run_id.startswith(REPLICATION_RUN_PREFIX)


def _model_short(model: str) -> str:
    short = model.split("/")[-1].lower()
    short = re.sub(r"[^a-z0-9]+", "-", short).strip("-")
    return short[:40] or "model"


def next_replication_run_id(store: ResultsStore, model: str, query_kind: str) -> str:
    """`repl-{model-short}-{kind}-{NNN}`, NNN incremented past any existing
    replication (either arm) for the same model+kind. Mirrors
    `cartographer.viewer.launch.next_run_id`'s convention so replication
    runs read like every other run_id in the store."""
    existing = {
        row["run_id"]
        for row in store._conn.execute("SELECT DISTINCT run_id FROM experiments")
    }
    base = f"{REPLICATION_RUN_PREFIX}{_model_short(model)}-{query_kind}"
    n = 1
    while any(f"{base}-{n:03d}{suffix}" in existing for suffix in (FAILING_ARM_SUFFIX, BASELINE_ARM_SUFFIX)):
        n += 1
    return f"{base}-{n:03d}"


# ---------------------------------------------------------------------------
# Cost estimation -- shown BEFORE the user commits to spending.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplicationEstimate:
    """What a replication will cost, derived from what this model's trials
    have ACTUALLY cost in this store -- not from a token guess.

    `mean_trial_cost_usd` is the mean `cost_usd` over every already-recorded
    trial for `model` (any run, any verdict: an ERROR still burned tokens).
    `n_observed` is how many trials that mean is over; when it is 0 the
    model has never been run here and `est_cost_usd` is None rather than a
    fabricated number.
    """

    model: str
    n_trials: int
    mean_trial_cost_usd: float | None
    n_observed: int
    est_cost_usd: float | None

    def human(self) -> str:
        if self.est_cost_usd is None:
            return (
                f"{self.n_trials} fresh-seed trials -- no prior trials for this "
                "model in this store, so there is no measured per-trial cost to "
                "estimate from"
            )
        return (
            f"~${self.est_cost_usd:.4f} "
            f"({self.n_trials} fresh-seed trials x ${self.mean_trial_cost_usd:.6f} "
            f"mean observed cost/trial over {self.n_observed:,} recorded trials)"
        )


def replication_trial_count(
    k_trials: int = REPLICATION_K_TRIALS, n_fresh_seeds: int = REPLICATION_N_FRESH_SEEDS
) -> int:
    """Total API calls a replication makes: both arms, all fresh seeds."""
    return 2 * k_trials * n_fresh_seeds


def estimate_replication_cost(
    store: ResultsStore,
    model: str,
    k_trials: int = REPLICATION_K_TRIALS,
    n_fresh_seeds: int = REPLICATION_N_FRESH_SEEDS,
) -> ReplicationEstimate:
    n_trials = replication_trial_count(k_trials, n_fresh_seeds)
    cur = store._conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0.0) AS total "
        "FROM experiments WHERE model_name = ?",
        (model,),
    )
    row = cur.fetchone()
    n_observed = int(row["n"]) if row is not None else 0
    total = float(row["total"]) if row is not None else 0.0
    if not n_observed:
        return ReplicationEstimate(model, n_trials, None, 0, None)
    mean = total / n_observed
    return ReplicationEstimate(model, n_trials, mean, n_observed, mean * n_trials)


# ---------------------------------------------------------------------------
# Running one replication.
# ---------------------------------------------------------------------------


@dataclass
class ReplicationOutcome:
    """Everything a caller needs to report what a replication concluded."""

    base_run_id: str
    failing_run_id: str
    baseline_run_id: str
    model: str
    query_kind: str
    confirmed: bool = False
    effect_size: float = 0.0
    raw_p: float = 1.0
    corrected_alpha_threshold: float = 0.05
    failing_config: dict = field(default_factory=dict)
    baseline_config: dict = field(default_factory=dict)
    n_trials_spent: int = 0
    spent_usd: float = 0.0
    cancelled: bool = False
    notes: str = ""


def _hypothesis_for(failing_config: dict, baseline_config: dict, query_kind: str) -> FailureHypothesis:
    """Wrap a (failing, baseline) config pair in the shape `confirm_all`
    already understands. `_extract_configs` reads `evidence[0].config` as
    the failing arm and `evidence[1].config` as the baseline arm, in that
    fixed order, for every hypothesis kind -- so a synthesized hypothesis
    with two zero-trial probes is a legitimate, non-hacky input: the probe
    records are what the configs CAME from, and here they came from stored
    campaign records rather than from a decompose search."""
    factors = tuple(
        sorted(
            k
            for k, v in failing_config.items()
            if abs(float(v) - float(baseline_config.get(k, 0.0))) > 1e-9
        )
    )
    return FailureHypothesis(
        kind=f"derived_finding:{query_kind}",
        factors=factors,
        necessary_factors=(),
        evidence=[
            ProbeOutcome(config=dict(failing_config), pass_rate=0.0, n_trials=0, trial_seeds=[]),
            ProbeOutcome(config=dict(baseline_config), pass_rate=1.0, n_trials=0, trial_seeds=[]),
        ],
        budget_spent=0,
        notes="configs recovered from stored campaign records, not a decompose search",
    )


def run_finding_replication(
    client: Client,
    model: str,
    store: ResultsStore,
    query_kind: str,
    failing_config: dict,
    baseline_config: dict | None = None,
    base_run_id: str | None = None,
    k_trials: int = REPLICATION_K_TRIALS,
    n_fresh_seeds: int = REPLICATION_N_FRESH_SEEDS,
    cost_cap: float = 0.05,
    max_tokens: int | None = None,
    cancel_event: threading.Event | None = None,
) -> ReplicationOutcome:
    """Re-test `failing_config` and its baseline on fresh seeds and return
    the corrected verdict.

    Every trial is persisted through the ordinary `PipelineEvaluator` ->
    `ResultsStore.append` path (one evaluator per arm, each bound to its own
    run_id), so the fresh evidence is as inspectable as campaign evidence:
    it shows up on /runs, /run/<id>, and /record/<id> with prompts and
    responses intact. `cost_cap` is split evenly between the two arms.
    """
    baseline_config = dict(baseline_config if baseline_config is not None else BASELINE_CONFIG)
    failing_config = dict(failing_config)
    base_run_id = base_run_id or next_replication_run_id(store, model, query_kind)
    failing_run_id, baseline_run_id = arm_run_ids(base_run_id)

    per_arm_cap = cost_cap / 2.0
    arms = {}
    for arm, run_id in (("failing", failing_run_id), ("baseline", baseline_run_id)):
        arms[arm] = PipelineEvaluator(
            client=client,
            model=model,
            store=store,
            run_id=run_id,
            query_kind=query_kind,
            cost_cap_usd=per_arm_cap,
            max_tokens=max_tokens,
            cancel_event=cancel_event,
        )

    def evaluator(config: dict, trial_seed: int) -> bool:
        # `confirm_hypothesis` passes back the exact dicts it was handed, so
        # identity-by-equality against the failing config is a safe router.
        which = "failing" if config == failing_config else "baseline"
        return arms[which](config, trial_seed)

    results = confirm_all(
        [_hypothesis_for(failing_config, baseline_config, query_kind)],
        evaluator,
        k_trials=k_trials,
        n_fresh_seeds=n_fresh_seeds,
    )
    result = results[0]
    spent = sum(ev.spent_usd for ev in arms.values())
    cancelled = any(ev.cancelled for ev in arms.values())
    capped = any(ev.capped for ev in arms.values())
    notes = result.notes
    if capped:
        notes += " [cost cap reached -- some trials recorded as ERROR without an API call]"
    if cancelled:
        notes += " [cancelled]"

    return ReplicationOutcome(
        base_run_id=base_run_id,
        failing_run_id=failing_run_id,
        baseline_run_id=baseline_run_id,
        model=model,
        query_kind=query_kind,
        confirmed=result.confirmed,
        effect_size=result.effect_size,
        raw_p=result.raw_p,
        corrected_alpha_threshold=result.corrected_alpha_threshold,
        failing_config=failing_config,
        baseline_config=baseline_config,
        n_trials_spent=result.n_trials_spent,
        spent_usd=spent,
        cancelled=cancelled,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Reading replication evidence back out of the store.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplicationEvidence:
    """What the store says about replications already run for one
    (model, query_kind). Derived at read time from the `repl-*` run_ids --
    no summary record, same rationale as `classify_verified_failure`."""

    n_runs: int
    failing_n: int
    failing_pass: int
    baseline_n: int
    baseline_pass: int
    latest_run_id: str | None

    @property
    def has_evidence(self) -> bool:
        return self.failing_n > 0

    @property
    def failing_rate(self) -> float | None:
        return self.failing_pass / self.failing_n if self.failing_n else None

    @property
    def baseline_rate(self) -> float | None:
        return self.baseline_pass / self.baseline_n if self.baseline_n else None

    @property
    def effect_size(self) -> float | None:
        if self.failing_rate is None or self.baseline_rate is None:
            return None
        return self.baseline_rate - self.failing_rate


def replication_evidence(store: ResultsStore, model: str, query_kind: str) -> ReplicationEvidence:
    """Aggregate every recorded replication arm for (model, query_kind).

    Scored trials only (PASS/FAIL) -- an ERROR arm trial burned tokens but
    says nothing about capability, exactly as everywhere else in this
    codebase.
    """
    prefix = f"{REPLICATION_RUN_PREFIX}{_model_short(model)}-{query_kind}-"
    cur = store._conn.execute(
        "SELECT run_id, verdict, COUNT(*) AS n FROM experiments "
        "WHERE model_name = ? AND run_id LIKE ? GROUP BY run_id, verdict",
        (model, prefix + "%"),
    )
    failing_n = failing_pass = baseline_n = baseline_pass = 0
    bases: set[str] = set()
    for row in cur.fetchall():
        run_id = row["run_id"]
        verdict = row["verdict"]
        n = int(row["n"])
        if verdict not in (Verdict.PASS.name, Verdict.FAIL.name):
            continue
        if run_id.endswith(FAILING_ARM_SUFFIX):
            bases.add(run_id[: -len(FAILING_ARM_SUFFIX)])
            failing_n += n
            failing_pass += n if verdict == Verdict.PASS.name else 0
        elif run_id.endswith(BASELINE_ARM_SUFFIX):
            bases.add(run_id[: -len(BASELINE_ARM_SUFFIX)])
            baseline_n += n
            baseline_pass += n if verdict == Verdict.PASS.name else 0
    return ReplicationEvidence(
        n_runs=len(bases),
        failing_n=failing_n,
        failing_pass=failing_pass,
        baseline_n=baseline_n,
        baseline_pass=baseline_pass,
        latest_run_id=max(bases) if bases else None,
    )
