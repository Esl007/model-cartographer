"""Phase 6 glue: wire the Phase 5 scheduler to the real generate/render/chat/
verify pipeline, with cost-capped, fully-persisted trials.

`PipelineEvaluator` is a `TaskEvaluator` (same `(config, trial_seed) -> bool`
contract as `cartographer.schedule.scheduler.TaskEvaluator`) that, on each
call: derives a deterministic task seed from (config, trial_seed), generates
a world, renders it, calls the chat client, verifies the response, and
appends a full `ExperimentRecord` to the store -- mirroring the pattern in
`cartographer.exec.pilot.run_pilot`, one trial at a time instead of a fixed
grid.

`run_state_campaign` / `run_belief_campaign` are thin `SchedulerConfig`
presets over `cartographer.schedule.scheduler.run_campaign` for the two
query kinds explored so far.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field, replace
from typing import Callable

from ..llmrender.gate import gated_render, record_gate_rejection
from ..llmrender.renderer import assemble_prompt
from ..render import build_name_map, render, verify
from ..schedule.escalate import BreakingPoint, EscalationRay, RayDim, find_breaking_point
from ..schedule.scheduler import CampaignState, DimSpec, SchedulerConfig, run_campaign
from ..store.schema import ExperimentRecord, RenderMode, Verdict
from ..store.store import ResultsStore
from ..world.generate import DimParams, generate
from ..world.measure import measure
from ..world.search import search_adversarial_seeds
from ..toolworld.generate import ToolDimParams
from ..toolworld.generate import generate as tool_generate
from ..toolworld.measure import measure as tool_measure
from ..toolworld.render import render as tool_render
from ..toolworld.search import search_adversarial_seeds as tool_search_adversarial_seeds
from ..toolworld.verify import verify_plan
from .client import Client
from .pricing import cost_usd

# Config keys that map onto integer-valued DimParams fields.
_INT_KEYS = {"chain_length", "coref_pressure", "distractor_events", "filler_events"}
# Config keys that map onto float-valued DimParams fields.
_FLOAT_KEYS = {"scramble", "witness_cutoff"}
_KNOWN_KEYS = _INT_KEYS | _FLOAT_KEYS

# Default token budgets, and the heavier budgets used for "ultra"-class
# reasoning models (id contains "ultra", case-insensitive) whose reasoning
# traces can exhaust even the standard auto-retry cap on escalated-difficulty
# tasks -- e.g. nvidia/Nemotron-3-Ultra-550b-a55b burning through 4000 tokens
# of reasoning before ever emitting an answer. These are heuristic defaults;
# an explicit `max_tokens` (and derived retry budget) passed to
# PipelineEvaluator or a campaign wrapper always wins.
_DEFAULT_MAX_TOKENS = 1500
_DEFAULT_MAX_TOKENS_RETRY = 4000
_ULTRA_MAX_TOKENS = 8000
_ULTRA_MAX_TOKENS_RETRY = 16000


def _is_ultra_model(model: str) -> bool:
    return "ultra" in model.lower()


def default_token_budget(model: str) -> tuple[int, int]:
    """(max_tokens, max_tokens_retry) heuristic defaults for `model`.

    Models whose id contains "ultra" get an 8000/16000 budget; everything
    else keeps the pre-existing 1500/4000 defaults.
    """
    if _is_ultra_model(model):
        return _ULTRA_MAX_TOKENS, _ULTRA_MAX_TOKENS_RETRY
    return _DEFAULT_MAX_TOKENS, _DEFAULT_MAX_TOKENS_RETRY


class CostCapExceeded(RuntimeError):
    """Raised (only informationally -- see PipelineEvaluator) when a
    campaign's cumulative spend has hit its cap. Carries the spend and cap
    so a caller can report exactly how far over the wall it got.
    """

    def __init__(self, spent: float, cap: float) -> None:
        super().__init__(f"cost cap exceeded: spent ${spent:.4f} >= cap ${cap:.4f}")
        self.spent = spent
        self.cap = cap


def _default_base(query_kind: str) -> DimParams:
    return DimParams(
        chain_length=2,
        scramble=0.0,
        coref_pressure=0,
        distractor_events=2,
        distractor_similarity="same_category",
        filler_events=2,
        query_kind=query_kind,
    )


def _apply_config(base: DimParams, config: dict[str, float]) -> DimParams:
    overrides: dict[str, object] = {}
    for key, value in config.items():
        if key in _INT_KEYS:
            overrides[key] = int(round(value))
        elif key in _FLOAT_KEYS:
            overrides[key] = float(value)
        else:
            raise ValueError(f"unknown config key: {key!r}")
    return replace(base, **overrides)


def _task_seed(config: dict[str, float], trial_seed: int) -> int:
    """Deterministic seed from (sorted config items, trial_seed).

    Same config + same trial_seed always replays the same world; different
    trial_seeds at the same config draw different worlds, so k-trial probes
    at one config point see genuine variety instead of k copies of one task.
    """
    items = sorted(config.items())
    payload = repr((items, trial_seed)).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:15], 16)


class PipelineEvaluator:
    """A `TaskEvaluator` backed by the real generate -> render -> chat ->
    verify pipeline, with every trial persisted to `store` and cumulative
    spend tracked against `cost_cap_usd`.

    Once `.spent_usd >= cost_cap_usd`, `.capped` becomes True and every
    subsequent call short-circuits to False with an ERROR record carrying
    `cost_capped=1` in `verifier_detail` -- no API call is made, so the
    scheduler's shared budget drains at zero additional cost once capped.

    Thread safety: the parallel scheduler (SchedulerConfig.parallelism > 1)
    invokes this evaluator concurrently from a ThreadPoolExecutor. The
    chat -> verify pipeline runs fully in parallel; only the shared mutable
    state -- cost/call/error counters, the cap check, and `store.append` --
    is serialized under `self._lock` (and `ResultsStore.append` additionally
    holds its own write lock over a `check_same_thread=False` connection).
    Because spend is only known after a call returns, up to `parallelism`
    in-flight calls may each start just under the cap, so the cap can be
    overshot by at most `parallelism - 1` calls' cost -- acceptable for a
    soft dollar cap on cheap trials.
    """

    def __init__(
        self,
        client: Client,
        model: str,
        store: ResultsStore,
        run_id: str,
        query_kind: str,
        cost_cap_usd: float,
        base: DimParams | None = None,
        max_tokens: int | None = None,
        max_tokens_retry: int | None = None,
        cancel_event: threading.Event | None = None,
        adversarial: bool = False,
        adversarial_candidates: int = 40,
        renderer_model: str | None = None,
        extractor_model: str | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.store = store
        self.run_id = run_id
        self.query_kind = query_kind
        self.cost_cap_usd = cost_cap_usd
        self.base = base if base is not None else _default_base(query_kind)
        self.adversarial = adversarial
        self.adversarial_candidates = adversarial_candidates
        # Phase 4 LLM-rendered pathway: when `renderer_model` is set, each
        # trial's world is narrated as prose by `renderer_model` behind the
        # mandatory fidelity gate (`extractor_model` verifies), instead of
        # the deterministic template renderer. Both must be set together.
        if (renderer_model is None) != (extractor_model is None):
            raise ValueError(
                "renderer_model and extractor_model must be set together"
            )
        self.renderer_model = renderer_model
        self.extractor_model = extractor_model

        default_max_tokens, default_max_tokens_retry = default_token_budget(model)
        if max_tokens is not None:
            self.max_tokens = max_tokens
            # An explicit max_tokens override with no matching retry
            # override doesn't inherit the model-heuristic retry budget
            # (that pairing no longer makes sense once max_tokens itself
            # has been overridden) -- fall back to double the override,
            # still capped/floored so retry_on_length has room to fire.
            self.max_tokens_retry = (
                max_tokens_retry if max_tokens_retry is not None else max_tokens * 2
            )
        else:
            self.max_tokens = default_max_tokens
            self.max_tokens_retry = (
                max_tokens_retry if max_tokens_retry is not None else default_max_tokens_retry
            )
        if self.max_tokens_retry <= self.max_tokens:
            self.max_tokens_retry = self.max_tokens * 2

        self.cancel_event = cancel_event

        self.spent_usd = 0.0
        self.call_count = 0
        self.error_count = 0
        self.capped = False
        self.cancelled = False
        self._lock = threading.Lock()

    def __call__(self, config: dict[str, float], trial_seed: int) -> bool:
        params = _apply_config(self.base, config)
        seed = _task_seed(config, trial_seed)

        with self._lock:
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True
                self._record_capped(params, config, seed, trial_seed, reason="cost_capped")
                return False
            if self.cancel_event is not None and self.cancel_event.is_set():
                self.cancelled = True
                self._record_capped(params, config, seed, trial_seed, reason="cancelled")
                return False

        adversarial_note = ""
        if self.adversarial:
            # Instead of generating the world directly from the derived
            # task seed, search a deterministic batch of candidate seeds
            # (all derived from the same task seed, so the whole thing
            # stays reproducible) and use the one that plants the most
            # salient trap(s) for a shallow reasoner. Pure simulation --
            # free relative to the API call that follows.
            winners = search_adversarial_seeds(
                params, seed, n_candidates=self.adversarial_candidates, top_k=1
            )
            if winners:
                seed, profile = winners[0]
                traps = ",".join(profile.trap_kinds)
                adversarial_note = (
                    f";adversarial=1;traps={traps}"
                    f";foil_score={round(profile.salience_weighted_score, 4)}"
                )
                adversarial_n_diverging = float(profile.n_diverging)
            else:
                adversarial_note = ";adversarial=1;traps=;foil_score=0"
                adversarial_n_diverging = 0.0

        world = generate(params, seed)
        name_map = build_name_map(world, seed)
        rendered = render(world, name_map, seed)
        realized_dims = measure(world)
        if self.adversarial:
            realized_dims = dict(realized_dims)
            realized_dims["foil_divergence"] = adversarial_n_diverging

        verifier_detail = f"kind={self.query_kind};campaign=1{adversarial_note}"

        prompt = rendered.prompt
        render_mode = RenderMode.TEMPLATE
        if self.renderer_model is not None:
            gate = gated_render(
                world,
                name_map,
                self.client,
                renderer_model=self.renderer_model,
                extractor_model=self.extractor_model,
                subject_model=self.model,
                seed=seed,
            )
            # GateResult only reports total tokens across renderer+extractor
            # calls; charge them all at the pricier of the two models' output
            # rate -- a conservative upper bound that keeps the cost cap honest.
            gate_rate = max(
                cost_usd(self.renderer_model, 0, gate.tokens_spent),
                cost_usd(self.extractor_model, 0, gate.tokens_spent),
            )
            with self._lock:
                self.call_count += gate.attempts
                self.spent_usd += gate_rate
                if self.spent_usd >= self.cost_cap_usd:
                    self.capped = True
            if not gate.accepted:
                with self._lock:
                    self.error_count += 1
                record_gate_rejection(
                    self.store, self.run_id, world, name_map, gate, task_seed=seed
                )
                return False
            prompt = assemble_prompt(gate.story, world, name_map)
            render_mode = RenderMode.NEMOTRON
            verifier_detail += f";gate_attempts={gate.attempts}"

        result = self.client.chat(
            model=self.model,
            prompt=prompt,
            max_tokens=self.max_tokens,
            max_tokens_retry=self.max_tokens_retry,
        )
        cost = cost_usd(self.model, result.tokens_in, result.tokens_out)
        with self._lock:
            self.call_count += 1
            self.spent_usd += cost

        if result.error is not None:
            # Retry once on error before giving up.
            retry = self.client.chat(
                model=self.model,
                prompt=prompt,
                max_tokens=self.max_tokens,
                max_tokens_retry=self.max_tokens_retry,
            )
            retry_cost = cost_usd(self.model, retry.tokens_in, retry.tokens_out)
            cost += retry_cost
            with self._lock:
                self.call_count += 1
                self.spent_usd += retry_cost
            result = retry

        if result.error is not None:
            verdict = Verdict.ERROR
            verifier_detail += f";error={result.error}"
        else:
            verdict = verify(result.text, rendered, name_map)
            if result.finish_reason == "length":
                verdict = Verdict.ERROR
                verifier_detail += ";cap_hit=1"
                # `result.retried` is only True when Client.chat's
                # auto-retry-on-length path actually fired (first attempt
                # came back empty + length-capped) and the retried call is
                # what we're looking at here. If that retried call is
                # STILL length-capped, the bigger `max_tokens_retry` budget
                # was exhausted too -- a persistent cap, not a transient
                # one-off -- so mark it distinctly for analysis.
                if result.retried:
                    verifier_detail += ";retry_exhausted=1"

        record = ExperimentRecord(
            run_id=self.run_id,
            model_name=self.model,
            latent_params={
                "chain_length": float(params.chain_length),
                "scramble": float(params.scramble),
                "coref_pressure": float(params.coref_pressure),
                "distractor_events": float(params.distractor_events),
                "filler_events": float(params.filler_events),
                "witness_cutoff": float(params.witness_cutoff),
            },
            realized_dims=realized_dims,
            render_mode=render_mode,
            task_seed=seed,
            trial_seed=trial_seed,
            rendering_seed=seed,
            renderer_model=self.renderer_model,
            prompt_text=prompt,
            response_text=result.text,
            verdict=verdict,
            verifier_detail=verifier_detail,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=cost,
            latency_ms=result.latency_ms,
            reasoning_text=result.reasoning,
            finish_reason=result.finish_reason,
        )
        with self._lock:
            if verdict == Verdict.ERROR:
                self.error_count += 1
            self.store.append(record)
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True

        return verdict == Verdict.PASS

    def _record_capped(
        self,
        params: DimParams,
        config: dict[str, float],
        seed: int,
        trial_seed: int,
        reason: str = "cost_capped",
    ) -> None:
        """Record a zero-cost, zero-call trial once the cost cap (or a
        cooperative cancellation) has been hit -- no API call is made, so
        the scheduler's shared budget drains without incurring further
        spend. `reason` is either "cost_capped" or "cancelled", written
        verbatim into `verifier_detail` as `<reason>=1` so the two
        short-circuit paths stay distinguishable in analysis. Caller must
        hold `self._lock`.
        """
        self.error_count += 1
        record = ExperimentRecord(
            run_id=self.run_id,
            model_name=self.model,
            latent_params={
                "chain_length": float(params.chain_length),
                "scramble": float(params.scramble),
                "coref_pressure": float(params.coref_pressure),
                "distractor_events": float(params.distractor_events),
                "filler_events": float(params.filler_events),
                "witness_cutoff": float(params.witness_cutoff),
            },
            realized_dims={},
            render_mode=RenderMode.TEMPLATE,
            task_seed=seed,
            trial_seed=trial_seed,
            rendering_seed=seed,
            prompt_text=None,
            response_text=None,
            verdict=Verdict.ERROR,
            verifier_detail=f"kind={self.query_kind};campaign=1;{reason}=1",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=0.0,
            reasoning_text=None,
            finish_reason=None,
        )
        self.store.append(record)


# ---------------------------------------------------------------------------
# Post-campaign verification: automatic resample-gating of FAIL records.
#
# See docs/lab-log.md and cartographer/ablate/resample.py for the origin
# story -- a single-trial FAIL against a big MoE model (temp=0) is
# frequently NOT reproducible (expert-routing noise), and trusting it
# without a resample check has already produced one retracted finding (the
# "Priya" case). `resample_failure` already does the resample+classify
# arithmetic for one (config, task_seed); `run_verify_failures` is the glue
# that runs it, automatically, over every FAIL in a just-finished campaign,
# and persists the outcome as new append-only records rather than mutating
# the original FAIL.
# ---------------------------------------------------------------------------

# Verify at most this many FAILs per run by default -- a pathological
# 100%-FAIL run must not blow the cost cap on verification alone. Most
# recent (by created_at) FAILs are chosen first when truncating.
_DEFAULT_MAX_VERIFIED = 30

VERIFY_SUFFIX = "/verify"


@dataclass
class VerifyFailuresResult:
    """Summary of one `run_verify_failures` call: how many FAILs were
    considered, how many were actually verified (vs. skipped by the cap or
    cancellation), and the aggregate stable/noise split."""

    run_id: str
    n_fails_total: int = 0
    n_verified: int = 0
    n_stable: int = 0
    n_noise: int = 0
    n_skipped_cap: int = 0
    truncated: bool = False
    cancelled: bool = False
    spent_usd: float = 0.0
    call_count: int = 0
    notes: list = field(default_factory=list)

    def to_json_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "n_fails_total": self.n_fails_total,
            "n_verified": self.n_verified,
            "n_stable": self.n_stable,
            "n_noise": self.n_noise,
            "n_skipped_cap": self.n_skipped_cap,
            "truncated": self.truncated,
            "cancelled": self.cancelled,
            "spent_usd": self.spent_usd,
            "call_count": self.call_count,
            "notes": list(self.notes),
        }


def run_verify_failures(
    client: Client,
    model: str,
    store: ResultsStore,
    run_id: str,
    n: int = 3,
    stable_threshold: float = 0.5,
    cost_cap_slice: float = 0.25,
    max_tokens: int | None = None,
    max_tokens_retry: int | None = None,
    max_verified: int = _DEFAULT_MAX_VERIFIED,
    cancel_event: threading.Event | None = None,
) -> VerifyFailuresResult:
    """Post-campaign verification pass: resample every stored FAIL for
    `run_id` `n` more times against the EXACT stored prompt (reusing
    `record.prompt_text` -- no regeneration, so there's zero drift from the
    original task) and record whether the failure is `stable` (the model
    still fails most of the time) or `noise` (it mostly passes on
    resample -- the original single trial was a fluke, most often MoE
    expert-routing variance at temp=0).

    Design: classification is DERIVED AT READ TIME from the verify trial
    records, not written as a separate summary/marker record. Each
    resample trial is stored as its own append-only `ExperimentRecord`
    with `run_id=f"{run_id}/verify"` (mirrors the full_map child-run-id
    convention already used elsewhere in this module) and
    `verifier_detail` carrying `verify_of=<original_record_id>;sample=<i>`
    plus `kind=<query_kind>;campaign=1;verify=1`. To classify an original
    FAIL, a reader queries `store.by_run(f"{run_id}/verify")`, filters for
    `verify_of=<record_id>`, and computes pass_rate including the original
    trial (see `classify_verified_failure` below) -- no new column, no
    schema migration, and the append-only invariant on the ORIGINAL record
    is never touched. This was chosen over a per-FAIL summary marker
    record because the summary would just be a cache of exactly this
    computation (cheap, small n) and a stale cache is worse than deriving
    it fresh every time; it also means a differently-configured re-read
    (e.g. a different `stable_threshold`) doesn't need new records.

    Cost discipline: only FAIL records are ever resampled (never PASS --
    that's the whole point, FAILs are a small 2-14% slice of trials so
    this adds only a few % to total campaign cost). `max_verified` caps
    how many FAILs get verified at all (most-recent-first by `created_at`,
    so a pathological high-FAIL-rate run can't blow the budget); anything
    beyond the cap is left unverified and `result.truncated` is set.
    `cost_cap_slice` is a SEPARATE budget from the parent campaign's own
    cost_cap -- callers that want verification cost counted inside one
    combined cap should size the campaign's own cap down by
    `cost_cap_slice` before calling this (see each `run_*_campaign`
    preset's verification wiring, which does exactly that).
    `cancel_event`, shared with the parent campaign's, is checked between
    every resample call so a cooperative cancel stops verification
    cleanly without leaving a half-verified run in a confusing state.

    `verify="manual"` at the campaign-preset layer means this function is
    never called at all -- zero extra calls, by construction (there is no
    other short-circuit to test).
    """
    result = VerifyFailuresResult(run_id=run_id)

    all_records = store.by_run(run_id)
    fails = [r for r in all_records if r.verdict == Verdict.FAIL]
    result.n_fails_total = len(fails)
    if not fails:
        return result

    # Most-recent-first by created_at (ties broken by record_id for
    # determinism), then cap at max_verified.
    fails_sorted = sorted(fails, key=lambda r: (r.created_at or "", r.record_id), reverse=True)
    if len(fails_sorted) > max_verified:
        result.truncated = True
        result.notes.append(
            f"truncated: {len(fails_sorted)} FAILs found, only verifying the "
            f"{max_verified} most recent (max_verified cap)"
        )
    to_verify = fails_sorted[:max_verified]
    result.n_skipped_cap = len(fails_sorted) - len(to_verify)

    default_max_tokens, default_max_tokens_retry = default_token_budget(model)
    eff_max_tokens = max_tokens if max_tokens is not None else default_max_tokens
    eff_max_tokens_retry = (
        max_tokens_retry if max_tokens_retry is not None else default_max_tokens_retry
    )

    verify_run_id = f"{run_id}{VERIFY_SUFFIX}"
    spent = 0.0
    calls = 0

    for original in to_verify:
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            result.notes.append(f"cancelled after verifying {result.n_verified}/{len(to_verify)}")
            break
        if spent >= cost_cap_slice:
            result.notes.append(
                f"cost_cap_slice (${cost_cap_slice:.4f}) hit after verifying "
                f"{result.n_verified}/{len(to_verify)}"
            )
            break
        if not original.prompt_text:
            # Nothing to replay (e.g. a pre-fidelity-gate record with no
            # stored prompt) -- skip, can't verify without the exact task.
            continue

        query_kind = _query_kind_of_verifier_detail(original.verifier_detail)
        n_pass_incl_original = 0  # the original FAIL never passed
        n_total = 1  # the original FAIL counts toward the denominator

        for i in range(n):
            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True
                break
            if spent >= cost_cap_slice:
                break

            call_result = client.chat(
                model=model,
                prompt=original.prompt_text,
                max_tokens=eff_max_tokens,
                max_tokens_retry=eff_max_tokens_retry,
            )
            cost = cost_usd(model, call_result.tokens_in, call_result.tokens_out)
            spent += cost
            calls += 1

            if call_result.error is not None:
                verdict = Verdict.ERROR
                detail = (
                    f"kind={query_kind};campaign=1;verify=1;"
                    f"verify_of={original.record_id};sample={i};error={call_result.error}"
                )
            else:
                # Re-verify against the SAME rendered task the original
                # prompt came from. `verify()` needs the RenderedTask +
                # name_map, which aren't stored directly -- but the record
                # carries everything needed to reconstruct them
                # deterministically (task_seed, latent_params, rendering_seed).
                try:
                    _, _, rendered, name_map = _regenerate_for_verify(original)
                    verdict = verify(call_result.text, rendered, name_map)
                except Exception as exc:  # pragma: no cover -- defensive
                    verdict = Verdict.ERROR
                    detail = (
                        f"kind={query_kind};campaign=1;verify=1;"
                        f"verify_of={original.record_id};sample={i};"
                        f"regen_error={exc!r}"
                    )
                    _append_verify_record(
                        store, verify_run_id, model, original, call_result, verdict, detail, cost
                    )
                    n_total += 1
                    continue
                if call_result.finish_reason == "length":
                    verdict = Verdict.ERROR
                detail = (
                    f"kind={query_kind};campaign=1;verify=1;"
                    f"verify_of={original.record_id};sample={i}"
                )

            _append_verify_record(
                store, verify_run_id, model, original, call_result, verdict, detail, cost
            )
            n_total += 1
            if verdict == Verdict.PASS:
                n_pass_incl_original += 1

        pass_rate = n_pass_incl_original / n_total if n_total else 0.0
        is_stable = pass_rate <= stable_threshold
        result.n_verified += 1
        if is_stable:
            result.n_stable += 1
        else:
            result.n_noise += 1

        if result.cancelled:
            break

    result.spent_usd = spent
    result.call_count = calls
    return result


def _query_kind_of_verifier_detail(detail: str | None) -> str:
    if detail:
        m = re.search(r"kind=([a-z]+)", detail)
        if m:
            return m.group(1)
    return "belief"


def _regenerate_for_verify(record: ExperimentRecord):
    """Rebuild the exact World/RenderedTask/name_map a stored record was
    generated from, so a resample response can be verified against the
    same ground truth as the original (mirrors
    `cartographer.report.verdict._regenerate`, duplicated narrowly here to
    avoid a circular import between `exec.evaluator` and `report.verdict`).
    """
    query_kind = _query_kind_of_verifier_detail(record.verifier_detail)
    base = _default_base(query_kind)
    params = _apply_config(base, record.latent_params)
    world = generate(params, record.task_seed)
    name_map = build_name_map(world, record.rendering_seed)
    rendered = render(world, name_map, record.rendering_seed)
    return world, name_map, rendered, name_map


def _append_verify_record(
    store: ResultsStore,
    verify_run_id: str,
    model: str,
    original: ExperimentRecord,
    call_result,
    verdict: Verdict,
    detail: str,
    cost: float,
) -> None:
    record = ExperimentRecord(
        run_id=verify_run_id,
        model_name=model,
        latent_params=dict(original.latent_params),
        realized_dims=dict(original.realized_dims),
        render_mode=original.render_mode,
        task_seed=original.task_seed,
        trial_seed=original.trial_seed,
        rendering_seed=original.rendering_seed,
        prompt_text=original.prompt_text,
        response_text=call_result.text,
        verdict=verdict,
        verifier_detail=detail,
        tokens_in=call_result.tokens_in,
        tokens_out=call_result.tokens_out,
        cost_usd=cost,
        latency_ms=call_result.latency_ms,
        reasoning_text=call_result.reasoning,
        finish_reason=call_result.finish_reason,
    )
    store.append(record)


@dataclass(frozen=True)
class FailureVerification:
    """Derived (not stored) classification of one original FAIL record,
    computed by reading its verify trials from `f"{run_id}/verify"`."""

    record_id: str
    status: str  # "stable" | "noise" | "unverified"
    n_samples: int  # verify trials found (0 if unverified)
    n_pass: int  # verify-trial passes only (original FAIL excluded)
    pass_rate: float | None  # including the original FAIL as a fail; None if unverified


def classify_verified_failure(
    store: ResultsStore,
    run_id: str,
    record_id: str,
    stable_threshold: float = 0.5,
    verify_trials: list | None = None,
) -> FailureVerification:
    """Derive the stable/noise/unverified status of one original FAIL
    record by reading its verify trials from `f"{run_id}/verify"`.

    `verify_trials`, if given, must be the full list of records from
    `store.by_run(f"{run_id}/verify")` -- callers classifying many FAILs
    from the same run should fetch that list ONCE and pass it in to avoid
    an O(n) store query per FAIL.
    """
    if verify_trials is None:
        verify_trials = store.by_run(f"{run_id}{VERIFY_SUFFIX}")

    needle = re.compile(rf"verify_of={re.escape(record_id)}(;|$)")
    mine = [r for r in verify_trials if needle.search(r.verifier_detail or "")]
    if not mine:
        return FailureVerification(record_id=record_id, status="unverified", n_samples=0, n_pass=0, pass_rate=None)

    n_pass = sum(1 for r in mine if r.verdict == Verdict.PASS)
    n_total = len(mine) + 1  # +1 for the original FAIL, which never passed
    pass_rate = n_pass / n_total
    status = "stable" if pass_rate <= stable_threshold else "noise"
    return FailureVerification(
        record_id=record_id, status=status, n_samples=len(mine), n_pass=n_pass, pass_rate=pass_rate
    )


def classify_all_verified_failures(
    store: ResultsStore, run_id: str, stable_threshold: float = 0.5
) -> dict[str, FailureVerification]:
    """Classify every FAIL in `run_id` at once (one store query for the
    FAILs, one for the verify trials, O(n) grouping) -- the batch-friendly
    counterpart to `classify_verified_failure` for verdict/viewer code
    that needs the whole run's picture."""
    fails = [r for r in store.by_run(run_id) if r.verdict == Verdict.FAIL]
    verify_trials = store.by_run(f"{run_id}{VERIFY_SUFFIX}")
    by_original: dict[str, list] = {}
    for r in verify_trials:
        detail = r.verifier_detail or ""
        m = re.search(r"verify_of=([0-9a-fA-F]+)", detail)
        if m:
            by_original.setdefault(m.group(1), []).append(r)

    out: dict[str, FailureVerification] = {}
    for f in fails:
        mine = by_original.get(f.record_id, [])
        if not mine:
            out[f.record_id] = FailureVerification(
                record_id=f.record_id, status="unverified", n_samples=0, n_pass=0, pass_rate=None
            )
            continue
        n_pass = sum(1 for r in mine if r.verdict == Verdict.PASS)
        n_total = len(mine) + 1
        pass_rate = n_pass / n_total
        status = "stable" if pass_rate <= stable_threshold else "noise"
        out[f.record_id] = FailureVerification(
            record_id=f.record_id, status=status, n_samples=len(mine), n_pass=n_pass, pass_rate=pass_rate
        )
    return out


def _state_scheduler_config(budget: int, parallelism: int = 6) -> SchedulerConfig:
    return SchedulerConfig(
        dims=[DimSpec("chain_length", 2, 30, True)],
        baseline={"chain_length": 2},
        ceiling={"chain_length": 30},
        k_trials=5,
        ci_width_stop=2.0,
        budget=budget,
        parallelism=parallelism,
    )


def _belief_scheduler_config(budget: int, parallelism: int = 6) -> SchedulerConfig:
    dims = [
        DimSpec("chain_length", 2, 10, True),
        DimSpec("scramble", 0.0, 1.0, False),
        DimSpec("coref_pressure", 0, 5, True),
        DimSpec("distractor_events", 2, 20, True),
        DimSpec("witness_cutoff", 0.0, 0.9, False),
    ]
    baseline = {d.name: d.lo for d in dims}
    ceiling = {d.name: d.hi for d in dims}
    return SchedulerConfig(
        dims=dims,
        baseline=baseline,
        ceiling=ceiling,
        k_trials=5,
        ci_width_stop=2.0,
        decompose_share=0.35,
        budget=budget,
        parallelism=parallelism,
    )


def _order_scheduler_config(budget: int, parallelism: int = 6) -> SchedulerConfig:
    dims = [
        DimSpec("chain_length", 2, 20, True),
        DimSpec("scramble", 0.0, 1.0, False),
        DimSpec("distractor_events", 2, 30, True),
        DimSpec("filler_events", 2, 40, True),
    ]
    baseline = {d.name: d.lo for d in dims}
    ceiling = {d.name: d.hi for d in dims}
    return SchedulerConfig(
        dims=dims,
        baseline=baseline,
        ceiling=ceiling,
        k_trials=5,
        ci_width_stop=2.0,
        decompose_share=0.35,
        budget=budget,
        parallelism=parallelism,
    )


def _default_parallelism(client: Client) -> int:
    provider = getattr(client, "provider", None)
    return getattr(provider, "suggested_parallelism", 6)


# Share of a campaign's own `cost_cap` reserved for the automatic
# post-campaign verification stage (Task 1). The scheduler's own budget is
# reduced by this fraction before the main loop runs, so
# "cost counted inside the existing cost_cap" holds literally: verification
# spend and main-loop spend together can never exceed the caller's original
# `cost_cap`.
_VERIFY_COST_CAP_SHARE = 0.15


def _run_campaign_with_verification(
    query_kind: str,
    scheduler_config_fn: Callable[[int, int], SchedulerConfig],
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    budget: int,
    cost_cap: float,
    parallelism: int | None,
    max_tokens: int | None,
    cancel_event: threading.Event | None,
    verify: str = "auto",
    adversarial: bool = False,
    renderer_model: str | None = None,
    extractor_model: str | None = None,
) -> CampaignState:
    """Shared body for the state/order/belief/adversarial_belief presets:
    run the main scheduler loop, then -- if `verify == "auto"` -- an
    automatic resample-verification pass over that run's FAIL records, all
    inside one combined `cost_cap`.

    `verify` is one of:
      - "auto" (default): run the resample-verification stage automatically
        after the main loop, spending real API calls, cost counted inside
        this same `cost_cap`.
      - "manual": record FAILs as usual but spend ZERO verification calls;
        a human (or `scripts/verify_past_failures.py --live`, or the
        run-detail page's "Verify failures now" button) can trigger
        verification for this run later, on demand.
    """
    if verify not in ("auto", "manual"):
        raise ValueError(f'verify must be "auto" or "manual", got {verify!r}')
    verify_failures_enabled = verify == "auto"
    main_cost_cap = cost_cap
    verify_cost_cap = 0.0
    if verify_failures_enabled:
        verify_cost_cap = cost_cap * _VERIFY_COST_CAP_SHARE
        main_cost_cap = cost_cap - verify_cost_cap

    evaluator = PipelineEvaluator(
        client=client,
        model=model,
        store=store,
        run_id=run_id,
        query_kind=query_kind,
        cost_cap_usd=main_cost_cap,
        max_tokens=max_tokens,
        cancel_event=cancel_event,
        adversarial=adversarial,
        renderer_model=renderer_model,
        extractor_model=extractor_model,
    )
    if parallelism is None:
        parallelism = _default_parallelism(client)
    config = scheduler_config_fn(budget, parallelism)

    def on_record(cfg: dict, passed: bool, seed: int) -> None:
        pass  # PipelineEvaluator already persists every trial itself.

    state = run_campaign(evaluator, config, on_record=on_record)
    if evaluator.capped:
        state.notes.append("cost_cap_hit")

    if verify_failures_enabled and not (cancel_event is not None and cancel_event.is_set()):
        vresult = run_verify_failures(
            client=client,
            model=model,
            store=store,
            run_id=run_id,
            cost_cap_slice=verify_cost_cap,
            max_tokens=max_tokens,
            cancel_event=cancel_event,
        )
        evaluator.spent_usd += vresult.spent_usd
        state.notes.append(
            f"verify_failures: {vresult.n_verified} verified "
            f"({vresult.n_stable} stable, {vresult.n_noise} noise)"
            + (", truncated" if vresult.truncated else "")
            + (", cancelled" if vresult.cancelled else "")
        )

    state.evaluator = evaluator  # type: ignore[attr-defined]
    return state


def run_state_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    budget: int = 160,
    cost_cap: float = 0.5,
    parallelism: int | None = None,
    max_tokens: int | None = None,
    cancel_event: threading.Event | None = None,
    verify: str = "auto",
    renderer_model: str | None = None,
    extractor_model: str | None = None,
) -> CampaignState:
    """Run the "state" query-kind campaign: sweep chain_length from 2 to 30.

    `renderer_model`/`extractor_model` (set together) switch every trial to
    the Phase 4 LLM-rendered pathway behind the mandatory fidelity gate; the
    default (None) keeps the deterministic template renderer.

    `parallelism=None` defaults to the client provider's
    `suggested_parallelism` (Groq 2, Nebius 6 -- rate-limit-aware).
    `max_tokens=None` defaults to the model-heuristic budget in
    `default_token_budget` (bigger for "ultra"-class reasoners).
    `cancel_event`, when provided, lets a caller (e.g. /launch's stop
    button) cooperatively cancel an in-flight campaign between trials.
    `verify="auto"` (default) runs the automatic resample-noise check
    (Task 1) over every FAIL after the main loop finishes, with its cost
    counted inside this same `cost_cap`; pass `verify="manual"` to record
    FAILs without spending any verification calls (they stay
    unverified until triggered later by hand -- the run-detail page's
    "Verify failures now" action or `scripts/verify_past_failures.py
    --live`).
    """
    return _run_campaign_with_verification(
        "state", _state_scheduler_config, client, store, run_id, model,
        budget, cost_cap, parallelism, max_tokens, cancel_event, verify,
        renderer_model=renderer_model, extractor_model=extractor_model,
    )


def run_order_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    budget: int = 400,
    cost_cap: float = 0.5,
    parallelism: int | None = None,
    max_tokens: int | None = None,
    cancel_event: threading.Event | None = None,
    verify: str = "auto",
) -> CampaignState:
    """Run the "order" query-kind campaign across four dims at once
    (chain_length, scramble, distractor_events, filler_events -- the dims
    that plausibly stress chronology recovery; witness/coref dims are
    belief-specific and stay at baseline).

    `parallelism=None` defaults to the client provider's
    `suggested_parallelism` (Groq 2, Nebius 6 -- rate-limit-aware).
    `max_tokens=None` defaults to the model-heuristic budget in
    `default_token_budget` (bigger for "ultra"-class reasoners).
    `cancel_event`, when provided, lets a caller (e.g. /launch's stop
    button) cooperatively cancel an in-flight campaign between trials.
    `verify="auto"` (default) runs the automatic resample-noise check
    (Task 1) over every FAIL after the main loop finishes, with its cost
    counted inside this same `cost_cap`; pass `verify="manual"` to record
    FAILs without spending any verification calls (they stay
    unverified until triggered later by hand -- the run-detail page's
    "Verify failures now" action or `scripts/verify_past_failures.py
    --live`).
    """
    return _run_campaign_with_verification(
        "order", _order_scheduler_config, client, store, run_id, model,
        budget, cost_cap, parallelism, max_tokens, cancel_event, verify,
    )


