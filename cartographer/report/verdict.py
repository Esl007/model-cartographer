"""Plain-English verdict panels: "what this run revealed."

`run_verdict(store, run_id)` is a pure function (deterministic, no LLM, no
network) that reads every stored `ExperimentRecord` for a run and turns it
into a short headline + a few paragraphs of plain English, following the
same world-regeneration idiom as `scripts/foil_report.py`: given a record's
stored `(latent_params, task_seed, rendering_seed)`, the exact `World` it
was generated from can be rebuilt (for runs at or after the generator-
hardening commit `313e3b2`, 2026-09-17 17:20:02 UTC -- earlier runs are
handled gracefully with a note instead of a crash).

This module never calls a model. Everything it says is derived from stored
verdicts/response text plus pure re-simulation of the world the record was
generated from.
"""

from __future__ import annotations

import re
import string
from collections import Counter
from dataclasses import dataclass, field

from ..exec.evaluator import _apply_config, _default_base
from ..render import build_name_map, render
from ..store.schema import ExperimentRecord, Verdict
from ..store.store import ResultsStore
from ..world.foils import foil_divergence
from ..world.generate import generate

# Below this per-dim-region pass rate (but above an outright flip), a dim
# region is called "diffuse weakness" rather than a sharp boundary.
_DIFFUSE_WEAKNESS_CEILING = 0.9
# At or above this overall pass rate, a run is called "saturated" -- it
# didn't meaningfully challenge the model.
_SATURATION_PASS_RATE = 0.97
_ARTICLES = {"the", "a", "an"}

# The generator-hardening commit after which stored (latent_params,
# task_seed) tuples reconstruct EXACTLY under the current generate() --
# mirrors scripts/foil_report.py's TARGET_RUN_IDS/KNOWN_UNRECONSTRUCTABLE
# split, but keyed by created_at timestamp instead of a run_id allowlist so
# this works for any run, not just the ones foil_report.py knows about.
_GENERATOR_HARDENING_ISO = "2026-09-17T17:20:42"


def _normalize(text: str) -> str:
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    tokens = [t for t in text.split() if t not in _ARTICLES]
    return " ".join(tokens)


def _query_kind_of(record: ExperimentRecord) -> str:
    if record.verifier_detail:
        m = re.search(r"kind=([a-z]+)", record.verifier_detail)
        if m:
            return m.group(1)
    return "belief"


def _is_reconstructable(record: ExperimentRecord) -> bool:
    """Best-effort: a record's created_at timestamp after the generator-
    hardening commit is assumed reconstructable; anything else (or a
    missing/malformed timestamp) is treated conservatively as NOT
    reconstructable so we never silently mis-attribute a failure to the
    wrong world."""
    try:
        return (record.created_at or "") >= _GENERATOR_HARDENING_ISO
    except TypeError:
        return False


def _regenerate(record: ExperimentRecord):
    query_kind = _query_kind_of(record)
    base = _default_base(query_kind)
    params = _apply_config(base, record.latent_params)
    world = generate(params, record.task_seed)
    name_map = build_name_map(world, record.rendering_seed)
    rendered = render(world, name_map, record.rendering_seed)
    profile = foil_divergence(world, name_map)
    return world, name_map, rendered, profile


def classify_belief_failure(record: ExperimentRecord) -> str | None:
    """Which foil (if any) the model's response matches, or None if it
    matches no diverging foil (unclassified). Returns None also when the
    record can't be safely regenerated."""
    if not _is_reconstructable(record):
        return None
    try:
        _, _, _, profile = _regenerate(record)
    except Exception:
        return None
    response_norm = _normalize(record.response_text or "")
    for trap_kind in profile.trap_kinds:
        foil_surface = profile.foils.get(trap_kind)
        if foil_surface is None:
            continue
        if _normalize(foil_surface) in response_norm:
            return trap_kind
    return None


@dataclass
class NotableFailure:
    """One concrete failure example: what was asked, what the model said,
    what the correct answer was, and a one-line why-it's-wrong."""

    record_id: str
    question: str
    model_answer: str
    correct_answer: str
    why_wrong: str

    def to_json_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "question": self.question,
            "model_answer": self.model_answer,
            "correct_answer": self.correct_answer,
            "why_wrong": self.why_wrong,
        }


@dataclass
class RunVerdict:
    """Plain-English summary of one run's records."""

    headline: str
    paragraphs: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    notable_failures: list = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "headline": self.headline,
            "paragraphs": list(self.paragraphs),
            "stats": dict(self.stats),
            "notable_failures": [f.to_json_dict() for f in self.notable_failures],
        }


