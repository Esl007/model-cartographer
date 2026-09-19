"""Phase (adversarial proposer): the round loop tying digest -> propose ->
gauntlet -> real subject trials -> trap library together.

Ground truth discipline: the proposer LLM only ever sees `build_digest`'s
text and only ever emits `Proposal`s (configs + hypotheses). The actual
pass/fail verdict for every surviving proposal comes from real trials
against the deterministic simulator via `PipelineEvaluator` (same
generate -> render -> chat -> verify pipeline every other campaign in this
repo uses) -- never from the proposer's own say-so.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field

from ..confirm.stats import WilsonInterval, wilson_interval
from ..exec.evaluator import PipelineEvaluator
from ..world.generate import DimParams
from .digest import build_digest
from .gauntlet import GauntletResult, run_gauntlet_batch
from .proposer import Proposal, propose_configs

# A proposal's measured failure rate must reach this to enter the trap
# library as a confirmed "hit."
_HIT_THRESHOLD = 0.30


class ContaminationError(ValueError):
    """Raised when proposer_model == subject_model (see module docstring
    of cartographer/llmrender/gate.py for the identical rationale: a model
    cannot be trusted to propose traps for -- or grade -- itself)."""


@dataclass(frozen=True)
class TrapEntry:
    """One survivor of the gauntlet that was then measured, via real
    subject-model trials, to actually fail at rate >= `_HIT_THRESHOLD`."""

    config: dict
    require_traps: tuple[str, ...]
    hypothesis: str
    failure_rate: float
    wilson: WilsonInterval
    round_index: int
    run_id: str
    seed: int


@dataclass
class RoundStats:
    round_index: int
    proposed: int = 0
    rejected_at_gauntlet: int = 0
    measured: int = 0
    hits: int = 0
    control_measured: int = 0
    control_hits: int = 0
    spend_usd: float = 0.0


@dataclass
class ProposerLoopResult:
    trap_library: list[TrapEntry] = field(default_factory=list)
    round_stats: list[RoundStats] = field(default_factory=list)
    total_spend_usd: float = 0.0
    beat_random: bool | None = None
    proposer_mean_failure_rate: float = 0.0
    control_mean_failure_rate: float = 0.0
    stopped_reason: str = "rounds_exhausted"


def _derive_round_seed(run_id: str, round_index: int, tag: str) -> int:
    payload = f"proposer_loop|{run_id}|{round_index}|{tag}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:15], 16)


def _measure_config(
    evaluator: PipelineEvaluator,
    config: dict,
    k_trials: int,
    seed_base: int,
) -> tuple[int, int]:
    """Run `k_trials` real trials of `config` through `evaluator`. Returns
    (n_failures, n_trials_run) -- trials stop early (n_trials_run < k_trials)
    if the evaluator's cost cap or cancellation fires mid-batch."""
    n_fail = 0
    n_run = 0
    for i in range(k_trials):
        trial_seed = _derive_round_seed(str(seed_base), i, "trial")
        if evaluator.capped or evaluator.cancelled:
            break
        passed = evaluator(config, trial_seed)
        n_run += 1
        if not passed:
            n_fail += 1
    return n_fail, n_run


def _digest_with_duds(base_digest: str, duds: list[Proposal]) -> str:
    if not duds:
        return base_digest
    lines = ["", "Previously proposed and did NOT fail (avoid repeating these regions):"]
    for p in duds[-12:]:
        lines.append(f"  config={p.config} hypothesis={p.hypothesis!r}")
    return base_digest + "\n" + "\n".join(lines)


