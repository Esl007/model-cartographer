"""Evidence Fidelity Campaign zoo (docs/spec-evidence-fidelity-campaign.md §6).

Four scripted "models", run through the campaign's real evaluators (via each
evaluator's `on_task` hook) so they see exactly the prompt / tool results a
model would, and decide ONLY from what is visible there:

  prior_follower     always gives the prior's answer
  evidence_follower  always uses the evidence (the perfect model)
  attention_limited  uses the evidence iff it is flagged (D) or restated (C)
  retrieval_limited  uses the evidence iff it is restated near the decision (C)

"Flagged" and "restated" are detected from the rendered text (the `Note:` /
back-ticked id / ` <<<` / `<<ticket>>` / aligned-diff markers; the `Recall:`
block / verbatim id list / `valid_tickets` / data-flow block), so a condition
that failed to render its feature fails the zoo. Predicted rescue patterns on
seeds where B is a real conflict (prior != evidence):

  arm                A     B     C     D     X     E
  prior_follower     pass  fail  fail  fail  fail  fail
  evidence_follower  pass  pass  pass  pass  pass  pass
  attention_limited  pass  fail  pass  pass  fail  fail
  retrieval_limited  pass  fail  pass  fail  fail  fail
"""

from __future__ import annotations

import json
import re
import threading

from ..exec.client import ChatResult, ToolChatResult

POLICIES = ("prior_follower", "evidence_follower", "attention_limited", "retrieval_limited")

EXPECTED = {
    "prior_follower": {"A": True, "B": False, "C": False, "D": False, "X": False, "E": False},
    "evidence_follower": {"A": True, "B": True, "C": True, "D": True, "X": True, "E": True},
    "attention_limited": {"A": True, "B": False, "C": True, "D": True, "X": False, "E": False},
    "retrieval_limited": {"A": True, "B": False, "C": True, "D": False, "X": False, "E": False},
}


def uses_evidence(policy: str, flagged: bool, restated: bool) -> bool:
    return {"prior_follower": False, "evidence_follower": True, "attention_limited": flagged or restated,
            "retrieval_limited": restated}[policy]


_local = threading.local()


def hook(family: str):
    """The `on_task` callback for `family`: remembers the task for the client
    call that follows on the same thread."""
    def on_task(**kw):
        _local.task = (family, kw)
    return on_task


# -- single-shot oracles: (correct text, prior text, flagged, restated) ---------------------------------

def _belief(prompt, world, name_map, rendered, config):
    from ..world.simulate import simulate

    final = simulate(world).final_locations[world.query.subject]
    return rendered.answer_key, name_map.location.get(final, final), 'Note: "' in prompt, "\nRecall:\n" in prompt


def _tools(prompt, world, base_world, config):
    from ..interactive.env import optimal_plan

    plan = optimal_plan(world, max_depth=max(world.budget, 1)) or []
    # the pre-collision ("pattern") id of each tool: `id_collision` re-ids the catalog in place, keeping
    # its order (names are not unique -- decoys share them -- so position, not name, is the key)
    if len(world.catalog) != len(base_world.catalog):
        raise AssertionError("catalog changed length; the zoo's id mapping assumes id_collision only")
    natural = {t.id: b.id for t, b in zip(world.catalog, base_world.catalog)}

    def plan_json(id_of):
        return json.dumps([{"tool": id_of(c.tool), "args": {k: str(v) for k, v in c.args.items()}} for c in plan])

    return (plan_json(lambda t: t), plan_json(lambda t: natural.get(t, t)),
            "- id: `" in prompt, "Tool ids, verbatim" in prompt)


def _comments(prompt, program, rendered, config):
    from ..codeworld.interpret import answer_from_trace, run

    truth = answer_from_trace(program, run(program, program.semantics))
    target = program.query.target
    asserted = [v for _sid, vid, v, _f, neutral in program.comments if not neutral and vid == target] or \
               [v for _sid, _vid, v, _f, neutral in program.comments if not neutral]
    prior = asserted[-1] if asserted else truth
    return f"ANSWER: {truth!r}", f"ANSWER: {prior!r}", " <<<" in prompt, False


