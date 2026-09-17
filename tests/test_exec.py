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
        return 200, _canned_ok_body()

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
        return 401, b'{"error": "unauthorized"}'

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
            return 429, b"rate limited"
        return 200, _canned_ok_body(text="retried ok")

    monkeypatch.setattr(client_mod, "_post", fake_post)
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    c = Client(GROQ, "gsk_fake")
    result = c.chat(model="openai/gpt-oss-20b", prompt="hello")

    assert len(calls) == 2
    assert result.error is None
    assert result.text == "retried ok"


def test_client_chat_malformed_json_sets_error(monkeypatch):
    def fake_post(url, headers, body):
        return 200, b"not json at all {{{"

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
        return 200, _canned_ok_body(text=text)

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
