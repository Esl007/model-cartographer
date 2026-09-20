"""/findings -- the capability map, derived live from the results store.

Two kinds of finding live on this page, and the split is the whole point:

**Derived findings** are computed from the store every time the page is
rendered. They answer "what can this model do, and where exactly does it
break" from the trials themselves, so they update when new trials land --
including the fresh-seed trials the page's own Replicate action produces.
Nothing here is transcribed by hand.

**Documented findings** are the curated claims from `docs/lab-log.md` that
CANNOT be derived from stored verdicts: a pronoun-ablation result, a
retracted renderer bug, a strategic comparison between two campaign
designs. Those are causal statements about WHY, established by ablation and
by reading responses, and no aggregate over pass/fail columns will ever
reproduce them. They stay, dated and run_id-tagged, in their own clearly
labelled section -- and each one's run_id is checked against the store at
render time, because several of this project's historical run_ids no longer
resolve (the store lost ~1,180 records to a git revert on 2026-09-19) and
rendering a dead citation as if it were live evidence would be dishonest.

Retracted findings are kept (not deleted) in a collapsed section: the
witness-rendering bug produced a real, later-retracted "false belief floor"
result, and showing that alongside the corrected numbers is a deliberate
demonstration of the instrument's self-auditing.

Arithmetic contract
-------------------
Every rate on this page uses the SAME denominator as
`cartographer.report.verdict`: SCORED trials only (PASS + FAIL), ERRORs
excluded (infra noise, not capability), and FAILs that
`cartographer.exec.evaluator.classify_all_verified_failures` classified as
resample `noise` excluded from both numerator and denominator. A previous
pass of this viewer shipped a KPI dividing by total trials, which put "16%"
directly above a verdict panel reading "92%" on the same screen; both
numbers were computed honestly and they still contradicted each other. One
denominator, one definition, reused -- not reimplemented.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field

from ..confirm.replicate import (
    BASELINE_CONFIG,
    ReplicationEvidence,
    estimate_replication_cost,
    is_replication_run,
    replication_evidence,
)
from ..exec.evaluator import VERIFY_SUFFIX, classify_all_verified_failures
from ..store.schema import Verdict
from ..store.store import ResultsStore, _row_to_record

# ---------------------------------------------------------------------------
# Thresholds. All three are deliberately explicit module constants rather
# than magic numbers buried in a comparison, because every derived claim on
# this page is only as trustworthy as the bar it had to clear, and a reader
# auditing the page needs to be able to find that bar.
# ---------------------------------------------------------------------------

# A STRENGTH needs both a high rate and enough scored trials that the rate
# means something. 20 scored trials at 95% has a Wilson lower bound around
# 0.76 -- weak evidence of "always passes", strong evidence of "not a weak
# spot", which is exactly the claim a strength makes here.
STRENGTH_PASS_RATE = 0.95
STRENGTH_MIN_N = 20

# A WEAKNESS is a cell whose pass rate is meaningfully depressed. 0.90
# matches `report/verdict.py`'s `_DIFFUSE_WEAKNESS_CEILING` so a cell called
# weak here is a cell the run verdict would also describe as weak -- the two
# surfaces must not disagree about what counts as a finding.
WEAKNESS_PASS_RATE = 0.90
WEAKNESS_MIN_N = 20

# Capability map columns.
CAPABILITY_KINDS = ("state", "order", "belief", "adversarial_belief", "tool_plan")
# The measured difficulty dimensions `cartographer.world.measure.measure`
# emits. A dim column reports the pass rate in that dim's HIGH region (top
# tercile of values observed across the whole store, so the column means the
# same thing on every row).
DIFFICULTY_DIMS = (
    "entity_state",
    "temporal",
    "coreference",
    "distractors",
    "context_length",
    "witness_gap",
)

# Which latent generator knob each measured dim is driven by -- used to turn
# "weak when coreference is high" into a config a replication can actually
# re-run.
DIM_TO_KNOB = {
    "entity_state": "chain_length",
    "temporal": "scramble",
    "coreference": "coref_pressure",
    "distractors": "distractor_events",
    "context_length": "filler_events",
    "witness_gap": "witness_cutoff",
}

_HEAT_STEPS = (
    # (exclusive upper bound, css class, plain-English label)
    (0.50, "heat-1", "under 50%"),
    (0.75, "heat-2", "50-75%"),
    (0.90, "heat-3", "75-90%"),
    (0.97, "heat-4", "90-97%"),
    (1.01, "heat-5", "97%+"),
)


def heat_class(pass_rate: float | None) -> str:
    """Map a pass rate onto the 5-step heat ramp (or the empty-cell state).

    The ramp runs verdict-pass green -> claim-belief amber -> salience ->
    verdict-fail-trap red: three tokens this product already uses to mean
    "correct", "provisional", and "a trap was sprung". It is not a rainbow
    and it introduces no new hue (docs/design-notes.md "Color world").
    """
    if pass_rate is None:
        return "heat-none"
    for upper, cls, _label in _HEAT_STEPS:
        if pass_rate < upper:
            return cls
    return "heat-5"


def short_model_name(model_name: str) -> str:
    name = model_name
    for prefix in ("nvidia/", "meta-llama/", "mistralai/", "anthropic/", "openai/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def model_slug(model_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model_name.lower()).strip("-") or "model"


def _query_kind_of(record) -> str | None:
    detail = record.verifier_detail or ""
    for part in detail.split(";"):
        if part.startswith("kind="):
            return part[len("kind="):] or None
    return None


def _traps_of(record) -> tuple[str, ...]:
    """Trap kinds the adversarial seed search planted in this record's
    world, parsed from `traps=` in `verifier_detail` (written by
    `PipelineEvaluator` in adversarial mode). Empty for non-adversarial
    trials -- which is most of them, and the page says so rather than
    implying trap data is missing."""
    detail = record.verifier_detail or ""
    for part in detail.split(";"):
        if part.startswith("traps="):
            raw = part[len("traps="):]
            return tuple(t for t in raw.split(",") if t)
    return ()


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimConcentration:
    """Where in one dimension a cell's failures pile up."""

    dim: str
    bucket: str  # "low" | "mid" | "high"
    lo: float
    hi: float
    n_scored: int
    n_pass: int
    pass_rate: float

    def range_text(self) -> str:
        if abs(self.lo - self.hi) < 1e-9:
            return f"{self.dim} = {_num(self.lo)}"
        return f"{self.dim} {_num(self.lo)}–{_num(self.hi)}"


@dataclass(frozen=True)
class Cell:
    """One (model, capability) square of the capability map."""

    model: str
    axis: str  # "kind" | "dim"
    key: str  # e.g. "belief" or "coreference"
    n_scored: int
    n_pass: int
    n_fail: int
    n_noise: int
    n_error: int
    n_stable: int
    n_unverified: int
    fail_record_ids: tuple = ()
    run_ids: tuple = ()

    @property
    def pass_rate(self) -> float | None:
        return self.n_pass / self.n_scored if self.n_scored else None

    @property
    def label(self) -> str:
        return self.key if self.axis == "kind" else f"{self.key} (high)"

    @property
    def href(self) -> str:
        return (
            f"/trials?model={_q(self.model)}&axis={self.axis}&key={_q(self.key)}"
        )

    @property
    def verification(self) -> str:
        """verified-stable / contains-noise / unverified, in that priority,
        plus `no-failures` for the degenerate case.

        A cell with zero failures is NOT "verified-stable": nothing was ever
        resampled, so claiming an audit happened would be the same kind of
        overstatement this page exists to avoid. It gets its own word, and
        `verification_detail` says why.
        """
        if self.n_noise:
            return "contains-noise"
        if self.n_unverified:
            return "unverified"
        if self.n_fail:
            return "verified-stable"
        return "no-failures"

    @property
    def verification_detail(self) -> str:
        total_fails = self.n_fail + self.n_noise
        if not total_fails:
            return "no failures recorded here -- nothing to re-sample"
        parts = []
        if self.n_stable:
            parts.append(f"{self.n_stable} survived resampling")
        if self.n_noise:
            parts.append(f"{self.n_noise} were sampling noise and are excluded")
        if self.n_unverified:
            parts.append(f"{self.n_unverified} not yet resample-checked")
        return f"{total_fails} failure(s): " + ", ".join(parts)

    @property
    def slug(self) -> str:
        return f"{model_slug(self.model)}-{self.axis}-{model_slug(self.key)}"