# The generator's hard validated ceilings (verified combined-clean across
# seeds in tests/test_witness_cutoff.py's combined-ceiling test).
_GENERATOR_LIMITS: dict[str, tuple[float, float, bool]] = {
    # name -> (base_value, limit_value, is_int)
    "chain_length": (2, 25, True),
    "scramble": (0.0, 1.0, False),
    "coref_pressure": (0, 8, True),
    "distractor_events": (2, 40, True),
    "filler_events": (2, 60, True),
    "witness_cutoff": (0.0, 0.9, False),
}


def default_belief_ray() -> EscalationRay:
    """The default escalation ray for belief queries: t=0 is the easy
    baseline on all six dims, t=1 is the generator's hard validated ceiling
    on all six simultaneously.
    """
    return EscalationRay(
        dims=tuple(
            RayDim(name, base, limit, is_int)
            for name, (base, limit, is_int) in _GENERATOR_LIMITS.items()
        )
    )


def run_breaking_point_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    query_kind: str = "belief",
    budget: int = 350,
    cost_cap: float = 0.75,
    max_tokens: int | None = None,
    cancel_event: threading.Event | None = None,
) -> BreakingPoint:
    """Escalation mode: walk the default all-dims ray from easy baseline to
    the generator's hard ceilings and find where the model actually breaks
    (see `cartographer.schedule.escalate.find_breaking_point`). Every trial
    is persisted through the same `PipelineEvaluator` the other campaigns
    use; escalation probing is inherently sequential (each stage depends on
    the last), so no parallelism knob.

    `max_tokens=None` defaults to the model-heuristic budget in
    `default_token_budget` (bigger for "ultra"-class reasoners).
    `cancel_event`, when provided, lets a caller (e.g. /launch's stop
    button) cooperatively cancel an in-flight campaign between trials.
    """
    evaluator = PipelineEvaluator(
        client=client,
        model=model,
        store=store,
        run_id=run_id,
        query_kind=query_kind,
        cost_cap_usd=cost_cap,
        max_tokens=max_tokens,
        cancel_event=cancel_event,
    )
    bp = find_breaking_point(evaluator, default_belief_ray(), budget=budget)
    bp_note = f"spent=${evaluator.spent_usd:.4f}"
    if evaluator.capped:
        bp_note += ";cost_cap_hit"
    bp.note = f"{bp.note}; {bp_note}" if bp.note else bp_note
    return bp


