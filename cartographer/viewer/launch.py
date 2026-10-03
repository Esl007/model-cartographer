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

from ..confirm.replicate import next_replication_run_id, run_finding_replication
from ..exec import client as client_mod
from ..exec.client import GROQ, NEBIUS, Client, Provider
from ..exec.evaluator import (
    _KNOWN_KEYS,
    run_adversarial_belief_campaign,
    run_belief_campaign,
    run_breaking_point_campaign,
    run_code_breaking_point_campaign,
    run_full_map_campaign,
    run_order_campaign,
    run_state_campaign,
    run_tool_breaking_point_campaign,
    run_tool_planning_campaign,
    run_verify_failures,
)
from ..exec.keys import load_keys
from ..store.store import ResultsStore

# One-line cost note shown next to the /launch "Verify failures" control --
# see run_state_campaign et al.'s `verify` param and
# cartographer.exec.evaluator.run_verify_failures.
VERIFY_FAILURES_COST_NOTE = (
    "re-tests each failure 3x -- typically adds a few percent to cost; "
    "uncheck to skip and verify later by hand"
)

_PROVIDERS: dict[str, Provider] = {"groq": GROQ, "nebius": NEBIUS}

# Campaign presets (mirrors the defaults of run_state_campaign /
# run_belief_campaign in exec/evaluator.py).
CAMPAIGN_PRESETS: dict[str, dict[str, float]] = {
    "state": {"budget": 160, "cost_cap": 0.5},
    "belief": {"budget": 700, "cost_cap": 1.0},
    "order": {"budget": 400, "cost_cap": 0.5},
    "breaking_point": {"budget": 350, "cost_cap": 0.75},
    "adversarial_belief": {"budget": 300, "cost_cap": 1.0},
    "tool_planning": {"budget": 400, "cost_cap": 0.75},
    "tool_breaking_point": {"budget": 250, "cost_cap": 0.5},
    "code_tracing": {"budget": 200, "cost_cap": 0.5},
    "full_map": {"budget": 600, "cost_cap": 2.0},
}

# The default-selected campaign preset on /launch -- the one-button
# "profile this model" option (see CAMPAIGN_DESCRIPTIONS["full_map"]).
DEFAULT_CAMPAIGN_TYPE = "full_map"

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
    "tool_planning": {
        "query_kind": "tool_plan",
        "summary": (
            "SECOND CAPABILITY PROVINCE. Gives the model a devops-flavored "
            "state machine, a goal, and a catalog of typed tools with real "
            "preconditions (some decoys, some destructive, some gated behind "
            "another tool's effect), and asks for a JSON plan of tool calls. "
            "The verifier SIMULATES the plan step by step -- an unmet "
            "precondition or an unreached goal is a hard FAIL. Sweeps plan "
            "depth, decoy count, precondition-chain length, state obscurity, "
            "and parallel goals. Ground truth is bounded search, never an LLM "
            "judge."
        ),
        "typical_cost": "~$0.02-0.06",
        "typical_duration": "10-25 minutes",
        "new": True,
    },
    "tool_breaking_point": {
        "query_kind": "tool_plan",
        "summary": (
            "Escalation over the tool-planning province's STRUCTURAL traps "
            "(not just scalar difficulty): decoys rewritten to sound "
            "load-bearing, preconditions collapsed into one general-rules "
            "paragraph, a poisoned tool that forecloses the goal, near-"
            "collision tool ids, a zero-slack budget, and interleaved "
            "parallel goals -- walked from off to all-on to find where the "
            "model actually breaks. Built because the tool-planning province "
            "saturated (Nano 39/40, Super 40/40) under scalar-only escalation."
        ),
        "typical_cost": "~$0.02-0.08",
        "typical_duration": "10-25 minutes",
        "new": True,
    },
    "code_tracing": {
        "query_kind": "code_trace",
        "summary": (
            "Province IV. Shows the model a generated Python program and asks for a "
            "variable's final value, escalating program length together with "
            "look-alike variable names (total / totl). Ground truth comes from "
            "the project's own interpreter, never from running model output; a "
            "wrong answer is matched to the shallow reading behind it "
            "(lists copied on assignment, closures bound early, range "
            "inclusive, a look-alike variable read instead)."
        ),
        "typical_cost": "~$0.05-0.45",
        "typical_duration": "10-30 minutes",
        "new": True,
    },
    "full_map": {
        "query_kind": "state, order, belief, adversarial_belief, breaking_point",
        "summary": (
            "RECOMMENDED. Runs the complete battery -- state, ordering, belief, "
            "adversarial traps, and breaking-point search -- one budget, one "
            "report. Answers \"what can this model actually do\" in a single "
            "launch instead of five separate campaign types."
        ),
        "typical_cost": "~$0.10-0.30",
        "typical_duration": "30-90 minutes",
        "new": True,
        "recommended": True,
    },
}

