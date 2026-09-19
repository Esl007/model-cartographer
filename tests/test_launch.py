"""Tests for the /launch flow (cartographer/viewer/launch.py).

Everything is mocked at the LaunchDeps level (list_models_fn, client_factory,
runners, key_loader) -- zero network anywhere. The key-secrecy tests assert
on a sentinel key string never appearing in any returned HTML.
"""

from __future__ import annotations

import threading
import time

import pytest

from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.viewer import launch as launch_mod
from cartographer.viewer.launch import (
    LaunchDeps,
    handle_launch_get,
    handle_models_step,
    handle_start,
    next_run_id,
)

SENTINEL_KEY = "sk-SENTINEL-NEVER-IN-HTML-12345"


@pytest.fixture(autouse=True)
def _clean_launch_state():
    launch_mod._reset_for_tests()
    yield
    launch_mod._reset_for_tests()


def _deps(tmp_path, **overrides) -> LaunchDeps:
    kwargs = dict(
        db_path=str(tmp_path / "launch.sqlite"),
        list_models_fn=lambda provider, key: ["acme/model-a", "acme/model-b"],
        key_loader=lambda: {},
    )
    kwargs.update(overrides)
    return LaunchDeps(**kwargs)


def test_launch_form_renders(tmp_path):
    page = handle_launch_get(_deps(tmp_path))
    assert 'name="provider"' in page
    assert "groq" in page and "nebius" in page
    assert 'type="password"' in page
    assert "env/ key file" in page


def test_models_step_populates_dropdown_without_leaking_key(tmp_path):
    deps = _deps(tmp_path)
    page = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, deps)

    assert "acme/model-a" in page and "acme/model-b" in page
    assert 'name="model"' in page
    assert 'name="campaign_type"' in page
    # presets prefilled (full_map is the default-selected preset -- the
    # one-button "profile this model" campaign, budget/cap from exec.evaluator)
    assert 'value="600"' in page
    assert 'value="2.0"' in page
    # THE key never appears anywhere in the HTML -- only an opaque token.
    assert SENTINEL_KEY not in page
    assert 'name="token"' in page


def test_models_step_unknown_provider_errors(tmp_path):
    page = handle_models_step({"provider": "wat", "api_key": "k"}, _deps(tmp_path))
    assert "unknown provider" in page


def test_models_step_empty_key_falls_back_to_key_file(tmp_path):
    captured = {}

    def list_models_fn(provider, key):
        captured["key"] = key
        return ["m1"]

    deps = _deps(
        tmp_path,
        list_models_fn=list_models_fn,
        key_loader=lambda: {"groq": SENTINEL_KEY},
    )
    page = handle_models_step({"provider": "groq", "api_key": ""}, deps)
    assert captured["key"] == SENTINEL_KEY
    assert SENTINEL_KEY not in page


def test_models_step_no_key_anywhere_errors(tmp_path):
    deps = _deps(tmp_path, key_loader=lambda: {})
    page = handle_models_step({"provider": "groq", "api_key": ""}, deps)
    assert "no API key" in page


class _FakeClient:
    """Stands in for exec.client.Client inside the campaign thread."""

    def __init__(self, provider, api_key):
        self.provider = provider
        self.api_key = api_key


def _fake_runner_writes_records(client, store, run_id, model, budget, cost_cap):
    """Fake campaign runner: writes 3 records to the store like a real one."""
    for i in range(3):
        store.append(
            ExperimentRecord(
                run_id=run_id,
                model_name=model,
                latent_params={"chain_length": 2.0},
                realized_dims={"chain_length": 2.0},
                render_mode=RenderMode.TEMPLATE,
                task_seed=i,
                trial_seed=i,
                verdict=Verdict.PASS,
                verifier_detail="kind=state;campaign=1",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                latency_ms=1.0,
            )
        )


def _launch(tmp_path, deps=None, runner=_fake_runner_writes_records):
    if deps is None:
        deps = _deps(
            tmp_path,
            client_factory=_FakeClient,
            runners={"state": runner, "belief": runner},
        )
    page = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, deps)
    token = page.split('name="token" value="')[1].split('"')[0]
    started, payload = handle_start(
        {
            "token": token,
            "model": "acme/Model-A",
            "campaign_type": "state",
            "budget": "160",
            "cost_cap": "0.5",
        },
        deps,
    )
    return started, payload, deps