def run_belief_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    budget: int = 700,
    cost_cap: float = 1.0,
    parallelism: int | None = None,
    max_tokens: int | None = None,
    cancel_event: threading.Event | None = None,
    verify: str = "auto",
) -> CampaignState:
    """Run the "belief" query-kind campaign across four dims at once.

    `parallelism=None` defaults to the client provider's
    `suggested_parallelism` (Groq 2, Nebius 6 -- rate-limit-aware).
    `max_tokens=None` defaults to the model-heuristic budget in
    `default_token_budget` (bigger for "ultra"-class reasoners).
    `cancel_event`, when provided, lets a caller (e.g. /launch's stop
    button) cooperatively cancel an in-flight campaign between trials.
    `verify="auto"` (default) runs the automatic resample-noise check
    (Task 1) over every FAIL after the main loop finishes, with its cost
    counted inside this same `cost_cap`; pass `verify="manual"` to record
    FAILs without spending any verification calls (they stay unverified
    until triggered later by hand -- the run-detail page's "Verify
    failures now" action or `scripts/verify_past_failures.py --live`).
    This is the query kind (belief) where the noise/stable distinction
    matters most -- see docs/lab-log.md, the Priya case.
    """
    return _run_campaign_with_verification(
        "belief", _belief_scheduler_config, client, store, run_id, model,
        budget, cost_cap, parallelism, max_tokens, cancel_event, verify,
    )