_RUNNERS: dict[str, Callable] = {
    "state": run_state_campaign,
    "belief": run_belief_campaign,
    "order": run_order_campaign,
    "breaking_point": run_breaking_point_campaign,
    "adversarial_belief": run_adversarial_belief_campaign,
    "tool_planning": run_tool_planning_campaign,
    "tool_breaking_point": run_tool_breaking_point_campaign,
    "code_tracing": run_code_breaking_point_campaign,
    "full_map": run_full_map_campaign,
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


def _preset_card(t: str, checked: bool, radio_name: str | None = None) -> str:
    """One preset as a radio-card (design-notes principle 23): the whole
    card is a <label>, selectable, with cost/duration expectations visible
    before submit. If `radio_name` is given the card includes an actual
    <input type="radio"> (used on the models step, where a real selection
    is made); otherwise it's a read-only description card (used on the
    provider step, before campaign_type even exists as a field yet)."""
    desc = CAMPAIGN_DESCRIPTIONS.get(t, {})
    preset = CAMPAIGN_PRESETS[t]
    badge = ""
    if desc.get("recommended"):
        badge += ' <span class="badge badge-recommended">RECOMMENDED</span>'
    if desc.get("new"):
        badge += ' <span class="badge badge-new">NEW</span>'
    radio = (
        f'<input type="radio" name="{radio_name}" value="{html.escape(t)}"'
        f'{" checked" if checked else ""} style="display:none;">'
        if radio_name
        else ""
    )
    return (
        f'<label class="preset-card">{radio}'
        f'<span class="preset-title"><span class="mono">{html.escape(t)}</span>{badge}</span>'
        f'<span class="preset-desc">{html.escape(str(desc.get("summary", "")))}</span>'
        f'<span class="preset-desc mono" style="display:block; margin-top:0.4rem;">'
        f'budget {int(preset["budget"])} trials &middot; cap ${preset["cost_cap"]} &middot; '
        f'{html.escape(str(desc.get("typical_cost", "-")))} &middot; '
        f'{html.escape(str(desc.get("typical_duration", "-")))}</span>'
        "</label>"
    )


def _preset_cards_html(campaign_types: list[str] | None = None, radio_name: str | None = None) -> str:
    """Plain-English description card per campaign preset. full_map (the
    recommended default) is shown first and prominently; the rest are
    grouped under an "Advanced" <details> so the primary path isn't
    competing visually with five equal-weight options (principle 23)."""
    types = campaign_types or sorted(
        CAMPAIGN_PRESETS, key=lambda t: (t != DEFAULT_CAMPAIGN_TYPE, t)
    )
    primary = [t for t in types if t == DEFAULT_CAMPAIGN_TYPE]
    rest = [t for t in types if t != DEFAULT_CAMPAIGN_TYPE]

    primary_html = "".join(
        _preset_card(t, checked=(t == DEFAULT_CAMPAIGN_TYPE), radio_name=radio_name) for t in primary
    )
    rest_html = "".join(
        _preset_card(t, checked=False, radio_name=radio_name) for t in rest
    )
    advanced = (
        f'<details><summary>Advanced: other campaign presets ({len(rest)})</summary>'
        f'<div class="preset-cards">{rest_html}</div></details>'
        if rest_html
        else ""
    )
    return f'<div class="preset-cards">{primary_html}</div>{advanced}'


def launch_form_html() -> str:
    """Step 1: provider + key form, plus a description card for every
    campaign preset so a first-time visitor can see what's available
    (including adversarial_belief, the newest mode) before even loading
    models."""
    return f"""
    <div class="page-head"><div class="page-head-main">
    <h1>Launch a campaign</h1>
    <p class="subtitle">Pick a provider, load its models, then choose one of the
    campaign presets described below. full_map is the recommended default --
    one budget, one report on everything this model can and can't do.</p>
    </div><div class="page-head-actions">
      <span class="meta">step 1 of 2 &middot; provider</span>
    </div></div>
    <div class="card">
      <div class="card-head">
        <span class="card-title">Configure campaign</span>
        <span class="card-meta">step 1 &middot; provider + key</span>
      </div>
      <div class="card-body">
      <form method="post" action="/launch">
        <input type="hidden" name="step" value="models">
        <p><label>Provider:
          <select name="provider">{_provider_options()}</select></label></p>
        <p><label>API key:
          <input type="password" name="api_key" value="" autocomplete="off"></label><br>
          <span class="meta">leave empty to use env/ key file</span></p>
        <p><button type="submit" class="btn-primary">Load models</button></p>
      </form>
      </div>
    </div>
    <div class="card">
      <div class="card-head">
        <span class="card-title">Campaign presets</span>
        <span class="card-meta">{len(CAMPAIGN_PRESETS)} presets</span>
      </div>
      <div class="card-body">{_preset_cards_html()}</div>
    </div>
    """


def busy_html(run_id: str) -> str:
    safe_run_id = html.escape(run_id)
    return (
        "<h1>Campaign in progress</h1>"
        f"<p>Run <strong>{safe_run_id}</strong> is currently running. "
        "Only one campaign can run at a time.</p>"
        f'<p><a href="/live">Watch it on the live monitor &rarr;</a> &middot; '
        f'<a href="/run/{safe_run_id}">jump straight to this run\'s page &rarr;</a></p>'
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
        f'<option value="{t}"{" selected" if t == DEFAULT_CAMPAIGN_TYPE else ""}>{t}</option>'
        for t in sorted(CAMPAIGN_PRESETS)
    )
    default = CAMPAIGN_PRESETS[DEFAULT_CAMPAIGN_TYPE]
    default_desc = CAMPAIGN_DESCRIPTIONS.get(DEFAULT_CAMPAIGN_TYPE, {})
    return f"""
    <p class="breadcrumb"><a href="/launch">&larr; start over</a></p>
    <div class="page-head"><div class="page-head-main">
    <h1>Launch a campaign &mdash; {html.escape(provider_name)}</h1>
    <p class="subtitle">{len(models)} chat-capable models found. Pick a model and a campaign
    type below -- cost/duration expectations are shown before you submit.</p>
    </div><div class="page-head-actions">
      <span class="meta">step 2 of 2 &middot; {len(models)} models loaded</span>
    </div></div>
    <div class="card">
      <div class="card-head">
        <span class="card-title">Configure campaign</span>
        <span class="card-meta">step 2 &middot; model + budget</span>
      </div>
      <div class="card-body">
      <form method="post" action="/launch">
        <input type="hidden" name="step" value="start">
        <input type="hidden" name="token" value="{html.escape(token)}">
        <p><label>Model: <select name="model">{model_opts}</select></label></p>
        <p><label>Campaign type: <select name="campaign_type">{type_opts}</select></label></p>
        <p><label>Budget (trials): <input name="budget" value="{int(default['budget'])}"></label></p>
        <p><label>Cost cap (USD): <input name="cost_cap" value="{default['cost_cap']}"></label></p>
        <p class="meta">Expect {html.escape(str(default_desc.get("typical_cost", "-")))},
           {html.escape(str(default_desc.get("typical_duration", "-")))} at the defaults above.</p>
        <p><label>Advanced: max tokens <input name="max_tokens" value=""></label><br>
           <span class="meta">blank = auto (model-aware default; "ultra"-class reasoning
           models get a bigger budget automatically)</span></p>
        <p><label><input type="checkbox" name="verify_failures" value="1" checked> Verify failures</label><br>
           <span class="meta">{html.escape(VERIFY_FAILURES_COST_NOTE)}</span></p>
        <p><button type="submit" class="btn-primary">Start mapping</button></p>
      </form>
      </div>
    </div>
    <div class="card">
      <div class="card-head">
        <span class="card-title">What each campaign type tests</span>
        <span class="card-meta">{len(CAMPAIGN_PRESETS)} presets</span>
      </div>
      <div class="card-body">{_preset_cards_html()}</div>
    </div>
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

    # Standard HTML checkbox semantics: a CHECKED box submits
    # verify_failures=1; an UNCHECKED box is omitted from the POST body
    # entirely (browsers never send unchecked checkboxes), so "absent"
    # means "unchecked" means "manual" -- not "auto". The form itself
    # defaults the box to `checked` (see models_step_html), so a real user
    # gets "auto" unless they deliberately uncheck it; a caller building
    # `form` programmatically must pass "verify_failures": "1" explicitly
    # to get "auto".
    verify = "auto" if form.get("verify_failures") else "manual"

    run_id = next_run_id(deps.db_path, campaign_type, model)
    provider = _PROVIDERS[provider_name]
    cancel_event = threading.Event()

    with _lock:
        if _CURRENT_RUN is not None:
            return False, busy_html(_CURRENT_RUN["run_id"])
        thread = threading.Thread(
            target=_campaign_thread,
            args=(deps, provider, key, model, runner, run_id, budget, cost_cap, max_tokens, cancel_event, verify),
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
    verify: str = "auto",
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
            if "verify" in params:
                kwargs["verify"] = verify
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


# ---------------------------------------------------------------------------
# "Verify failures now" -- manual-mode escape hatch for an existing run.
#
# A run launched with verify="manual" (or an old run from before this
# feature existed) has FAILs with no verify trials. This lets a human
# trigger `run_verify_failures` for that one run_id on demand, from the run
# detail page, WITHOUT re-running the whole campaign. It reuses the exact
# same one-campaign-at-a-time thread + cancel_event machinery as
# `handle_start`/`_campaign_thread` above (verification against a live
# model is itself a small campaign -- same concurrency hazards, same
# guard), and `scripts/verify_past_failures.py --live` calls this same
# `handle_verify_now` function rather than duplicating the thread-launch
# logic.
# ---------------------------------------------------------------------------


def handle_verify_now(
    run_id: str, provider_name: str, api_key: str, model: str, deps: LaunchDeps
) -> tuple[bool, str]:
    """Kick off `run_verify_failures` for an already-recorded run_id.
    Returns (started, message_or_run_id) mirroring `handle_start`'s
    contract; on success the second element is `run_id` (so callers can
    redirect the same way /launch does), on failure it's an HTML error/busy
    page.
    """
    global _CURRENT_RUN

    provider = _PROVIDERS.get(provider_name)
    if provider is None:
        return False, error_html(f"unknown provider: {provider_name}")

    key = _resolve_key(provider_name, api_key.strip(), deps)
    if not key:
        return False, error_html(
            f"no API key provided and none found in the env/ key file for {provider_name}"
        )

    if not model.strip():
        return False, error_html("missing model")

    cancel_event = threading.Event()
    with _lock:
        if _CURRENT_RUN is not None:
            return False, busy_html(_CURRENT_RUN["run_id"])
        thread = threading.Thread(
            target=_verify_now_thread,
            args=(deps, provider, key, model.strip(), run_id, cancel_event),
            name=f"verify-{run_id}",
            daemon=True,
        )
        _CURRENT_RUN = {"run_id": run_id, "thread": thread, "cancel_event": cancel_event}
    thread.start()
    return True, run_id


def _verify_now_thread(
    deps: LaunchDeps,
    provider: Provider,
    key: str,
    model: str,
    run_id: str,
    cancel_event: threading.Event,
) -> None:
    global _CURRENT_RUN
    try:
        store = ResultsStore(deps.db_path)
        try:
            campaign_client = deps.client_factory(provider, key)
            run_verify_failures(
                client=campaign_client,
                model=model,
                store=store,
                run_id=run_id,
                cancel_event=cancel_event,
            )
        finally:
            store.close()
    except Exception as exc:  # never let this crash kill silently
        import sys

        sys.stderr.write(f"[launch] verify-now for {run_id} failed: {exc!r}\n")
    finally:
        with _lock:
            _CURRENT_RUN = None


# ---------------------------------------------------------------------------
# "Replicate this finding" -- fresh-seed re-test of one derived finding from
# /findings, triggered from that page.
#
# Same shape as handle_verify_now above, and deliberately so: a replication
# is a small live campaign, so it takes the SAME one-at-a-time slot, the
# SAME cancel_event, and the SAME "spawn a daemon thread with its own store
# connection" pattern. The statistics are not reimplemented here either --
# `cartographer.confirm.replicate.run_finding_replication` calls the Phase 7
# `confirm_all` (Wilson intervals + Holm-Bonferroni) that decompose
# hypotheses already go through.
# ---------------------------------------------------------------------------

_CFG_PREFIX = "cfg_"


def parse_replication_config(form: dict[str, str]) -> dict[str, float]:
    """Pull the `cfg_<knob>` hidden fields out of a /replicate POST body.

    Unknown knob names are dropped rather than passed through: `_apply_config`
    raises on an unrecognized key, and a form field is untrusted input even
    on localhost.
    """
    out: dict[str, float] = {}
    for name, raw in form.items():
        if not name.startswith(_CFG_PREFIX):
            continue
        knob = name[len(_CFG_PREFIX):]
        if knob not in _KNOWN_KEYS:
            continue
        try:
            out[knob] = float(raw)
        except (TypeError, ValueError):
            continue
    return out


def handle_replicate_now(
    form: dict[str, str], deps: LaunchDeps
) -> tuple[bool, str]:
    """Kick off a fresh-seed replication of one derived finding.

    Returns `(started, run_id_or_html)`, mirroring `handle_start` /
    `handle_verify_now`: on success the caller redirects to the new run's
    page, on failure it renders the returned error/busy HTML.
    """
    global _CURRENT_RUN

    provider_name = form.get("provider", "")
    provider = _PROVIDERS.get(provider_name)
    if provider is None:
        return False, error_html(f"unknown provider: {provider_name}")

    key = _resolve_key(provider_name, form.get("api_key", "").strip(), deps)
    if not key:
        return False, error_html(
            f"no API key provided and none found in the env/ key file for {provider_name}"
        )

    store_model = form.get("model", "").strip()
    api_model = (form.get("api_model", "") or store_model).strip()
    query_kind = form.get("query_kind", "").strip() or "belief"
    if not api_model:
        return False, error_html("missing model")

    failing_config = parse_replication_config(form)
    if not failing_config:
        return False, error_html(
            "no failing configuration to replicate -- this finding has no "
            "recorded latent params to re-run"
        )

    try:
        cost_cap = float(form.get("cost_cap", "0.05") or "0.05")
    except ValueError:
        return False, error_html("cost cap must be numeric")
    if cost_cap <= 0:
        return False, error_html("cost cap must be greater than zero")

    store = ResultsStore(deps.db_path)
    try:
        base_run_id = next_replication_run_id(store, api_model, query_kind)
    finally:
        store.close()

    cancel_event = threading.Event()
    with _lock:
        if _CURRENT_RUN is not None:
            return False, busy_html(_CURRENT_RUN["run_id"])
        thread = threading.Thread(
            target=_replicate_thread,
            args=(
                deps, provider, key, api_model, query_kind, failing_config,
                base_run_id, cost_cap, cancel_event,
            ),
            name=f"replicate-{base_run_id}",
            daemon=True,
        )
        _CURRENT_RUN = {"run_id": base_run_id, "thread": thread, "cancel_event": cancel_event}
    thread.start()
    return True, base_run_id


def _replicate_thread(
    deps: LaunchDeps,
    provider: Provider,
    key: str,
    model: str,
    query_kind: str,
    failing_config: dict,
    base_run_id: str,
    cost_cap: float,
    cancel_event: threading.Event,
) -> None:
    global _CURRENT_RUN
    try:
        store = ResultsStore(deps.db_path)
        try:
            campaign_client = deps.client_factory(provider, key)
            run_finding_replication(
                client=campaign_client,
                model=model,
                store=store,
                query_kind=query_kind,
                failing_config=failing_config,
                base_run_id=base_run_id,
                cost_cap=cost_cap,
                cancel_event=cancel_event,
            )
        finally:
            store.close()
    except Exception as exc:  # never let this crash kill silently
        import sys

        sys.stderr.write(f"[launch] replication {base_run_id} failed: {exc!r}\n")
    finally:
        with _lock:
            _CURRENT_RUN = None