_SINGLE = {"belief": _belief, "tools": _tools, "comments": _comments}


# -- multi-turn agents -------------------------------------------------------------------------------

def _call(i: int, name: str, args: dict) -> ToolChatResult:
    tc = {"id": f"z{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return ToolChatResult(message={"content": "", "tool_calls": [tc]}, tool_calls=((tc["id"], name, json.dumps(args)),),
                          text="", tokens_in=10, tokens_out=5, latency_ms=1.0, finish_reason="tool_calls")


def _done() -> ToolChatResult:
    return ToolChatResult(message={"content": "done"}, tool_calls=(), text="done", tokens_in=10, tokens_out=1,
                          latency_ms=1.0, finish_reason="stop")


def _tickets_turn(policy, kw, messages):
    env = kw["env"]
    done = sum(1 for s in env.steps if s.ok)
    if env.goal_reached or done >= len(env.plan) or env.calls_left <= 0:
        return _done()
    call = env.plan[done]
    args = {k: str(v) for k, v in call.args.items()}
    if call.tool in env.producer_for:
        producer = env.producer_for[call.tool]
        last = next((m["content"] for m in reversed(messages) if m.get("role") == "tool"), "")
        restated = '"valid_tickets"' in last
        flagged = "<<" in "".join(m.get("content") or "" for m in messages if m.get("role") == "tool")
        if uses_evidence(policy, flagged, restated):
            args["ticket"] = env.tickets.get(producer, "")
        else:  # the pattern scaffold a fabricating model writes
            pcall = next(c for c in env.plan if c.tool == producer)
            args["ticket"] = f"ticket_{env._entity_for(producer, {k: str(v) for k, v in pcall.args.items()})}"
    return _call(len(env.steps), call.tool, args)


def _repair_turn(policy, kw, messages):
    from ..repo.zoo import _hunk

    env, task = kw["env"], kw["task"]
    n = len(env.steps)
    seen = "\n".join(str(m.get("content") or "") for m in messages)
    if n == 0:
        return _call(0, "run_tests", {})
    if n == 1:
        restated, flagged = "Data flow for" in seen, "| expected:" in seen
        bug, entry = f"{task.bug_module}.py", f"{task.entry_module}.py"
        if uses_evidence(policy, flagged, restated) or bug == entry:  # the locality prior: edit the test's file
            h = _hunk(task.files[bug], task.reference_files[bug])
            return _call(1, "edit_file", {"path": bug, "old": h[0], "new": h[1]})
        # the symptom patch in the test's file: special-case the visible input at the top of solve
        src = env.files[entry]
        m = re.search(r"^def solve\((.*)\):\n", src, re.M)
        params = [x.strip() for x in m.group(1).split(",")]
        cond = " and ".join(f"{x} == {a!r}" for x, a in zip(params, task.visible_input))
        return _call(1, "edit_file", {"path": entry, "old": m.group(0),
                                      "new": f"{m.group(0)}    if {cond}:\n        return {task.visible_expected!r}\n"})
    if n == 2:
        return _call(2, "run_tests", {})
    return _done()


_TURN = {"tickets": _tickets_turn, "repair": _repair_turn}


class ZooClient:
    """A `Client` stand-in that plays one of `POLICIES`."""

    def __init__(self, policy: str) -> None:
        if policy not in POLICIES:
            raise ValueError(policy)
        self.policy = policy

    def chat(self, model, prompt, max_tokens=0, max_tokens_retry=0, **_):
        family, kw = _local.task
        if kw["prompt"] != prompt:
            raise AssertionError("zoo hook / prompt mismatch")
        correct, prior, flagged, restated = _SINGLE[family](**kw)
        text = correct if uses_evidence(self.policy, flagged, restated) else prior
        return ChatResult(text=text, tokens_in=10, tokens_out=5, latency_ms=1.0, finish_reason="stop")

    def chat_turn(self, model, messages, tools=None, max_tokens=0, temperature=0.0, stop=None):
        family, kw = _local.task
        return _TURN[family](self.policy, kw, messages)


__all__ = ["POLICIES", "EXPECTED", "ZooClient", "hook", "uses_evidence"]