@dataclass(frozen=True)
class DerivedFinding:
    """A strength or a weakness, computed from a `Cell`."""

    cell: Cell
    kind: str  # "strength" | "weakness"
    headline: str
    concentrations: tuple = ()
    kind_breakdown: tuple = ()  # (query_kind, n_scored, n_pass) for dim cells
    traps: tuple = ()  # (trap_kind, count)
    examples: tuple = ()  # (record_id, one-line description)
    failing_config: dict = field(default_factory=dict)
    replication: ReplicationEvidence | None = None

    @property
    def replication_status(self) -> str:
        """not-replicated / replication-incomplete / replicated /
        replication-disagrees.

        `replication-incomplete` is not a theoretical case: a replication
        that is cancelled or hits its cost cap records the remaining trials
        as ERRORs with no API call, which can leave one arm with zero
        scored trials. Calling that "replication disagrees" would report a
        cancelled job as evidence against the finding.
        """
        r = self.replication
        if r is None or not r.has_evidence:
            return "not-replicated"
        if r.failing_n == 0 or r.baseline_n == 0:
            return "replication-incomplete"
        effect = r.effect_size
        if effect is not None and effect > 0.2:
            return "replicated"
        return "replication-disagrees"


@dataclass
class ModelFindings:
    model: str
    cells: dict = field(default_factory=dict)  # (axis, key) -> Cell
    strengths: list = field(default_factory=list)
    weaknesses: list = field(default_factory=list)
    n_trials: int = 0
    n_scored: int = 0
    n_pass: int = 0
    cost_usd: float = 0.0

    @property
    def pass_rate(self) -> float | None:
        return self.n_pass / self.n_scored if self.n_scored else None


@dataclass
class FindingsData:
    models: list = field(default_factory=list)  # ModelFindings, stable order
    dim_cuts: dict = field(default_factory=dict)  # dim -> (lo_cut, hi_cut)
    # dim -> the SMALLEST observed value that actually lands in the high
    # bucket. Not the same as `dim_cuts[dim][1]` whenever a dim's
    # distribution is degenerate: `distractors` is 2 for two thirds of the
    # store, so its terciles collapse to (2, 2) and `_bucket` -- which tests
    # `<= lo_cut` first -- puts every 2 in "low" and only >2 in "high". A
    # column header reading "distractors >= 2" would then be a lie about
    # what the column contains.
    dim_high_min: dict = field(default_factory=dict)
    n_trials: int = 0
    n_primary_trials: int = 0

    def by_model(self, model: str):
        for m in self.models:
            if m.model == model:
                return m
        return None


def _num(v: float) -> str:
    return f"{v:.0f}" if abs(v - round(v)) < 1e-9 else f"{v:.2f}"