def run_adversarial_belief_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    budget: int = 300,
    cost_cap: float = 1.0,
    parallelism: int | None = None,
    max_tokens: int | None = None,
    cancel_event: threading.Event | None = None,
    verify: str = "auto",
) -> CampaignState:
    """Belief-query campaign with `PipelineEvaluator.adversarial=True`: at
    every config point probed by the scheduler (including the BASELINE),
    the world actually used is the winner of an adversarial seed search
    over that config, not just whatever the derived task seed happens to
    produce. The point is that every task administered is already a
    trap -- the five scalar knobs (chain_length, scramble, coref_pressure,
    distractor_events, filler_events) plus witness_cutoff then measure how
    trap-*compounding* interacts with those traps, rather than measuring
    whether a trap exists at all (see docs/lab-log.md's foil-model entry).

    Uses the same dims as `run_belief_campaign` (which already includes
    `witness_cutoff` in its sweep) -- `_belief_scheduler_config` is reused
    verbatim.

    `parallelism=None` defaults to the client provider's
    `suggested_parallelism` (Groq 2, Nebius 6 -- rate-limit-aware).
    `max_tokens=None` defaults to the model-heuristic budget in
    `default_token_budget` (bigger for "ultra"-class reasoners).
    `cancel_event`, when provided, lets a caller (e.g. /launch's stop
    button) cooperatively cancel an in-flight campaign between trials.
    `verify="auto"` (default) runs the automatic resample-noise check
    (Task 1) over every FAIL after the main loop finishes, with its cost
    counted inside this same `cost_cap`; pass `verify="manual"` to skip it
    (adversarial FAILs are exactly the kind of thing worth checking for
    noise before trusting, so "auto" is the default here too).
    """
    return _run_campaign_with_verification(
        "belief", _belief_scheduler_config, client, store, run_id, model,
        budget, cost_cap, parallelism, max_tokens, cancel_event, verify,
        adversarial=True,
    )


# ---------------------------------------------------------------------------
# TOOL-CALL PLANNING province: a second capability family.
#
# Reuses the same generate -> render -> chat -> verify -> persist pipeline
# shape as PipelineEvaluator, but over cartographer.toolworld (typed tool
# catalog + precondition dependency chains) instead of cartographer.world
# (narrative event log). Emits `kind=tool_plan` in verifier_detail so
# /findings and /atlas pick the new query kind up automatically.
# ---------------------------------------------------------------------------

# Config keys that map onto integer-valued ToolDimParams fields.
_TOOL_INT_KEYS = {"plan_depth", "decoy_tools", "precondition_chain", "parallel_goals"}
# Config keys that map onto float-valued ToolDimParams fields.
_TOOL_FLOAT_KEYS = {"state_obscurity"}
# Config keys that map onto boolean ToolDimParams fields (0/1 in config space).
_TOOL_BOOL_KEYS = {"irreversibility"}
_TOOL_KNOWN_KEYS = _TOOL_INT_KEYS | _TOOL_FLOAT_KEYS | _TOOL_BOOL_KEYS


def _default_tool_base() -> ToolDimParams:
    return ToolDimParams(
        plan_depth=3,
        decoy_tools=2,
        precondition_chain=1,
        state_obscurity=0.0,
        irreversibility=False,
        parallel_goals=1,
    )


