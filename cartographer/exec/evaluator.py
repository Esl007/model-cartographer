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
_FLOAT_KEYS = {"scramble"}
_KNOWN_KEYS = _INT_KEYS | _FLOAT_KEYS


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
        max_tokens: int = 1500,
    ) -> None:
        self.client = client
        self.model = model
        self.store = store
        self.run_id = run_id
        self.query_kind = query_kind
        self.cost_cap_usd = cost_cap_usd
        self.base = base if base is not None else _default_base(query_kind)
        self.max_tokens = max_tokens

        self.spent_usd = 0.0
        self.call_count = 0
        self.error_count = 0
        self.capped = False
        self._lock = threading.Lock()

    def __call__(self, config: dict[str, float], trial_seed: int) -> bool:
        params = _apply_config(self.base, config)
        seed = _task_seed(config, trial_seed)

        with self._lock:
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True
                self._record_capped(params, config, seed, trial_seed)
                return False

        world = generate(params, seed)
        name_map = build_name_map(world, seed)
        rendered = render(world, name_map, seed)
        realized_dims = measure(world)

        verifier_detail = f"kind={self.query_kind};campaign=1"

        result = self.client.chat(model=self.model, prompt=rendered.prompt, max_tokens=self.max_tokens)
        cost = cost_usd(self.model, result.tokens_in, result.tokens_out)
        with self._lock:
            self.call_count += 1
            self.spent_usd += cost

        if result.error is not None:
            # Retry once on error before giving up.
            retry = self.client.chat(model=self.model, prompt=rendered.prompt, max_tokens=self.max_tokens)
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

        record = ExperimentRecord(
            run_id=self.run_id,
            model_name=self.model,
            latent_params={
                "chain_length": float(params.chain_length),
                "scramble": float(params.scramble),
                "coref_pressure": float(params.coref_pressure),
                "distractor_events": float(params.distractor_events),
                "filler_events": float(params.filler_events),
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
    ) -> None:
        """Record a zero-cost, zero-call trial once the cost cap has been
        hit -- no API call is made, so the scheduler's shared budget drains
        without incurring further spend. Caller must hold `self._lock`.
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
            },
            realized_dims={},
            render_mode=RenderMode.TEMPLATE,
            task_seed=seed,
            trial_seed=trial_seed,
            rendering_seed=seed,
            prompt_text=None,
            response_text=None,
            verdict=Verdict.ERROR,
            verifier_detail=f"kind={self.query_kind};campaign=1;cost_capped=1",
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
) -> CampaignState:
    """Run the "state" query-kind campaign: sweep chain_length from 2 to 30.

    `parallelism=None` defaults to the client provider's
    `suggested_parallelism` (Groq 2, Nebius 6 -- rate-limit-aware).
    """
    evaluator = PipelineEvaluator(
        client=client,
        model=model,
        store=store,
        run_id=run_id,
        query_kind="state",
        cost_cap_usd=cost_cap,
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


def run_belief_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    budget: int = 700,
    cost_cap: float = 1.0,
    parallelism: int | None = None,
) -> CampaignState:
    """Run the "belief" query-kind campaign across four dims at once.

    `parallelism=None` defaults to the client provider's
    `suggested_parallelism` (Groq 2, Nebius 6 -- rate-limit-aware).
    """
    evaluator = PipelineEvaluator(
        client=client,
        model=model,
        store=store,
        run_id=run_id,
        query_kind="belief",
        cost_cap_usd=cost_cap,
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
