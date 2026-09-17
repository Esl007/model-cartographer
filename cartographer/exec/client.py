"""Minimal OpenAI-compatible chat client over `urllib`, stdlib only.

No API-level failure ever raises: HTTP errors, timeouts, and malformed
responses are all captured in `ChatResult.error` so callers (notably the
pilot runner) can keep going through a batch of calls. `Client.__repr__`
never includes the API key.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace as dataclasses_replace
from typing import Callable


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    chat_path: str = "/chat/completions"
    models_path: str = "/models"
    # Default max concurrent in-flight calls a campaign should aim for
    # against this provider, used when the caller doesn't specify one.
    # Groq's free tier is rate-limited to ~30 RPM, so more than 2 workers
    # just means more threads sleeping in 429 backoff; Nebius's default
    # limits (~60 RPM, auto-scaling TPM) comfortably absorb 6.
    suggested_parallelism: int = 6


GROQ = Provider(
    "groq", "https://api.groq.com/openai/v1", "/chat/completions", suggested_parallelism=2
)
NEBIUS = Provider("nebius", "https://api.studio.nebius.com/v1", "/chat/completions")

# Model-id substrings that mark an entry in a provider's /models listing as
# not chat-capable (audio, embeddings, safety classifiers, etc.).
_NON_CHAT_ID_MARKERS = (
    "whisper",
    "embed",
    "tts",
    "guard",
    "moderation",
    "rerank",
    "transcrib",
    "audio",
    "flux",  # image generation
    "stable-diffusion",
    "sdxl",
)


@dataclass(frozen=True)
class ChatResult:
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: float
    error: str | None = None
    finish_reason: str | None = None
    reasoning: str | None = None
    # True when `Client.chat`'s auto-retry-on-length path actually fired
    # (first attempt came back empty + length-capped, so a second call was
    # made at `max_tokens_retry`). Lets callers distinguish a cap-hit that
    # already got the bigger retry budget and still failed (persistent)
    # from one that never got the retry at all (e.g. some text came back on
    # the first attempt, so no retry was attempted).
    retried: bool = False


@dataclass(frozen=True)
class RetryPolicy:
    """Retry behavior for transient failures (429 / 5xx / timeout).

    `max_retries` counts additional attempts after the first (so
    `max_retries=4` means up to 5 total attempts). `base_delay` seconds is
    used for exponential backoff (base_delay * 2**attempt_index) when the
    server doesn't tell us how long to wait via `Retry-After`.
    """

    max_retries: int = 4
    base_delay: float = 1.0


_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)

# Sentinel status used internally to signal a socket timeout through the
# same (status, body, headers) contract as _post, without conflating it
# with a real HTTP status code.
_TIMEOUT_STATUS = -1


def _post(
    url: str, headers: dict[str, str], body: bytes, timeout: float = 60
) -> tuple[int, bytes, dict[str, str]]:
    """Issue the HTTP POST. Isolated as a module function so tests can
    monkeypatch it without touching the network.

    Returns (status, body_bytes, response_headers). On a socket timeout,
    returns (_TIMEOUT_STATUS, b"", {}) rather than raising, so callers have
    a single uniform contract to handle.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_headers = dict(resp.headers.items())
            return resp.status, resp.read(), resp_headers
    except urllib.error.HTTPError as exc:
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
        return exc.code, exc.read(), resp_headers
    except (socket.timeout, TimeoutError, urllib.error.URLError) as exc:
        # urllib.error.URLError wraps socket.timeout in some Python versions;
        # only treat it as a timeout if it actually is one, otherwise
        # re-raise so genuine connection errors aren't silently swallowed
        # as retryable timeouts with no information.
        if isinstance(exc, urllib.error.URLError) and not isinstance(
            exc.reason, (socket.timeout, TimeoutError)
        ):
            raise
        return _TIMEOUT_STATUS, b"", {}