def _apply_tool_config(base: ToolDimParams, config: dict[str, float]) -> ToolDimParams:
    overrides: dict[str, object] = {}
    for key, value in config.items():
        if key in _TOOL_INT_KEYS:
            overrides[key] = int(round(value))
        elif key in _TOOL_FLOAT_KEYS:
            overrides[key] = float(value)
        elif key in _TOOL_BOOL_KEYS:
            overrides[key] = bool(round(value))
        else:
            raise ValueError(f"unknown tool config key: {key!r}")
    return replace(base, **overrides)


class ToolPlanningEvaluator:
    """A `TaskEvaluator` backed by the toolworld generate -> render -> chat ->
    verify pipeline, mirroring `PipelineEvaluator` (same cost-cap / cancel /
    thread-safety discipline). Each trial persists a full `ExperimentRecord`
    with `kind=tool_plan`; realized dims come from `toolworld.measure`.

    When `adversarial=True`, the world used at each config point is the winner
    of a `toolworld.search.search_adversarial_seeds` search over that config
    (trap-dense selection at zero extra API cost).
    """

    def __init__(
        self,
        client: Client,
        model: str,
        store: ResultsStore,
        run_id: str,
        cost_cap_usd: float,
        base: ToolDimParams | None = None,
        max_tokens: int | None = None,
        max_tokens_retry: int | None = None,
        cancel_event: threading.Event | None = None,
        adversarial: bool = False,
        adversarial_candidates: int = 40,
    ) -> None:
        self.client = client
        self.model = model
        self.store = store
        self.run_id = run_id
        self.query_kind = "tool_plan"
        self.cost_cap_usd = cost_cap_usd
        self.base = base if base is not None else _default_tool_base()
        self.adversarial = adversarial
        self.adversarial_candidates = adversarial_candidates

        default_max_tokens, default_max_tokens_retry = default_token_budget(model)
        self.max_tokens = max_tokens if max_tokens is not None else default_max_tokens
        self.max_tokens_retry = (
            max_tokens_retry if max_tokens_retry is not None else default_max_tokens_retry
        )
        if self.max_tokens_retry <= self.max_tokens:
            self.max_tokens_retry = self.max_tokens * 2

        self.cancel_event = cancel_event
        self.spent_usd = 0.0
        self.call_count = 0
        self.error_count = 0
        self.capped = False
        self.cancelled = False
        self._lock = threading.Lock()

    def __call__(self, config: dict[str, float], trial_seed: int) -> bool:
        params = _apply_tool_config(self.base, config)
        seed = _task_seed(config, trial_seed)

        with self._lock:
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True
                self._record_capped(params, seed, trial_seed, reason="cost_capped")
                return False
            if self.cancel_event is not None and self.cancel_event.is_set():
                self.cancelled = True
                self._record_capped(params, seed, trial_seed, reason="cancelled")
                return False

        adversarial_note = ""
        adversarial_n_diverging = 0.0
        if self.adversarial:
            winners = tool_search_adversarial_seeds(
                params, seed, n_candidates=self.adversarial_candidates, top_k=1
            )
            if winners:
                seed, profile = winners[0]
                traps = ",".join(profile.trap_kinds)
                adversarial_note = (
                    f";adversarial=1;traps={traps}"
                    f";foil_score={round(profile.divergence_score, 4)}"
                )
                adversarial_n_diverging = float(profile.n_diverging)
            else:
                adversarial_note = ";adversarial=1;traps=;foil_score=0"

        world = tool_generate(params, seed)
        rendered = tool_render(world, seed)
        realized_dims = tool_measure(world)
        if self.adversarial:
            realized_dims = dict(realized_dims)
            realized_dims["foil_divergence"] = adversarial_n_diverging

        verifier_detail = f"kind=tool_plan;campaign=1{adversarial_note}"

        result = self.client.chat(
            model=self.model,
            prompt=rendered.prompt,
            max_tokens=self.max_tokens,
            max_tokens_retry=self.max_tokens_retry,
        )
        cost = cost_usd(self.model, result.tokens_in, result.tokens_out)
        with self._lock:
            self.call_count += 1
            self.spent_usd += cost

        if result.error is not None:
            retry = self.client.chat(
                model=self.model,
                prompt=rendered.prompt,
                max_tokens=self.max_tokens,
                max_tokens_retry=self.max_tokens_retry,
            )
            retry_cost = cost_usd(self.model, retry.tokens_in, retry.tokens_out)
            cost += retry_cost
            with self._lock:
                self.call_count += 1
                self.spent_usd += retry_cost
            result = retry

        if result.error is not None:
            verdict = Verdict.ERROR
            verifier_detail += f";error={result.error}"
        else:
            pv = verify_plan(result.text, world)
            verdict = pv.verdict
            verifier_detail += (
                f";detail={pv.detail};n_calls={pv.n_calls}"
                f";optimal_depth={pv.optimal_depth};overhead={pv.overhead}"
            )
            realized_dims = dict(realized_dims)
            if pv.overhead >= 0:
                realized_dims["plan_overhead"] = float(pv.overhead)
            if result.finish_reason == "length":
                verdict = Verdict.ERROR
                verifier_detail += ";cap_hit=1"
                if result.retried:
                    verifier_detail += ";retry_exhausted=1"

        record = ExperimentRecord(
            run_id=self.run_id,
            model_name=self.model,
            latent_params={
                "plan_depth": float(params.plan_depth),
                "decoy_tools": float(params.decoy_tools),
                "precondition_chain": float(params.precondition_chain),
                "state_obscurity": float(params.state_obscurity),
                "irreversibility": float(params.irreversibility),
                "parallel_goals": float(params.parallel_goals),
            },
            realized_dims=realized_dims,
            render_mode=RenderMode.TEMPLATE,
            task_seed=seed,
            trial_seed=trial_seed,
            rendering_seed=seed,
            prompt_text=rendered.prompt,
            response_text=result.text,
            verdict=verdict,
            verifier_detail=verifier_detail,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=cost,
            latency_ms=result.latency_ms,
            reasoning_text=result.reasoning,
            finish_reason=result.finish_reason,
        )
        with self._lock:
            if verdict == Verdict.ERROR:
                self.error_count += 1
            self.store.append(record)
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True

        return verdict == Verdict.PASS

    def _record_capped(
        self,
        params: ToolDimParams,
        seed: int,
        trial_seed: int,
        reason: str = "cost_capped",
    ) -> None:
        self.error_count += 1
        record = ExperimentRecord(
            run_id=self.run_id,
            model_name=self.model,
            latent_params={
                "plan_depth": float(params.plan_depth),
                "decoy_tools": float(params.decoy_tools),
                "precondition_chain": float(params.precondition_chain),
                "state_obscurity": float(params.state_obscurity),
                "irreversibility": float(params.irreversibility),
                "parallel_goals": float(params.parallel_goals),
            },
            realized_dims={},
            render_mode=RenderMode.TEMPLATE,
            task_seed=seed,
            trial_seed=trial_seed,
            rendering_seed=seed,
            prompt_text=None,
            response_text=None,
            verdict=Verdict.ERROR,
            verifier_detail=f"kind=tool_plan;campaign=1;{reason}=1",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=0.0,
            reasoning_text=None,
            finish_reason=None,
        )
        self.store.append(record)


def _tool_planning_scheduler_config(budget: int, parallelism: int = 6) -> SchedulerConfig:
    dims = [
        DimSpec("plan_depth", 1, 6, True),
        DimSpec("decoy_tools", 0, 5, True),
        DimSpec("precondition_chain", 0, 2, True),
        DimSpec("state_obscurity", 0.0, 1.0, False),
        DimSpec("parallel_goals", 1, 2, True),
    ]
    baseline = {d.name: d.lo for d in dims}
    ceiling = {d.name: d.hi for d in dims}
    return SchedulerConfig(
        dims=dims,
        baseline=baseline,
        ceiling=ceiling,
        k_trials=5,
        ci_width_stop=2.0,
        decompose_share=0.35,
        budget=budget,
        parallelism=parallelism,
    )


def run_tool_planning_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    budget: int = 400,
    cost_cap: float = 0.75,
    parallelism: int | None = None,
    max_tokens: int | None = None,
    cancel_event: threading.Event | None = None,
    verify: str = "manual",
    adversarial: bool = False,
) -> CampaignState:
    """Run the TOOL-CALL PLANNING campaign: sweep plan_depth, decoy_tools,
    precondition_chain, state_obscurity, and parallel_goals over the toolworld
    generator. The model is asked to emit a JSON plan of tool calls; the
    verifier simulates it against the latent state machine (kind=tool_plan).

    `verify="manual"` is the default here (unlike the belief campaigns): a
    tool-plan FAIL is a hard, deterministic verifier outcome (an unmet
    precondition or an unreached goal), not the soft MoE-routing noise the
    resample check was built for -- so auto-resampling adds cost without the
    same payoff. Pass `verify="auto"` to resample FAILs anyway. `adversarial`
    selects trap-dense worlds via the foil-model seed search at every config
    point.

    `parallelism=None` defaults to the client provider's suggested_parallelism.
    `max_tokens=None` defaults to the model-heuristic budget.
    `cancel_event`, when provided, cooperatively cancels between trials.
    """
    if verify not in ("auto", "manual"):
        raise ValueError(f'verify must be "auto" or "manual", got {verify!r}')

    evaluator = ToolPlanningEvaluator(
        client=client,
        model=model,
        store=store,
        run_id=run_id,
        cost_cap_usd=cost_cap,
        max_tokens=max_tokens,
        cancel_event=cancel_event,
        adversarial=adversarial,
    )
    if parallelism is None:
        parallelism = _default_parallelism(client)
    config = _tool_planning_scheduler_config(budget, parallelism)

    def on_record(cfg: dict, passed: bool, seed: int) -> None:
        pass  # ToolPlanningEvaluator persists every trial itself.

    state = run_campaign(evaluator, config, on_record=on_record)
    if evaluator.capped:
        state.notes.append("cost_cap_hit")

    if verify == "auto" and not (cancel_event is not None and cancel_event.is_set()):
        vresult = run_verify_failures(
            client=client,
            model=model,
            store=store,
            run_id=run_id,
            cost_cap_slice=cost_cap * _VERIFY_COST_CAP_SHARE,
            max_tokens=max_tokens,
            cancel_event=cancel_event,
        )
        evaluator.spent_usd += vresult.spent_usd
        state.notes.append(
            f"verify_failures: {vresult.n_verified} verified "
            f"({vresult.n_stable} stable, {vresult.n_noise} noise)"
        )

    state.evaluator = evaluator  # type: ignore[attr-defined]
    return state


# ---------------------------------------------------------------------------
# tool_breaking_point: escalation over the toolworld's STRUCTURAL trap
# mutators (cartographer.toolworld.mutate), not just the scalar ToolDimParams
# knobs -- mirrors run_breaking_point_campaign's shape (find_breaking_point +
# decompose + confirm) but the ray's dims include 0/1 toggles for the trap
# mutators plus the existing precondition_chain/parallel_goals scalars.
#
# Every mutator maps cleanly onto a 0/1 RayDim (is_int=True) EXCEPT
# `interleaved_goals`, which isn't a separate toggle at all: it only ever
# applies when parallel_goals==2 (see mutate.interleaved_goals_applicable),
# so it rides the existing `parallel_goals` dim for free rather than needing
# its own knob -- there is nothing to represent it as.
# ---------------------------------------------------------------------------

