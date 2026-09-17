"""Tests for cartographer.exec: keys, client, pricing, pilot runner.

All network I/O is mocked via monkeypatching `cartographer.exec.client._post`.
Zero real network calls are made.
"""

from __future__ import annotations

import json

import pytest

from cartographer.exec import client as client_mod
from cartographer.exec.client import GROQ, ChatResult, Client
from cartographer.exec.keys import load_keys
from cartographer.exec.pilot import build_pilot_tasks, run_pilot
from cartographer.exec.pricing import cost_usd
from cartographer.store.schema import Verdict
from cartographer.store.store import ResultsStore


# --- keys --------------------------------------------------------------


def test_load_keys_absolute_path(tmp_path, monkeypatch):
    key_file = tmp_path / "Nebius api.txt"
    key_file.write_text(
        "v1.FAKEnebiuskeycontent1234567890\n"
        "some other line\n"
        "groq key - gsk_FAKEabc123\n",
        encoding="utf-8",
    )
    # Point load_keys's repo-root-relative resolution at tmp_path by monkeypatching
    # Path(__file__).resolve().parents[2] indirectly: simplest is to call with an
    # absolute path and confirm it is used as-is via Path(repo_root / path) when
    # path is already absolute (Path / absolute_path == absolute_path).
    keys = load_keys(path=str(key_file))
    assert keys["nebius"] == "v1.FAKEnebiuskeycontent1234567890"
    assert keys["groq"] == "gsk_FAKEabc123"


def test_client_repr_never_contains_key():
    c = Client(GROQ, "gsk_SUPER_SECRET_VALUE")
    r = repr(c)
    assert "gsk_SUPER_SECRET_VALUE" not in r
    assert "SUPER_SECRET" not in r


# --- client success/error paths -----------------------------------------


def _canned_ok_body(text="the front office", tokens_in=42, tokens_out=7):
    return json.dumps(
        {
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
        }
    ).encode("utf-8")


def test_client_chat_success(monkeypatch):
    def fake_post(url, headers, body):
        return 200, _canned_ok_body(), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(GROQ, "gsk_fake")
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello")

    assert isinstance(result, ChatResult)
    assert result.text == "the front office"
    assert result.tokens_in == 42
    assert result.tokens_out == 7
    assert result.error is None
    assert result.latency_ms >= 0.0


def test_client_chat_http_401_sets_error_no_raise(monkeypatch):
    def fake_post(url, headers, body):
        return 401, b'{"error": "unauthorized"}', {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(GROQ, "gsk_fake")
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello")

    assert result.error is not None
    assert result.text == ""


def test_client_chat_retries_on_429_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, headers, body):
        calls.append(1)
        if len(calls) == 1:
            return 429, b"rate limited", {}
        return 200, _canned_ok_body(text="retried ok"), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(GROQ, "gsk_fake", sleep=lambda s: None)
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello")

    assert len(calls) == 2
    assert result.error is None
    assert result.text == "retried ok"


def test_client_chat_429_honors_retry_after_header(monkeypatch):
    calls = []
    delays = []

    def fake_post(url, headers, body):
        calls.append(1)
        if len(calls) == 1:
            return 429, b"rate limited", {"Retry-After": "2"}
        return 200, _canned_ok_body(text="retried ok"), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(GROQ, "gsk_fake", sleep=lambda s: delays.append(s))
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello")

    assert len(calls) == 2
    assert delays == [2.0]
    assert result.error is None
    assert result.text == "retried ok"


def test_client_chat_429_exhausts_retries_and_errors(monkeypatch):
    calls = []

    def fake_post(url, headers, body):
        calls.append(1)
        return 429, b"rate limited", {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(
        GROQ,
        "gsk_fake",
        retry_policy=client_mod.RetryPolicy(max_retries=4, base_delay=0.01),
        sleep=lambda s: None,
    )
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello")

    assert len(calls) == 5  # 1 initial + 4 retries
    assert result.error is not None
    assert result.text == ""


def test_client_chat_timeout_retries_once_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, headers, body):
        calls.append(1)
        if len(calls) == 1:
            return client_mod._TIMEOUT_STATUS, b"", {}
        return 200, _canned_ok_body(text="recovered"), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(GROQ, "gsk_fake", sleep=lambda s: None)
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello")

    assert len(calls) == 2
    assert result.error is None
    assert result.text == "recovered"