def test_start_spawns_thread_that_writes_records(tmp_path):
    started, run_id, deps = _launch(tmp_path)
    assert started
    assert run_id == "camp-state-model-a-001"

    # Wait for the daemon thread to finish (slot cleared).
    for _ in range(200):
        if launch_mod.current_run() is None:
            break
        time.sleep(0.01)
    assert launch_mod.current_run() is None

    store = ResultsStore(deps.db_path)
    assert len(store.by_run(run_id)) == 3
    store.close()


def test_run_id_increments_until_unused(tmp_path):
    db = str(tmp_path / "launch.sqlite")
    store = ResultsStore(db)
    _fake_runner_writes_records(None, store, "camp-state-model-a-001", "m", 0, 0)
    _fake_runner_writes_records(None, store, "camp-state-model-a-002", "m", 0, 0)
    store.close()
    assert next_run_id(db, "state", "acme/Model-A") == "camp-state-model-a-003"


def test_busy_state_page_while_campaign_running(tmp_path):
    release = []

    def slow_runner(client, store, run_id, model, budget, cost_cap):
        while not release:
            time.sleep(0.005)

    deps = _deps(
        tmp_path,
        client_factory=_FakeClient,
        runners={"state": slow_runner, "belief": slow_runner},
    )
    started, run_id, _ = _launch(tmp_path, deps=deps, runner=slow_runner)
    assert started
    try:
        busy_page = handle_launch_get(deps)
        assert "campaign in progress" in busy_page.lower()
        assert run_id in busy_page
        assert "/live" in busy_page

        # A second start attempt is refused.
        page2 = handle_models_step({"provider": "nebius", "api_key": "k2"}, deps)
        token2 = page2.split('name="token" value="')[1].split('"')[0]
        started2, payload2 = handle_start(
            {"token": token2, "model": "m", "campaign_type": "state"}, deps
        )
        assert not started2
        assert "campaign in progress" in payload2.lower()
    finally:
        release.append(True)
        for _ in range(200):
            if launch_mod.current_run() is None:
                break
            time.sleep(0.01)


def test_key_never_in_any_returned_html(tmp_path):
    deps = _deps(
        tmp_path,
        client_factory=_FakeClient,
        runners={"state": _fake_runner_writes_records},
    )
    pages = []
    pages.append(handle_launch_get(deps))
    step2 = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, deps)
    pages.append(step2)
    token = step2.split('name="token" value="')[1].split('"')[0]
    started, payload = handle_start(
        {"token": token, "model": "m", "campaign_type": "state"}, deps
    )
    if not started:
        pages.append(payload)
    # Error paths too: stale token reuse.
    started2, err = handle_start(
        {"token": token, "model": "m", "campaign_type": "state"}, deps
    )
    assert not started2
    pages.append(err)

    for page in pages:
        assert SENTINEL_KEY not in page

    for _ in range(200):
        if launch_mod.current_run() is None:
            break
        time.sleep(0.01)


def test_stale_token_rejected(tmp_path):
    deps = _deps(tmp_path)
    started, payload = handle_start(
        {"token": "nonsense", "model": "m", "campaign_type": "state"}, deps
    )
    assert not started
    assert "invalid or expired" in payload


# ---------------------------------------------------------------------------
# TASK 1 -- Advanced: max tokens field, blank = auto.
# ---------------------------------------------------------------------------


def test_models_step_form_has_max_tokens_field(tmp_path):
    page = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, _deps(tmp_path))
    assert 'name="max_tokens"' in page
    assert "auto" in page.lower()


def _fake_runner_captures_kwargs(client, store, run_id, model, budget, cost_cap, max_tokens=None, cancel_event=None):
    """Fake runner that records exactly what it was called with, so tests
    can assert the /launch form's max_tokens and the cancel_event flow
    through the campaign thread unmodified."""
    _fake_runner_captures_kwargs.calls.append(
        {"max_tokens": max_tokens, "cancel_event": cancel_event}
    )
    for i in range(1):
        store.append(
            ExperimentRecord(
                run_id=run_id,
                model_name=model,
                latent_params={"chain_length": 2.0},
                realized_dims={"chain_length": 2.0},
                render_mode=RenderMode.TEMPLATE,
                task_seed=i,
                trial_seed=i,
                verdict=Verdict.PASS,
                verifier_detail="kind=state;campaign=1",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                latency_ms=1.0,
            )
        )


_fake_runner_captures_kwargs.calls = []