# t=0 baseline: no traps active, current-easy scalar values. t=1 ceiling:
# every structural trap on, scalars at their hard-but-generator-valid values.
_TOOL_TRAP_LIMITS: dict[str, tuple[float, float, bool]] = {
    # name -> (base_value, limit_value, is_int)
    "poison_traps": (0, 1, True),
    "dishonest_decoys_on": (0, 1, True),
    "implicit_preconditions_on": (0, 1, True),
    "zero_slack_on": (0, 1, True),
    "id_collisions_on": (0, 1, True),
    "precondition_chain": (0, 2, True),
    "parallel_goals": (1, 2, True),
}


def default_tool_ray() -> EscalationRay:
    """The default escalation ray for the tool-planning province's trap
    structures: t=0 is the current-easy baseline (no traps, low scalars),
    t=1 ramps every structural mutator on simultaneously (poison_ladder,
    dishonest_decoys, implicit_preconditions, zero_slack, id_collision) plus
    precondition_chain and parallel_goals up to their generator ceilings
    (interleaved_goals comes along for free once parallel_goals hits 2).
    """
    return EscalationRay(
        dims=tuple(
            RayDim(name, base, limit, is_int)
            for name, (base, limit, is_int) in _TOOL_TRAP_LIMITS.items()
        )
    )


# Catalog-size ceiling for the tool-count ray. The published tool-selection
# literature puts the "safe zone" at roughly 10-20 tools, first real
# degradation around 40-50, and severe collapse in the hundreds. Our other
# campaigns sweep `decoy_tools` only to 5 -- a ~14-tool catalog, i.e.
# entirely inside the safe zone, which is a plausible reason Super/Ultra
# never broke on the depth or trap rays. 60 decoys puts the catalog near 69
# tools, past the documented degradation onset while staying small enough
# that the prompt is ~11k chars and BFS ground truth stays cheap.
_TOOL_COUNT_CEILING = 60


def default_tool_count_ray() -> EscalationRay:
    """Escalation ray over CATALOG SIZE alone: `decoy_tools` 2 -> 60 (catalog
    ~11 -> ~69 tools), every other dim held at its generator default.

    Deliberately single-dim: any break found on this ray is attributable to
    tool-count pressure specifically, with no depth/trap confound. The decoys
    are the ordinary self-describing kind (`generate._decoy_tool`), NOT
    `mutate.dishonest_decoys` -- so this measures pure selection pressure
    from catalog size, not deception.
    """
    return EscalationRay(dims=(RayDim("decoy_tools", 2, _TOOL_COUNT_CEILING, True),))


def default_tool_confusability_ray() -> EscalationRay:
    """The corrected catalog-size ray: same `decoy_tools` 2 -> 60 sweep, but
    with `dishonest_decoys_on` pinned ON for every probe point.

    `default_tool_count_ray` produced a flat 100% on Super (see docs/lab-log
    "Super is untouched by a 66-tool catalog"): the plain `decoy_tools`
    decoys self-label ("no effect on state"), so a 66-tool catalog is really
    an 8-way choice with 58 entries that say ignore-me -- prompt length, not
    selection pressure. `mutate.dishonest_decoys` renames decoys to sound
    load-bearing and strips the confession, making all 66 tools look real.
    That is the honest analogue of the published large-catalog condition, and
    it isolates SEMANTIC CONFUSABILITY at scale rather than raw tool count.

    The pinned dim uses base == limit, so `RayDim.value_at` returns 1.0 at
    every t and the sweep stays effectively single-dim in `decoy_tools`.
    """
    return EscalationRay(
        dims=(
            RayDim("decoy_tools", 2, _TOOL_COUNT_CEILING, True),
            RayDim("dishonest_decoys_on", 1, 1, True),
        )
    )


def _apply_tool_trap_config(
    world, config: dict[str, float], seed: int
):
    """Apply the trap mutators indicated by `config` (each a 0.5-threshold
    0/1 dim) to `world`, in a fixed deterministic order so composing several
    at once is reproducible. Returns the (possibly) mutated world; also
    returns the render `style` to use (implicit vs explicit), since that
    mutator lives at the render layer, not the world layer."""
    from ..toolworld.mutate import (
        dishonest_decoys as _dishonest_decoys,
    )
    from ..toolworld.mutate import id_collision as _id_collision
    from ..toolworld.mutate import interleaved_goals as _interleaved_goals
    from ..toolworld.mutate import interleaved_goals_applicable
    from ..toolworld.mutate import poison_ladder as _poison_ladder
    from ..toolworld.mutate import zero_slack as _zero_slack

    style = "implicit" if config.get("implicit_preconditions_on", 0.0) >= 0.5 else "explicit"

    if config.get("poison_traps", 0.0) >= 0.5:
        world = _poison_ladder(world, seed)
    if config.get("dishonest_decoys_on", 0.0) >= 0.5:
        world = _dishonest_decoys(world, seed)
    if config.get("id_collisions_on", 0.0) >= 0.5:
        world = _id_collision(world, seed)
    if interleaved_goals_applicable(world):
        world = _interleaved_goals(world, seed)
    # zero_slack tightens budget to optimal_depth -- apply LAST so every
    # other structural addition above is already reflected in the plan
    # length the tightened budget has to accommodate.
    if config.get("zero_slack_on", 0.0) >= 0.5:
        world = _zero_slack(world, seed)

    return world, style


class ToolBreakingPointEvaluator:
    """A `find_breaking_point` `Evaluator` over the toolworld's structural
    trap mutators, mirroring `ToolPlanningEvaluator`'s cost-cap / cancel /
    thread-safety discipline and persisting through the same
    `ExperimentRecord` shape (kind=tool_plan). Config keys are the
    `_TOOL_TRAP_LIMITS` dim names above; scalar dims (`precondition_chain`,
    `parallel_goals`) feed `ToolDimParams` exactly like `_apply_tool_config`,
    the 0/1 trap dims drive `_apply_tool_trap_config`.
    """

    def __init__(
        self,
        client: Client,
        model: str,
        store: ResultsStore,
        run_id: str,
        cost_cap_usd: float,
        base: ToolDimParams | None = None,
        max_tokens: int | None = None,
        max_tokens_retry: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.store = store
        self.run_id = run_id
        self.cost_cap_usd = cost_cap_usd
        self.base = base if base is not None else _default_tool_base()

        default_max_tokens, default_max_tokens_retry = default_token_budget(model)
        self.max_tokens = max_tokens if max_tokens is not None else default_max_tokens
        self.max_tokens_retry = (
            max_tokens_retry if max_tokens_retry is not None else default_max_tokens_retry
        )
        if self.max_tokens_retry <= self.max_tokens:
            self.max_tokens_retry = self.max_tokens * 2

        self.cancel_event = cancel_event
        self.spent_usd = 0.0
        self.call_count = 0
        self.error_count = 0
        self.capped = False
        self.cancelled = False
        self._lock = threading.Lock()

    def __call__(self, config: dict[str, float], trial_seed: int) -> bool:
        scalar_cfg = {
            k: v
            for k, v in config.items()
            if k in _TOOL_KNOWN_KEYS
        }
        params = _apply_tool_config(self.base, scalar_cfg)
        seed = _task_seed(config, trial_seed)

        with self._lock:
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True
                self._record_capped(params, seed, trial_seed, reason="cost_capped")
                return False
            if self.cancel_event is not None and self.cancel_event.is_set():
                self.cancelled = True
                self._record_capped(params, seed, trial_seed, reason="cancelled")
                return False

        world = tool_generate(params, seed)
        world, style = _apply_tool_trap_config(world, config, seed)
        rendered = tool_render(world, seed, style=style)
        realized_dims = tool_measure(world)

        trap_dim_names = (
            "poison_traps", "dishonest_decoys_on", "implicit_preconditions_on",
            "zero_slack_on", "id_collisions_on",
        )
        # Trap 0/1 dims, needed in latent_params/realized_dims (in addition
        # to verifier_detail below) so world reconstruction from a record's
        # latent_params alone is possible -- see docs/lab-log.md "Provenance
        # bug" entry. Additive only; verifier_detail kept for back-compat.
        trap_dims = {
            k: float(1.0 if config.get(k, 0.0) >= 0.5 else 0.0) for k in trap_dim_names
        }
        trap_note = ";".join(
            f"{k}={int(round(config[k]))}"
            for k in trap_dim_names
            if k in config
        )
        verifier_detail = f"kind=tool_plan;campaign=1;traps={trap_note}"

        result = self.client.chat(
            model=self.model,
            prompt=rendered.prompt,
            max_tokens=self.max_tokens,
            max_tokens_retry=self.max_tokens_retry,
        )
        cost = cost_usd(self.model, result.tokens_in, result.tokens_out)
        with self._lock:
            self.call_count += 1
            self.spent_usd += cost

        if result.error is not None:
            retry = self.client.chat(
                model=self.model,
                prompt=rendered.prompt,
                max_tokens=self.max_tokens,
                max_tokens_retry=self.max_tokens_retry,
            )
            retry_cost = cost_usd(self.model, retry.tokens_in, retry.tokens_out)
            cost += retry_cost
            with self._lock:
                self.call_count += 1
                self.spent_usd += retry_cost
            result = retry

        if result.error is not None:
            verdict = Verdict.ERROR
            verifier_detail += f";error={result.error}"
        else:
            pv = verify_plan(result.text, world)
            verdict = pv.verdict
            verifier_detail += (
                f";detail={pv.detail};n_calls={pv.n_calls}"
                f";optimal_depth={pv.optimal_depth};overhead={pv.overhead}"
            )
            realized_dims = dict(realized_dims)
            if pv.overhead >= 0:
                realized_dims["plan_overhead"] = float(pv.overhead)
            if result.finish_reason == "length":
                verdict = Verdict.ERROR
                verifier_detail += ";cap_hit=1"
                if result.retried:
                    verifier_detail += ";retry_exhausted=1"

        record = ExperimentRecord(
            run_id=self.run_id,
            model_name=self.model,
            latent_params={
                "plan_depth": float(params.plan_depth),
                "decoy_tools": float(params.decoy_tools),
                "precondition_chain": float(params.precondition_chain),
                "state_obscurity": float(params.state_obscurity),
                "irreversibility": float(params.irreversibility),
                "parallel_goals": float(params.parallel_goals),
                **trap_dims,
            },
            realized_dims={**realized_dims, **trap_dims},
            render_mode=RenderMode.TEMPLATE,
            task_seed=seed,
            trial_seed=trial_seed,
            rendering_seed=seed,
            prompt_text=rendered.prompt,
            response_text=result.text,
            verdict=verdict,
            verifier_detail=verifier_detail,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=cost,
            latency_ms=result.latency_ms,
            reasoning_text=result.reasoning,
            finish_reason=result.finish_reason,
        )
        with self._lock:
            if verdict == Verdict.ERROR:
                self.error_count += 1
            self.store.append(record)
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True

        return verdict == Verdict.PASS

    def _record_capped(
        self,
        params: ToolDimParams,
        seed: int,
        trial_seed: int,
        reason: str = "cost_capped",
    ) -> None:
        self.error_count += 1
        record = ExperimentRecord(
            run_id=self.run_id,
            model_name=self.model,
            latent_params={
                "plan_depth": float(params.plan_depth),
                "decoy_tools": float(params.decoy_tools),
                "precondition_chain": float(params.precondition_chain),
                "state_obscurity": float(params.state_obscurity),
                "irreversibility": float(params.irreversibility),
                "parallel_goals": float(params.parallel_goals),
            },
            realized_dims={},
            render_mode=RenderMode.TEMPLATE,
            task_seed=seed,
            trial_seed=trial_seed,
            rendering_seed=seed,
            prompt_text=None,
            response_text=None,
            verdict=Verdict.ERROR,
            verifier_detail=f"kind=tool_plan;campaign=1;{reason}=1",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=0.0,
            reasoning_text=None,
            finish_reason=None,
        )
        self.store.append(record)


def run_tool_breaking_point_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    budget: int = 250,
    cost_cap: float = 0.5,
    max_tokens: int | None = None,
    cancel_event: threading.Event | None = None,
    verify: str = "auto",
    ray: EscalationRay | None = None,
    parallelism: int = 1,
) -> BreakingPoint:
    """Escalation mode over the toolworld's STRUCTURAL trap mutators (see
    `default_tool_ray`): walk the ray from no-traps baseline to full-trap
    ceiling (poison_ladder, dishonest_decoys, implicit_preconditions,
    zero_slack, id_collision all on, plus precondition_chain/parallel_goals
    up) and find where the model actually breaks. Mirrors
    `run_breaking_point_campaign`'s shape (find_breaking_point + decompose +
    confirm), budget/cost_cap defaults chosen to match its sibling.

    `ray` overrides the default trap ray -- pass `default_tool_count_ray()`
    for the catalog-size sweep, or any custom `EscalationRay` over
    `_TOOL_KNOWN_KEYS` / trap-dim names. `parallelism` runs each probe's
    k_trials concurrently (see `find_breaking_point`); the trial set is
    identical at any worker count.

    `verify` is accepted for signature symmetry with the other campaign
    runners (so /launch's generic kwarg-forwarding in `_campaign_thread`
    keeps working unchanged) but is currently unused -- escalation's FAILs
    are hard deterministic simulation outcomes, same rationale as
    `run_tool_planning_campaign`'s `verify="manual"` default.
    """
    evaluator = ToolBreakingPointEvaluator(
        client=client,
        model=model,
        store=store,
        run_id=run_id,
        cost_cap_usd=cost_cap,
        max_tokens=max_tokens,
        cancel_event=cancel_event,
    )
    bp = find_breaking_point(
        evaluator,
        ray if ray is not None else default_tool_ray(),
        budget=budget,
        parallelism=parallelism,
    )
    bp_note = f"spent=${evaluator.spent_usd:.4f}"
    if evaluator.capped:
        bp_note += ";cost_cap_hit"
    bp.note = f"{bp.note}; {bp_note}" if bp.note else bp_note
    return bp