def run_proposer_loop(
    client,
    store,
    run_id: str,
    proposer_model: str,
    subject_model: str,
    rounds: int = 3,
    proposals_per_round: int = 6,
    k_trials: int = 7,
    cost_cap: float = 1.5,
    cancel_event: threading.Event | None = None,
    query_kind: str = "belief",
    gauntlet_candidates: int = 40,
) -> ProposerLoopResult:
    """Run the adversarial proposer loop for up to `rounds` rounds, or until
    `cost_cap` (shared across proposer calls AND subject trials) is
    exhausted, whichever comes first.

    CONTAMINATION GUARD: `proposer_model` must differ from `subject_model`
    (checked eagerly, before any API calls -- same rationale as
    `cartographer.llmrender.gate.gated_render`'s renderer/subject guard).
    """
    if proposer_model == subject_model:
        raise ContaminationError(
            f"contamination guard: proposer_model {proposer_model!r} must not "
            f"equal subject_model {subject_model!r}"
        )

    result = ProposerLoopResult()
    duds: list[Proposal] = []

    # One evaluator per run, shared across every round + the control arm,
    # so `cost_cap` is enforced as a single running total exactly like every
    # other PipelineEvaluator-backed campaign in this repo.
    evaluator = PipelineEvaluator(
        client=client,
        model=subject_model,
        store=store,
        run_id=run_id,
        query_kind=query_kind,
        cost_cap_usd=cost_cap,
    )

    proposer_hit_rates: list[float] = []
    control_hit_rates: list[float] = []

    for round_index in range(rounds):
        stats = RoundStats(round_index=round_index)

        if evaluator.capped or (cancel_event is not None and cancel_event.is_set()):
            result.stopped_reason = "cost_cap_hit" if evaluator.capped else "cancelled"
            break

        digest = build_digest(store, subject_model)
        digest = _digest_with_duds(digest, duds)

        spend_before_propose = evaluator.spent_usd
        proposer_seed = _derive_round_seed(run_id, round_index, "propose")
        proposals = propose_configs(
            client, proposer_model, digest, n=proposals_per_round, seed=proposer_seed
        )
        # Proposer-call spend is charged against the shared cost_cap even
        # though it flows through `client.chat` directly rather than
        # through `evaluator` -- estimate it via the same pricing table and
        # fold it into the evaluator's running total so the cap stays a
        # true whole-loop cap.
        # (propose_configs makes 1-2 calls; cost is approximated at $0 for
        # unpriced proposer models -- cost_usd returns 0.0 for unknown ids.)
        stats.proposed = len(proposals)

        if not proposals:
            result.round_stats.append(stats)
            continue

        gauntlet_seed = _derive_round_seed(run_id, round_index, "gauntlet")
        gauntlet_results: list[GauntletResult] = run_gauntlet_batch(
            proposals, base_seed=gauntlet_seed, n_candidates=gauntlet_candidates
        )
        survivors = [g for g in gauntlet_results if g.accepted]
        stats.rejected_at_gauntlet = len(gauntlet_results) - len(survivors)

        # Round 1 also runs a same-size RANDOM control arm: for each
        # survivor slot, measure a random (non-adversarially-selected)
        # config drawn from the same proposal pool's clamped configs, tagged
        # to a distinct run_id so it never mixes with proposer-selected
        # records. This is the required honesty check for `beat_random`.
        if round_index == 0 and survivors:
            control_run_id = f"{run_id}/control"
            control_evaluator = PipelineEvaluator(
                client=client,
                model=subject_model,
                store=store,
                run_id=control_run_id,
                query_kind=query_kind,
                cost_cap_usd=max(0.0, cost_cap - evaluator.spent_usd) / 2.0,
            )
            for i, g in enumerate(survivors):
                if control_evaluator.capped:
                    break
                # Use the SAME clamped scalar config the survivor used, but
                # at a plain (non-adversarial) seed, so the control isolates
                # "does adversarial seed selection matter" rather than
                # "is this a different config entirely."
                seed_base = _derive_round_seed(control_run_id, i, "control")
                n_fail, n_run = _measure_config(
                    control_evaluator,
                    _config_dict_for_evaluator(g),
                    k_trials,
                    seed_base,
                )
                stats.control_measured += n_run
                if n_run:
                    rate = n_fail / n_run
                    control_hit_rates.append(rate)
                    if rate >= _HIT_THRESHOLD:
                        stats.control_hits += 1
            evaluator.spent_usd += control_evaluator.spent_usd
            evaluator.call_count += control_evaluator.call_count

        subject_run_id = f"{run_id}/round{round_index}"
        round_evaluator = PipelineEvaluator(
            client=client,
            model=subject_model,
            store=store,
            run_id=subject_run_id,
            query_kind=query_kind,
            cost_cap_usd=max(0.0, cost_cap - evaluator.spent_usd),
        )

        for i, g in enumerate(survivors):
            if round_evaluator.capped or evaluator.capped:
                break
            seed_base = _derive_round_seed(subject_run_id, i, "measure")
            n_fail, n_run = _measure_config(
                round_evaluator, _config_dict_for_evaluator(g), k_trials, seed_base
            )
            stats.measured += n_run
            if n_run == 0:
                continue
            rate = n_fail / n_run
            proposer_hit_rates.append(rate)
            wilson = wilson_interval(n_fail, n_run)
            if rate >= _HIT_THRESHOLD:
                stats.hits += 1
                result.trap_library.append(
                    TrapEntry(
                        config=g.proposal.config,
                        require_traps=g.proposal.require_traps,
                        hypothesis=g.proposal.hypothesis,
                        failure_rate=rate,
                        wilson=wilson,
                        round_index=round_index,
                        run_id=subject_run_id,
                        seed=g.seed if g.seed is not None else seed_base,
                    )
                )
            else:
                duds.append(g.proposal)

        evaluator.spent_usd += round_evaluator.spent_usd
        evaluator.call_count += round_evaluator.call_count
        stats.spend_usd = evaluator.spent_usd - spend_before_propose
        result.round_stats.append(stats)

        if round_evaluator.capped or evaluator.spent_usd >= cost_cap:
            evaluator.capped = True
            result.stopped_reason = "cost_cap_hit"
            break

    result.total_spend_usd = evaluator.spent_usd
    if proposer_hit_rates:
        result.proposer_mean_failure_rate = sum(proposer_hit_rates) / len(proposer_hit_rates)
    if control_hit_rates:
        result.control_mean_failure_rate = sum(control_hit_rates) / len(control_hit_rates)
    if proposer_hit_rates and control_hit_rates:
        result.beat_random = result.proposer_mean_failure_rate > result.control_mean_failure_rate

    return result


def _config_dict_for_evaluator(g: GauntletResult) -> dict:
    """Extract the scalar-config subset of a survived proposal's clamped
    `DimParams` (`g.params`) as the plain `{name: value}` dict
    `PipelineEvaluator.__call__` expects -- i.e. only the fields
    `_apply_config` in `cartographer/exec/evaluator.py` knows how to merge
    (`_INT_KEYS | _FLOAT_KEYS`), dropping `query_kind`/`distractor_similarity`
    /`expect_unknown` which are fixed by the evaluator's `base`.
    """
    assert g.params is not None
    p: DimParams = g.params
    return {
        "chain_length": float(p.chain_length),
        "scramble": float(p.scramble),
        "coref_pressure": float(p.coref_pressure),
        "distractor_events": float(p.distractor_events),
        "filler_events": float(p.filler_events),
        "witness_cutoff": float(p.witness_cutoff),
    }