def test_client_chat_length_finish_reason_empty_text_auto_retries(monkeypatch):
    request_bodies = []

    def fake_post(url, headers, body):
        request_bodies.append(json.loads(body))
        if len(request_bodies) == 1:
            body_dict = {
                "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 60},
            }
        else:
            body_dict = {
                "choices": [{"message": {"content": "the front office"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8},
            }
        return 200, json.dumps(body_dict).encode("utf-8"), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(GROQ, "gsk_fake", sleep=lambda s: None)
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello", max_tokens=60, max_tokens_retry=4000)

    assert len(request_bodies) == 2
    assert request_bodies[0]["max_tokens"] == 60
    assert request_bodies[1]["max_tokens"] == 4000
    assert result.text == "the front office"
    assert result.finish_reason == "stop"
    assert result.error is None


def test_client_chat_malformed_json_sets_error(monkeypatch):
    def fake_post(url, headers, body):
        return 200, b"not json at all {{{", {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(GROQ, "gsk_fake")
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello")

    assert result.error is not None
    assert result.text == ""


# --- pricing -------------------------------------------------------------


def test_cost_usd_exact_arithmetic():
    # openai/gpt-oss-20b: (0.075, 0.30) usd per 1M tokens
    cost = cost_usd("openai/gpt-oss-20b", tokens_in=1_000_000, tokens_out=1_000_000)
    assert cost == pytest.approx(0.075 + 0.30)

    cost2 = cost_usd("openai/gpt-oss-20b", tokens_in=500_000, tokens_out=0)
    assert cost2 == pytest.approx(0.0375)


def test_cost_usd_unknown_model_is_zero():
    assert cost_usd("totally/unknown-model", tokens_in=1000, tokens_out=1000) == 0.0


# --- pilot dry run ---------------------------------------------------------


def test_build_pilot_tasks_default_count():
    tasks = build_pilot_tasks(n_per_cell=2)
    assert len(tasks) == 3 * 3 * 2


def test_run_pilot_dry_run(tmp_path, monkeypatch):
    responses = ["the front office", "the wrong room", ""]
    call_counter = {"n": 0}

    def fake_post(url, headers, body):
        idx = call_counter["n"] % len(responses)
        call_counter["n"] += 1
        text = responses[idx]
        return 200, _canned_ok_body(text=text), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    client = Client(GROQ, "gsk_fake")

    store_path = tmp_path / "pilot_test.sqlite"
    store = ResultsStore(store_path)

    summary = run_pilot(
        client,
        store,
        run_id="pilot-test-001",
        n_per_cell=1,
        progress=lambda msg: None,
    )

    records = store.by_run("pilot-test-001")
    assert len(records) == 27  # 9 tasks x 3 models

    verdict_counts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    for r in records:
        verdict_counts[r.verdict.name] = verdict_counts.get(r.verdict.name, 0) + 1
        assert r.realized_dims  # non-empty
        assert r.verifier_detail  # non-empty string

    # cycle pattern is [correct-ish "the front office", "the wrong room", ""]
    # third response is empty -> ERROR verdict always.
    assert verdict_counts["ERROR"] > 0
    assert sum(verdict_counts.values()) == 27

    assert summary["total_cost_usd"] > 0
    assert summary["n_records"] == 27
    store.close()


# --- observability: finish_reason + reasoning ------------------------------


def test_client_chat_parses_reasoning_and_finish_reason(monkeypatch):
    def fake_post(url, headers, body):
        body_dict = {
            "choices": [
                {
                    "message": {"content": "the front office", "reasoning": "I think it's the office."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return 200, json.dumps(body_dict).encode("utf-8"), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(GROQ, "gsk_fake")
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello")

    assert result.finish_reason == "stop"
    assert result.reasoning == "I think it's the office."


def test_client_chat_no_reasoning_field_is_none(monkeypatch):
    def fake_post(url, headers, body):
        return 200, _canned_ok_body(), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(GROQ, "gsk_fake")
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello")

    assert result.reasoning is None
    assert result.finish_reason is None


def test_reasoning_and_finish_reason_round_trip_through_pilot(tmp_path, monkeypatch):
    def fake_post(url, headers, body):
        body_dict = {
            "choices": [
                {
                    "message": {
                        "content": "the front office",
                        "reasoning": "Deduced from the last move mentioned.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return 200, json.dumps(body_dict).encode("utf-8"), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    client = Client(GROQ, "gsk_fake")

    store = ResultsStore(tmp_path / "reasoning_test.sqlite")
    run_pilot(client, store, run_id="reasoning-run", n_per_cell=1, progress=lambda msg: None)

    records = store.by_run("reasoning-run")
    assert len(records) > 0
    for r in records:
        assert r.reasoning_text == "Deduced from the last move mentioned."
        assert r.finish_reason == "stop"
    store.close()


def test_finish_reason_length_forces_error_with_cap_hit(tmp_path, monkeypatch):
    def fake_post(url, headers, body):
        body_dict = {
            "choices": [
                {"message": {"content": "the front"}, "finish_reason": "length"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return 200, json.dumps(body_dict).encode("utf-8"), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    client = Client(GROQ, "gsk_fake")

    store = ResultsStore(tmp_path / "cap_hit_test.sqlite")
    run_pilot(client, store, run_id="cap-hit-run", n_per_cell=1, progress=lambda msg: None)

    records = store.by_run("cap-hit-run")
    assert len(records) > 0
    for r in records:
        assert r.verdict == Verdict.ERROR
        assert "cap_hit=1" in r.verifier_detail
    store.close()


def test_empty_stop_answer_flows_through_pilot_as_empty_answer_marker(tmp_path, monkeypatch):
    def fake_post(url, headers, body):
        body_dict = {
            "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        }
        return 200, json.dumps(body_dict).encode("utf-8"), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    client = Client(GROQ, "gsk_fake", sleep=lambda s: None)

    store = ResultsStore(tmp_path / "empty_answer_test.sqlite")
    run_pilot(client, store, run_id="empty-answer-run", n_per_cell=1, progress=lambda msg: None)

    records = store.by_run("empty-answer-run")
    assert len(records) > 0
    for r in records:
        assert r.verdict == Verdict.ERROR
        assert "empty_answer=1" in r.verifier_detail
        assert "cap_hit=1" not in r.verifier_detail
    store.close()


def test_open_legacy_schema_db_migrates_and_appends(tmp_path):
    import sqlite3

    from cartographer.store.schema import ExperimentRecord, RenderMode

    db_path = tmp_path / "legacy.sqlite"

    legacy_schema = """
    CREATE TABLE experiments (
        record_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        wave_id INTEGER NOT NULL,
        parent_id TEXT,
        mutation TEXT,
        model_name TEXT NOT NULL,
        latent_params TEXT NOT NULL,
        realized_dims TEXT NOT NULL,
        render_mode TEXT NOT NULL,
        renderer_model TEXT,
        task_seed INTEGER NOT NULL,
        rendering_seed INTEGER NOT NULL,
        trial_seed INTEGER NOT NULL,
        prompt_text TEXT,
        response_text TEXT,
        verdict TEXT NOT NULL,
        verifier_detail TEXT,
        tokens_in INTEGER NOT NULL,
        tokens_out INTEGER NOT NULL,
        cost_usd REAL NOT NULL,
        latency_ms REAL NOT NULL,
        created_at TEXT NOT NULL
    );
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute(legacy_schema)
    conn.execute(
        "INSERT INTO experiments (record_id, run_id, wave_id, parent_id, mutation, "
        "model_name, latent_params, realized_dims, render_mode, renderer_model, "
        "task_seed, rendering_seed, trial_seed, prompt_text, response_text, verdict, "
        "verifier_detail, tokens_in, tokens_out, cost_usd, latency_ms, created_at) "
        "VALUES ('old-1', 'run-legacy', 0, NULL, NULL, 'legacy_model', '{}', '{}', "
        "'TEMPLATE', NULL, 1, 1, 1, 'prompt', 'response', 'PASS', 'detail', 1, 1, "
        "0.0, 1.0, '2020-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = ResultsStore(db_path)

    old_records = store.by_run("run-legacy")
    assert len(old_records) == 1
    assert old_records[0].reasoning_text is None
    assert old_records[0].finish_reason is None

    new_record = ExperimentRecord(
        run_id="run-legacy",
        model_name="new_model",
        latent_params={},
        realized_dims={},
        render_mode=RenderMode.TEMPLATE,
        task_seed=2,
        trial_seed=2,
        verdict=Verdict.PASS,
        reasoning_text="some reasoning",
        finish_reason="stop",
    )
    store.append(new_record)

    records = store.by_run("run-legacy")
    assert len(records) == 2
    by_id = {r.record_id: r for r in records}
    assert by_id["old-1"].reasoning_text is None
    assert by_id[new_record.record_id].reasoning_text == "some reasoning"
    assert by_id[new_record.record_id].finish_reason == "stop"
    store.close()
