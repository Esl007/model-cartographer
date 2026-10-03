"""Frontier summary: the answer to "where does this model break, and why?"

Every other surface in the viewer shows the instrument's internals -- runs,
records, cells, rays, t values -- and leaves the reader to assemble the
conclusion. `frontier_summary(store, model)` assembles it, deterministically,
from the store alone (no LLM, no network):

    one row per capability ->
        status       HOLDS / DEGRADES / BREAKS / NOT TESTED
        boundary     in the capability's own units ("22-call plans", "37 tools")
        evidence     n_pass / n_scored at the boundary, Wilson 95% CI
        confidence   A / B / C  (see `_grade`)
        cause        dominant failure code at/after the boundary, in words
        exemplar     one concrete failing trial, with its record id
        curve        pass rate at every tested level, for a chart
    plus a one-sentence verdict per model.

Thresholds are absolute pass rates, the same everywhere: BREAKS when a level
falls to <= 30% (the escalation's collapse threshold), DEGRADES when it falls
to <= 75%, HOLDS otherwise. "Level" means a group of at least `MIN_N` scored
trials at one setting of the capability's axis. Record hygiene (invalidated
runs, resample children, fidelity-gate rejections) is inherited from
`viewer.findings._primary_records`, so this never counts evidence the rest of
the platform has excluded.

The capability rows are declared in `SPECS`: which records evidence each
capability (by query kind, run-id pattern and condition) and what its axis is.
That table is the only curated part; every number is computed.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Callable

from ..confirm.stats import wilson_interval
from ..store.schema import Verdict
from ..viewer.findings import _primary_records, _query_kind_of, short_model_name

MIN_N = 5
BREAK_RATE = 0.30
DEGRADE_RATE = 0.75
HOLD_UPPER_BOUND = 0.95  # rate-mode rows: DEGRADES only if the CI is confidently below this

STATUS_ORDER = {"BREAKS": 0, "DEGRADES": 1, "HOLDS": 2, "NOT TESTED": 3}

CAUSE_WORDS = {
    "unknown_tool": "calls a tool that does not exist -- usually a misspelled look-alike of a real tool name",
    "precondition_violation": "calls a real tool before the step it depends on is done",
    "bad_arg": "passes the wrong entity to a tool",
    "missing_arg": "leaves out a required argument",
    "goal_not_reached": "stops before the goal is reached",
    "malformed_plan": "does not produce a usable plan",
    "budget_exceeded": "uses more calls than allowed",
    "budget_exceeded_plan": "uses more calls than allowed",
    "fabricated_plan_on_unsolvable": "writes a plan for an impossible goal instead of declining",
    "incorrectly_declined": "declines a task that was possible",
    "invalid_ticket": "fails to pass on a value returned by an earlier call",
    "transient": "does not retry after a temporary failure",
    "stopped_short": "stops acting before the goal is reached",
    "budget_exhausted": "runs out of calls",
    "turn_limit": "runs out of turns",
    # narrative belief foils (classify_belief_failure)
    "final_true_state": "answers where the object really is, not where the person believes it is",
    "last_mentioned_location": "answers the last place mentioned in the story",
    "last_witnessed_by_anyone": "answers where anyone last saw it, not what this person saw",
    "query_agent_own_last_move": "answers where the person last put it themselves",
    "most_frequent_location": "answers the most frequently mentioned place",
    "abstained": "answers 'unknown' when the belief can be worked out",
    "other_wrong_answer": "gives a wrong answer that matches no known shortcut",
}


# ---------------------------------------------------------------------------
# Capability specs
# ---------------------------------------------------------------------------


def _latent(key: str) -> Callable:
    return lambda r: r.latent_params.get(key)


def _plan_len_label(value, recs) -> str:
    lens = Counter(int(r.realized_dims.get("realized_plan_depth", value + 3)) for r in recs)
    return f"{lens.most_common(1)[0][0]}-call plans"


_TRAP_KEYS = ("poison_traps", "dishonest_decoys_on", "implicit_preconditions_on",
              "zero_slack_on", "id_collisions_on")


def _traps_on(r) -> float:
    """Active trap mechanisms. Records written before the 2026-09-21
    provenance fix carry trap settings only inside `verifier_detail`
    (`...;traps=poison_traps=1;dishonest_decoys_on=1;...`), so fall back to it."""
    if all(k in r.latent_params for k in _TRAP_KEYS):
        return float(sum(1 for k in _TRAP_KEYS if r.latent_params[k] >= 0.5))
    on = 0
    for part in (r.verifier_detail or "").split(";"):
        if part.startswith("traps="):  # first trap rides on the `traps=` segment
            part = part[len("traps="):]
        key, _, val = part.partition("=")
        if key in _TRAP_KEYS and val.strip() in ("1", "1.0"):
            on += 1
    return float(on)


def _traps_label(value, recs) -> str:
    return f"{int(value)} of 5 traps active"


def _catalog_label(value, recs) -> str:
    sizes = Counter(int(r.realized_dims.get("catalog_size", value + 6)) for r in recs)
    return f"{sizes.most_common(1)[0][0]}-tool catalogs"


def _interactive_label(value, recs) -> str:
    base = _plan_len_label(value, recs)
    links = Counter(int(r.realized_dims.get("n_links", 0)) for r in recs).most_common(1)[0][0]
    return f"{base}, {links} values to carry" if links else f"{base}, no values to carry"


@dataclass(frozen=True)
class Spec:
    key: str
    label: str  # row title, user-facing
    short: str  # phrase used inside the one-line verdict
    province: str
    kind: str | None  # `_query_kind_of` value; None = any
    mode: str  # "axis" | "rate" | "absent"
    runs: tuple[str, ...] = ()  # regexes over the full run_id; empty = any run
    where: Callable | None = None
    axis: Callable | None = None
    axis_label: Callable | None = None
    note: str = ""


def _flag(key: str, on: bool) -> Callable:
    return lambda r: (r.latent_params.get(key, 0.0) >= 0.5) == on


SPECS: tuple[Spec, ...] = (
    Spec("state", "Tracks where things actually are", "tracking where things are",
         "I · Narrative", "state", "rate"),
    Spec("order", "Recovers the true order of events", "event order", "I · Narrative", "order", "rate"),
    Spec("belief", "Tracks what a person believes from what they saw", "belief tracking",
         "I · Narrative", "belief", "rate"),
    Spec("adversarial_belief", "Belief tracking in deliberately misleading stories",
         "misleading stories", "I · Narrative", "adversarial_belief", "rate"),
    Spec("plan_length_clear", "Plans long tool sequences (clear tool names)",
         "long plans", "II · Tool planning", "tool_plan", "axis",
         runs=(r"^camp-tool-depth-\w+-003(/k15)?$",), where=_flag("id_collisions_on", False),
         axis=_latent("plan_depth"), axis_label=_plan_len_label),
    Spec("plan_length_lookalike", "Plans long tool sequences when tool names look alike",
         "long plans with look-alike tool names", "II · Tool planning", "tool_plan", "axis",
         runs=(r"^camp-tool-depth-\w+-003(/k15)?$", r"^camp-tool-deep-\w+-001$"),
         where=_flag("id_collisions_on", True), axis=_latent("plan_depth"), axis_label=_plan_len_label),
    Spec("traps", "Plans around traps (poisoned tools, misleading descriptions, zero slack)",
         "trap-laden catalogs", "II · Tool planning", "tool_plan", "axis",
         runs=(r"^camp-tool_bp-nano-002$", r"^camp-tool_bp-(super|ultra)-001$"),
         axis=_traps_on, axis_label=_traps_label),
    Spec("catalog_size", "Picks the right tool from a large catalog", "large catalogs",
         "II · Tool planning", "tool_plan", "axis",
         runs=(r"^camp-tool-(count|confuse)-\w+-001$",), axis=_latent("decoy_tools"),
         axis_label=_catalog_label),
    Spec("impossible", "Notices when a task is impossible", "spotting impossible tasks",
         "II · Tool planning", "tool_plan", "rate",
         runs=(r"^camp-irrelevance-\w+-\d+$",), where=lambda r: "unsolvable=1" in (r.verifier_detail or "")),
    Spec("interactive", "Executes a plan step by step, carrying values between calls",
         "step-by-step execution with values to carry", "III · Interactive execution", "interactive", "axis",
         runs=(r"^camp-interactive-",), axis=_latent("plan_depth"), axis_label=_interactive_label),
    Spec("consistency", "Gets the same task right every time (pass^k)", "consistency",
         "Reliability", None, "absent",
         note="Measured once outside the store (Nano: 63% single-try vs 17% on all 5 tries); no campaign yet."),
)


# ---------------------------------------------------------------------------
# Output objects
# ---------------------------------------------------------------------------


@dataclass
class Level:
    value: float
    label: str
    n_pass: int
    n_scored: int
    n_error: int

    @property
    def rate(self) -> float | None:
        return self.n_pass / self.n_scored if self.n_scored else None


@dataclass
class FrontierRow:
    key: str
    label: str
    province: str
    status: str
    headline: str
    boundary: str | None = None
    held_through: str | None = None
    n_pass: int = 0
    n_scored: int = 0
    n_error: int = 0
    ci: tuple[float, float] | None = None
    confidence: str | None = None
    p_value: float | None = None
    cause_code: str | None = None
    cause: str | None = None
    cause_share: float | None = None
    exemplar: dict | None = None
    curve: list[dict] = field(default_factory=list)
    note: str = ""
    short: str = ""


@dataclass
class ModelFrontier:
    model: str
    name: str
    verdict: str
    rows: list[FrontierRow]

    def to_json_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def _fisher_one_sided(k1: int, n1: int, k2: int, n2: int) -> float:
    """P(group 1 has >= k1 passes | margins): is group 1 better than group 2?"""
    total = k1 + k2
    denom = math.comb(n1 + n2, total)
    if denom == 0:
        return 1.0
    return sum(math.comb(n1, x) * math.comb(n2, total - x)
               for x in range(k1, min(n1, total) + 1)) / denom


def _detail_code(r) -> str | None:
    for part in (r.verifier_detail or "").split(";"):
        if part.startswith("detail="):
            return part[len("detail="):].split(":", 1)[0] or None
    return None


def _belief_cause(r) -> str:
    text = (r.response_text or "").strip().lower()
    if "unknown" in text[-200:]:
        return "abstained"
    from .verdict import classify_belief_failure

    return classify_belief_failure(r) or "other_wrong_answer"


def _cause(spec: Spec, fails: list) -> tuple[str | None, float | None]:
    if not fails:
        return None, None
    sample = fails[:40]
    if spec.kind in ("belief", "adversarial_belief", "state", "order"):
        codes = Counter(_belief_cause(r) if spec.kind != "order" and spec.kind != "state" else "other_wrong_answer"
                        for r in sample)
    else:
        codes = Counter(_detail_code(r) or "unknown" for r in sample)
    code, count = codes.most_common(1)[0]
    return code, count / len(sample)


def _exemplar(rec) -> dict:
    ex = {"record_id": rec.record_id, "run_id": rec.run_id, "detail": None, "excerpt": None}
    for part in (rec.verifier_detail or "").split(";"):
        if part.startswith("detail="):
            ex["detail"] = part[len("detail="):]
    text = rec.response_text or ""
    if text.startswith("[{") and '"distance_after"' in text:
        try:
            steps = json.loads(text)
            bad = next((s for s in steps if not s["ok"]), None)
            if bad:
                text = f"step {bad['step']}: {bad['tool']}({bad['args']}) -> {bad['result'].get('error')}"
        except ValueError:
            pass
    ex["excerpt"] = " ".join(text.split())[:400]
    return ex


def _grade_axis(before: tuple[int, int] | None, after: tuple[int, int], boundary_upper: float,
                status: str) -> tuple[str, float | None]:
    if before and before[1] and after[1]:
        p = _fisher_one_sided(before[0], before[1], after[0], after[1])
        if p < 0.001 and after[1] >= 15:
            return "A", p
        if p < 0.05:
            return "B", p
        return "C", p
    limit = 0.5 if status == "BREAKS" else DEGRADE_RATE
    return ("B" if boundary_upper < limit else "C"), None


def _row_axis(spec: Spec, recs: list) -> FrontierRow:
    groups: dict[float, list] = defaultdict(list)
    for r in recs:
        v = spec.axis(r)
        if v is not None:
            groups[float(v)].append(r)
    levels: list[tuple[Level, list]] = []
    for v in sorted(groups):
        g = groups[v]
        lvl = Level(v, spec.axis_label(v, g),
                    sum(r.verdict == Verdict.PASS for r in g),
                    sum(r.verdict in (Verdict.PASS, Verdict.FAIL) for r in g),
                    sum(r.verdict == Verdict.ERROR for r in g))
        levels.append((lvl, g))
    curve = [{"value": l.value, "label": l.label, "n_pass": l.n_pass, "n_scored": l.n_scored,
              "n_error": l.n_error} for l, _ in levels]
    eligible = [(l, g) for l, g in levels if l.n_scored >= MIN_N]
    row = FrontierRow(spec.key, spec.label, spec.province, "NOT TESTED", "Not tested yet",
                      curve=curve, short=spec.short)
    if not eligible:
        if levels:
            row.headline = "Too few trials to call"
        return row

    brk = next(((l, g) for l, g in eligible if l.rate <= BREAK_RATE), None)
    deg = next(((l, g) for l, g in eligible if l.rate <= DEGRADE_RATE), None)
    if brk:
        row.status, (bl, _) = "BREAKS", brk
    elif deg:
        row.status, (bl, _) = "DEGRADES", deg
    else:
        top = eligible[-1][0]
        k = sum(l.n_pass for l, _ in eligible)
        n = sum(l.n_scored for l, _ in eligible)
        ci = wilson_interval(k, n)
        row.status, row.held_through = "HOLDS", top.label
        row.headline = f"Holds through {top.label}: {k}/{n} pass across every tested level"
        row.n_pass, row.n_scored = k, n
        row.n_error = sum(l.n_error for l, _ in levels)
        row.ci = (round(ci.low, 3), round(ci.high, 3))
        row.confidence = "A" if n >= 30 and ci.low >= 0.90 else ("B" if n >= 14 else "C")
        fails = [r for _, g in eligible for r in g if r.verdict == Verdict.FAIL]
        row.cause_code, row.cause_share = _cause(spec, fails)
        row.cause = CAUSE_WORDS.get(row.cause_code) if row.cause_code else None
        return row

    idx = next(i for i, (l, _) in enumerate(eligible) if l.value == bl.value)
    before_lv = [l for l, _ in eligible[:idx] if l.rate > DEGRADE_RATE]
    held = before_lv[-1] if before_lv else None
    after = [(l, g) for l, g in eligible[idx:]]
    ci = wilson_interval(bl.n_pass, bl.n_scored)
    before_pool = (sum(l.n_pass for l in before_lv), sum(l.n_scored for l in before_lv)) if before_lv else None
    after_pool = (sum(l.n_pass for l, _ in after), sum(l.n_scored for l, _ in after))
    row.confidence, row.p_value = _grade_axis(before_pool, after_pool, ci.high, row.status)
    row.boundary = bl.label
    row.held_through = held.label if held else None
    row.n_pass, row.n_scored, row.n_error = bl.n_pass, bl.n_scored, bl.n_error
    row.ci = (round(ci.low, 3), round(ci.high, 3))
    verb = "Breaks" if row.status == "BREAKS" else "Degrades"
    row.headline = f"{verb} at {bl.label}: {bl.n_pass}/{bl.n_scored} pass"
    first = eligible[0][0]
    if held:
        row.headline += f" (was {held.n_pass}/{held.n_scored} at {held.label})"
    elif idx == 0:
        row.headline += " -- already weak at the easiest level tested"
    else:
        row.headline += f" (already only {first.n_pass}/{first.n_scored} at {first.label})"
    fails = [r for _, g in after for r in g if r.verdict == Verdict.FAIL]
    row.cause_code, row.cause_share = _cause(spec, fails)
    row.cause = CAUSE_WORDS.get(row.cause_code) if row.cause_code else None
    boundary_fails = [r for r in next(g for l, g in eligible if l.value == bl.value) if r.verdict == Verdict.FAIL]
    if boundary_fails:
        row.exemplar = _exemplar(boundary_fails[0])
    return row


_CEILING_DIMS = (("chain_length", "chain length", "{:.0f}"), ("scramble", "scramble", "{:.1f}"),
                 ("coref_pressure", "pronoun pressure", "{:.0f}"),
                 ("distractor_events", "distractor events", "{:.0f}"),
                 ("witness_cutoff", "witness cutoff", "{:.1f}"))


def _row_rate(spec: Spec, recs: list) -> FrontierRow:
    row = FrontierRow(spec.key, spec.label, spec.province, "NOT TESTED", "Not tested yet", short=spec.short)
    k = sum(r.verdict == Verdict.PASS for r in recs)
    n = sum(r.verdict in (Verdict.PASS, Verdict.FAIL) for r in recs)
    row.n_error = sum(r.verdict == Verdict.ERROR for r in recs)
    if n < MIN_N:
        if n:
            row.headline = "Too few trials to call"
        return row
    ci = wilson_interval(k, n)
    rate = k / n
    # Rate-mode rows have no axis to locate a boundary on, so the call is
    # statistical: DEGRADES only when the 95% CI sits entirely below 95%
    # (30/30 is HOLDS even though its CI dips to 0.89; 521/578 is DEGRADES).
    if rate <= BREAK_RATE:
        row.status = "BREAKS"
    elif ci.high < HOLD_UPPER_BOUND:
        row.status = "DEGRADES"
    else:
        row.status = "HOLDS"
    row.n_pass, row.n_scored, row.ci = k, n, (round(ci.low, 3), round(ci.high, 3))
    row.confidence = "A" if n >= 100 else ("B" if n >= 20 else "C")
    if spec.key == "impossible":
        row.headline = f"Declines {k} of {n} impossible tasks; writes a plan for the other {n - k}"
    else:
        row.headline = f"{rate:.0%} correct ({k}/{n})"
        maxima = []
        for key, name, fmt in _CEILING_DIMS:
            vals = [r.latent_params[key] for r in recs if key in r.latent_params]
            if vals and max(vals) > 0:
                maxima.append(f"{name} {fmt.format(max(vals))}")
        if maxima:
            row.held_through = "tested up to " + ", ".join(maxima)
    fails = [r for r in recs if r.verdict == Verdict.FAIL]
    row.cause_code, row.cause_share = _cause(spec, fails)
    row.cause = CAUSE_WORDS.get(row.cause_code) if row.cause_code else None
    if fails:
        row.exemplar = _exemplar(fails[0])
    return row


def _verdict(name: str, rows: list[FrontierRow]) -> str:
    breaks = [r for r in rows if r.status == "BREAKS"]
    degrades = [r for r in rows if r.status == "DEGRADES"]
    tested = [r for r in rows if r.status != "NOT TESTED"]
    holds = [r for r in tested if r.status == "HOLDS"]
    if not tested:
        return f"{name}: not tested yet."
    parts = []
    for r in breaks:
        parts.append(f"breaks on {r.short}" + (f" ({r.boundary})" if r.boundary else ""))
    for r in degrades:
        parts.append(f"slips on {r.short}" + (f" ({r.boundary})" if r.boundary else ""))
    if not parts:
        return f"{name}: holds on all {len(tested)} capabilities tested."
    return f"{name}: " + "; ".join(parts) + f". Holds on {len(holds)} of {len(tested)} capabilities tested."


def _matches(spec: Spec, r) -> bool:
    if spec.kind is not None and _query_kind_of(r) != spec.kind:
        return False
    if spec.runs and not any(re.search(p, r.run_id) for p in spec.runs):
        return False
    return spec.where is None or bool(spec.where(r))


def frontier_summaries(store, models: list[str] | None = None) -> list[ModelFrontier]:
    """One `ModelFrontier` per subject model in the store (or `models`)."""
    records = sorted(_primary_records(store), key=lambda r: (r.created_at, r.record_id))
    by_model: dict[str, list] = defaultdict(list)
    for r in records:
        by_model[r.model_name].append(r)
    out = []
    for model in models or sorted(by_model):
        recs = by_model.get(model, [])
        rows = []
        for spec in SPECS:
            mine = [r for r in recs if _matches(spec, r)] if spec.mode != "absent" else []
            if spec.mode == "axis":
                row = _row_axis(spec, mine)
            elif spec.mode == "rate":
                row = _row_rate(spec, mine)
            else:
                row = FrontierRow(spec.key, spec.label, spec.province, "NOT TESTED", "Not tested yet",
                                  short=spec.short)
            row.note = row.note or spec.note
            rows.append(row)
        name = short_model_name(model)
        out.append(ModelFrontier(model, name, _verdict(name, rows), rows))
    return out


def frontier_summary(store, model: str) -> ModelFrontier:
    return frontier_summaries(store, [model])[0]