def _wait_for_idle():
    for _ in range(200):
        if launch_mod.current_run() is None:
            break
        time.sleep(0.01)


def test_explicit_max_tokens_flows_into_runner(tmp_path):
    _fake_runner_captures_kwargs.calls.clear()
    deps = _deps(
        tmp_path,
        client_factory=_FakeClient,
        runners={"state": _fake_runner_captures_kwargs},
    )
    page = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, deps)
    token = page.split('name="token" value="')[1].split('"')[0]
    started, run_id = handle_start(
        {
            "token": token,
            "model": "acme/Model-A",
            "campaign_type": "state",
            "budget": "160",
            "cost_cap": "0.5",
            "max_tokens": "9000",
        },
        deps,
    )
    assert started
    _wait_for_idle()

    assert len(_fake_runner_captures_kwargs.calls) == 1
    assert _fake_runner_captures_kwargs.calls[0]["max_tokens"] == 9000
    assert isinstance(_fake_runner_captures_kwargs.calls[0]["cancel_event"], threading.Event)


def test_blank_max_tokens_means_auto(tmp_path):
    _fake_runner_captures_kwargs.calls.clear()
    deps = _deps(
        tmp_path,
        client_factory=_FakeClient,
        runners={"state": _fake_runner_captures_kwargs},
    )
    page = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, deps)
    token = page.split('name="token" value="')[1].split('"')[0]
    started, run_id = handle_start(
        {
            "token": token,
            "model": "acme/Model-A",
            "campaign_type": "state",
            "budget": "160",
            "cost_cap": "0.5",
            "max_tokens": "",
        },
        deps,
    )
    assert started
    _wait_for_idle()

    assert _fake_runner_captures_kwargs.calls[0]["max_tokens"] is None


def test_non_numeric_max_tokens_errors(tmp_path):
    deps = _deps(tmp_path, client_factory=_FakeClient, runners={"state": _fake_runner_writes_records})
    page = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, deps)
    token = page.split('name="token" value="')[1].split('"')[0]
    started, payload = handle_start(
        {
            "token": token,
            "model": "acme/Model-A",
            "campaign_type": "state",
            "budget": "160",
            "cost_cap": "0.5",
            "max_tokens": "banana",
        },
        deps,
    )
    assert not started
    assert "numeric" in payload


def test_old_style_runner_without_new_kwargs_still_works(tmp_path):
    """A runner that only declares the pre-existing 5-arg signature (like
    _fake_runner_writes_records, or a real campaign wrapper predating this
    change) must keep working -- the launch thread should not pass kwargs
    the runner doesn't declare."""
    started, run_id, deps = _launch(tmp_path)
    assert started
    _wait_for_idle()
    store = ResultsStore(deps.db_path)
    assert len(store.by_run(run_id)) == 3
    store.close()


# ---------------------------------------------------------------------------
# TASK 2 -- stop / cooperative cancellation registration.
# ---------------------------------------------------------------------------


def test_handle_start_registers_a_cancel_event(tmp_path):
    release = []

    def slow_runner(client, store, run_id, model, budget, cost_cap, max_tokens=None, cancel_event=None):
        while not release:
            time.sleep(0.005)

    deps = _deps(
        tmp_path,
        client_factory=_FakeClient,
        runners={"state": slow_runner},
    )
    started, run_id, _ = _launch(tmp_path, deps=deps, runner=slow_runner)
    assert started
    try:
        assert launch_mod.is_stopping() is False
        assert launch_mod.request_stop() is True
        assert launch_mod.is_stopping() is True
    finally:
        release.append(True)
        _wait_for_idle()


def test_request_stop_with_no_active_campaign_is_a_noop(tmp_path):
    assert launch_mod.current_run() is None
    assert launch_mod.request_stop() is False


# ---------------------------------------------------------------------------
# adversarial_belief preset registration
# ---------------------------------------------------------------------------


def test_adversarial_belief_preset_registered():
    assert "adversarial_belief" in launch_mod.CAMPAIGN_PRESETS
    preset = launch_mod.CAMPAIGN_PRESETS["adversarial_belief"]
    assert preset == {"budget": 300, "cost_cap": 1.0}
    assert "adversarial_belief" in launch_mod._RUNNERS
    from cartographer.exec.evaluator import run_adversarial_belief_campaign

    assert launch_mod._RUNNERS["adversarial_belief"] is run_adversarial_belief_campaign


