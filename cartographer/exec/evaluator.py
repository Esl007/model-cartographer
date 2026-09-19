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

from ..render import build_name_map, render, verify
from ..schedule.escalate import BreakingPoint, EscalationRay, RayDim, find_breaking_point
from ..schedule.scheduler import CampaignState, DimSpec, SchedulerConfig, run_campaign
from ..store.schema import ExperimentRecord, RenderMode, Verdict
from ..store.store import ResultsStore
from ..world.generate import DimParams, generate
from ..world.measure import measure
from ..world.search import search_adversarial_seeds
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
            # Retry once on error before giving up.
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
) -> CampaignState:
    """Run the "state" query-kind campaign: sweep chain_length from 2 to 30.

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
