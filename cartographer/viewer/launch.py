"""/launch -- start a campaign from the browser instead of the CLI.

Server-rendered, stdlib-only, two-step form flow (no JS):

  step 1 (GET /launch)         -> provider dropdown + optional API key.
  step 2 (POST /launch,        -> server calls the provider's /models
          step=models)            endpoint via `exec.client.list_models`,
                                  re-renders the form with a model dropdown,
                                  campaign type, budget and cost-cap fields
                                  prefilled with the presets from
                                  `exec.evaluator`.
  launch  (POST /launch,       -> spawns the campaign in a daemon
          step=start)             `threading.Thread`, auto-generates a
                                  `camp-{type}-{model-short}-{NNN}` run_id,
                                  and redirects to /live.

Key handling: API keys live ONLY in process memory. The re-rendered step-2
form carries an opaque one-shot token referencing an in-memory dict
(`_KEY_TOKENS`) -- the key itself is never echoed into any HTML, never
logged, and never written to disk. An empty key field means "use the local
env/ key file" (`exec.keys.load_keys`).

Concurrency: at most one campaign thread at a time -- a module-level
"current run" slot guarded by a lock. While busy, /launch renders a
"campaign in progress" page linking to /live. The campaign thread opens its
own `ResultsStore` connection (never sharing the viewer's), so the only
sqlite cross-thread traffic is the store's own locked appends.

Security: this is a local tool, not a deployment -- the viewer binds to
127.0.0.1 only (see `serve.serve`), so the form (and any key typed into it)
never transits a network interface.
"""

from __future__ import annotations

import html
import inspect
import re
import secrets
import threading
from dataclasses import dataclass, field
from typing import Callable

from ..exec import client as client_mod
from ..exec.client import GROQ, NEBIUS, Client, Provider
from ..exec.evaluator import (
    run_adversarial_belief_campaign,
    run_belief_campaign,
    run_breaking_point_campaign,
    run_order_campaign,
    run_state_campaign,
)
from ..exec.keys import load_keys
from ..store.store import ResultsStore

_PROVIDERS: dict[str, Provider] = {"groq": GROQ, "nebius": NEBIUS}

# Campaign presets (mirrors the defaults of run_state_campaign /
# run_belief_campaign in exec/evaluator.py).
CAMPAIGN_PRESETS: dict[str, dict[str, float]] = {
    "state": {"budget": 160, "cost_cap": 0.5},
    "belief": {"budget": 700, "cost_cap": 1.0},
    "order": {"budget": 400, "cost_cap": 0.5},
    "breaking_point": {"budget": 350, "cost_cap": 0.75},
    "adversarial_belief": {"budget": 300, "cost_cap": 1.0},
}

# Plain-English description of every campaign preset, sourced from the
# run_* docstrings in cartographer/exec/evaluator.py and docs/lab-log.md.
# Shown on /launch so a first-time visitor can tell what each preset does
# without reading source. `new` marks presets recently added that a
# returning user might not know about yet.
CAMPAIGN_DESCRIPTIONS: dict[str, dict[str, object]] = {
    "state": {
        "query_kind": "state",
        "summary": (
            "Sweeps chain_length from 2 to 30 -- pure multi-step object-state "
            "tracking, no perspective involved. Both Nemotron tiers pass this "
            "cleanly even at 30-step chains; it hasn't found a boundary yet."
        ),
        "typical_cost": "~$0.005-0.015",
        "typical_duration": "a few minutes",
        "new": False,
    },
    "belief": {
        "query_kind": "belief",
        "summary": (
            "Sweeps chain_length, scramble, coref_pressure, and "
            "distractor_events (plus witness_cutoff) at once -- theory-of-mind "
            "false-belief tracking. This is the dimension where real capability "
            "gaps have shown up (Nano's continuity-of-observation error)."
        ),
        "typical_cost": "~$0.03-0.09",
        "typical_duration": "10-30 minutes",
        "new": False,
    },
    "order": {
        "query_kind": "order",
        "summary": (
            "Sweeps chain_length, scramble, distractor_events, and "
            "filler_events -- tests recovering true chronology when narration "
            "order is scrambled relative to real event timestamps."
        ),
        "typical_cost": "~$0.01-0.03",
        "typical_duration": "5-15 minutes",
        "new": False,
    },
    "breaking_point": {
        "query_kind": "belief (default)",
        "summary": (
            "Escalation mode: walks a single ray through the FULL joint "
            "difficulty space (all six dims at once, base to generator ceiling), "
            "bisects to find where the model actually breaks, then hands the "
            "frontier to decomposition and fresh-seed confirmation."
        ),
        "typical_cost": "~$0.05-0.15",
        "typical_duration": "15-40 minutes",
        "new": False,
    },
    "adversarial_belief": {
        "query_kind": "belief",
        "summary": (
            "Foil-driven trap selection -- picks worlds where plausible-but-"
            "wrong answers compete with the truth; broke Ultra where "
            "max-difficulty escalation couldn't. At every config point "
            "(including baseline), the world used is the winner of an "
            "adversarial seed search over that config, not just whatever the "
            "derived task seed happens to produce."
        ),
        "typical_cost": "~$0.02-0.06",
        "typical_duration": "10-25 minutes",
        "new": True,
    },
}