def _get(
    url: str, headers: dict[str, str], timeout: float = 30
) -> tuple[int, bytes, dict[str, str]]:
    """Issue an HTTP GET. Same contract and monkeypatch-isolation rationale
    as `_post`: returns (status, body_bytes, response_headers), with
    (_TIMEOUT_STATUS, b"", {}) on a socket timeout.
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {}
    except (socket.timeout, TimeoutError, urllib.error.URLError) as exc:
        if isinstance(exc, urllib.error.URLError) and not isinstance(
            exc.reason, (socket.timeout, TimeoutError)
        ):
            raise
        return _TIMEOUT_STATUS, b"", {}


def list_models(provider: Provider, api_key: str) -> list[str]:
    """Fetch the provider's /models listing and return chat-capable model
    ids, sorted. Raises RuntimeError (never leaking the key) on HTTP or
    parse failure -- callers like the /launch page surface the message.
    """
    url = provider.base_url + provider.models_path
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "model-cartographer/0.1",
    }
    status, raw, _ = _get(url, headers)
    if status == _TIMEOUT_STATUS:
        raise RuntimeError(f"{provider.name}: /models request timed out")
    if status < 200 or status >= 300:
        raise RuntimeError(f"{provider.name}: /models returned HTTP {status}")
    try:
        payload = json.loads(raw)
        ids = [str(entry["id"]) for entry in payload["data"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{provider.name}: could not parse /models response: {exc!r}")
    chat_ids = [
        mid
        for mid in ids
        if not any(marker in mid.lower() for marker in _NON_CHAT_ID_MARKERS)
    ]
    return sorted(set(chat_ids))


def _parse_retry_after(headers: dict[str, str]) -> float | None:
    """Parse a Retry-After header value (seconds, case-insensitive key)."""
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


class Client:
    def __init__(
        self,
        provider: Provider,
        api_key: str,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._provider = provider
        self._api_key = api_key
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"Client(provider={self._provider.name!r}, api_key=<redacted>)"

    @property
    def provider(self) -> Provider:
        return self._provider

    def _build_request(self, model: str, prompt: str, max_tokens: float, temperature: float) -> tuple[str, dict[str, str], bytes]:
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
        return url, headers, body

    def _post_with_retries(
        self, url: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes, dict[str, str], str | None]:
        """Perform the POST, retrying on 429/5xx/timeout per `_retry_policy`.

        Returns (status, raw_body, response_headers, error_kind) where
        error_kind is None on eventual success, or "timeout" / "http_retry_exhausted"
        if retries were exhausted.
        """
        policy = self._retry_policy
        attempt = 0
        last_status = None
        last_raw = b""
        last_headers: dict[str, str] = {}
        timed_out = False

        while True:
            status, raw, resp_headers = _post(url, headers, body)
            last_status, last_raw, last_headers = status, raw, resp_headers
            timed_out = status == _TIMEOUT_STATUS

            retryable = timed_out or status in _RETRYABLE_STATUSES
            if not retryable:
                return status, raw, resp_headers, None

            if attempt >= policy.max_retries:
                break

            retry_after = _parse_retry_after(resp_headers)
            if retry_after is not None:
                delay = retry_after
            else:
                delay = policy.base_delay * (2**attempt)
            self._sleep(delay)
            attempt += 1

        error_kind = "timeout" if timed_out else "http_retry_exhausted"
        return last_status, last_raw, last_headers, error_kind

    def chat(
        self,
        model: str,
        prompt: str,
        max_tokens: int = 60,
        temperature: float = 0.0,
        retry_on_length: bool = True,
        max_tokens_retry: int = 4000,
    ) -> ChatResult:
        result = self._chat_once(model, prompt, max_tokens, temperature)

        if (
            retry_on_length
            and result.error is None
            and result.finish_reason == "length"
            and not result.text
            and max_tokens_retry > max_tokens
        ):
            retried = self._chat_once(model, prompt, max_tokens_retry, temperature)
            # Preserve whichever call actually produced something; if the
            # retry is still length-capped, its finish_reason is returned
            # as-is so callers can classify the cap-hit distinctly.
            return dataclasses_replace(retried, retried=True)

        return result

    def _chat_once(
        self,
        model: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        url, headers, body = self._build_request(model, prompt, max_tokens, temperature)

        start = time.monotonic()
        status, raw, resp_headers, error_kind = self._post_with_retries(url, headers, body)
        latency_ms = (time.monotonic() - start) * 1000.0

        if error_kind == "timeout":
            return ChatResult(
                text="",
                tokens_in=0,
                tokens_out=0,
                latency_ms=latency_ms,
                error="timeout",
            )

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
            choice = payload["choices"][0]
            text = choice["message"]["content"]
            usage = payload.get("usage", {})
            tokens_in = int(usage.get("prompt_tokens", 0))
            tokens_out = int(usage.get("completion_tokens", 0))
            finish_reason = choice.get("finish_reason")
            reasoning = choice.get("message", {}).get("reasoning")
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
            finish_reason=finish_reason,
            reasoning=reasoning,
        )