# ---------------------------------------------------------------------------
# full_map: the one-button profile -- chains every preset as stages under one
# parent run, one shared cost budget.
# ---------------------------------------------------------------------------

# Trial-budget target for each stage at the full_map preset's default
# budget=600 (state ~60, order ~80, belief ~200, adversarial_belief ~160,
# breaking_point gets the remainder ~100). Scales linearly with whatever
# `budget` the caller passes.
_FULL_MAP_STAGE_BUDGET_SHARES: dict[str, float] = {
    "state": 60 / 600,
    "order": 80 / 600,
    "belief": 200 / 600,
    "adversarial_belief": 160 / 600,
    "breaking_point": 100 / 600,
}

# Proportional share of the shared cost_cap each stage gets (sums to 1.0),
# mirroring the trial-budget weights above.
_FULL_MAP_STAGE_COST_SHARES: dict[str, float] = {
    "state": 0.08,
    "order": 0.14,
    "belief": 0.32,
    "adversarial_belief": 0.22,
    "breaking_point": 0.24,
}

_FULL_MAP_STAGE_ORDER = ["state", "order", "belief", "adversarial_belief", "breaking_point"]


@dataclass
class StageSummary:
    """Per-stage summary for a full_map run: enough to render a card without
    re-reading every record."""

    stage: str
    run_id: str
    n_records: int = 0
    n_pass: int = 0
    n_fail: int = 0
    n_error: int = 0
    pass_rate: float | None = None
    spend_usd: float = 0.0
    boundaries: dict = field(default_factory=dict)
    hypotheses_n: int = 0
    breaking_point_t_star: float | None = None
    breaking_point_found: bool | None = None
    exhausted: bool = False
    skipped: bool = False
    note: str = ""

    def to_json_dict(self) -> dict:
        return {
            "stage": self.stage,
            "run_id": self.run_id,
            "n_records": self.n_records,
            "n_pass": self.n_pass,
            "n_fail": self.n_fail,
            "n_error": self.n_error,
            "pass_rate": self.pass_rate,
            "spend_usd": self.spend_usd,
            "boundaries": dict(self.boundaries),
            "hypotheses_n": self.hypotheses_n,
            "breaking_point_t_star": self.breaking_point_t_star,
            "breaking_point_found": self.breaking_point_found,
            "exhausted": self.exhausted,
            "skipped": self.skipped,
            "note": self.note,
        }


@dataclass
class FullMapResult:
    """Aggregated result of a `run_full_map_campaign` call: one parent
    `run_id` fanning out into per-stage child run_ids
    (`f"{run_id}/{stage}"`), each a full campaign in its own right."""

    run_id: str
    stages: list = field(default_factory=list)
    total_spend_usd: float = 0.0
    total_records: int = 0
    cancelled: bool = False
    notes: list = field(default_factory=list)

    def stage_run_ids(self) -> list:
        return [s.run_id for s in self.stages]

    def to_json_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "stages": [s.to_json_dict() for s in self.stages],
            "total_spend_usd": self.total_spend_usd,
            "total_records": self.total_records,
            "cancelled": self.cancelled,
            "notes": list(self.notes),
        }


def _child_run_id(parent_run_id: str, stage: str) -> str:
    return f"{parent_run_id}/{stage}"


def _campaign_state_summary(
    stage: str, run_id: str, state: CampaignState, spend_usd: float, n_records: int
) -> StageSummary:
    evaluator = getattr(state, "evaluator", None)
    n_error = getattr(evaluator, "error_count", 0) if evaluator is not None else 0
    n_pass = 0
    n_fail = 0
    for pts in state.points.values():
        for _, passed in pts:
            if passed:
                n_pass += 1
            else:
                n_fail += 1
    pass_rate = n_pass / (n_pass + n_fail) if (n_pass + n_fail) else None
    exhausted = any("exhaust" in n for n in state.notes)
    return StageSummary(
        stage=stage,
        run_id=run_id,
        n_records=n_records,
        n_pass=n_pass,
        n_fail=n_fail,
        n_error=n_error,
        pass_rate=pass_rate,
        spend_usd=spend_usd,
        boundaries=dict(state.boundaries),
        hypotheses_n=len(state.hypotheses),
        exhausted=exhausted,
        note="; ".join(state.notes),
    )


def run_full_map_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    budget: int = 600,
    cost_cap: float = 2.0,
    max_tokens: int | None = None,
    cancel_event: threading.Event | None = None,
    parallelism: int | None = None,
    verify: str = "auto",
) -> FullMapResult:
    """The one-button profile: chains state -> order -> belief ->
    adversarial_belief -> breaking_point as STAGES under one parent
    `run_id`, sharing one overall trial budget and one overall cost cap.

    Each stage runs as its own campaign against a child run_id
    (`f"{run_id}/{stage}"`), using the SAME `store` and `client` -- so the
    viewer can group child stages under the parent (see
    `cartographer.report.verdict` and the viewer's run grouping) while each
    stage's records stay independently queryable via
    `store.by_run(child_run_id)`.

    Budget/cost allocation: `budget` (trials) and `cost_cap` (USD) are split
    across stages proportionally, matching the target ~60/80/200/160/
    remainder trial split at the default budget=600
    (`_FULL_MAP_STAGE_BUDGET_SHARES`) and the cost shares in
    `_FULL_MAP_STAGE_COST_SHARES`. If a stage exhausts its own budget or
    cost cap, that's logged in its `StageSummary` and the battery continues
    to the next stage -- one stage running dry never blocks the others.

    Breaking-point seeding: if the belief stage found any dim boundaries,
    the breaking_point stage's ray is re-seeded so each `RayDim.base_value`
    sits at the discovered boundary (clamped inside the dim's own
    [base, limit] span) instead of the generator's easy default --
    concentrating the escalation ray near the region belief already
    flagged as risky. If no boundary was found (or the belief stage didn't
    run), the default all-ceiling ray (`default_belief_ray()`) is used
    unmodified and a note records the fallback.

    `cancel_event` is checked before each stage starts (a cancellation
    between stages skips all remaining stages immediately) AND is passed
    through to every stage's own evaluator (a cancellation mid-stage also
    stops that stage cleanly without wasting the rest of its budget).

    `verify="auto"` (default) is passed through to every
    state/order/belief/adversarial_belief stage -- each verifies its own
    FAILs, inside its own stage cost cap, immediately after its main loop
    (see `_run_campaign_with_verification`'s "run per stage, not once at
    the end" design note). Pass `verify="manual"` to skip verification
    everywhere in the battery. breaking_point does not use
    `_run_campaign_with_verification` (it's driven by
    `find_breaking_point`, not `run_campaign`) so it is never auto-verified
    here regardless of `verify` -- wiring it in would mean re-deriving
    "which of its escalation-ray trials are FAILs worth resampling" outside
    the CampaignState idiom this helper relies on, which would contort the
    API for one stage; skipped deliberately rather than forced in.
    """
    result = FullMapResult(run_id=run_id)
    belief_boundaries: dict = {}

    for stage in _FULL_MAP_STAGE_ORDER:
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            result.notes.append(f"cancelled before stage={stage}")
            break

        stage_budget = max(1, round(budget * _FULL_MAP_STAGE_BUDGET_SHARES[stage]))
        stage_cost_cap = max(0.0001, cost_cap * _FULL_MAP_STAGE_COST_SHARES[stage])
        child_run_id = _child_run_id(run_id, stage)

        try:
            if stage in ("state", "order", "belief", "adversarial_belief"):
                runner = {
                    "state": run_state_campaign,
                    "order": run_order_campaign,
                    "belief": run_belief_campaign,
                    "adversarial_belief": run_adversarial_belief_campaign,
                }[stage]
                state = runner(
                    client, store, child_run_id, model,
                    budget=stage_budget, cost_cap=stage_cost_cap,
                    max_tokens=max_tokens, cancel_event=cancel_event,
                    parallelism=parallelism, verify=verify,
                )
                summary = _campaign_state_summary(
                    stage, child_run_id, state, state.evaluator.spent_usd,
                    len(store.by_run(child_run_id)),
                )
                if stage == "belief":
                    belief_boundaries = dict(state.boundaries)
            elif stage == "breaking_point":
                ray = default_belief_ray()
                if belief_boundaries:
                    new_dims = []
                    for d in ray.dims:
                        if d.name in belief_boundaries:
                            midpoint, _, _ = belief_boundaries[d.name]
                            lo, hi = d.base_value, d.limit_value
                            seeded_base = min(max(midpoint, min(lo, hi)), max(lo, hi))
                            new_dims.append(replace(d, base_value=seeded_base))
                        else:
                            new_dims.append(d)
                    ray = EscalationRay(dims=tuple(new_dims))
                    seed_note = (
                        f"breaking_point ray re-seeded near belief boundaries: "
                        f"{sorted(belief_boundaries)}"
                    )
                else:
                    seed_note = (
                        "breaking_point ray used default "
                        "(no belief boundaries found to seed from)"
                    )

                evaluator = PipelineEvaluator(
                    client=client, model=model, store=store, run_id=child_run_id,
                    query_kind="belief", cost_cap_usd=stage_cost_cap,
                    max_tokens=max_tokens, cancel_event=cancel_event,
                )
                bp = find_breaking_point(evaluator, ray, budget=stage_budget)
                summary = StageSummary(
                    stage=stage,
                    run_id=child_run_id,
                    n_records=len(store.by_run(child_run_id)),
                    n_error=evaluator.error_count,
                    spend_usd=evaluator.spent_usd,
                    breaking_point_t_star=bp.t_star,
                    breaking_point_found=bp.found,
                    hypotheses_n=1 if bp.hypothesis is not None else 0,
                    note=f"{bp.note}; {seed_note}".strip("; "),
                )
            else:  # pragma: no cover -- exhaustive by construction
                continue
        except Exception as exc:  # a stage failing must not kill the battery
            summary = StageSummary(
                stage=stage,
                run_id=child_run_id,
                skipped=True,
                note=f"stage raised: {exc!r}",
            )
            result.notes.append(f"stage={stage} raised {exc!r}; continuing to next stage")

        result.stages.append(summary)
        result.total_spend_usd += summary.spend_usd
        result.total_records += summary.n_records

        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            result.notes.append(f"cancelled during/after stage={stage}")
            break

    return result