def _pass_rate(records: list) -> float | None:
    scored = [r for r in records if r.verdict in (Verdict.PASS, Verdict.FAIL)]
    if not scored:
        return None
    passed = sum(1 for r in scored if r.verdict == Verdict.PASS)
    return passed / len(scored)


def _empty_verdict(run_id: str) -> RunVerdict:
    return RunVerdict(
        headline=f"Run {run_id} has no records yet.",
        paragraphs=["No trials have been recorded for this run."],
        stats={"n_records": 0},
    )


def _all_error_verdict(run_id: str, records: list) -> RunVerdict:
    n = len(records)
    details = Counter()
    for r in records:
        if r.verifier_detail:
            for key in ("cost_capped", "cancelled", "cap_hit", "error"):
                if f"{key}=1" in r.verifier_detail or f"{key}=" in r.verifier_detail:
                    details[key] += 1
    reasons = ", ".join(f"{k} x{v}" for k, v in details.most_common()) or "unspecified errors"
    return RunVerdict(
        headline=f"Run {run_id}: all {n} trials errored -- no usable signal.",
        paragraphs=[
            f"Every one of the {n} recorded trials ended in ERROR ({reasons}). "
            "This run produced no PASS/FAIL signal to report on -- check the "
            "provider/API key, cost cap, or cancellation state before "
            "re-running."
        ],
        stats={"n_records": n, "pass_rate": None},
    )


