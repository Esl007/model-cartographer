"""Adversarial proposer for the TOOL-PLANNING province: Nemotron proposes
toolworld trap configs (scalar `ToolDimParams` dims plus the five structural
0/1 mutator dims from `cartographer.toolworld.mutate`), a deterministic
gauntlet validates and trap-scores them offline, and survivors are measured
with real subject-model trials through `ToolBreakingPointEvaluator` -- the
same generate -> mutate -> render -> chat -> verify pipeline the escalation
campaign uses.

Ground truth discipline is identical to `loop.py`: the proposer LLM only
ever sees a digest and only ever emits configs + hypotheses; every verdict
comes from the deterministic plan verifier, never from the proposer.

The control arm here differs deliberately from the narrative loop's: instead
of re-measuring the survivors' own configs at plain seeds, round 0 measures
an equal-count batch of RANDOM configs drawn uniformly (sha256-derived) from
the same legal ranges. `beat_random` therefore answers the sharper question
"do Nemotron's proposed CONFIGS fail the subject more than random configs
do," which is the honest test when the proposal space is the config itself.
"""

from __future__ import annotations

import hashlib
import statistics
import threading
from dataclasses import dataclass, field

from ..confirm.stats import WilsonInterval, wilson_interval
from ..exec.evaluator import (
    ToolBreakingPointEvaluator,
    _apply_tool_config,
    _apply_tool_trap_config,
    _default_tool_base,
)
from ..store.schema import Verdict
from ..toolworld.foils import foil_greedy_lookahead0
from ..toolworld.generate import generate as tool_generate
from ..toolworld.simulate import simulate_plan, validate
from .loop import ContaminationError, RoundStats, _derive_round_seed
from .proposer import Proposal, propose_configs

_HIT_THRESHOLD = 0.30

# (base_default, min, max, is_int) per config key the proposer may set.
# Scalar ranges mirror `_tool_planning_scheduler_config`'s DimSpecs; the five
# trap dims mirror `_TOOL_TRAP_LIMITS` (0/1 toggles). `interleaved_goals` is
# not a key: it applies automatically whenever parallel_goals == 2 (see
# `mutate.interleaved_goals_applicable`).
_TOOL_FIELD_RANGES: dict[str, tuple[float, float, float, bool]] = {
    "plan_depth": (3, 1, 6, True),
    "decoy_tools": (2, 0, 5, True),
    "precondition_chain": (1, 0, 2, True),
    "state_obscurity": (0.0, 0.0, 1.0, False),
    "parallel_goals": (1, 1, 2, True),
    "poison_traps": (0, 0, 1, True),
    "dishonest_decoys_on": (0, 0, 1, True),
    "implicit_preconditions_on": (0, 0, 1, True),
    "zero_slack_on": (0, 0, 1, True),
    "id_collisions_on": (0, 0, 1, True),
}

_SCALAR_KEYS = (
    "plan_depth", "decoy_tools", "precondition_chain",
    "state_obscurity", "parallel_goals",
)
_TRAP_KEYS = (
    "poison_traps", "dishonest_decoys_on", "implicit_preconditions_on",
    "zero_slack_on", "id_collisions_on",
)

TOOL_SCHEMA_TEXT = """\
Config schema for the TOOL-CALL PLANNING province (all fields optional;
omitted fields use generator defaults). The subject model is shown a devops
tool catalog and must emit a JSON plan of tool calls; a deterministic
simulator verifies the plan step by step.

Scalar difficulty dims:
  plan_depth: int, 1..6           (length of the optimal tool-call chain)
  decoy_tools: int, 0..5          (irrelevant tools mixed into the catalog)
  precondition_chain: int, 0..2   (extra flag/artifact dependency layers)
  state_obscurity: float, 0.0..1.0 (how scrambled/indirect the state narration is)
  parallel_goals: int, 1..2       (2 = two independent goals in one plan;
    also automatically interleaves the goals' tool orderings)

Structural trap dims (each 0 or 1):
  poison_traps: 1 plants a trap tool that silently forecloses the goal if called
  dishonest_decoys_on: 1 renames decoy tools to sound load-bearing and removes
    their self-confessing descriptions
  implicit_preconditions_on: 1 strips per-tool "Precondition:" sentences into
    one general environment-mechanics paragraph the model must apply itself
  zero_slack_on: 1 tightens the call budget to exactly the optimal depth
    (any wasted call fails the plan)
  id_collisions_on: 1 adds near-collision tool ids (e.g. advance_o1 vs
    adva-nce_1) so sloppy id binding picks a wrong tool

There is no require_traps vocabulary for this province; omit it.
"""


