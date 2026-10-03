"""Province III campaigns: an escalation `Evaluator` over interactive
episodes, so ramp / bisect / logistic t* / decompose / confirm /
degradation-onset all work unchanged on the new province."""

from __future__ import annotations

import threading

from ..exec.client import Client
from ..exec.evaluator import (
    _TOOL_KNOWN_KEYS,
    _apply_tool_config,
    _apply_tool_trap_config,
    _default_tool_base,
    _task_seed,
)
from ..exec.pricing import cost_usd
from ..schedule.escalate import BreakingPoint, EscalationRay, RayDim, find_breaking_point
from ..store.schema import ExperimentRecord, RenderMode, Verdict
from ..store.store import ResultsStore
from ..toolworld.generate import ToolDimParams
from ..toolworld.generate import generate as tool_generate
from ..toolworld.measure import measure as tool_measure
from .env import InteractiveEnv, InteractiveSpec
from .episode import MODEL_OUTCOMES, run_episode

_TRAP_DIMS = (
    "poison_traps", "dishonest_decoys_on", "implicit_preconditions_on",
    "zero_slack_on", "id_collisions_on",
)


def default_interactive_ray() -> EscalationRay:
    """Plan depth 4 -> 16 together with value passing 0 -> 1 (fraction of the
    plan's dependency edges that carry a ticket). Decompose at the frontier
    then separates "too long" from "can't carry values" from their product."""
    return EscalationRay(
        dims=(RayDim("plan_depth", 4, 16, True), RayDim("value_links", 0.0, 1.0, False))
    )