def _dim_region_rates(records: list) -> dict:
    """Per-realized-dim pass rate, bucketing each dim's observed values
    into low/mid/high terciles (simple, doesn't need scheduler internals)
    so we can say "the model is weak when X is high" in plain English."""
    scored = [r for r in records if r.verdict in (Verdict.PASS, Verdict.FAIL)]
    if not scored:
        return {}
    dims = set()
    for r in scored:
        dims.update(r.realized_dims.keys())

    out: dict = {}
    for dim in sorted(dims):
        values = [(r.realized_dims.get(dim), r.verdict == Verdict.PASS) for r in scored if dim in r.realized_dims]
        if len(values) < 4:
            continue
        vals_sorted = sorted(v for v, _ in values)
        n = len(vals_sorted)
        lo_cut = vals_sorted[n // 3]
        hi_cut = vals_sorted[(2 * n) // 3]

        def bucket(v):
            if v <= lo_cut:
                return "low"
            if v >= hi_cut:
                return "high"
            return "mid"

        tallies: dict = {"low": [0, 0], "mid": [0, 0], "high": [0, 0]}
        for v, passed in values:
            b = bucket(v)
            tallies[b][0] += 1 if passed else 0
            tallies[b][1] += 1
        rates = {
            b: (t[0] / t[1] if t[1] else None) for b, t in tallies.items()
        }
        out[dim] = rates
    return out


def _notable_failures(run_id: str, records: list, limit: int = 3) -> list:
    fails = [r for r in records if r.verdict == Verdict.FAIL]
    out: list = []
    for r in fails:
        if len(out) >= limit:
            break
        query_kind = _query_kind_of(r)
        question = (r.prompt_text or "").strip().splitlines()[-1] if r.prompt_text else "(prompt not recorded)"
        model_answer = (r.response_text or "").strip() or "(no answer)"
        correct_answer = "(unknown)"
        why_wrong = "answer did not match the verified ground truth"
        if _is_reconstructable(r):
            try:
                _, _, _, profile = _regenerate(r)
                correct_answer = profile.true_answer
                trap = classify_belief_failure(r)
                if query_kind == "belief":
                    if trap == "final_true_state":
                        why_wrong = (
                            "the model reported the object's TRUE final "
                            "location, not what the queried entity actually "
                            "believed -- it collapsed belief into reality"
                        )
                    elif trap is not None:
                        why_wrong = f"the model's answer matches the '{trap}' shortcut, not the verified belief"
                    else:
                        why_wrong = "the model's answer matches no known shortcut -- an unclassified miss"
                elif query_kind == "order":
                    why_wrong = "the model recovered narration order, not the true event timestamps"
                else:
                    why_wrong = "the model's tracked object state diverged from the simulated ground truth"
            except Exception:
                pass
        out.append(
            NotableFailure(
                record_id=r.record_id,
                question=question,
                model_answer=model_answer,
                correct_answer=correct_answer,
                why_wrong=why_wrong,
            )
        )
    return out


def _belief_collapse_paragraph(records: list) -> str | None:
    """For belief/adversarial_belief runs: classify every FAIL against the
    foil model and, if a majority pattern emerges, say it in English."""
    fails = [r for r in records if r.verdict == Verdict.FAIL and _query_kind_of(r) == "belief"]
    if not fails:
        return None
    reconstructable = [r for r in fails if _is_reconstructable(r)]
    if not reconstructable:
        return (
            f"{len(fails)} belief FAIL(s) were recorded, but they predate the "
            "generator-hardening fix and can't be safely re-classified against "
            "regenerated ground truth."
        )
    classified = Counter()
    for r in reconstructable:
        trap = classify_belief_failure(r)
        classified[trap if trap is not None else "UNCLASSIFIED"] += 1
    total = len(reconstructable)
    top_trap, top_n = classified.most_common(1)[0]
    if top_trap == "final_true_state" and top_n > 0:
        return (
            f"In {top_n} of {total} classifiable belief failures the model "
            "reported where the object ACTUALLY was, not where the queried "
            "entity BELIEVED it was -- it collapsed their perspective into "
            "reality."
        )
    if top_trap != "UNCLASSIFIED" and top_n > 0:
        return (
            f"In {top_n} of {total} classifiable belief failures the model's "
            f"wrong answer matches the '{top_trap}' shortcut -- a plausible "
            "but ungrounded guess rather than tracking what that entity "
            "actually witnessed."
        )
    return (
        f"{total} belief failure(s) were classified against known shortcuts, "
        "but none of them dominate -- the failures don't share one obvious "
        "wrong-answer pattern."
    )


def _headline_and_summary_paragraphs(run_id: str, records: list) -> tuple:
    pass_rate = _pass_rate(records)
    n = len(records)
    scored_n = sum(1 for r in records if r.verdict in (Verdict.PASS, Verdict.FAIL))
    error_n = sum(1 for r in records if r.verdict == Verdict.ERROR)

    query_kinds = Counter(_query_kind_of(r) for r in records)
    kind = query_kinds.most_common(1)[0][0] if query_kinds else "unknown"

    paragraphs: list = []

    if pass_rate is None:
        return f"Run {run_id}: no scored trials.", paragraphs

    if pass_rate >= _SATURATION_PASS_RATE:
        headline = (
            f"Run {run_id}: passed essentially everything "
            f"({pass_rate * 100:.0f}%) -- this test did not challenge the model."
        )
        paragraphs.append(
            f"Of {scored_n} scored trials ({kind} query kind), the model passed "
            f"{pass_rate * 100:.0f}%. That's saturation: the difficulty range "
            "probed here isn't finding this model's limits. Consider a harder "
            "preset (breaking_point or adversarial_belief) or a wider dimension "
            "range."
        )
    else:
        headline = f"Run {run_id}: {pass_rate * 100:.0f}% pass rate on {kind} queries."
        paragraphs.append(
            f"Of {scored_n} scored trials ({kind} query kind), the model passed "
            f"{pass_rate * 100:.0f}% and failed {100 - pass_rate * 100:.0f}%."
        )

    if error_n:
        paragraphs.append(
            f"{error_n} additional trial(s) ended in ERROR (infra failures, cost "
            "cap, or cancellation) and are excluded from the pass rate above."
        )

    dim_rates = _dim_region_rates(records)
    sharp = []
    diffuse = []
    for dim, rates in dim_rates.items():
        lo, hi = rates.get("low"), rates.get("high")
        if lo is None or hi is None:
            continue
        if lo >= _SATURATION_PASS_RATE and hi is not None and hi < 0.5:
            sharp.append((dim, hi))
        elif hi is not None and _DIFFUSE_WEAKNESS_CEILING > hi >= 0.5 and lo >= _DIFFUSE_WEAKNESS_CEILING:
            diffuse.append((dim, hi))

    if sharp:
        parts = ", ".join(f"{d} (high-end pass rate {r * 100:.0f}%)" for d, r in sharp)
        paragraphs.append(f"Sharp boundary detected on: {parts}.")
    if diffuse:
        parts = ", ".join(f"{d} (pass rate {r * 100:.0f}% at the high end)" for d, r in diffuse)
        paragraphs.append(
            f"Diffuse weakness (below 90% but no outright flip) on: {parts}."
        )

    if kind == "belief":
        belief_para = _belief_collapse_paragraph(records)
        if belief_para:
            paragraphs.append(belief_para)

    return headline, paragraphs


def _stage_from_run_id(run_id: str) -> str | None:
    if "/" in run_id:
        return run_id.rsplit("/", 1)[-1]
    return None


def _is_full_map_parent(store: ResultsStore, run_id: str) -> bool:
    """A full_map parent run_id itself has no directly-recorded records
    (all records live under f"{run_id}/{stage}" child run_ids) -- detect it
    by checking whether any child run_ids exist."""
    cur = store._conn.execute(
        "SELECT DISTINCT run_id FROM experiments WHERE run_id LIKE ?",
        (f"{run_id}/%",),
    )
    return cur.fetchone() is not None


_FULL_MAP_STAGES = ["state", "order", "belief", "adversarial_belief", "breaking_point"]


def _full_map_verdict(store: ResultsStore, run_id: str) -> RunVerdict:
    stage_paragraphs = []
    stats = {"stages": {}}
    all_failures: list = []
    any_records = False

    for stage in _FULL_MAP_STAGES:
        child_run_id = f"{run_id}/{stage}"
        records = store.by_run(child_run_id)
        if not records:
            stage_paragraphs.append(f"{stage}: not run (no records).")
            continue
        any_records = True
        pass_rate = _pass_rate(records)
        n = len(records)
        stats["stages"][stage] = {"n_records": n, "pass_rate": pass_rate}
        if pass_rate is None:
            stage_paragraphs.append(f"{stage}: {n} trial(s), all errored.")
            continue
        if stage == "breaking_point":
            # breaking_point's "pass rate" over raw records is less
            # meaningful than its own found/t_star -- report qualitatively.
            fails = sum(1 for r in records if r.verdict == Verdict.FAIL)
            passes = sum(1 for r in records if r.verdict == Verdict.PASS)
            stage_paragraphs.append(
                f"breaking_point: escalation ray probed with {passes} pass / "
                f"{fails} fail across {n} trials."
            )
        elif pass_rate >= _SATURATION_PASS_RATE:
            stage_paragraphs.append(
                f"Passes {stage} cleanly ({pass_rate * 100:.0f}%, {n} trials)."
            )
        else:
            stage_paragraphs.append(
                f"{stage.capitalize()} is a weak province: {pass_rate * 100:.0f}% "
                f"pass rate across {n} trials."
            )
        if stage in ("belief", "adversarial_belief"):
            belief_para = _belief_collapse_paragraph(records)
            if belief_para:
                stage_paragraphs.append(f"({stage}) {belief_para}")
        all_failures.extend(_notable_failures(child_run_id, records, limit=2))

    if not any_records:
        return _empty_verdict(run_id)

    weak_stages = [
        s for s, v in stats["stages"].items()
        if v.get("pass_rate") is not None and v["pass_rate"] < _SATURATION_PASS_RATE and s != "breaking_point"
    ]
    if weak_stages:
        headline = (
            f"Full-map run {run_id}: clean on "
            f"{', '.join(s for s in stats['stages'] if s not in weak_stages) or 'nothing'}, "
            f"weak province: {', '.join(weak_stages)}."
        )
    else:
        headline = f"Full-map run {run_id}: passes every stage cleanly -- this battery did not find a limit."

    return RunVerdict(
        headline=headline,
        paragraphs=stage_paragraphs,
        stats=stats,
        notable_failures=all_failures[:3],
    )


def run_verdict(store: ResultsStore, run_id: str) -> RunVerdict:
    """Convert every stored record for `run_id` into a plain-English
    verdict. Deterministic, no LLM, no network.

    Handles full_map parent run_ids specially (records live under child
    `f"{run_id}/{stage}"` run_ids, not the parent itself) by aggregating
    per-stage verdicts into one profile paragraph set.
    """
    if _is_full_map_parent(store, run_id):
        return _full_map_verdict(store, run_id)

    records = store.by_run(run_id)
    if not records:
        return _empty_verdict(run_id)

    if all(r.verdict == Verdict.ERROR for r in records):
        return _all_error_verdict(run_id, records)

    headline, paragraphs = _headline_and_summary_paragraphs(run_id, records)
    stats = {
        "n_records": len(records),
        "pass_rate": _pass_rate(records),
        "n_pass": sum(1 for r in records if r.verdict == Verdict.PASS),
        "n_fail": sum(1 for r in records if r.verdict == Verdict.FAIL),
        "n_error": sum(1 for r in records if r.verdict == Verdict.ERROR),
    }
    notable = _notable_failures(run_id, records)

    return RunVerdict(
        headline=headline,
        paragraphs=paragraphs,
        stats=stats,
        notable_failures=notable,
    )
