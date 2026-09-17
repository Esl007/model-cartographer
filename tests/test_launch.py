"""Tests for the /launch flow (cartographer/viewer/launch.py).

Everything is mocked at the LaunchDeps level (list_models_fn, client_factory,
runners, key_loader) -- zero network anywhere. The key-secrecy tests assert
on a sentinel key string never appearing in any returned HTML.
"""

from __future__ import annotations

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
    # presets prefilled (state budget/cap from exec.evaluator)
    assert 'value="160"' in page
    assert 'value="0.5"' in page
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