def _q(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _primary_records(store: ResultsStore) -> list:
    """Every record that counts as primary campaign evidence.

    Excluded: `*/verify` resample trials (they exist to judge a FAIL, not to
    measure capability -- counting them would let a verified-noisy failure
    vote three more times) and `repl-*` replication arms (a replication
    deliberately over-samples one hard config and one easy one, so folding
    it in would bias the map in both directions at once; it is read back
    separately as a finding's replication status).
    """
    cur = store._conn.execute("SELECT * FROM experiments")
    out = []
    for row in cur.fetchall():
        record = _row_to_record(row)
        if record.run_id.endswith(VERIFY_SUFFIX) or is_replication_run(record.run_id):
            continue
        out.append(record)
    return out


def _verification_map(store: ResultsStore, records: list) -> dict:
    """record_id -> "stable" | "noise" | "unverified" for every FAIL.

    Reuses `classify_all_verified_failures` per run_id rather than
    reimplementing the resample arithmetic -- same function the run detail
    page and the verdict panel call.
    """
    run_ids = sorted({r.run_id for r in records if r.verdict == Verdict.FAIL})
    out: dict[str, str] = {}
    for run_id in run_ids:
        for record_id, fv in classify_all_verified_failures(store, run_id).items():
            out[record_id] = fv.status
    return out


def _dim_cuts(records: list) -> dict:
    """Global tercile cut points per measured dim.

    Global (not per-model) on purpose: the capability map is a comparison
    grid, and a "coreference high" column that means a different value range
    on every row would be a grid of incomparable numbers wearing the same
    header. Mirrors `report/verdict.py::_dim_region_rates`' bucketing rule
    so "high region" means the same thing on both surfaces.
    """
    cuts: dict[str, tuple[float, float]] = {}
    for dim in DIFFICULTY_DIMS:
        values = sorted(
            float(r.realized_dims[dim])
            for r in records
            if dim in r.realized_dims and r.verdict in (Verdict.PASS, Verdict.FAIL)
        )
        if len(values) < 4:
            continue
        n = len(values)
        cuts[dim] = (values[n // 3], values[(2 * n) // 3])
    return cuts


def _dim_high_mins(records: list, cuts: dict) -> dict:
    """dim -> smallest value that actually buckets as "high" (see
    `FindingsData.dim_high_min` for why this is not just `cuts[dim][1]`)."""
    out: dict[str, float] = {}
    for dim, cut in cuts.items():
        highs = [
            float(r.realized_dims[dim])
            for r in records
            if dim in r.realized_dims
            and r.verdict in (Verdict.PASS, Verdict.FAIL)
            and _bucket(float(r.realized_dims[dim]), cut) == "high"
        ]
        if highs:
            out[dim] = min(highs)
    return out


def _bucket(value: float, cut: tuple[float, float]) -> str:
    lo_cut, hi_cut = cut
    if value <= lo_cut:
        return "low"
    if value >= hi_cut:
        return "high"
    return "mid"


def _tally(records: list, verification: dict, model: str, axis: str, key: str) -> Cell:
    n_pass = n_fail = n_noise = n_error = n_stable = n_unverified = 0
    fail_ids: list[str] = []
    run_ids: list[str] = []
    for r in records:
        if r.run_id not in run_ids:
            run_ids.append(r.run_id)
        if r.verdict == Verdict.ERROR:
            n_error += 1
            continue
        if r.verdict == Verdict.PASS:
            n_pass += 1
            continue
        status = verification.get(r.record_id, "unverified")
        if status == "noise":
            n_noise += 1
            continue
        n_fail += 1
        fail_ids.append(r.record_id)
        if status == "stable":
            n_stable += 1
        else:
            n_unverified += 1
    return Cell(
        model=model,
        axis=axis,
        key=key,
        n_scored=n_pass + n_fail,
        n_pass=n_pass,
        n_fail=n_fail,
        n_noise=n_noise,
        n_error=n_error,
        n_stable=n_stable,
        n_unverified=n_unverified,
        fail_record_ids=tuple(fail_ids),
        run_ids=tuple(run_ids),
    )


def _cell_members(records: list, cuts: dict, axis: str, key: str) -> list:
    """The subset of `records` that belongs in cell (axis, key)."""
    if axis == "kind":
        return [r for r in records if _query_kind_of(r) == key]
    cut = cuts.get(key)
    if cut is None:
        return []
    return [
        r
        for r in records
        if key in r.realized_dims and _bucket(float(r.realized_dims[key]), cut) == "high"
    ]


def _concentrations(members: list, verification: dict, cuts: dict) -> list:
    """Per-dim low/mid/high pass rates for one cell's records, sorted worst
    first -- this is the "WHERE did it fail" answer: not just a rate, but
    which dimension and which value range the failures live in."""
    scored = [
        r
        for r in members
        if r.verdict in (Verdict.PASS, Verdict.FAIL)
        and not (r.verdict == Verdict.FAIL and verification.get(r.record_id) == "noise")
    ]
    out: list[DimConcentration] = []
    for dim in DIFFICULTY_DIMS:
        cut = cuts.get(dim)
        if cut is None:
            continue
        buckets: dict[str, list] = {"low": [], "mid": [], "high": []}
        for r in scored:
            if dim not in r.realized_dims:
                continue
            value = float(r.realized_dims[dim])
            buckets[_bucket(value, cut)].append((value, r.verdict == Verdict.PASS))
        for bucket_name, rows in buckets.items():
            if len(rows) < 4:
                continue
            values = [v for v, _ in rows]
            n_pass = sum(1 for _, passed in rows if passed)
            out.append(
                DimConcentration(
                    dim=dim,
                    bucket=bucket_name,
                    lo=min(values),
                    hi=max(values),
                    n_scored=len(rows),
                    n_pass=n_pass,
                    pass_rate=n_pass / len(rows),
                )
            )
    out.sort(key=lambda c: (c.pass_rate, -c.n_scored))
    return out


_MIN_CONFIG_TRIALS = 3


def _config_key(record) -> tuple:
    return tuple(sorted((k, float(v)) for k, v in record.latent_params.items()))


def _is_baseline_config(config: dict) -> bool:
    if not config:
        return False
    return all(
        abs(float(v) - float(BASELINE_CONFIG.get(k, 0.0))) < 1e-9 for k, v in config.items()
    )


def _failing_config(members: list, verification: dict) -> dict:
    """The exact latent config this cell does WORST at -- what a replication
    has to re-run on fresh seeds for the comparison to mean anything.

    Worst pass rate, not most failures: "where the most failures happened"
    is confounded by how many trials the scheduler happened to spend there,
    and on a store where the baseline is probed most often it reliably
    returns the baseline itself -- which would make the replication compare
    the baseline against the baseline and measure nothing but run-to-run
    variance. Configs identical to the generator baseline are therefore
    only used as a last resort, and configs with fewer than
    `_MIN_CONFIG_TRIALS` scored trials are ignored entirely.
    """
    tallies: dict[tuple, list] = {}
    for r in members:
        if r.verdict not in (Verdict.PASS, Verdict.FAIL):
            continue
        if r.verdict == Verdict.FAIL and verification.get(r.record_id) == "noise":
            continue
        slot = tallies.setdefault(_config_key(r), [0, 0, 0])
        slot[0] += 1
        slot[1] += 1 if r.verdict == Verdict.PASS else 0
        slot[2] += 1 if r.verdict == Verdict.FAIL else 0

    candidates = [
        (dict(key), counts)
        for key, counts in tallies.items()
        if counts[0] >= _MIN_CONFIG_TRIALS and counts[2] > 0
    ]
    if not candidates:
        return {}
    non_baseline = [c for c in candidates if not _is_baseline_config(c[0])]
    pool = non_baseline or candidates
    pool.sort(key=lambda t: (t[1][1] / t[1][0], -t[1][2]))
    return pool[0][0]


def _examples(members: list, verification: dict, limit: int = 3, config: dict | None = None) -> tuple:
    """Up to `limit` concrete failures, preferring ones drawn from `config`
    (the cell's worst configuration) so the examples illustrate the same
    "where" the finding names rather than an unrelated slice."""
    fails = [
        r
        for r in members
        if r.verdict == Verdict.FAIL and verification.get(r.record_id) != "noise"
    ]
    if config:
        target = tuple(sorted((k, float(v)) for k, v in config.items()))
        fails.sort(key=lambda r: 0 if _config_key(r) == target else 1)
    out = []
    for r in fails:
        if len(out) >= limit:
            break
        dims = ", ".join(
            f"{d}={_num(float(r.realized_dims[d]))}"
            for d in DIFFICULTY_DIMS
            if d in r.realized_dims
        )
        answer = (r.response_text or "").strip().replace("\n", " ")
        if len(answer) > 70:
            answer = answer[:67] + "..."
        desc = dims or "no realized dims recorded"
        if answer:
            desc += f" — answered “{answer}”"
        out.append((r.record_id, desc))
    return tuple(out)


def _weakness_headline(cell: Cell, concentrations: list) -> str:
    rate = cell.pass_rate or 0.0
    what = (
        f"{cell.key} queries"
        if cell.axis == "kind"
        else f"trials in the high-{cell.key} region"
    )
    head = (
        f"{short_model_name(cell.model)} fails {what}: "
        f"{rate * 100:.0f}% pass ({cell.n_pass}/{cell.n_scored} scored, "
        f"{cell.n_fail} real failures)"
    )
    worst = concentrations[0] if concentrations else None
    if worst is not None and worst.pass_rate < rate - 0.02:
        head += (
            f" — concentrated at {worst.range_text()}, "
            f"where it drops to {worst.pass_rate * 100:.0f}% "
            f"({worst.n_pass}/{worst.n_scored})"
        )
    return head


def _strength_headline(cell: Cell) -> str:
    rate = cell.pass_rate or 0.0
    what = (
        f"{cell.key} queries"
        if cell.axis == "kind"
        else f"the high-{cell.key} region"
    )
    if cell.n_fail == 0:
        return (
            f"{short_model_name(cell.model)} passes {what} cleanly: "
            f"{cell.n_pass}/{cell.n_scored}, no failures at all"
        )
    return (
        f"{short_model_name(cell.model)} handles {what}: "
        f"{rate * 100:.0f}% pass ({cell.n_pass}/{cell.n_scored} scored)"
    )


def derive(store: ResultsStore) -> FindingsData:
    """Compute the whole capability map + every derived finding, from the
    store only. Pure read; opens no connection of its own."""
    records = _primary_records(store)
    verification = _verification_map(store, records)
    cuts = _dim_cuts(records)
    high_mins = _dim_high_mins(records, cuts)

    by_model: dict[str, list] = {}
    for r in records:
        by_model.setdefault(r.model_name, []).append(r)

    models: list[ModelFindings] = []
    for model in sorted(by_model):
        model_records = by_model[model]
        mf = ModelFindings(model=model)
        mf.n_trials = len(model_records)
        mf.cost_usd = sum(r.cost_usd for r in model_records)
        overall = _tally(model_records, verification, model, "all", "all")
        mf.n_scored = overall.n_scored
        mf.n_pass = overall.n_pass

        for axis, keys in (("kind", CAPABILITY_KINDS), ("dim", DIFFICULTY_DIMS)):
            for key in keys:
                members = _cell_members(model_records, cuts, axis, key)
                cell = _tally(members, verification, model, axis, key)
                mf.cells[(axis, key)] = cell
                rate = cell.pass_rate
                if rate is None or cell.n_scored < WEAKNESS_MIN_N:
                    continue
                if rate >= STRENGTH_PASS_RATE and cell.n_scored >= STRENGTH_MIN_N:
                    mf.strengths.append(
                        DerivedFinding(
                            cell=cell,
                            kind="strength",
                            headline=_strength_headline(cell),
                            concentrations=tuple(
                                _concentrations(members, verification, cuts)[:2]
                            ),
                        )
                    )
                elif rate < WEAKNESS_PASS_RATE:
                    concentrations = _concentrations(members, verification, cuts)
                    traps: Counter = Counter()
                    for r in members:
                        if r.verdict == Verdict.FAIL and verification.get(r.record_id) != "noise":
                            traps.update(_traps_of(r))
                    kind_breakdown = []
                    if axis == "dim":
                        per_kind: dict[str, list[int]] = {}
                        for r in members:
                            if r.verdict not in (Verdict.PASS, Verdict.FAIL):
                                continue
                            if r.verdict == Verdict.FAIL and verification.get(r.record_id) == "noise":
                                continue
                            k = _query_kind_of(r) or "unknown"
                            slot = per_kind.setdefault(k, [0, 0])
                            slot[0] += 1
                            slot[1] += 1 if r.verdict == Verdict.PASS else 0
                        kind_breakdown = [
                            (k, v[0], v[1]) for k, v in sorted(per_kind.items())
                        ]
                    query_kind = key if axis == "kind" else (
                        kind_breakdown[0][0] if kind_breakdown else "belief"
                    )
                    failing_config = _failing_config(members, verification)
                    mf.weaknesses.append(
                        DerivedFinding(
                            cell=cell,
                            kind="weakness",
                            headline=_weakness_headline(cell, concentrations),
                            concentrations=tuple(concentrations[:3]),
                            kind_breakdown=tuple(kind_breakdown),
                            traps=tuple(traps.most_common()),
                            examples=_examples(members, verification, config=failing_config),
                            failing_config=failing_config,
                            replication=replication_evidence(store, model, query_kind),
                        )
                    )

        mf.strengths.sort(key=lambda f: (-(f.cell.pass_rate or 0.0), -f.cell.n_scored))
        mf.weaknesses.sort(key=lambda f: (f.cell.pass_rate or 0.0, -f.cell.n_scored))
        mf.weaknesses = _prune_dim_weaknesses(mf)
        models.append(mf)

    return FindingsData(
        models=models,
        dim_cuts=cuts,
        dim_high_min=high_mins,
        n_trials=len(records),
        n_primary_trials=len(records),
    )


# A dim-region weakness only earns its own card if the region genuinely
# LOCALIZES the failure -- at least this many points below the model's own
# overall scored pass rate. Without this, a model that is simply bad at
# belief queries produces six near-identical cards (one per dimension, all
# restating the same 50%) and buries the one finding that says something.
# The rejected cells are not hidden: every one of them is still a square in
# the capability map, still clickable, still counted.
_DIM_LOCALIZATION_MARGIN = 0.05
_MAX_DIM_WEAKNESSES = 2


def _prune_dim_weaknesses(mf: ModelFindings) -> list:
    overall = mf.pass_rate
    kept = [f for f in mf.weaknesses if f.cell.axis == "kind"]
    dim_findings = [f for f in mf.weaknesses if f.cell.axis == "dim"]
    if overall is not None:
        dim_findings = [
            f
            for f in dim_findings
            if (f.cell.pass_rate or 0.0) <= overall - _DIM_LOCALIZATION_MARGIN
        ]
    kept.extend(dim_findings[:_MAX_DIM_WEAKNESSES])
    kept.sort(key=lambda f: (f.cell.pass_rate or 0.0, -f.cell.n_scored))
    return kept


def cell_trials(store: ResultsStore, model: str, axis: str, key: str) -> tuple:
    """(Cell, [records]) behind one capability-map square.

    Shared with `/trials` so the drill-down page's counts are computed by
    the same code that painted the square -- a filtered view that disagreed
    with the cell it came from would be worse than no drill-down at all.
    """
    records = [r for r in _primary_records(store) if r.model_name == model]
    verification = _verification_map(store, records)
    cuts = _dim_cuts(_primary_records(store))
    if axis == "all":
        members = records
    else:
        members = _cell_members(records, cuts, axis, key)
    return _tally(members, verification, model, axis, key), members, verification


_TRIALS_LIMIT = 200


def capability_trials_html(store: ResultsStore, params: dict) -> str:
    """`/trials?model=...&axis=kind|dim&key=...` -- every trial behind one
    square of the capability map.

    Shares `cell_trials` with the map itself, so the cell's "34%  n=214" and
    the list it opens are the same computation run twice, not two
    computations that happen to agree today. (`/trials` is also the atlas's
    drill-down target with a different parameter vocabulary; `serve.py`
    routes on the presence of `axis`.)
    """
    from .serve import (
        _chip,
        _chip_set,
        _claim_strip,
        _parse_verifier_detail,
        _rate_bar,
        _shell,
        _short_answer,
        _trial_count,
    )

    model = params.get("model", "")
    axis = params.get("axis", "")
    key = params.get("key", "")
    breadcrumb = f'<a href="/findings">Findings</a><span class="sep">/</span>trials'

    if not model or axis not in ("kind", "dim", "all"):
        body = (
            '<div class="page-head"><div class="page-head-main"><h1>Trials</h1>'
            '<p class="subtitle">That capability cell does not exist.</p></div></div>'
            '<div class="empty-state"><p>No such selection.</p>'
            '<p class="meta"><a href="/findings">Back to the capability map &rarr;</a></p></div>'
        )
        return _shell("Trials", "findings", body, breadcrumb=breadcrumb, trial_count=_trial_count(store))

    cell, members, verification = cell_trials(store, model, axis, key)
    rate = (cell.pass_rate or 0.0) * 100.0

    rows = []
    for r in members[:_TRIALS_LIMIT]:
        detail = _parse_verifier_detail(r.verifier_detail)
        answer = _short_answer(r.response_text, max_chars=56)
        if r.verdict == Verdict.PASS:
            evidence = f'answered <b>"{html.escape(answer)}"</b>' if answer else "matched ground truth"
        elif r.verdict == Verdict.FAIL:
            evidence = f'answered <b>"{html.escape(answer)}"</b>' if answer else None
        else:
            evidence = html.escape(detail.get("error", "") or r.finish_reason or "infra failure")
        stability = (
            verification.get(r.record_id, "unverified") if r.verdict == Verdict.FAIL else None
        )
        sev = {"FAIL": "row-sev row-sev-fail", "ERROR": "row-sev row-sev-error"}.get(
            r.verdict.name, ""
        )
        dims = " ".join(
            _chip(f"{d}={_num(float(r.realized_dims[d]))}", "chip-muted")
            for d in DIFFICULTY_DIMS
            if d in r.realized_dims
        )
        rows.append(
            f'<tr class="{sev}">'
            f'<td>{_chip(detail.get("kind", "") or "?", "chip-muted")}</td>'
            f'<td class="claim-cell">{_claim_strip(r.verdict.name, evidence, stability=stability)}</td>'
            f'<td><span class="chip-set">{dims}</span></td>'
            f"<td>{_chip_set(detail.get('traps', ''), 'chip-trap')}</td>"
            f'<td class="mono"><a href="/run/{html.escape(r.run_id)}">{html.escape(r.run_id)}</a></td>'
            f'<td><a href="/record/{html.escape(r.record_id)}">transcript &rarr;</a></td>'
            "</tr>"
        )

    truncated = (
        f'<p class="meta">Showing the first {_TRIALS_LIMIT} of {len(members):,} matching trials.</p>'
        if len(members) > _TRIALS_LIMIT
        else ""
    )
    if not rows:
        rows_html = (
            '<tbody><tr><td colspan="6"><span class="meta">No trials in this '
            "capability cell -- this model has not been probed here.</span></td></tr></tbody>"
        )
    else:
        rows_html = f"<tbody>{''.join(rows)}</tbody>"

    body = (
        '<div class="page-head"><div class="page-head-main">'
        f"<h1>Trials &middot; {html.escape(cell.label)}</h1>"
        f'<p class="subtitle">Every trial behind one square of the capability map: '
        f"{html.escape(short_model_name(model))} on {html.escape(cell.label)}.</p>"
        f'<p class="meta">{_chip(short_model_name(model), "chip-muted")} '
        f'{_chip(cell.label, "chip-muted")} '
        f'{_chip(cell.verification, "chip-muted")}</p>'
        "</div><div class=\"page-head-actions\">"
        f'<a href="/findings?model={_q(model)}">&larr; back to this model\'s findings</a>'
        "</div></div>"
        '<div class="stat-grid">'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{len(members):,}</div>'
        f'<div class="stat-delta">{cell.n_error:,} errors</div></div>'
        '<div class="stat-label">Trials in this cell</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{rate:.0f}%</div>'
        f'<div class="stat-delta">{cell.n_pass:,}/{cell.n_scored:,} scored</div></div>'
        '<div class="stat-label">Pass rate</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{cell.n_fail:,}</div>'
        f'<div class="stat-delta">{cell.n_noise} noise excluded</div></div>'
        '<div class="stat-label">Real failures</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">'
        f'${sum(r.cost_usd for r in members):.4f}</div></div>'
        '<div class="stat-label">Cell cost</div></div>'
        "</div>"
        '<div class="card"><div class="card-head">'
        '<span class="card-title">Trials</span>'
        f'<span class="card-meta">{_rate_bar(rate, sub=f"{cell.n_pass}/{cell.n_scored}")}</span>'
        "</div>"
        '<table><thead><tr><th>kind</th><th>verdict</th><th>realized dims</th>'
        "<th>seeded traps</th><th>run</th><th></th></tr></thead>"
        f"{rows_html}</table></div>" + truncated
    )
    return _shell(
        f"Trials {cell.label}", "findings", body, breadcrumb=breadcrumb, trial_count=_trial_count(store)
    )


# ---------------------------------------------------------------------------
# Curated findings (NOT derivable -- see the module docstring).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    name: str
    mechanism: str
    evidence: str
    status: str  # "confirmed" | "needs-replication" | "retracted"
    date: str
    run_ids: tuple[str, ...]


FINDINGS: tuple[Finding, ...] = (
    Finding(
        name="Belief-to-reality collapse (witness-rendering bug, corrected)",
        mechanism=(
            "Once non-actor witnesses are actually rendered into prose, "
            "Nemotron Super shows NO belief/reality collapse: it tracks "
            "false beliefs correctly across chain_length, scramble, "
            "coref_pressure, and distractor_events."
        ),
        evidence=(
            "camp-belief-nemotron-super-002: 105 calls, baseline 3/3, no "
            "boundary found on any of the four swept dims."
        ),
        status="confirmed",
        date="2026-09-17",
        run_ids=("camp-belief-nemotron-super-002",),
    ),
    Finding(
        name="Nano continuity-of-observation over-generalization",
        mechanism=(
            "Nemotron Nano assumes an entity keeps observing an object's "
            "moves even after the story stops placing that entity at the "
            "scene -- it over-extends 'last witnessed' into 'still "
            "watching', producing a false-belief error the true reasoning "
            "chain doesn't support."
        ),
        evidence=(
            "camp-belief-nemotron-nano-001: 105 calls, 93% pass, 7 real "
            "failures verified against regenerated ground truth -- the "
            "first confirmed tier discrimination between Super and Nano."
        ),
        status="confirmed",
        date="2026-09-17",
        run_ids=("camp-belief-nemotron-nano-001",),
    ),
    Finding(
        name="witness_cutoff generalizes the Nano failure mode",
        mechanism=(
            "Sweeping witness_cutoff (how early an entity's last witnessed "
            "exposure to an object is cut off from the object's later "
            "moves) reproduces the same over-generalization systematically: "
            "at witness_cutoff=0.7, belief/truth divergence hits 94%."
        ),
        evidence="docs/lab-log.md 2026-09-17 entry, commit 313e3b2.",
        status="confirmed",
        date="2026-09-17",
        run_ids=(),
    ),
    Finding(
        name="Ultra protagonist-continuity pronoun bias",
        mechanism=(
            "Ultra misresolves pronouns toward the story's protagonist "
            "specifically -- an ablation that swaps pronoun/name surface "
            "form while holding the answer key fixed separates this "
            "cleanly from ordinary coreference noise in 6/6 ablated cases."
        ),
        evidence=(
            "5 Super belief failures verified real against regenerated "
            "ground truth: 4 were pronoun misresolution, 1 was a "
            "continuity over-generalization (first recorded Super "
            "failures) -- ablation catalog run gave 6/6 separation."
        ),
        status="confirmed",
        date="2026-09-17",
        run_ids=(),
    ),
    Finding(
        name="Ultra abstention under trap pressure",
        mechanism=(
            "Under adversarial (foil-selected) belief queries at high trap "
            "density, Ultra sometimes abstains/answers 'unknown' rather "
            "than committing to a wrong-but-plausible foil answer -- 2 "
            "stable cases so far, not yet replicated on fresh seeds."
        ),
        evidence=(
            "2 stable failures observed during adversarial_belief probing; "
            "flagged explicitly as unreplicated in docs/lab-log.md."
        ),
        status="needs-replication",
        date="2026-09-17",
        run_ids=(),
    ),
    Finding(
        name="Trap-selection breaks Ultra where magnitude escalation didn't",
        mechanism=(
            "Escalating every difficulty dim to its generator ceiling "
            "(chain_length 25, scramble 1.0, coref_pressure 8, "
            "distractor_events 40, filler_events 60, witness_cutoff 0.9 "
            "simultaneously) never broke Ultra. Adversarial seed search "
            "at a much shorter chain_length=2 -- picking the single world "
            "among many candidates where a plausible-but-wrong foil answer "
            "most directly competes with the truth -- did break it."
        ),
        evidence=(
            "run_breaking_point_campaign (escalation ray, t up to 1.0): "
            "no breaking point found. run_adversarial_belief_campaign at "
            "chain_length=2: confirmed failures. This is the strategic "
            "headline result: difficulty magnitude and trap salience are "
            "different axes, and only the second one moved Ultra."
        ),
        status="confirmed",
        date="2026-09-17",
        run_ids=(),
    ),
)

RETRACTED: tuple[Finding, ...] = (
    Finding(
        name="[RETRACTED] Nemotron Super has an intrinsic false-belief floor",
        mechanism=(
            "First read of camp-belief-nemotron-super-001 (baseline only "
            "3/5) looked like a real ~60%% capability ceiling on false "
            "belief tracking, independent of surface difficulty."
        ),
        evidence=(
            "Root cause was a renderer bug, not a model limitation: "
            "cartographer/world/simulate.py assigned beliefs to every "
            "witness, but cartographer/render/templates.py only ever "
            "interpolated the acting entity into prose -- non-actor "
            "witnesses were silently never mentioned in the story the "
            "model actually read. The model was correctly saying "
            "'unknown' about entities it was never told were present. "
            "Fixed in commit 5bd297f; corrected re-run "
            "(camp-belief-nemotron-super-002) reversed the result "
            "completely -- see the corrected finding above."
        ),
        status="retracted",
        date="2026-09-17",
        run_ids=("camp-belief-nemotron-super-001",),
    ),
)


def resolve_run_ids(store: ResultsStore, run_ids: tuple) -> dict:
    """run_id -> True if the store still holds records for it.

    ~1,180 records were destroyed by a git revert on 2026-09-19, so several
    historical run_ids cited in docs/lab-log.md no longer resolve. Rendering
    a dead citation as a working link would silently overstate the evidence
    still on hand.
    """
    out = {}
    for run_id in run_ids:
        cur = store._conn.execute(
            "SELECT 1 FROM experiments WHERE run_id = ? OR run_id LIKE ? LIMIT 1",
            (run_id, f"{run_id}/%"),
        )
        out[run_id] = cur.fetchone() is not None
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _status_pill(status: str) -> str:
    """A finding's trust level -- confirmed / needs-replication / retracted
    -- is a DISTINCT concept from a trial verdict (PASS/FAIL/ERROR), so it
    gets its own `.status-tag` treatment rather than reusing the claim
    strip's verdict marks: this is a statement about how much to trust a
    written claim while reading dense prose, not a per-trial outcome to
    scan in a table column (see docs/design-notes.md "The signature")."""
    label = status.replace("-", " ")
    return f'<span class="status-tag {html.escape(status)}">{html.escape(label)}</span>'


def _verification_pill(state: str) -> str:
    return f'<span class="status-tag verif-{html.escape(state)}">{html.escape(state.replace("-", " "))}</span>'


def _finding_card(f: Finding, resolved: dict) -> str:
    parts = []
    for run_id in f.run_ids:
        safe = html.escape(run_id)
        if resolved.get(run_id):
            parts.append(f'<a class="mono" href="/run/{safe}">{safe}</a>')
        else:
            parts.append(
                f'<span class="mono dangling">{safe}</span> '
                '<span class="meta">(evidence no longer in store)</span>'
            )
    run_ids_html = " &middot; ".join(parts) if parts else "-"
    return f"""
    <div class="card">
      <div class="card-head">
        <span class="card-title card-title-name">{html.escape(f.name)}</span>
        <span class="card-meta">{_status_pill(f.status)}</span>
      </div>
      <div class="card-body">
        <p style="margin:0 0 0.4rem;">{html.escape(f.mechanism)}</p>
        <p class="meta" style="margin:0;">evidence: {html.escape(f.evidence)}</p>
        <p class="meta" style="margin:0.3rem 0 0;">date: {html.escape(f.date)} &middot; run_id(s): {run_ids_html}</p>
      </div>
    </div>
    """


def _cell_html(cell: Cell) -> str:
    rate = cell.pass_rate
    cls = heat_class(rate)
    if rate is None:
        return (
            f'<td class="cap-cell"><span class="cap-block {cls}" '
            'title="no scored trials">'
            '<span class="cap-rate">&mdash;</span>'
            '<span class="cap-n">n=0</span></span></td>'
        )
    title = (
        f"{short_model_name(cell.model)} / {cell.label}: "
        f"{cell.n_pass}/{cell.n_scored} scored trials passed "
        f"({cell.n_fail} real failures, {cell.n_noise} noise-excluded, "
        f"{cell.n_error} errors excluded)"
    )
    return (
        f'<td class="cap-cell"><a class="cap-block {cls}" href="{cell.href}" '
        f'title="{html.escape(title)}">'
        f'<span class="cap-rate">{rate * 100:.0f}%</span>'
        f'<span class="cap-n">n={cell.n_scored}</span></a></td>'
    )


def _capability_map_html(data: FindingsData, selected: str | None) -> str:
    if not data.models:
        return (
            '<div class="empty-state"><p>No trials in this store yet.</p>'
            '<p class="meta"><a href="/launch">Launch a campaign to build a '
            'capability map &rarr;</a></p></div>'
        )

    dim_headers = []
    for dim in DIFFICULTY_DIMS:
        cut = data.dim_cuts.get(dim)
        high_min = data.dim_high_min.get(dim)
        if cut is None:
            hint = f"{dim}: not probed in this store"
        elif high_min is None:
            hint = f"{dim}: no trial in this store lands in the high tercile"
        else:
            hint = f"high region: {dim} >= {_num(high_min)} (top tercile over the whole store)"
        dim_headers.append(
            f'<th class="num" title="{html.escape(hint)}">{html.escape(dim)}<span class="cap-arrow">&uarr;</span></th>'
        )

    rows = []
    for mf in data.models:
        selected_row = selected is not None and mf.model == selected
        cells = "".join(
            _cell_html(mf.cells[(axis, key)])
            for axis, keys in (("kind", CAPABILITY_KINDS), ("dim", DIFFICULTY_DIMS))
            for key in keys
        )
        rate = mf.pass_rate
        overall = f"{rate * 100:.0f}%" if rate is not None else "&mdash;"
        rows.append(
            f'<tr class="cap-row{" cap-row-selected" if selected_row else ""}" '
            f'data-model="{html.escape(mf.model)}">'
            f'<th class="cap-model"><a href="/findings?model={_q(mf.model)}">'
            f'{html.escape(short_model_name(mf.model))}</a>'
            f'<span class="cap-model-meta">{overall} overall &middot; {mf.n_trials:,} trials</span></th>'
            f"{cells}</tr>"
        )

    legend = "".join(
        f'<span class="cap-legend-item"><span class="cap-swatch {cls}"></span>{html.escape(label)}</span>'
        for _upper, cls, label in _HEAT_STEPS
    )
    legend += '<span class="cap-legend-item"><span class="cap-swatch heat-none"></span>no data</span>'

    n_cells = len(data.models) * (len(CAPABILITY_KINDS) + len(DIFFICULTY_DIMS))
    return f"""
    <div class="card" id="capability-map">
      <div class="card-head">
        <span class="card-title">Capability map</span>
        <span class="card-meta">{len(data.models)} models &times; {len(CAPABILITY_KINDS) + len(DIFFICULTY_DIMS)} capabilities = {n_cells} cells</span>
      </div>
      <div class="card-body">
        <p class="meta" style="margin-top:0;">Pass rate over SCORED trials
        (PASS+FAIL; infra ERRORs and resample-noise failures excluded, same
        denominator as every run's verdict panel). Every cell is a link into
        the trials behind it. Dimension columns report the HIGH region only
        -- the top tercile of values observed across this whole store, so a
        column means the same thing on every row.</p>
        <div class="cap-legend">{legend}</div>
        <div class="cap-map-wrap">
        <table class="cap-map">
          <thead>
            <tr>
              <th rowspan="2" class="cap-corner">model</th>
              <th colspan="{len(CAPABILITY_KINDS)}" class="cap-group">query kind</th>
              <th colspan="{len(DIFFICULTY_DIMS)}" class="cap-group">difficulty region (high tercile)</th>
            </tr>
            <tr>
              {"".join(f'<th class="num">{html.escape(k)}</th>' for k in CAPABILITY_KINDS)}
              {"".join(dim_headers)}
            </tr>
          </thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        </div>
      </div>
    </div>
    """


def _comparison_html(data: FindingsData) -> str:
    """Model-vs-model: for each capability, who is best, who is worst, and
    how wide the gap is. The grid shows every number; this card answers the
    question the grid makes you scan for."""
    rows = []
    for axis, keys in (("kind", CAPABILITY_KINDS), ("dim", DIFFICULTY_DIMS)):
        for key in keys:
            scored = [
                (mf.model, mf.cells[(axis, key)])
                for mf in data.models
                if mf.cells[(axis, key)].n_scored >= WEAKNESS_MIN_N
            ]
            if len(scored) < 2:
                continue
            scored.sort(key=lambda t: t[1].pass_rate or 0.0)
            worst_model, worst = scored[0]
            best_model, best = scored[-1]
            spread = (best.pass_rate or 0.0) - (worst.pass_rate or 0.0)
            label = key if axis == "kind" else f"{key} (high)"
            rows.append(
                "<tr>"
                f"<td>{html.escape(label)}</td>"
                f'<td class="mono">{html.escape(short_model_name(best_model))}</td>'
                f'<td class="num">{(best.pass_rate or 0) * 100:.0f}% <span class="meta">n={best.n_scored}</span></td>'
                f'<td class="mono">{html.escape(short_model_name(worst_model))}</td>'
                f'<td class="num">{(worst.pass_rate or 0) * 100:.0f}% <span class="meta">n={worst.n_scored}</span></td>'
                f'<td class="num">{spread * 100:.0f} pts</td>'
                "</tr>"
            )
    if not rows:
        return (
            '<div class="card"><div class="card-head">'
            '<span class="card-title">Model vs model</span>'
            '<span class="card-meta">not enough overlap</span></div>'
            '<div class="card-body"><p class="meta">No capability has two or more '
            f"models with at least {WEAKNESS_MIN_N} scored trials, so there is "
            "nothing honest to compare yet.</p></div></div>"
        )
    return (
        '<div class="card"><div class="card-head">'
        '<span class="card-title">Model vs model</span>'
        f'<span class="card-meta">{len(rows)} contested capabilities</span></div>'
        "<table><thead><tr><th>capability</th><th>strongest</th>"
        '<th class="num">rate</th><th>weakest</th><th class="num">rate</th>'
        '<th class="num">spread</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _replicate_form_html(finding: DerivedFinding, store: ResultsStore) -> str:
    cell = finding.cell
    config = finding.failing_config or dict(BASELINE_CONFIG)
    query_kind = cell.key if cell.axis == "kind" else (
        finding.kind_breakdown[0][0] if finding.kind_breakdown else "belief"
    )
    estimate = estimate_replication_cost(store, cell.model)
    config_text = ", ".join(f"{k}={_num(v)}" for k, v in sorted(config.items())) or "generator defaults"
    config_fields = "".join(
        f'<input type="hidden" name="cfg_{html.escape(k)}" value="{_num(v)}">'
        for k, v in sorted(config.items())
    )
    cap_default = max(0.02, round((estimate.est_cost_usd or 0.0) * 4, 4))
    return f"""
    <details class="replicate">
      <summary>Replicate on fresh seeds
        <span class="meta">({html.escape(estimate.human())})</span>
      </summary>
      <p class="meta" style="margin-top:0.4rem;">Re-runs
      <span class="mono">{html.escape(config_text)}</span> and the generator
      baseline on seeds no campaign has used, then scores the pair with
      Wilson 95% intervals and Holm-Bonferroni correction
      (<span class="mono">cartographer/confirm/confirmation.py</span>). The
      fresh trials are appended to the store under a
      <span class="mono">repl-</span> run_id, so this finding's replication
      status updates the next time this page loads.</p>
      <form method="post" action="/replicate">
        <input type="hidden" name="model" value="{html.escape(cell.model)}">
        <input type="hidden" name="query_kind" value="{html.escape(query_kind)}">
        <input type="hidden" name="axis" value="{html.escape(cell.axis)}">
        <input type="hidden" name="key" value="{html.escape(cell.key)}">
        {config_fields}
        <p><label>Provider:
          <select name="provider">{_provider_options()}</select></label></p>
        <p><label>API key:
          <input type="password" name="api_key" value="" autocomplete="off"></label>
          <span class="meta">leave empty to use env/ key file</span></p>
        <p><label>Model id:
          <input name="api_model" value="{html.escape(cell.model)}"></label></p>
        <p><label>Cost cap (USD):
          <input name="cost_cap" value="{cap_default}"></label>
          <span class="meta">hard ceiling; trials stop being sent once reached</span></p>
        <p><button type="submit">Replicate this finding</button></p>
      </form>
    </details>
    """


def _provider_options() -> str:
    from .launch import _provider_options as opts

    return opts()


def _replication_line(finding: DerivedFinding) -> str:
    r = finding.replication
    status = finding.replication_status
    if r is None or not r.has_evidence:
        return (
            '<p class="meta" style="margin:0.3rem 0 0;">replication: '
            "<span class=\"status-tag repl-not-replicated\">not replicated</span> "
            "no fresh-seed trials for this finding yet.</p>"
        )
    effect = r.effect_size
    effect_text = f"{effect * 100:.0f} pts" if effect is not None else "n/a"
    word = status.replace("-", " ")
    tail = ""
    if status == "replication-incomplete":
        tail = (
            " One arm has no scored trials -- the job was cancelled or hit "
            "its cost cap before it could finish, so this is not evidence "
            "either way."
        )
    return (
        '<p class="meta" style="margin:0.3rem 0 0;">replication: '
        f'<span class="status-tag repl-{html.escape(status)}">{html.escape(word)}</span> '
        f"fresh seeds &mdash; failing config {r.failing_pass}/{r.failing_n} pass, "
        f"baseline {r.baseline_pass}/{r.baseline_n} pass, effect {effect_text} "
        f"(run <span class=\"mono\">{html.escape(r.latest_run_id or '-')}</span>).{tail}</p>"
    )


def _where_html(finding: DerivedFinding) -> str:
    bits = []
    if finding.concentrations:
        items = "".join(
            f"<li><span class=\"mono\">{html.escape(c.range_text())}</span> "
            f"&rarr; {c.pass_rate * 100:.0f}% pass ({c.n_pass}/{c.n_scored})</li>"
            for c in finding.concentrations
        )
        bits.append(f"<p class=\"eyebrow\">Where it fails</p><ul>{items}</ul>")
    if finding.kind_breakdown:
        items = "".join(
            f"<li><span class=\"mono\">{html.escape(k)}</span> &rarr; "
            f"{(p / n * 100) if n else 0:.0f}% pass ({p}/{n})</li>"
            for k, n, p in finding.kind_breakdown
        )
        bits.append(f"<p class=\"eyebrow\">By query kind</p><ul>{items}</ul>")
    if finding.traps:
        items = ", ".join(
            f'<span class="mono">{html.escape(t)}</span> &times;{n}' for t, n in finding.traps
        )
        bits.append(f'<p class="eyebrow">Traps that fired</p><p class="meta">{items}</p>')
    else:
        bits.append(
            '<p class="eyebrow">Traps that fired</p><p class="meta">None recorded -- '
            "these trials were not adversarial (no <span class=\"mono\">traps=</span> "
            "in their verifier detail), so there is no trap attribution to report.</p>"
        )
    if finding.examples:
        items = "".join(
            f'<li><a class="mono" href="/record/{html.escape(rid)}">{html.escape(rid[:12])}</a> '
            f'<span class="meta">{html.escape(desc)}</span></li>'
            for rid, desc in finding.examples
        )
        bits.append(f'<p class="eyebrow">Example failures</p><ul>{items}</ul>')
    return "".join(bits)


def _derived_card(finding: DerivedFinding, store: ResultsStore) -> str:
    cell = finding.cell
    if finding.kind == "strength":
        body = (
            f'<p style="margin:0 0 0.4rem;">{html.escape(finding.headline)}.</p>'
            f'<p class="meta" style="margin:0;">{html.escape(cell.verification_detail)}</p>'
        )
        if finding.concentrations:
            worst = min(finding.concentrations, key=lambda c: c.pass_rate)
            body += (
                '<p class="meta" style="margin:0.3rem 0 0;">Hardest slice inside this '
                f'strength: <span class="mono">{html.escape(worst.range_text())}</span> at '
                f"{worst.pass_rate * 100:.0f}% ({worst.n_pass}/{worst.n_scored}).</p>"
            )
        action = f'<a href="{cell.href}">see all {cell.n_scored} trials &rarr;</a>'
    else:
        body = (
            f'<p style="margin:0 0 0.4rem;">{html.escape(finding.headline)}.</p>'
            f"{_where_html(finding)}"
            f'<p class="meta" style="margin:0.4rem 0 0;">{html.escape(cell.verification_detail)}</p>'
            f"{_replication_line(finding)}"
            f"{_replicate_form_html(finding, store)}"
        )
        action = f'<a href="{cell.href}">see all {cell.n_scored} trials &rarr;</a>'

    title = (
        f"{cell.label} — {short_model_name(cell.model)}"
    )
    return f"""
    <div class="card finding-{html.escape(finding.kind)}" id="f-{html.escape(cell.slug)}">
      <div class="card-head">
        <span class="card-title card-title-name">{html.escape(title)}</span>
        <span class="card-meta">{_verification_pill(cell.verification)}</span>
      </div>
      <div class="card-body">
        {body}
        <p class="meta" style="margin:0.5rem 0 0;">{action}</p>
      </div>
    </div>
    """


def _model_section_html(mf: ModelFindings, store: ResultsStore, visible: bool) -> str:
    strengths = "".join(_derived_card(f, store) for f in mf.strengths)
    weaknesses = "".join(_derived_card(f, store) for f in mf.weaknesses)
    if not strengths:
        strengths = (
            '<div class="card"><div class="card-body"><p class="meta">No capability '
            f"reached {STRENGTH_PASS_RATE * 100:.0f}% over at least {STRENGTH_MIN_N} "
            "scored trials for this model yet.</p></div></div>"
        )
    if not weaknesses:
        weaknesses = (
            '<div class="card"><div class="card-body"><p class="meta">No capability '
            f"fell below {WEAKNESS_PASS_RATE * 100:.0f}% over at least {WEAKNESS_MIN_N} "
            "scored trials for this model -- nothing here is broken at the "
            "difficulty range probed so far.</p></div></div>"
        )
    style = "" if visible else ' style="display:none;"'
    rate = mf.pass_rate
    return f"""
    <section class="model-findings" data-model="{html.escape(mf.model)}"{style}>
      <h2>{html.escape(short_model_name(mf.model))}
        <span class="meta">&middot; {mf.n_trials:,} trials &middot;
        {(f"{rate * 100:.0f}% overall" if rate is not None else "no scored trials")}
        &middot; ${mf.cost_usd:.4f} spent</span></h2>
      <p class="eyebrow">What it does well ({len(mf.strengths)})</p>
      {strengths}
      <p class="eyebrow">Where it fails ({len(mf.weaknesses)})</p>
      {weaknesses}
    </section>
    """


_SWITCHER_JS = """
(function () {
  var tabs = document.querySelectorAll('[data-model-tab]');
  var sections = document.querySelectorAll('.model-findings');
  var rows = document.querySelectorAll('.cap-row');
  function show(model) {
    sections.forEach(function (s) {
      s.style.display = (model === '' || s.dataset.model === model) ? '' : 'none';
    });
    rows.forEach(function (r) {
      r.classList.toggle('cap-row-selected', model !== '' && r.dataset.model === model);
    });
    tabs.forEach(function (t) {
      t.classList.toggle('active', t.dataset.modelTab === model);
    });
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function (e) {
      e.preventDefault();
      var model = t.dataset.modelTab;
      show(model);
      history.pushState({ model: model }, '', t.getAttribute('href'));
    });
  });
  window.addEventListener('popstate', function () {
    var p = new URLSearchParams(window.location.search);
    show(p.get('model') || '');
  });
})();
"""


def _switcher_html(data: FindingsData, selected: str | None) -> str:
    """Model tabs.

    These are REAL `/findings?model=...` links that the server honours, and
    the server also renders every model's section into the page; the script
    below just toggles which one is visible and rewrites the URL. That
    combination is the snappy option: switching costs zero round-trips and
    zero re-layout of the shell/sidebar (a server round-trip here repaints
    the whole page, and the capability map above the sections would flicker
    even though it never changes), while the URLs stay shareable,
    bookmarkable, and fully functional with JavaScript disabled -- a
    client-only tab widget would have given up all three. The payload cost
    is small and bounded: one section per model in the store.
    """
    tabs = [
        f'<a class="model-tab{" active" if selected is None else ""}" '
        f'data-model-tab="" href="/findings">All models</a>'
    ]
    for mf in data.models:
        active = " active" if selected == mf.model else ""
        tabs.append(
            f'<a class="model-tab{active}" data-model-tab="{html.escape(mf.model)}" '
            f'href="/findings?model={_q(mf.model)}">'
            f"{html.escape(short_model_name(mf.model))}</a>"
        )
    return f'<div class="model-tabs" role="tablist">{"".join(tabs)}</div>'


def findings_html(store: ResultsStore, model: str | None = None) -> str:
    """The /findings page, derived from `store`.

    `model` selects one model's section (from `/findings?model=...`); None
    means the all-models comparison view. An unknown model falls back to the
    all-models view rather than an empty page.
    """
    from .serve import _shell

    data = derive(store)
    known = {mf.model for mf in data.models}
    selected = model if model in known else None

    n_strengths = sum(len(mf.strengths) for mf in data.models)
    n_weaknesses = sum(len(mf.weaknesses) for mf in data.models)
    resolved = resolve_run_ids(
        store, tuple({r for f in FINDINGS + RETRACTED for r in f.run_ids})
    )
    n_dangling = sum(1 for ok in resolved.values() if not ok)

    sections = "".join(
        _model_section_html(mf, store, visible=(selected is None or selected == mf.model))
        for mf in data.models
    )

    cards = "".join(_finding_card(f, resolved) for f in FINDINGS)
    retracted_cards = "".join(_finding_card(f, resolved) for f in RETRACTED)
    n_confirmed = sum(1 for f in FINDINGS if f.status == "confirmed")
    n_open = sum(1 for f in FINDINGS if f.status == "needs-replication")

    dangling_note = ""
    if n_dangling:
        dangling_note = (
            f'<p class="meta">{n_dangling} of the run_ids cited below no longer '
            "resolve in this store (records destroyed by a git revert on "
            "2026-09-19). They are marked in place rather than linked, because a "
            "dead link that looks alive is a worse claim than an honest gap.</p>"
        )

    body = f"""
    <div class="page-head"><div class="page-head-main">
    <h1>Findings</h1>
    <p class="subtitle">The capability map, derived live from
    {data.n_trials:,} recorded trials: what each model does well, where
    exactly it breaks, and how far you should trust each claim. Every number
    here is computed from the store when this page loads &mdash; replicate a
    finding and it updates.</p>
    </div><div class="page-head-actions">
      <a class="cta-primary" href="/launch">Map a model &rarr;</a>
    </div></div>

    <div class="stat-grid">
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{len(data.models)}</div></div><div class="stat-label">Models mapped</div></div>
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_strengths}</div></div><div class="stat-label">Derived strengths</div></div>
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_weaknesses}</div></div><div class="stat-label">Derived weaknesses</div></div>
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{len(FINDINGS)}</div><div class="stat-delta">{n_confirmed} confirmed &middot; {n_open} open</div></div><div class="stat-label">Documented findings</div></div>
    </div>

    {_capability_map_html(data, selected)}

    {_switcher_html(data, selected)}

    {sections}

    {_comparison_html(data)}

    <div class="card">
      <div class="card-head">
        <span class="card-title">Documented findings</span>
        <span class="card-meta">curated &middot; not auto-derived</span>
      </div>
      <div class="card-body">
        <p class="meta" style="margin-top:0;">These are causal results from
        <span class="mono">docs/lab-log.md</span> that no aggregate over
        pass/fail columns can reproduce &mdash; a pronoun ablation, a renderer-bug
        retraction, a strategic comparison between two campaign designs. They
        are written by hand, dated, and tagged with the run_ids they came from,
        and they do NOT update when the store does.</p>
        {dangling_note}
        {cards}
      </div>
    </div>

    <div class="card card-gap">
      <div class="card-head">
        <span class="card-title">Retractions</span>
        <span class="card-meta">{len(RETRACTED)} withdrawn</span>
      </div>
      <div class="card-body">
        <details>
          <summary>Retractions ({len(RETRACTED)})</summary>
          <p class="meta">Kept deliberately, not deleted -- self-skepticism is
          a feature of this instrument, not a blemish. A retracted finding
          means the audit trail caught its own mistake before it shipped as a
          public claim.</p>
          {retracted_cards}
        </details>
      </div>
    </div>
    <script>{_SWITCHER_JS}</script>
    """
    return _shell("Findings", "findings", body, trial_count=data.n_trials)
