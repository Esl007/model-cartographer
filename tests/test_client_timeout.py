"""Pins the per-provider read-timeout derivation and the retry decision
table (spec-province-v-ambiguities.md section C). Transport is mocked."""

from __future__ import annotations

import json

import pytest

from cartographer.exec import client as client_mod
from cartographer.exec.client import (
    GROQ, NEBIUS, OPENROUTER, Client, RetryPolicy, modal_provider, read_timeout_for,
)

MODAL = modal_provider("https://ws--gemma4-qat-serve.modal.run/v1/")
TIMEOUT = client_mod._TIMEOUT_STATUS
CONNERR = client_mod._CONNECT_ERROR_STATUS


def _ok():
    return json.dumps({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }).encode()


@pytest.mark.parametrize("provider", [GROQ, NEBIUS, OPENROUTER])
@pytest.mark.parametrize("max_tokens", [None, 60, 4000, 16000, 100000])
def test_hosted_providers_keep_60s(provider, max_tokens):
    assert read_timeout_for(provider, max_tokens) == 60.0


@pytest.mark.parametrize("max_tokens,expected", [
    (None, 360.0), (60, 360.0), (4000, 360.0), (7500, 360.0),
    (8000, 380.0), (16000, 700.0), (32000, 1340.0),
])
def test_modal_timeout_formula(max_tokens, expected):
    # max(360, 60 + max_tokens / 25)
    assert read_timeout_for(MODAL, max_tokens) == expected


@pytest.mark.parametrize("provider,max_tokens,expected", [
    (GROQ, 16000, 60.0), (NEBIUS, 16000, 60.0), (MODAL, 16000, 700.0), (MODAL, 60, 360.0),
])
def test_timeout_reaches_transport(monkeypatch, provider, max_tokens, expected):
    seen = []

    def fake_post(url, headers, body, timeout=60):
        seen.append(timeout)
        return 200, _ok(), {}

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(provider, "k", sleep=lambda s: None)
    c.chat(model="m", prompt="p", max_tokens=max_tokens)
    c.chat_turn(model="m", messages=[{"role": "user", "content": "p"}], max_tokens=max_tokens)
    assert seen == [expected, expected]


POLICY = RetryPolicy(max_retries=4, base_delay=0.0)


@pytest.mark.parametrize("status,attempt,retry", [
    (TIMEOUT, 0, False), (TIMEOUT, 1, False),          # read timeout: never
    (CONNERR, 0, True), (CONNERR, 1, False),           # before response: once
    (500, 0, True), (502, 0, True), (503, 0, True), (504, 0, True),
    (503, 1, False),
    (429, 0, True), (429, 3, True), (429, 4, False),   # rate-limit pacing unchanged
    (400, 0, False), (401, 0, False), (200, 0, False),
])
def test_retry_decision_table(status, attempt, retry):
    assert client_mod._retry_decision(status, attempt, POLICY) is retry


def test_retry_decision_respects_zero_retries():
    assert client_mod._retry_decision(CONNERR, 0, RetryPolicy(max_retries=0)) is False


def _run(monkeypatch, provider, sequence, max_tokens=16000):
    calls = []

    def fake_post(url, headers, body, timeout=60):
        calls.append(timeout)
        return sequence[min(len(calls) - 1, len(sequence) - 1)]

    monkeypatch.setattr(client_mod, "_post", fake_post)
    c = Client(provider, "k", sleep=lambda s: None)
    return c.chat(model="m", prompt="p", max_tokens=max_tokens), calls


def test_modal_read_timeout_single_attempt_and_note(monkeypatch):
    res, calls = _run(monkeypatch, MODAL, [(TIMEOUT, b"", {}), (200, _ok(), {})])
    assert calls == [700.0]
    assert res.error.startswith("timeout read_timeout:700s attempts:1 elapsed:")
    assert ";" not in res.error and "=" not in res.error


def test_connect_error_retried_once_then_succeeds(monkeypatch):
    res, calls = _run(monkeypatch, MODAL, [(CONNERR, b"refused", {}), (200, _ok(), {})])
    assert len(calls) == 2 and res.error is None and res.text == "ok"


def test_connect_error_twice_gives_up(monkeypatch):
    res, calls = _run(monkeypatch, GROQ, [(CONNERR, b"ConnectionRefusedError()", {})])
    assert len(calls) == 2
    assert res.error.startswith("connect_error read_timeout:60s attempts:2")


def test_5xx_retried_at_most_once(monkeypatch):
    res, calls = _run(monkeypatch, NEBIUS, [(503, b"busy", {})])
    assert len(calls) == 2
    assert res.error.startswith("http 503")


def test_timeout_after_connect_retry_stops(monkeypatch):
    res, calls = _run(monkeypatch, MODAL, [(CONNERR, b"x", {}), (TIMEOUT, b"", {}), (200, _ok(), {})])
    assert len(calls) == 2
    assert res.error.startswith("timeout read_timeout:700s attempts:2")


def test_post_classifies_connection_error(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(req, timeout):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    status, _, _ = client_mod._post("http://x", {}, b"{}", timeout=5)
    assert status == CONNERR


def test_post_classifies_timeout(monkeypatch):
    import socket
    import urllib.request

    def boom(req, timeout):
        raise socket.timeout("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert client_mod._post("http://x", {}, b"{}", timeout=5)[0] == TIMEOUT