_RUNNERS: dict[str, Callable] = {
    "state": run_state_campaign,
    "belief": run_belief_campaign,
    "order": run_order_campaign,
    "breaking_point": run_breaking_point_campaign,
    "adversarial_belief": run_adversarial_belief_campaign,
}


@dataclass
class LaunchDeps:
    """Injection points so tests can run the whole flow with zero network."""

    db_path: str
    client_factory: Callable[..., Client] = Client
    list_models_fn: Callable[[Provider, str], list[str]] = client_mod.list_models
    key_loader: Callable[[], dict[str, str]] = load_keys
    runners: dict[str, Callable] = field(default_factory=lambda: dict(_RUNNERS))


# ---------------------------------------------------------------------------
# In-memory state (module-level; the viewer is a single local process).
# ---------------------------------------------------------------------------

_lock = threading.Lock()
# token -> (provider_name, api_key). One-shot: popped on launch.
_KEY_TOKENS: dict[str, tuple[str, str]] = {}
# The single in-flight campaign, or None. {"run_id": str, "thread": Thread}
_CURRENT_RUN: dict | None = None


def current_run() -> dict | None:
    with _lock:
        if _CURRENT_RUN is None:
            return None
        out = dict(_CURRENT_RUN)
        out.pop("cancel_event", None)  # not for callers outside this module
        return out


def request_stop() -> bool:
    """Signal the active campaign (if any) to cooperatively cancel.

    Returns True if a run was active and its cancel_event was set, False if
    there's no active run to stop. Setting the event doesn't kill the
    thread immediately -- `PipelineEvaluator` checks it before each API
    call, so the scheduler drains at the next trial boundary and the
    thread exits on its own.
    """
    with _lock:
        if _CURRENT_RUN is None:
            return False
        event = _CURRENT_RUN.get("cancel_event")
        if event is None:
            return False
        event.set()
        return True


def is_stopping() -> bool:
    """True if the active run's cancel_event has been set but the thread
    hasn't exited yet (used by /live to render a "stopping..." state)."""
    with _lock:
        if _CURRENT_RUN is None:
            return False
        event = _CURRENT_RUN.get("cancel_event")
        return event.is_set() if event is not None else False


def _reset_for_tests() -> None:
    global _CURRENT_RUN
    with _lock:
        _KEY_TOKENS.clear()
        _CURRENT_RUN = None


# ---------------------------------------------------------------------------
# HTML builders
# ---------------------------------------------------------------------------


def _provider_options(selected: str = "nebius") -> str:
    return "".join(
        f'<option value="{name}"{" selected" if name == selected else ""}>{name}</option>'
        for name in sorted(_PROVIDERS)
    )


def _preset_cards_html(campaign_types: list[str] | None = None) -> str:
    """Plain-English description card per campaign preset, in
    CAMPAIGN_PRESETS iteration order (alphabetical) unless a caller
    supplies an explicit order. Each card names its query kind, a
    one-sentence description of what it tests, typical cost, and typical
    duration -- and a NEW badge for recently added presets."""
    types = campaign_types or sorted(CAMPAIGN_PRESETS)
    cards = []
    for t in types:
        desc = CAMPAIGN_DESCRIPTIONS.get(t, {})
        preset = CAMPAIGN_PRESETS[t]
        badge = ' <span class="pill pill-PASS">NEW</span>' if desc.get("new") else ""
        cards.append(
            '<div class="stat-box">'
            f'<p style="margin:0 0 0.3rem;"><strong class="mono">{html.escape(t)}</strong>{badge}'
            f' <span class="meta">&middot; query kind: {html.escape(str(desc.get("query_kind", "-")))}</span></p>'
            f'<p class="meta" style="margin:0 0 0.4rem;">{html.escape(str(desc.get("summary", "")))}</p>'
            f'<p class="meta" style="margin:0;">budget {int(preset["budget"])} trials &middot; '
            f'cap ${preset["cost_cap"]} &middot; typical cost {html.escape(str(desc.get("typical_cost", "-")))} '
            f'&middot; typical duration {html.escape(str(desc.get("typical_duration", "-")))}</p>'
            "</div>"
        )
    return f'<div class="stat-grid" style="grid-template-columns: 1fr;">{"".join(cards)}</div>'