class InteractiveEvaluator:
    """`(config, trial_seed) -> bool` over one interactive episode, persisting
    a full `ExperimentRecord` (kind=interactive). Config keys: any
    `ToolDimParams` scalar, the five trap toggles, `value_links` (0..1) and
    `fault_step` (1-based call number to fail transiently; 0 = none)."""

    def __init__(
        self,
        client: Client,
        model: str,
        store: ResultsStore,
        run_id: str,
        cost_cap_usd: float,
        base: ToolDimParams | None = None,
        max_tokens: int = 16000,
        cancel_event: threading.Event | None = None,
        protocol: str = "native",
    ) -> None:
        self.client, self.model, self.store, self.run_id = client, model, store, run_id
        self.protocol = protocol
        self.cost_cap_usd = cost_cap_usd
        self.base = base if base is not None else _default_tool_base()
        self.max_tokens = max_tokens
        self.cancel_event = cancel_event
        self.spent_usd = 0.0
        self.call_count = 0
        self.error_count = 0
        self.capped = False
        self.cancelled = False
        self._lock = threading.Lock()

    def __call__(self, config: dict[str, float], trial_seed: int) -> bool:
        params = _apply_tool_config(self.base, {k: v for k, v in config.items() if k in _TOOL_KNOWN_KEYS})
        seed = _task_seed(config, trial_seed)
        latent = {
            "plan_depth": float(params.plan_depth),
            "decoy_tools": float(params.decoy_tools),
            "precondition_chain": float(params.precondition_chain),
            "state_obscurity": float(params.state_obscurity),
            "parallel_goals": float(params.parallel_goals),
            "value_links": float(config.get("value_links", 0.0)),
            "fault_step": float(int(round(config.get("fault_step", 0.0)))),
            **{k: float(1.0 if config.get(k, 0.0) >= 0.5 else 0.0) for k in _TRAP_DIMS},
        }

        with self._lock:
            reason = None
            if self.spent_usd >= self.cost_cap_usd:
                self.capped, reason = True, "cost_capped"
            elif self.cancel_event is not None and self.cancel_event.is_set():
                self.cancelled, reason = True, "cancelled"
            if reason:
                self.error_count += 1
                self._append(latent, {}, seed, trial_seed, None, None, Verdict.ERROR,
                             f"kind=interactive;campaign=1;{reason}=1", 0, 0, 0.0, 0.0, None)
                return False

        world = tool_generate(params, seed)
        world, style = _apply_tool_trap_config(world, config, seed)
        env = InteractiveEnv(
            world, seed,
            InteractiveSpec(value_links=latent["value_links"], fault_step=int(latent["fault_step"]), style=style),
        )
        res = run_episode(
            self.client, self.model, env, max_tokens=self.max_tokens, cost_fn=cost_usd,
            may_continue=lambda ep_cost: self.spent_usd + ep_cost < self.cost_cap_usd,
            protocol=self.protocol,
        )
        s = res.summary
        if res.outcome == "goal_reached":
            verdict = Verdict.PASS
        elif res.outcome in MODEL_OUTCOMES:
            verdict = Verdict.FAIL
        else:
            verdict = Verdict.ERROR
        detail = (
            f"kind=interactive;campaign=1;protocol={res.protocol};outcome={res.outcome}"
            f";detail={s['first_error_kind'] or res.outcome};n_calls={s['n_calls']};optimal={s['optimal']}"
            f";errors={s['n_errors']};invalid_tickets={s['invalid_tickets']};recovered={int(s['recovered'])}"
            f";first_deviation_step={s['first_deviation_step']};links={s['n_links']}"
            f";fault_step={int(latent['fault_step'])};turns={res.turns}"
        )
        if res.error:
            detail += f";error={res.error}"
        realized = dict(tool_measure(world))
        realized.update({
            "n_calls": float(s["n_calls"]), "n_errors": float(s["n_errors"]),
            "invalid_tickets": float(s["invalid_tickets"]), "n_links": float(s["n_links"]),
            "recovered": 1.0 if s["recovered"] else 0.0,
            "first_deviation_step": float(s["first_deviation_step"] or -1),
            "final_distance": float(s["final_distance"] if s["final_distance"] is not None else -1),
        })
        prompt = env.opening_messages()[1]["content"]
        with self._lock:
            self.spent_usd += res.cost_usd
            self.call_count += res.turns
            if verdict == Verdict.ERROR:
                self.error_count += 1
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True
            self._append(latent, realized, seed, trial_seed, prompt, env.transcript(), verdict, detail,
                         res.tokens_in, res.tokens_out, res.cost_usd, res.latency_ms, res.finish_reason)
        return verdict == Verdict.PASS

    def _append(self, latent, realized, seed, trial_seed, prompt, response, verdict, detail,
                t_in, t_out, cost, latency, finish) -> None:
        self.store.append(
            ExperimentRecord(
                run_id=self.run_id, model_name=self.model, latent_params=latent,
                realized_dims=realized, render_mode=RenderMode.TEMPLATE, task_seed=seed,
                trial_seed=trial_seed, rendering_seed=seed, prompt_text=prompt,
                response_text=response, verdict=verdict, verifier_detail=detail,
                tokens_in=t_in, tokens_out=t_out, cost_usd=cost, latency_ms=latency,
                finish_reason=finish,
            )
        )


def run_interactive_breaking_point_campaign(
    client: Client,
    store: ResultsStore,
    run_id: str,
    model: str,
    ray: EscalationRay | None = None,
    budget: int = 60,
    cost_cap: float = 0.2,
    k_trials: int = 5,
    parallelism: int = 4,
    max_tokens: int = 16000,
    cancel_event: threading.Event | None = None,
    protocol: str = "native",
) -> BreakingPoint:
    """Escalation over interactive episodes (default ray: plan depth 4 -> 16
    with value passing 0 -> 1). Episodes cost several calls each, so the
    defaults are smaller than Province II's: k_trials 5, budget 60."""
    ev = InteractiveEvaluator(client, model, store, run_id, cost_cap, max_tokens=max_tokens,
                              cancel_event=cancel_event, protocol=protocol)
    bp = find_breaking_point(ev, ray if ray is not None else default_interactive_ray(),
                             budget=budget, k_trials=k_trials, parallelism=parallelism)
    note = f"spent=${ev.spent_usd:.4f}" + (";cost_cap_hit" if ev.capped else "")
    bp.note = f"{bp.note}; {note}" if bp.note else note
    return bp