@dataclass(frozen=True)
class ToolGauntletResult:
    """Outcome of running one tool-config `Proposal` through the offline
    gauntlet. `accepted=True` iff the config is well-formed, generates a
    solvable (BFS-validated) world after trap mutation at some candidate
    seed, and its greedy-foil failure rate is at least the median of a
    random-seed control batch at the same config."""

    proposal: Proposal
    accepted: bool
    reason: str
    config: dict | None = None
    seed: int | None = None
    greedy_fail_rate: float = 0.0
    random_median_fail_rate: float = 0.0


@dataclass(frozen=True)
class ToolTrapEntry:
    config: dict
    hypothesis: str
    failure_rate: float
    wilson: WilsonInterval
    round_index: int
    run_id: str


@dataclass
class ToolProposerLoopResult:
    trap_library: list[ToolTrapEntry] = field(default_factory=list)
    round_stats: list[RoundStats] = field(default_factory=list)
    total_spend_usd: float = 0.0
    beat_random: bool | None = None
    proposer_mean_failure_rate: float = 0.0
    control_mean_failure_rate: float = 0.0
    stopped_reason: str = "rounds_exhausted"


def clamp_tool_config(raw_config: dict) -> dict | None:
    """Clamp `raw_config` into the legal `_TOOL_FIELD_RANGES`. Unknown keys
    (including require_traps echoes) are dropped; a non-numeric value for a
    known key makes the config unsalvageable (returns None)."""
    clamped: dict = {}
    for key, value in raw_config.items():
        if key not in _TOOL_FIELD_RANGES:
            continue
        _, lo, hi, is_int = _TOOL_FIELD_RANGES[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        v = max(lo, min(hi, float(value)))
        clamped[key] = float(int(round(v))) if is_int else v
    return clamped


def _greedy_fails(config: dict, seed: int) -> bool | None:
    """Deterministically test one (config, seed): generate the world, apply
    the trap mutators, and simulate the zero-lookahead greedy foil. Returns
    True if the world is valid AND the greedy foil misses the goal (a salient
    trap), False if valid but greedy still solves it, None if the mutated
    world fails `validate` (unusable seed)."""
    params = _apply_tool_config(
        _default_tool_base(), {k: v for k, v in config.items() if k in _SCALAR_KEYS}
    )
    world = tool_generate(params, seed)
    world, _style = _apply_tool_trap_config(world, config, seed)
    if validate(world):
        return None
    plan = foil_greedy_lookahead0(world)
    return not simulate_plan(world, plan).reached_goal


def run_tool_gauntlet(
    proposal: Proposal,
    base_seed: int,
    n_candidates: int = 12,
    n_random_control: int = 12,
) -> ToolGauntletResult:
    """Run one tool-config `Proposal` through the offline gauntlet. Zero API
    calls: everything is generator + BFS + greedy-foil simulation."""
    config = clamp_tool_config(proposal.config)
    if config is None or not config:
        return ToolGauntletResult(proposal=proposal, accepted=False, reason="invalid_config")

    fails = 0
    valid = 0
    best_seed: int | None = None
    for i in range(n_candidates):
        payload = f"tool_gauntlet_seed|{base_seed}|{i}".encode("utf-8")
        seed = int(hashlib.sha256(payload).hexdigest()[:15], 16)
        outcome = _greedy_fails(config, seed)
        if outcome is None:
            continue
        valid += 1
        if outcome:
            fails += 1
            if best_seed is None:
                best_seed = seed
    if valid == 0:
        return ToolGauntletResult(
            proposal=proposal, accepted=False, reason="no_valid_world", config=config
        )
    greedy_fail_rate = fails / valid

    # Random-config control: the same seeds scored under random configs from
    # the same legal ranges (distinct hash namespace). The proposal must meet
    # the median random config's greedy-fail rate to earn subject trials.
    random_rates: list[float] = []
    for j in range(n_random_control):
        rcfg = _random_config(base_seed, j)
        r_fails = 0
        r_valid = 0
        for i in range(n_candidates):
            payload = f"tool_gauntlet_ctrl_seed|{base_seed}|{j}|{i}".encode("utf-8")
            seed = int(hashlib.sha256(payload).hexdigest()[:15], 16)
            outcome = _greedy_fails(rcfg, seed)
            if outcome is None:
                continue
            r_valid += 1
            if outcome:
                r_fails += 1
        if r_valid:
            random_rates.append(r_fails / r_valid)
    median = statistics.median(random_rates) if random_rates else 0.0

    if greedy_fail_rate < median:
        return ToolGauntletResult(
            proposal=proposal,
            accepted=False,
            reason="below_random_median",
            config=config,
            greedy_fail_rate=greedy_fail_rate,
            random_median_fail_rate=median,
        )
    return ToolGauntletResult(
        proposal=proposal,
        accepted=True,
        reason="accepted",
        config=config,
        seed=best_seed,
        greedy_fail_rate=greedy_fail_rate,
        random_median_fail_rate=median,
    )


def _random_config(base_seed: int, index: int) -> dict:
    """A uniform random config over `_TOOL_FIELD_RANGES`, sha256-derived so
    the control arm is reproducible."""
    config: dict = {}
    for key, (_, lo, hi, is_int) in _TOOL_FIELD_RANGES.items():
        payload = f"tool_random_config|{base_seed}|{index}|{key}".encode("utf-8")
        u = int(hashlib.sha256(payload).hexdigest()[:15], 16) / float(16**15)
        v = lo + u * (hi - lo)
        config[key] = float(int(round(v))) if is_int else v
    return config


def _measure_tool_config(
    evaluator: ToolBreakingPointEvaluator,
    config: dict,
    k_trials: int,
    seed_base: int,
) -> tuple[int, int]:
    """`k_trials` real subject trials of `config`. Returns (n_failures,
    n_trials_run); stops early on cost cap / cancellation. ERROR trials
    (API failures, token caps) are excluded from both counts so failure
    rates only reflect verifier FAILs."""
    n_fail = 0
    n_run = 0
    for i in range(k_trials):
        trial_seed = _derive_round_seed(str(seed_base), i, "trial")
        if evaluator.capped or evaluator.cancelled:
            break
        errors_before = evaluator.error_count
        passed = evaluator(config, trial_seed)
        if evaluator.error_count > errors_before:
            continue
        n_run += 1
        if not passed:
            n_fail += 1
    return n_fail, n_run


def build_tool_digest(store, subject_model: str, max_chars: int = 4000) -> str:
    """Plain-text evidence digest of `subject_model`'s tool_plan records:
    overall pass rate, per-trap-dim pass rates, and recent FAIL configs with
    verifier detail codes. Deterministic given store contents; no API."""
    records = [
        r
        for r in store.by_model(subject_model)
        if "kind=tool_plan" in (r.verifier_detail or "")
    ]
    if not records:
        return (
            f"No prior tool-planning evidence for subject model {subject_model!r}."
        )
    lines = [f"Tool-planning evidence digest for subject model {subject_model!r}:"]

    scored = [r for r in records if r.verdict in (Verdict.PASS, Verdict.FAIL)]
    n_pass = sum(1 for r in scored if r.verdict == Verdict.PASS)
    lines.append("")
    lines.append(
        f"Overall: {n_pass}/{len(scored)} pass"
        + (f" ({n_pass / len(scored):.0%})" if scored else "")
    )

    lines.append("")
    lines.append("Pass rates with each trap dim active:")
    for key in _TRAP_KEYS:
        on = [r for r in scored if r.latent_params.get(key, 0.0) >= 0.5]
        if not on:
            continue
        on_pass = sum(1 for r in on if r.verdict == Verdict.PASS)
        lines.append(f"  {key}=1: {on_pass}/{len(on)} pass")

    fails = [r for r in records if r.verdict == Verdict.FAIL]
    fails.sort(key=lambda r: r.created_at, reverse=True)
    if fails:
        lines.append("")
        lines.append("Recent FAIL configs (most recent first):")
        for r in fails[:12]:
            cfg = ", ".join(
                f"{k}={r.latent_params[k]:g}"
                for k in (*_SCALAR_KEYS, *_TRAP_KEYS)
                if k in r.latent_params
            )
            detail = ""
            for part in (r.verifier_detail or "").split(";"):
                if part.startswith("detail="):
                    detail = f" detail={part[len('detail='):]}"
                    break
            lines.append("  {" + cfg + "}" + detail)

    digest = "\n".join(lines)
    if len(digest) > max_chars:
        digest = digest[: max_chars - 20].rstrip() + "\n...[truncated]"
    return digest


def run_tool_proposer_loop(
    client,
    store,
    run_id: str,
    proposer_model: str,
    subject_model: str,
    rounds: int = 3,
    proposals_per_round: int = 6,
    k_trials: int = 5,
    cost_cap: float = 0.5,
    cancel_event: threading.Event | None = None,
) -> ToolProposerLoopResult:
    """The tool-province analogue of `run_proposer_loop`: digest -> propose
    (tool schema) -> offline gauntlet -> real subject trials via
    `ToolBreakingPointEvaluator` -> trap library, plus a round-0 random-config
    control arm for `beat_random`. Same contamination guard, same
    single-running-total cost cap."""
    if proposer_model == subject_model:
        raise ContaminationError(
            f"contamination guard: proposer_model {proposer_model!r} must not "
            f"equal subject_model {subject_model!r}"
        )

    result = ToolProposerLoopResult()
    duds: list[Proposal] = []

    evaluator = ToolBreakingPointEvaluator(
        client=client,
        model=subject_model,
        store=store,
        run_id=run_id,
        cost_cap_usd=cost_cap,
    )

    proposer_rates: list[float] = []
    control_rates: list[float] = []

    for round_index in range(rounds):
        stats = RoundStats(round_index=round_index)

        if evaluator.capped or (cancel_event is not None and cancel_event.is_set()):
            result.stopped_reason = "cost_cap_hit" if evaluator.capped else "cancelled"
            break

        digest = build_tool_digest(store, subject_model)
        if duds:
            dud_lines = [
                "",
                "Previously proposed and did NOT fail (avoid repeating these regions):",
            ]
            for p in duds[-12:]:
                dud_lines.append(f"  config={p.config} hypothesis={p.hypothesis!r}")
            digest = digest + "\n" + "\n".join(dud_lines)

        spend_before = evaluator.spent_usd
        proposer_seed = _derive_round_seed(run_id, round_index, "propose")
        proposals = propose_configs(
            client,
            proposer_model,
            digest,
            n=proposals_per_round,
            seed=proposer_seed,
            schema=TOOL_SCHEMA_TEXT,
        )
        stats.proposed = len(proposals)
        if not proposals:
            result.round_stats.append(stats)
            continue

        gauntlet_seed = _derive_round_seed(run_id, round_index, "gauntlet")
        gauntlet_results = []
        for i, proposal in enumerate(proposals):
            payload = f"tool_gauntlet_proposal|{gauntlet_seed}|{i}".encode("utf-8")
            pseed = int(hashlib.sha256(payload).hexdigest()[:15], 16)
            gauntlet_results.append(run_tool_gauntlet(proposal, base_seed=pseed))
        survivors = [g for g in gauntlet_results if g.accepted]
        stats.rejected_at_gauntlet = len(gauntlet_results) - len(survivors)

        # Round-0 random-config control arm, same survivor count, distinct
        # run_id so control records never mix with proposer-selected ones.
        if round_index == 0 and survivors:
            control_run_id = f"{run_id}/control"
            control_evaluator = ToolBreakingPointEvaluator(
                client=client,
                model=subject_model,
                store=store,
                run_id=control_run_id,
                cost_cap_usd=max(0.0, cost_cap - evaluator.spent_usd) / 2.0,
            )
            control_seed = _derive_round_seed(control_run_id, round_index, "cfg")
            for i in range(len(survivors)):
                if control_evaluator.capped:
                    break
                rcfg = _random_config(control_seed, i)
                seed_base = _derive_round_seed(control_run_id, i, "control")
                n_fail, n_run = _measure_tool_config(
                    control_evaluator, rcfg, k_trials, seed_base
                )
                stats.control_measured += n_run
                if n_run:
                    rate = n_fail / n_run
                    control_rates.append(rate)
                    if rate >= _HIT_THRESHOLD:
                        stats.control_hits += 1
            evaluator.spent_usd += control_evaluator.spent_usd
            evaluator.call_count += control_evaluator.call_count

        subject_run_id = f"{run_id}/round{round_index}"
        round_evaluator = ToolBreakingPointEvaluator(
            client=client,
            model=subject_model,
            store=store,
            run_id=subject_run_id,
            cost_cap_usd=max(0.0, cost_cap - evaluator.spent_usd),
        )
        for i, g in enumerate(survivors):
            if round_evaluator.capped or evaluator.capped:
                break
            assert g.config is not None
            seed_base = _derive_round_seed(subject_run_id, i, "measure")
            n_fail, n_run = _measure_tool_config(
                round_evaluator, g.config, k_trials, seed_base
            )
            stats.measured += n_run
            if n_run == 0:
                continue
            rate = n_fail / n_run
            proposer_rates.append(rate)
            if rate >= _HIT_THRESHOLD:
                stats.hits += 1
                result.trap_library.append(
                    ToolTrapEntry(
                        config=g.config,
                        hypothesis=g.proposal.hypothesis,
                        failure_rate=rate,
                        wilson=wilson_interval(n_fail, n_run),
                        round_index=round_index,
                        run_id=subject_run_id,
                    )
                )
            else:
                duds.append(g.proposal)

        evaluator.spent_usd += round_evaluator.spent_usd
        evaluator.call_count += round_evaluator.call_count
        stats.spend_usd = evaluator.spent_usd - spend_before
        result.round_stats.append(stats)

        if round_evaluator.capped or evaluator.spent_usd >= cost_cap:
            evaluator.capped = True
            result.stopped_reason = "cost_cap_hit"
            break

    result.total_spend_usd = evaluator.spent_usd
    if proposer_rates:
        result.proposer_mean_failure_rate = sum(proposer_rates) / len(proposer_rates)
    if control_rates:
        result.control_mean_failure_rate = sum(control_rates) / len(control_rates)
    if proposer_rates and control_rates:
        result.beat_random = (
            result.proposer_mean_failure_rate > result.control_mean_failure_rate
        )
    return result
