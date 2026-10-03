"""One interactive episode: the model acts through native function calling,
each call is executed in `InteractiveEnv`, and the result (or error) is fed
back as a `tool` message before the model's next turn."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .env import InteractiveEnv

# Outcomes that say something about the MODEL (graded PASS/FAIL) vs. about the
# measurement (graded ERROR, excluded from rates like every other province).
MODEL_OUTCOMES = ("goal_reached", "stopped_short", "budget_exhausted", "turn_limit")
INFRA_OUTCOMES = ("api_error", "cap_hit", "cost_capped")


@dataclass
class EpisodeResult:
    outcome: str
    summary: dict
    turns: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float
    error: str | None = None
    finish_reason: str | None = None


def run_episode(
    client,
    model: str,
    env: InteractiveEnv,
    max_tokens: int = 16000,
    temperature: float = 0.0,
    cost_fn: Callable[[str, int, int], float] | None = None,
    may_continue: Callable[[float], bool] | None = None,
    retry_max_tokens: int | None = 32000,
) -> EpisodeResult:
    """Run until the goal is reached, the model stops calling tools, the
    call budget is spent, or an infrastructure problem ends the episode.
    `may_continue(cost_so_far)` is checked before every turn so a campaign's
    cost cap binds mid-episode, not only between episodes. A turn truncated
    at `max_tokens` is re-issued once at `retry_max_tokens` (the single-shot
    evaluators' 16000/32000 policy) before the episode is called `cap_hit`."""
    messages = env.opening_messages()
    tools = env.tools_schema()
    t_in = t_out = 0
    cost = latency = 0.0
    outcome, error, finish = "turn_limit", None, None
    turns = 0
    for _ in range(env.call_budget + 4):
        if may_continue is not None and not may_continue(cost):
            outcome = "cost_capped"
            break
        caps = [max_tokens]
        if retry_max_tokens and retry_max_tokens > max_tokens:
            caps.append(retry_max_tokens)
        for cap in caps:
            r = client.chat_turn(model, messages, tools, max_tokens=cap, temperature=temperature)
            t_in += r.tokens_in
            t_out += r.tokens_out
            latency += r.latency_ms
            if cost_fn is not None:
                cost += cost_fn(model, r.tokens_in, r.tokens_out)
            if r.error is not None or r.finish_reason != "length":
                break
            if may_continue is not None and not may_continue(cost):
                break
        turns += 1
        finish = r.finish_reason
        if r.error is not None:
            outcome, error = "api_error", r.error
            break
        if r.finish_reason == "length":
            outcome = "cap_hit"
            break
        if not r.tool_calls:
            outcome = "goal_reached" if env.goal_reached else "stopped_short"
            break
        messages.append(
            {"role": "assistant", "content": r.text or "", "tool_calls": r.message.get("tool_calls")}
        )
        for call_id, name, arg_json in r.tool_calls:
            if env.calls_left <= 0:
                result = {"status": "error", "error": "call budget exhausted"}
            else:
                try:
                    args = json.loads(arg_json) if arg_json else {}
                except ValueError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                result = env.step(name, args)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})
        if env.goal_reached:
            outcome = "goal_reached"
            break
        if env.calls_left <= 0:
            outcome = "budget_exhausted"
            break
    return EpisodeResult(
        outcome=outcome,
        summary=env.summary(),
        turns=turns,
        tokens_in=t_in,
        tokens_out=t_out,
        cost_usd=cost,
        latency_ms=latency,
        error=error,
        finish_reason=finish,
    )