# ---------------------------------------------------------------------------
# tool_irrelevance: can the model tell an IMPOSSIBLE task from a possible one?
#
# Every other tool-province campaign asks "does the model find the plan?" on
# worlds that always have one. That cannot distinguish a model that reasons
# about feasibility from one that simply always emits a confident plan -- and
# the tool-calling literature treats exactly that confusion (fabricating
# calls for an unachievable goal) as a first-class failure mode.
#
# Design notes that matter for the result to mean anything:
#   * The decline affordance is rendered into EVERY trial, solvable or not
#     (`render(..., allow_decline=True)`). If only the unsolvable worlds
#     carried it, its presence would be the answer.
#   * Both error directions are measured. Fabricating a plan for an
#     impossible goal is the headline failure, but wrongly declining a
#     solvable task is the opposite pathology and a model can trivially
#     "win" the first metric by declining everything. Reporting one without
#     the other is how you get a meaningless benchmark.
# ---------------------------------------------------------------------------


@dataclass
class IrrelevanceResult:
    """Outcome of one `run_tool_irrelevance_campaign`."""

    n_solvable: int = 0
    n_solvable_correct: int = 0
    n_unsolvable: int = 0
    n_unsolvable_correct: int = 0
    n_error: int = 0
    n_skipped_inapplicable: int = 0
    spent_usd: float = 0.0
    capped: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def solvable_accuracy(self) -> float:
        return self.n_solvable_correct / self.n_solvable if self.n_solvable else 0.0

    @property
    def decline_accuracy(self) -> float:
        """Fraction of impossible tasks correctly declined."""
        return (
            self.n_unsolvable_correct / self.n_unsolvable if self.n_unsolvable else 0.0
        )

    @property
    def fabrication_rate(self) -> float:
        """Fraction of impossible tasks answered with a confident plan."""
        return 1.0 - self.decline_accuracy if self.n_unsolvable else 0.0

    @property
    def balanced_accuracy(self) -> float:
        """Mean of the two per-class accuracies -- the metric that cannot be
        gamed by always-plan or always-decline."""
        parts = []
        if self.n_solvable:
            parts.append(self.solvable_accuracy)
        if self.n_unsolvable:
            parts.append(self.decline_accuracy)
        return sum(parts) / len(parts) if parts else 0.0

    def to_json_dict(self) -> dict:
        return {
            "n_solvable": self.n_solvable,
            "n_solvable_correct": self.n_solvable_correct,
            "n_unsolvable": self.n_unsolvable,
            "n_unsolvable_correct": self.n_unsolvable_correct,
            "n_error": self.n_error,
            "n_skipped_inapplicable": self.n_skipped_inapplicable,
            "solvable_accuracy": self.solvable_accuracy,
            "decline_accuracy": self.decline_accuracy,
            "fabrication_rate": self.fabrication_rate,
            "balanced_accuracy": self.balanced_accuracy,
            "spent_usd": self.spent_usd,
            "capped": self.capped,
            "notes": list(self.notes),
        }


def _irrelevance_trial_seed(run_id: str, index: int) -> int:
    payload = f"tool_irrelevance|{run_id}|{index}".encode("utf-8")
    return int(hashlib.sha256(payload).hexdigest()[:15], 16)


def run_tool_irrelevance_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    n_trials: int = 40,
    unsolvable_rate: float = 0.4,
    base: ToolDimParams | None = None,
    cost_cap: float = 0.3,
    max_tokens: int | None = None,
    max_tokens_retry: int | None = None,
    parallelism: int | None = None,
    cancel_event: threading.Event | None = None,
) -> IrrelevanceResult:
    """Measure whether `model` can distinguish an achievable tool-planning
    goal from an impossible one.

    `unsolvable_rate` of the `n_trials` worlds have one load-bearing tool
    removed via `mutate.make_unsolvable`, making the goal provably
    unreachable by exhaustive BFS; the rest are ordinary solvable worlds.
    Class assignment is deterministic in `run_id` (index-based), so a rerun
    reproduces the same split. Seeds where no single-tool removal yields an
    unreachable goal are counted in `n_skipped_inapplicable` and re-drawn up
    to a few times rather than silently downgraded to solvable -- which
    would quietly shift the class balance and inflate the decline metric.
    """
    if not 0.0 <= unsolvable_rate <= 1.0:
        raise ValueError(f"unsolvable_rate must be in [0,1], got {unsolvable_rate}")

    from concurrent.futures import ThreadPoolExecutor

    from ..toolworld.mutate import make_unsolvable

    params = base if base is not None else _default_tool_base()
    default_max, default_retry = default_token_budget(model)
    mt = max_tokens if max_tokens is not None else default_max
    mtr = max_tokens_retry if max_tokens_retry is not None else default_retry
    if mtr <= mt:
        mtr = mt * 2
    if parallelism is None:
        parallelism = _default_parallelism(client)

    result = IrrelevanceResult()
    lock = threading.Lock()

    # Deterministic class assignment: the first `n_unsolvable` indices of a
    # stable shuffle are the impossible ones.
    n_unsolvable = int(round(n_trials * unsolvable_rate))
    order = sorted(
        range(n_trials),
        key=lambda i: int(
            hashlib.sha256(f"{run_id}|class|{i}".encode("utf-8")).hexdigest()[:15], 16
        ),
    )
    unsolvable_idx = set(order[:n_unsolvable])

    def one_trial(index: int) -> None:
        with lock:
            if result.spent_usd >= cost_cap:
                result.capped = True
                return
            if cancel_event is not None and cancel_event.is_set():
                return

        want_unsolvable = index in unsolvable_idx
        world = None
        seed = _irrelevance_trial_seed(run_id, index)
        if want_unsolvable:
            # Re-draw a few seeds before giving up on this slot.
            for attempt in range(4):
                probe_seed = _irrelevance_trial_seed(run_id, index + 10_000 * attempt)
                candidate = tool_generate(params, probe_seed)
                mutated = make_unsolvable(candidate, probe_seed)
                if mutated is not None:
                    world, seed = mutated, probe_seed
                    break
            if world is None:
                with lock:
                    result.n_skipped_inapplicable += 1
                return
        else:
            world = tool_generate(params, seed)

        rendered = tool_render(world, seed, allow_decline=True)
        realized = dict(tool_measure(world))
        realized["unsolvable"] = 1.0 if want_unsolvable else 0.0

        chat = client.chat(
            model=model,
            prompt=rendered.prompt,
            max_tokens=mt,
            max_tokens_retry=mtr,
        )
        cost = cost_usd(model, chat.tokens_in, chat.tokens_out)
        with lock:
            result.spent_usd += cost

        detail = f"kind=tool_plan;campaign=irrelevance;unsolvable={int(want_unsolvable)}"
        if chat.error is not None:
            verdict = Verdict.ERROR
            detail += f";error={chat.error}"
        else:
            pv = verify_plan(chat.text, world, allow_decline=True)
            verdict = pv.verdict
            detail += f";detail={pv.detail};n_calls={pv.n_calls}"
            if chat.finish_reason == "length":
                verdict = Verdict.ERROR
                detail += ";cap_hit=1"

        record = ExperimentRecord(
            run_id=run_id,
            model_name=model,
            latent_params={
                "plan_depth": float(params.plan_depth),
                "decoy_tools": float(params.decoy_tools),
                "precondition_chain": float(params.precondition_chain),
                "state_obscurity": float(params.state_obscurity),
                "parallel_goals": float(params.parallel_goals),
                "unsolvable": 1.0 if want_unsolvable else 0.0,
            },
            realized_dims=realized,
            render_mode=RenderMode.TEMPLATE,
            task_seed=seed,
            trial_seed=index,
            rendering_seed=seed,
            prompt_text=rendered.prompt,
            response_text=chat.text,
            verdict=verdict,
            verifier_detail=detail,
            tokens_in=chat.tokens_in,
            tokens_out=chat.tokens_out,
            cost_usd=cost,
            latency_ms=chat.latency_ms,
            reasoning_text=chat.reasoning,
            finish_reason=chat.finish_reason,
        )
        with lock:
            store.append(record)
            if verdict == Verdict.ERROR:
                result.n_error += 1
            elif want_unsolvable:
                result.n_unsolvable += 1
                if verdict == Verdict.PASS:
                    result.n_unsolvable_correct += 1
            else:
                result.n_solvable += 1
                if verdict == Verdict.PASS:
                    result.n_solvable_correct += 1
            if result.spent_usd >= cost_cap:
                result.capped = True

    with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
        list(pool.map(one_trial, range(n_trials)))

    if result.capped:
        result.notes.append("cost_cap_hit")
    if result.n_skipped_inapplicable:
        result.notes.append(
            f"{result.n_skipped_inapplicable} slot(s) had no single-tool "
            "removal that made the goal unreachable"
        )
    return result
