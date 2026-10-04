"""Evidence tiers: how much evidence a campaign buys, as ONE dial.

Why tiers are about evidence and not difficulty: the escalation already finds
difficulty by itself -- it walks the ray from easy to the generator's
ceiling and bisects to the break -- so a "hard mode" knob would only move
where the walk starts. What a user actually has to choose is how sure they
need to be, and how much they will pay for it. Every campaign knob that
trades money for certainty (trials per level, level budget, cost cap,
stopping early once the break is clear, re-running failures before they
count, fresh-seed confirmation, decomposition at the frontier) moves
together under one name:

    scout     -- "is anything interesting here?"  k=3 trials per level, 35% of
                 the budget and cost cap, stop as soon as the break is clear,
                 no resample gate / confirmation / decomposition. Roughly 3x
                 cheaper than rigorous (cost cap x0.35, and fewer trials per
                 level on top). Results are NOT REPORTABLE: with k=3 a level's
                 Wilson interval is ~+-0.4, and without the resample gate a
                 transient failure counts as a break, so a scout row can never
                 earn better than grade C. Use it to choose what to run next.
    standard  -- the default: k=5, 70% budget, early stop, every verification
                 step on. Good enough to report.
    rigorous  -- k=7, full budget, no early stop (every level is walked to its
                 full trial count), every verification step on. For headline
                 numbers and anything that contradicts an earlier result.

`apply_tier` maps a tier onto whatever keyword arguments a campaign runner
actually declares (runners differ: some take `k_trials`, some `verify`,
some `resample_gate`), so the same dial works on every campaign without
editing the runners.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

# Count-like kwargs that scale with the budget dial.
_BUDGET_KEYS = ("budget", "n_trials", "m_tasks")
_MIN_COUNT = {"budget": 10, "n_trials": 10, "m_tasks": 4}


@dataclass(frozen=True)
class Tier:
    k_trials: int
    budget_scale: float
    cost_scale: float
    early_stop: bool
    resample_gate: bool
    confirm: bool
    decompose: bool

    @property
    def reportable(self) -> bool:
        """Only gated + confirmed evidence is reportable."""
        return self.resample_gate and self.confirm

    @property
    def grade_ceiling(self) -> str:
        """Best confidence grade (frontier A/B/C) a result from this tier can earn."""
        return "A" if self.reportable else "C"


TIERS: dict[str, Tier] = {
    "scout": Tier(k_trials=3, budget_scale=0.35, cost_scale=0.35, early_stop=True,
                  resample_gate=False, confirm=False, decompose=False),
    "standard": Tier(k_trials=5, budget_scale=0.7, cost_scale=0.7, early_stop=True,
                     resample_gate=True, confirm=True, decompose=True),
    "rigorous": Tier(k_trials=7, budget_scale=1.0, cost_scale=1.0, early_stop=False,
                     resample_gate=True, confirm=True, decompose=True),
}


def _params(fn) -> tuple[dict[str, inspect.Parameter], bool]:
    params = dict(inspect.signature(fn).parameters)
    var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    return params, var_kw


def find_breaking_point_accepts(name: str) -> bool:
    """Checked at call time, not import time: `find_breaking_point` is gaining
    knobs (e.g. `early_stop`) while this module is in use."""
    try:
        from ..schedule.escalate import find_breaking_point
    except Exception:  # noqa: BLE001
        return False
    params, var_kw = _params(find_breaking_point)
    return name in params or var_kw


def apply_tier(spec, tier: Tier | str, base_kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Runner kwargs for `spec` at `tier`.

    Starts from `spec.default_kwargs`, overlays `base_kwargs` (explicit user
    choices win over registry defaults), then:
      * scales budget-like counts (`budget`, `n_trials`, `m_tasks`) by
        `budget_scale` and `cost_cap` by `cost_scale`;
      * sets `k_trials` -- only if the runner declares it;
      * sets `early_stop` -- only if the runner accepts it AND
        `find_breaking_point` accepts it at runtime;
      * sets `resample_gate` -- only if the runner declares it; runners that
        instead take `verify` ("auto" re-tests each failure, "manual" skips)
        get the gate mapped onto that, unless `base_kwargs` sets `verify`;
      * sets `confirm` / `decompose` -- only if the runner declares them.
    Finally drops every key the runner does not declare (unless it takes
    **kwargs), so a registry default can never crash a runner.
    `k` (the pass^k repeat count) is deliberately not tied to the tier: it
    defines the metric, and pass^3 is not comparable to pass^7.
    """
    if isinstance(tier, str):
        tier = TIERS[tier]
    base_kwargs = dict(base_kwargs or {})
    params, var_kw = _params(spec.runner)

    def declared(name: str) -> bool:
        return name in params

    out: dict[str, Any] = dict(getattr(spec, "default_kwargs", {}) or {})
    out.update(base_kwargs)
    for key in _BUDGET_KEYS:
        if key in out and key not in base_kwargs:
            out[key] = max(_MIN_COUNT[key], int(round(out[key] * tier.budget_scale)))
    if declared("budget") and "budget" not in out and params["budget"].default is not inspect.Parameter.empty:
        out["budget"] = max(_MIN_COUNT["budget"], int(round(params["budget"].default * tier.budget_scale)))
    if "cost_cap" in out and "cost_cap" not in base_kwargs:
        out["cost_cap"] = round(out["cost_cap"] * tier.cost_scale, 4)
    elif declared("cost_cap") and "cost_cap" not in out and isinstance(params["cost_cap"].default, (int, float)):
        out["cost_cap"] = round(params["cost_cap"].default * tier.cost_scale, 4)
    if declared("k_trials") and "k_trials" not in base_kwargs:
        out["k_trials"] = tier.k_trials
    if (declared("early_stop") or var_kw) and find_breaking_point_accepts("early_stop"):
        out.setdefault("early_stop", tier.early_stop)
    if declared("resample_gate"):
        out.setdefault("resample_gate", tier.resample_gate)
    elif declared("verify") and "verify" not in base_kwargs:
        out["verify"] = "auto" if tier.resample_gate else "manual"
    for knob in ("confirm", "decompose"):
        if declared(knob):
            out.setdefault(knob, getattr(tier, knob))
    if not var_kw:
        out = {k: v for k, v in out.items() if k in params}
    return out