def launch_form_html() -> str:
    """Step 1: provider + key form, plus a description card for every
    campaign preset so a first-time visitor can see what's available
    (including adversarial_belief, the newest mode) before even loading
    models."""
    return f"""
    <h1>Launch a campaign</h1>
    <p class="meta">Pick a provider, load its models, then choose one of the
    campaign presets described below.</p>
    <form method="post" action="/launch">
      <input type="hidden" name="step" value="models">
      <p><label>Provider:
        <select name="provider">{_provider_options()}</select></label></p>
      <p><label>API key:
        <input type="password" name="api_key" value="" autocomplete="off"></label><br>
        <span class="meta">leave empty to use env/ key file</span></p>
      <p><button type="submit">Load models</button></p>
    </form>
    <h2>Campaign presets</h2>
    {_preset_cards_html()}
    """


def busy_html(run_id: str) -> str:
    return (
        "<h1>Campaign in progress</h1>"
        f"<p>Run <strong>{html.escape(run_id)}</strong> is currently running. "
        "Only one campaign can run at a time.</p>"
        '<p><a href="/live">Watch it on the live monitor &rarr;</a></p>'
    )


def error_html(message: str) -> str:
    return (
        "<h1>Launch error</h1>"
        f"<p>{html.escape(message)}</p>"
        '<p><a href="/launch">&larr; back to launch</a></p>'
    )


def models_step_html(provider_name: str, token: str, models: list[str]) -> str:
    """Step 2: model dropdown + campaign settings. Carries `token`, never
    the key."""
    model_opts = "".join(
        f'<option value="{html.escape(m)}">{html.escape(m)}</option>' for m in models
    )
    type_opts = "".join(
        f'<option value="{t}">{t}</option>' for t in sorted(CAMPAIGN_PRESETS)
    )
    default = CAMPAIGN_PRESETS["state"]
    return f"""
    <p><a href="/launch">&larr; start over</a></p>
    <h1>Launch a campaign &mdash; {html.escape(provider_name)}</h1>
    <p class="meta">{len(models)} chat-capable models found. Pick a model and a campaign type below.</p>
    <form method="post" action="/launch">
      <input type="hidden" name="step" value="start">
      <input type="hidden" name="token" value="{html.escape(token)}">
      <p><label>Model: <select name="model">{model_opts}</select></label></p>
      <p><label>Campaign type: <select name="campaign_type">{type_opts}</select></label></p>
      <p><label>Budget (trials): <input name="budget" value="{int(default['budget'])}"></label></p>
      <p><label>Cost cap (USD): <input name="cost_cap" value="{default['cost_cap']}"></label></p>
      <p><label>Advanced: max tokens <input name="max_tokens" value=""></label><br>
         <span class="meta">blank = auto (model-aware default; "ultra"-class reasoning
         models get a bigger budget automatically)</span></p>
      <p><button type="submit">Launch</button></p>
    </form>
    <h2>What each campaign type tests</h2>
    {_preset_cards_html()}
    """


# ---------------------------------------------------------------------------
# Flow handlers -- return (status, headers, body_html_or_empty)
# ---------------------------------------------------------------------------


def handle_launch_get(deps: LaunchDeps) -> str:
    run = current_run()
    if run is not None:
        return busy_html(run["run_id"])
    return launch_form_html()


def _resolve_key(provider_name: str, api_key: str, deps: LaunchDeps) -> str | None:
    if api_key:
        return api_key
    try:
        keys = deps.key_loader()
    except OSError:
        return None
    return keys.get(provider_name)


