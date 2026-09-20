"""Verify a model's JSON plan against a `ToolWorld` by simulation.

No model calls, no network. `verify_plan`:

  1. Robustly parses the response into a list of `Call`s: strips markdown
     fences and any reasoning preamble/postamble, extracts the first
     top-level JSON array, and coerces each element into a `Call`.
     Unparseable -> FAIL with detail=malformed_plan.
  2. Simulates the plan step by step against the latent world. The first call
     whose preconditions aren't met (or that is otherwise invalid) -> FAIL
     with detail=precondition_violation and the offending step/tool.
  3. After executing all calls, if the goal predicate holds -> PASS; else
     FAIL (detail=goal_not_reached).

An inefficient-but-correct plan PASSES (we measure capability, not style); the
plan length vs. optimal is returned as `overhead` for logging as a realized
dim. Returns a `(Verdict, detail_string, PlanVerification)` triple; the detail
string mirrors the narrative province's `verifier_detail` conventions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..store.schema import Verdict
from .model import Call, ToolWorld
from .simulate import simulate_plan


@dataclass(frozen=True)
class PlanVerification:
    verdict: Verdict
    detail: str
    n_calls: int
    optimal_depth: int
    overhead: int  # n_calls - optimal_depth (>=0 for a PASS; -1 sentinel if N/A)
    failed_step: int | None
    reached_goal: bool


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _extract_json_array(text: str) -> str | None:
    """Pull the first top-level JSON array out of `text`, tolerating markdown
    fences and surrounding prose/reasoning. Returns the array substring, or
    None if no balanced top-level array is found."""
    if not text:
        return None

    # Prefer the contents of a fenced block if one is present and holds an
    # array; otherwise scan the whole text.
    candidates: list[str] = []
    for m in _FENCE_RE.finditer(text):
        candidates.append(m.group(1))
    candidates.append(text)

    for candidate in candidates:
        start = candidate.find("[")
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(candidate)):
            ch = candidate[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return candidate[start : i + 1]
    return None


def parse_plan(text: str) -> list[Call] | None:
    """Parse a response string into a list of `Call`s, or None if malformed.

    Accepts args as a dict (preferred) or omitted (treated as no args). A
    single object instead of an array is coerced to a one-element plan. An
    empty array is a valid (empty) plan."""
    array_str = _extract_json_array(text)
    parsed = None
    if array_str is not None:
        try:
            parsed = json.loads(array_str)
        except (ValueError, TypeError):
            parsed = None
    if parsed is None:
        # Last resort: maybe the whole thing is a bare JSON object/array.
        try:
            parsed = json.loads(text.strip())
        except (ValueError, TypeError):
            return None

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return None

    calls: list[Call] = []
    for elem in parsed:
        if not isinstance(elem, dict):
            return None
        tool = elem.get("tool")
        if not isinstance(tool, str) or not tool:
            return None
        args = elem.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return None
        # Coerce arg values to strings (entity ids).
        coerced = {str(k): str(v) for k, v in args.items()}
        calls.append(Call(tool=tool, args=coerced))
    return calls


def verify_plan(response: str, world: ToolWorld) -> PlanVerification:
    optimal = world.optimal_depth if world.optimal_depth is not None else 0

    if response is None or not response.strip():
        return PlanVerification(
            verdict=Verdict.FAIL,
            detail="malformed_plan:empty_response",
            n_calls=0,
            optimal_depth=optimal,
            overhead=-1,
            failed_step=None,
            reached_goal=False,
        )

    plan = parse_plan(response)
    if plan is None:
        return PlanVerification(
            verdict=Verdict.FAIL,
            detail="malformed_plan",
            n_calls=0,
            optimal_depth=optimal,
            overhead=-1,
            failed_step=None,
            reached_goal=False,
        )

    if len(plan) > world.budget:
        return PlanVerification(
            verdict=Verdict.FAIL,
            detail=f"budget_exceeded:{len(plan)}>{world.budget}",
            n_calls=len(plan),
            optimal_depth=optimal,
            overhead=len(plan) - optimal,
            failed_step=None,
            reached_goal=False,
        )

    result = simulate_plan(world, plan)

    if result.failed_step is not None:
        # detail like precondition_violation:step=2:tool=advance_1
        kind = result.detail.split(":", 1)[0] if result.detail else "invalid_call"
        offending = result.detail.split(":", 1)[1] if ":" in result.detail else "?"
        return PlanVerification(
            verdict=Verdict.FAIL,
            detail=f"{kind}:step={result.failed_step}:{offending}",
            n_calls=len(plan),
            optimal_depth=optimal,
            overhead=len(plan) - optimal,
            failed_step=result.failed_step,
            reached_goal=result.reached_goal,
        )

    if result.reached_goal:
        return PlanVerification(
            verdict=Verdict.PASS,
            detail="goal_reached",
            n_calls=len(plan),
            optimal_depth=optimal,
            overhead=max(0, len(plan) - optimal),
            failed_step=None,
            reached_goal=True,
        )

    return PlanVerification(
        verdict=Verdict.FAIL,
        detail="goal_not_reached",
        n_calls=len(plan),
        optimal_depth=optimal,
        overhead=len(plan) - optimal,
        failed_step=None,
        reached_goal=False,
    )
