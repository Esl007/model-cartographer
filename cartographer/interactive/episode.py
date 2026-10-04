"""One interactive episode: the model acts through native function calling,
each call is executed in `InteractiveEnv`, and the result (or error) is fed
back as a `tool` message before the model's next turn.

`protocol="text_fallback"` additionally accepts calls written as text (see
`textcalls.py`) on turns with no native call, feeding results back as a user
message. Native stays the default so earlier results keep their meaning;
every episode records which protocol it actually used."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from .env import InteractiveEnv
from .textcalls import catalog_prompt, format_tool_results, parse_text_tool_calls

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
    protocol: str = "none"  # native | text | mixed | none (no call ever made)


def run_episode(
    client,
    model: str,
    env: InteractiveEnv,
    max_tokens: int = 16000,
    temperature: float = 0.0,
    cost_fn: Callable[[str, int, int], float] | None = None,
    may_continue: Callable[[float], bool] | None = None,
    retry_max_tokens: int | None = 32000,
    protocol: str = "native",
) -> EpisodeResult:
    """Run until the goal is reached, the model stops calling tools, the
    call budget is spent, or an infrastructure problem ends the episode.
    `may_continue(cost_so_far)` is checked before every turn so a campaign's
    cost cap binds mid-episode, not only between episodes. A turn truncated
    at `max_tokens` is re-issued once at `retry_max_tokens` (the single-shot
    evaluators' 16000/32000 policy) before the episode is called `cap_hit`."""
    if protocol not in ("native", "text_fallback"):
        raise ValueError(f"unknown protocol {protocol!r}")
    messages = env.opening_messages()
    tools = env.tools_schema()
    param_orders = {t["function"]["name"]: list(t["function"]["parameters"]["properties"]) for t in tools}
    if protocol == "text_fallback":
        # some providers drop `tools` for some models; put the catalog in the prompt too
        messages[0] = {**messages[0], "content": messages[0]["content"] + catalog_prompt(tools)}
    # In text_fallback the request must not carry `tools` at all: a server
    # with no tool-call parser for the model 400s the whole request (the
    # self-hosted vLLM Gemma 4 endpoint did exactly this on 2026-10-03,
    # turning every episode into api_error/protocol=none). Nebius merely
    # dropped the field, which is why the Gemma 3 runs never hit it.
    request_tools = None if protocol == "text_fallback" else tools
    used: set[str] = set()
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
            r = client.chat_turn(model, messages, request_tools, max_tokens=cap, temperature=temperature)
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
            text_calls = parse_text_tool_calls(r.text, param_orders) if protocol == "text_fallback" else []
            if not text_calls:
                outcome = "goal_reached" if env.goal_reached else "stopped_short"
                break
            used.add("text")
            messages.append({"role": "assistant", "content": r.text or ""})
            results = []
            for name, args in text_calls:
                if env.calls_left <= 0:
                    results.append((name, {"status": "error", "error": "call budget exhausted"}))
                    continue
                results.append((name, env.step(name, args if isinstance(args, dict) else {})))
                if env.goal_reached:
                    break
            messages.append({"role": "user", "content": format_tool_results(results)})
            if env.goal_reached:
                outcome = "goal_reached"
                break
            if env.calls_left <= 0:
                outcome = "budget_exhausted"
                break
            continue
        used.add("native")
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
        protocol="mixed" if len(used) > 1 else (next(iter(used)) if used else "none"),
    )
