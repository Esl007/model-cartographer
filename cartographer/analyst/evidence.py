"""Deterministic evidence pack -- NO LLM, NO network.

`build_evidence_pack(store, model=None)` computes a JSON-able digest of the
results store that the Evidence Analyst reads and narrates. It is the ONLY
source of numbers the analyst is permitted to cite, so every figure here is
computed straight from stored verdicts under the project's one arithmetic
contract:

  * scored trials only (PASS + FAIL); infra ERRORs excluded;
  * FAILs classified as resample `noise` excluded from numerator AND
    denominator.

Rather than re-derive any of that, this module drives
`cartographer.viewer.findings.derive`, which already implements the
contract (and is the same computation the /findings page paints). We read
its `Cell`/`ModelFindings`/`DerivedFinding` objects and flatten them into a
compact dict. The curated `FINDINGS`/`RETRACTED` sets are copied verbatim
with each run_id's live-resolution checked against the store, exactly as
the findings page does, so a diagnosis can never cite a finding whose
evidence rows were destroyed (see the 2026-09-19 INCIDENT in the lab log).

Size is capped most-informative-first: overall per-model profile and its
weak regions/traps come first, and the full per-cell grid is truncated so a
rendered pack stays in the ~8-12KB range regardless of store size.
"""

from __future__ import annotations

import json

from ..store.store import ResultsStore
from ..viewer.findings import (
    CAPABILITY_KINDS,
    DIFFICULTY_DIMS,
    FINDINGS,
    RETRACTED,
    derive,
    resolve_run_ids,
    short_model_name,
)

# How many weak regions / trap kinds / curated findings to carry per model,
# most-informative-first, so the pack stays small on a large store.
_MAX_WEAKNESSES = 4
_MAX_STRENGTHS = 3
_MAX_TRAPS = 5


def _round_rate(pass_rate: float | None) -> float | None:
    """Percent, rounded to one decimal -- the audit's numeric-matching
    precision is defined by exactly this rounding, so it lives here."""
    if pass_rate is None:
        return None
    return round(pass_rate * 100.0, 1)


def _cell_digest(cell) -> dict:
    return {
        "key": cell.key,
        "axis": cell.axis,
        "label": cell.label,
        "pass_rate_pct": _round_rate(cell.pass_rate),
        "n_scored": cell.n_scored,
        "n_pass": cell.n_pass,
        "n_fail": cell.n_fail,
        "n_noise_excluded": cell.n_noise,
        "n_error_excluded": cell.n_error,
        "verification": cell.verification,
    }


def _weakness_digest(finding) -> dict:
    cell = finding.cell
    concentrations = [
        {
            "dim": c.dim,
            "bucket": c.bucket,
            "range": c.range_text(),
            "pass_rate_pct": _round_rate(c.pass_rate),
            "n_scored": c.n_scored,
            "n_pass": c.n_pass,
        }
        for c in finding.concentrations
    ]
    traps = [{"trap_kind": k, "count": n} for k, n in finding.traps[:_MAX_TRAPS]]
    repl = None
    if finding.replication is not None and finding.replication.has_evidence:
        r = finding.replication
        repl = {
            "status": finding.replication_status,
            "baseline_pass_rate_pct": _round_rate(getattr(r, "baseline_rate", None)),
            "failing_pass_rate_pct": _round_rate(getattr(r, "failing_rate", None)),
            "baseline_n": getattr(r, "baseline_n", None),
            "failing_n": getattr(r, "failing_n", None),
        }
    return {
        "headline": finding.headline,
        "label": cell.label,
        "pass_rate_pct": _round_rate(cell.pass_rate),
        "n_scored": cell.n_scored,
        "n_pass": cell.n_pass,
        "n_fail": cell.n_fail,
        "concentrations": concentrations,
        "traps": traps,
        "replication": repl,
    }


def _strength_digest(finding) -> dict:
    cell = finding.cell
    return {
        "headline": finding.headline,
        "label": cell.label,
        "pass_rate_pct": _round_rate(cell.pass_rate),
        "n_scored": cell.n_scored,
        "n_pass": cell.n_pass,
    }


