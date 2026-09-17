"""Minimal OpenAI-compatible chat client over `urllib`, stdlib only.

No API-level failure ever raises: HTTP errors, timeouts, and malformed
responses are all captured in `ChatResult.error` so callers (notably the
pilot runner) can keep going through a batch of calls. `Client.__repr__`
never includes the API key.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    chat_path: str = "/chat/completions"


GROQ = Provider("groq", "https://api.groq.com/openai/v1", "/chat/completions")
NEBIUS = Provider("nebius", "https://api.studio.nebius.com/v1", "/chat/completions")


@dataclass(frozen=True)
class ChatResult:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    error: str | None = None


def _post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    """Issue the HTTP POST. Isolated as a module function so tests can
    monkeypatch it without touching the network."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class Client:
    def __init__(self, provider: Provider, api_key: str) -> None:
        self._provider = provider
        self._api_key = api_key

    def __repr__(self) -> str:
        return f"Client(provider={self._provider.name!r}, api_key=<redacted>)"

    def chat(
        self,
        model: str,
        prompt: str,
        max_tokens: int = 60,
        temperature: float = 0.0,
    ) -> ChatResult:
        url = self._provider.base_url + self._provider.chat_path
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "model-cartographer/0.1",
        }
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        ).encode("utf-8")

        start = time.monotonic()
        status, raw = _post(url, headers, body)
        if status in (429, 500, 502, 503, 504):
            time.sleep(2)
            status, raw = _post(url, headers, body)
        latency_ms = (time.monotonic() - start) * 1000.0

        if status < 200 or status >= 300:
            return ChatResult(
                text="",
                tokens_in=0,
                tokens_out=0,
                latency_ms=latency_ms,
                error=f"http {status}: {raw[:200]!r}",
            )

        try:
            payload = json.loads(raw)
            text = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage", {})
            tokens_in = int(usage.get("prompt_tokens", 0))
            tokens_out = int(usage.get("completion_tokens", 0))
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            return ChatResult(
                text="",
                tokens_in=0,
                tokens_out=0,
                latency_ms=latency_ms,
                error=f"parse error: {exc!r}",
            )

        return ChatResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            error=None,
        )