def test_adversarial_belief_option_appears_in_models_step_form(tmp_path):
    deps = _deps(tmp_path)
    page = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, deps)
    assert 'value="adversarial_belief"' in page
    assert "budget 300 trials" in page
    assert "cap $1.0" in page
    # plain-English description + NEW badge for the recently added preset
    assert "foil-driven trap selection" in page.lower()
    assert "NEW" in page


def test_adversarial_belief_dispatches_through_start(tmp_path):
    calls = []

    def fake_adversarial_runner(client, store, run_id, model, budget, cost_cap, max_tokens=None, cancel_event=None):
        calls.append((run_id, model, budget, cost_cap))
        store.append(
            ExperimentRecord(
                run_id=run_id, model_name=model, latent_params={}, realized_dims={},
                render_mode=RenderMode.TEMPLATE, task_seed=0, trial_seed=0,
                verdict=Verdict.PASS, tokens_in=1, tokens_out=1, cost_usd=0.0, latency_ms=1.0,
            )
        )

    deps = _deps(
        tmp_path,
        client_factory=_FakeClient,
        runners={"adversarial_belief": fake_adversarial_runner},
    )
    page = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, deps)
    token = page.split('name="token" value="')[1].split('"')[0]
    started, run_id = handle_start(
        {
            "token": token,
            "model": "acme/Model-A",
            "campaign_type": "adversarial_belief",
            "budget": "300",
            "cost_cap": "1.0",
        },
        deps,
    )
    assert started
    for _ in range(200):
        if launch_mod.current_run() is None:
            break
        time.sleep(0.01)
    assert calls and calls[0][0] == run_id
    store = ResultsStore(deps.db_path)
    assert len(store.by_run(run_id)) == 1
    store.close()


# ---------------------------------------------------------------------------
# full_map preset: default-selected, RECOMMENDED badge
# ---------------------------------------------------------------------------


def test_full_map_preset_registered():
    assert "full_map" in launch_mod.CAMPAIGN_PRESETS
    preset = launch_mod.CAMPAIGN_PRESETS["full_map"]
    assert preset == {"budget": 600, "cost_cap": 2.0}
    assert "full_map" in launch_mod._RUNNERS
    from cartographer.exec.evaluator import run_full_map_campaign

    assert launch_mod._RUNNERS["full_map"] is run_full_map_campaign


def test_full_map_is_the_default_campaign_type():
    assert launch_mod.DEFAULT_CAMPAIGN_TYPE == "full_map"


def test_launch_form_shows_full_map_as_recommended(tmp_path):
    page = launch_mod.launch_form_html()
    assert "full_map" in page
    assert "RECOMMENDED" in page
    assert "one budget, one report" in page.lower() or "one report" in page.lower()


def test_models_step_defaults_to_full_map_selected_and_prefilled(tmp_path):
    deps = _deps(tmp_path)
    page = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, deps)
    # full_map option is marked selected in the campaign_type dropdown.
    assert '<option value="full_map" selected>' in page
    # its preset values are prefilled into the budget/cost_cap fields.
    assert 'value="600"' in page
    assert 'value="2.0"' in page
    assert "RECOMMENDED" in page


def test_full_map_dispatches_through_start(tmp_path):
    calls = []

    def fake_full_map_runner(client, store, run_id, model, budget, cost_cap, max_tokens=None, cancel_event=None):
        calls.append((run_id, model, budget, cost_cap))
        store.append(
            ExperimentRecord(
                run_id=f"{run_id}/state", model_name=model, latent_params={}, realized_dims={},
                render_mode=RenderMode.TEMPLATE, task_seed=0, trial_seed=0,
                verdict=Verdict.PASS, tokens_in=1, tokens_out=1, cost_usd=0.0, latency_ms=1.0,
            )
        )

    deps = _deps(
        tmp_path,
        client_factory=_FakeClient,
        runners={"full_map": fake_full_map_runner},
    )
    page = handle_models_step({"provider": "nebius", "api_key": SENTINEL_KEY}, deps)
    token = page.split('name="token" value="')[1].split('"')[0]
    started, run_id = handle_start(
        {
            "token": token,
            "model": "acme/Model-A",
            "campaign_type": "full_map",
            "budget": "600",
            "cost_cap": "2.0",
        },
        deps,
    )
    assert started is True
    thread = launch_mod._CURRENT_RUN["thread"]
    thread.join(timeout=5)
    assert calls and calls[0][0] == run_id