def _model_digest(mf) -> dict:
    grid = [
        _cell_digest(mf.cells[(axis, key)])
        for axis, keys in (("kind", CAPABILITY_KINDS), ("dim", DIFFICULTY_DIMS))
        for key in keys
        if (axis, key) in mf.cells
    ]
    return {
        "model": mf.model,
        "short_name": short_model_name(mf.model),
        "overall_pass_rate_pct": _round_rate(mf.pass_rate),
        "n_trials": mf.n_trials,
        "n_scored": mf.n_scored,
        "n_pass": mf.n_pass,
        "spend_usd": round(mf.cost_usd, 4),
        "strengths": [_strength_digest(f) for f in mf.strengths[:_MAX_STRENGTHS]],
        "weaknesses": [_weakness_digest(f) for f in mf.weaknesses[:_MAX_WEAKNESSES]],
        "capability_grid": grid,
    }


def _curated_findings_digest(store: ResultsStore) -> dict:
    """The documented findings from viewer/findings.py, with each run_id's
    live resolution against the store recorded so a diagnosis can't lean on
    a finding whose evidence rows are gone."""

    def one(f) -> dict:
        resolved = resolve_run_ids(store, f.run_ids)
        return {
            "name": f.name,
            "mechanism": f.mechanism,
            "evidence": f.evidence,
            "status": f.status,
            "date": f.date,
            "run_ids": list(f.run_ids),
            # True only if every cited run_id still resolves (or there are no
            # run_ids to check -- an ablation-only finding with a prose
            # citation). A finding whose evidence rows were destroyed is
            # marked so and MUST NOT be treated as live evidence.
            "evidence_resolves": all(resolved.get(rid, False) for rid in f.run_ids),
        }

    return {
        "confirmed_or_needs_replication": [one(f) for f in FINDINGS],
        "retracted": [one(f) for f in RETRACTED],
    }


def build_evidence_pack(store: ResultsStore, model: str | None = None) -> dict:
    """A structured, JSON-able digest of the store for the analyst to read.

    Every number here is derived deterministically under the project's
    scored-only/noise-excluded arithmetic contract (via
    `cartographer.viewer.findings.derive`). If `model` is given, only that
    model's profile is included in `models` (cross-model comparison then has
    a single row); the store-wide totals still reflect the whole store so
    spend/trial figures aren't silently narrowed.
    """
    data = derive(store)

    model_digests = []
    for mf in data.models:
        if model is not None and mf.model != model:
            continue
        model_digests.append(_model_digest(mf))

    total_spend = round(sum(mf.cost_usd for mf in data.models), 4)
    total_trials = sum(mf.n_trials for mf in data.models)
    total_scored = sum(mf.n_scored for mf in data.models)

    # Dimension high-region cut points, so the analyst can say what "high
    # coreference" actually means in realized units rather than inventing a
    # threshold.
    dim_high_regions = {
        dim: {
            "high_min": data.dim_high_min.get(dim),
            "tercile_cuts": list(data.dim_cuts.get(dim, ())) or None,
        }
        for dim in DIFFICULTY_DIMS
        if dim in data.dim_cuts
    }

    pack = {
        "schema": "evidence_pack_v1",
        "arithmetic_contract": (
            "All rates are over SCORED trials only (PASS+FAIL); infra ERRORs "
            "excluded; FAILs classified as resample noise excluded from both "
            "numerator and denominator. Percentages are rounded to one decimal."
        ),
        "store_totals": {
            "n_models": len(data.models),
            "n_trials": total_trials,
            "n_scored": total_scored,
            "spend_usd": total_spend,
        },
        "capability_kinds": list(CAPABILITY_KINDS),
        "difficulty_dims": list(DIFFICULTY_DIMS),
        "dim_high_regions": dim_high_regions,
        "models": model_digests,
        "curated_findings": _curated_findings_digest(store),
    }
    return pack


def render_pack(pack: dict) -> str:
    """The exact text handed to the analyst model -- a stable, pretty JSON
    serialization. Kept as one function so the prompt and the audit see the
    identical rendered bytes."""
    return json.dumps(pack, indent=2, sort_keys=False, default=str)