def handle_models_step(form: dict[str, str], deps: LaunchDeps) -> str:
    provider_name = form.get("provider", "")
    provider = _PROVIDERS.get(provider_name)
    if provider is None:
        return error_html(f"unknown provider: {provider_name}")

    key = _resolve_key(provider_name, form.get("api_key", "").strip(), deps)
    if not key:
        return error_html(
            f"no API key provided and none found in the env/ key file for {provider_name}"
        )

    try:
        models = deps.list_models_fn(provider, key)
    except RuntimeError as exc:
        return error_html(str(exc))
    if not models:
        return error_html(f"{provider_name} returned no chat-capable models")

    token = secrets.token_hex(16)
    with _lock:
        _KEY_TOKENS[token] = (provider_name, key)
    return models_step_html(provider_name, token, models)


def _model_short(model: str) -> str:
    short = model.split("/")[-1].lower()
    short = re.sub(r"[^a-z0-9]+", "-", short).strip("-")
    return short[:40] or "model"


def next_run_id(db_path: str, campaign_type: str, model: str) -> str:
    """`camp-{type}-{model-short}-{NNN}`, NNN incremented until unused."""
    store = ResultsStore(db_path)
    try:
        existing = {
            row["run_id"]
            for row in store._conn.execute("SELECT DISTINCT run_id FROM experiments")
        }
    finally:
        store.close()
    base = f"camp-{campaign_type}-{_model_short(model)}"
    n = 1
    while f"{base}-{n:03d}" in existing:
        n += 1
    return f"{base}-{n:03d}"


def handle_start(
    form: dict[str, str], deps: LaunchDeps
) -> tuple[bool, str]:
    """Attempt to launch. Returns (started, html_or_run_id): on success,
    (True, run_id) -- caller should redirect to /live; on failure,
    (False, error/busy page html).
    """
    global _CURRENT_RUN

    token = form.get("token", "")
    with _lock:
        cred = _KEY_TOKENS.pop(token, None)
    if cred is None:
        return False, error_html("invalid or expired launch token; start over")
    provider_name, key = cred

    model = form.get("model", "").strip()
    campaign_type = form.get("campaign_type", "")
    runner = deps.runners.get(campaign_type)
    if not model or runner is None:
        return False, error_html("missing model or unknown campaign type")

    preset = CAMPAIGN_PRESETS.get(campaign_type, {"budget": 160, "cost_cap": 0.5})
    try:
        budget = int(float(form.get("budget", preset["budget"])))
        cost_cap = float(form.get("cost_cap", preset["cost_cap"]))
        max_tokens_raw = form.get("max_tokens", "").strip()
        max_tokens = int(float(max_tokens_raw)) if max_tokens_raw else None
    except ValueError:
        return False, error_html("budget, cost cap, and max tokens must be numeric")

    run_id = next_run_id(deps.db_path, campaign_type, model)
    provider = _PROVIDERS[provider_name]
    cancel_event = threading.Event()

    with _lock:
        if _CURRENT_RUN is not None:
            return False, busy_html(_CURRENT_RUN["run_id"])
        thread = threading.Thread(
            target=_campaign_thread,
            args=(deps, provider, key, model, runner, run_id, budget, cost_cap, max_tokens, cancel_event),
            name=f"campaign-{run_id}",
            daemon=True,
        )
        _CURRENT_RUN = {"run_id": run_id, "thread": thread, "cancel_event": cancel_event}
    thread.start()
    return True, run_id


def _campaign_thread(
    deps: LaunchDeps,
    provider: Provider,
    key: str,
    model: str,
    runner: Callable,
    run_id: str,
    budget: int,
    cost_cap: float,
    max_tokens: int | None,
    cancel_event: threading.Event,
) -> None:
    global _CURRENT_RUN
    try:
        store = ResultsStore(deps.db_path)
        try:
            campaign_client = deps.client_factory(provider, key)
            kwargs = dict(
                run_id=run_id,
                model=model,
                budget=budget,
                cost_cap=cost_cap,
            )
            # Runners in tests may be simple fakes that don't accept these
            # newer kwargs -- only pass them through when the runner
            # actually declares the parameter, so old/fake runners keep
            # working unchanged.
            try:
                params = inspect.signature(runner).parameters
            except (TypeError, ValueError):
                params = {}
            if "max_tokens" in params:
                kwargs["max_tokens"] = max_tokens
            if "cancel_event" in params:
                kwargs["cancel_event"] = cancel_event
            runner(campaign_client, store, **kwargs)
        finally:
            store.close()
    except Exception as exc:  # never let a campaign crash kill silently
        # Key material is never part of these messages (Client.__repr__
        # redacts, and we only format the exception itself).
        import sys

        sys.stderr.write(f"[launch] campaign {run_id} failed: {exc!r}\n")
    finally:
        with _lock:
            _CURRENT_RUN = None
