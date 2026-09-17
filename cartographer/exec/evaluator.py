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
import threading
from dataclasses import dataclass, replace
from typing import Callable

from ..render import build_name_map, render, verify
from ..schedule.escalate import BreakingPoint, EscalationRay, RayDim, find_breaking_point
from ..schedule.scheduler import CampaignState, DimSpec, SchedulerConfig, run_campaign
from ..store.schema import ExperimentRecord, RenderMode, Verdict
from ..store.store import ResultsStore
from ..world.generate import DimParams, generate
from ..world.measure import measure
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
    ) -> None:
        self.client = client
        self.model = model
        self.store = store
        self.run_id = run_id
        self.query_kind = query_kind
        self.cost_cap_usd = cost_cap_usd
        self.base = base if base is not None else _default_base(query_kind)

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

        world = generate(params, seed)
        name_map = build_name_map(world, seed)
        rendered = render(world, name_map, seed)
        realized_dims = measure(world)

        verifier_detail = f"kind={self.query_kind};campaign=1"

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
) -> CampaignState:
    """Run the "state" query-kind campaign: sweep chain_length from 2 to 30.

    `parallelism=None` defaults to the client provider's
    `suggested_parallelism` (Groq 2, Nebius 6 -- rate-limit-aware).
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
        query_kind="state",
        cost_cap_usd=cost_cap,
        max_tokens=max_tokens,
        cancel_event=cancel_event,
    )
    if parallelism is None:
        parallelism = _default_parallelism(client)
    config = _state_scheduler_config(budget, parallelism)

    def on_record(cfg: dict, passed: bool, seed: int) -> None:
        pass  # PipelineEvaluator already persists every trial itself.

    state = run_campaign(evaluator, config, on_record=on_record)
    if evaluator.capped:
        state.notes.append("cost_cap_hit")
    state.evaluator = evaluator  # type: ignore[attr-defined]
    return state


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
    """
    evaluator = PipelineEvaluator(
        client=client,
        model=model,
        store=store,
        run_id=run_id,
        query_kind="order",
        cost_cap_usd=cost_cap,
        max_tokens=max_tokens,
        cancel_event=cancel_event,
    )
    if parallelism is None:
        parallelism = _default_parallelism(client)
    config = _order_scheduler_config(budget, parallelism)

    def on_record(cfg: dict, passed: bool, seed: int) -> None:
        pass  # PipelineEvaluator already persists every trial itself.

    state = run_campaign(evaluator, config, on_record=on_record)
    if evaluator.capped:
        state.notes.append("cost_cap_hit")
    state.evaluator = evaluator  # type: ignore[attr-defined]
    return state


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
) -> CampaignState:
    """Run the "belief" query-kind campaign across four dims at once.

    `parallelism=None` defaults to the client provider's
    `suggested_parallelism` (Groq 2, Nebius 6 -- rate-limit-aware).
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
        query_kind="belief",
        cost_cap_usd=cost_cap,
        max_tokens=max_tokens,
        cancel_event=cancel_event,
    )
    if parallelism is None:
        parallelism = _default_parallelism(client)
    config = _belief_scheduler_config(budget, parallelism)

    def on_record(cfg: dict, passed: bool, seed: int) -> None:
        pass  # PipelineEvaluator already persists every trial itself.

    state = run_campaign(evaluator, config, on_record=on_record)
    if evaluator.capped:
        state.notes.append("cost_cap_hit")
    state.evaluator = evaluator  # type: ignore[attr-defined]
    return state
