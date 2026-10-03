"""Province III (interactive execution): environment, episode loop and
escalation, all offline via scripted agents."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from cartographer.exec.client import ToolChatResult
from cartographer.exec.evaluator import _default_tool_base
from cartographer.interactive import (
    InteractiveEnv,
    InteractiveSpec,
    choose_links,
    optimal_plan,
    run_episode,
)
from cartographer.interactive import campaign as campaign_mod
from cartographer.interactive.campaign import InteractiveEvaluator, run_interactive_breaking_point_campaign
from cartographer.schedule.escalate import EscalationRay, RayDim
from cartographer.store.schema import Verdict
from cartographer.store.store import ResultsStore
from cartographer.toolworld.generate import generate

BASE = _default_tool_base()


def _world(pd=6, seed=3):
    return generate(replace(BASE, plan_depth=pd), seed)


# --- environment ------------------------------------------------------------


@pytest.mark.parametrize("pd", [2, 4, 8])
def test_optimal_plan_matches_bfs_depth(pd):
    for seed in range(3):
        w = _world(pd, seed)
        assert len(optimal_plan(w, w.budget)) == w.optimal_depth


def test_link_fraction_is_monotone_superset():
    w = _world(10, 1)
    plan = optimal_plan(w, w.budget)
    prev: set = set()
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        links = set(choose_links(w, plan, 1, frac))
        assert prev <= links
        prev = links
    assert choose_links(w, plan, 1, 0.0) == ()
    assert len(prev) > 0


def test_tickets_are_deterministic_and_seed_dependent():
    def first_ticket(seed):
        env = InteractiveEnv(_world(6, 3), seed, InteractiveSpec(value_links=1.0))
        for c in env.plan:
            args = dict(c.args)
            if c.tool in env.producer_for:
                args["ticket"] = env.tickets[env.producer_for[c.tool]]
            r = env.step(c.tool, args)
            if "ticket" in r:
                return r["ticket"]
    assert first_ticket(11) == first_ticket(11)
    assert first_ticket(11) != first_ticket(12)


def test_missing_or_wrong_ticket_is_rejected_without_state_change():
    env = InteractiveEnv(_world(6, 3), 5, InteractiveSpec(value_links=1.0))
    consumer_idx = next(i for i, c in enumerate(env.plan) if c.tool in env.producer_for)
    for c in env.plan[:consumer_idx]:
        args = dict(c.args)
        if c.tool in env.producer_for:
            args["ticket"] = env.tickets[env.producer_for[c.tool]]
        env.step(c.tool, args)
    before = env.state
    c = env.plan[consumer_idx]
    r = env.step(c.tool, {**c.args, "ticket": "tkt-000000"})
    assert r["status"] == "error" and r["error"].startswith("invalid_ticket")
    assert env.state == before


def test_fault_fires_once_and_changes_nothing():
    env = InteractiveEnv(_world(4, 2), 9, InteractiveSpec(fault_step=1))
    before = env.state
    c = env.plan[0]
    assert env.step(c.tool, dict(c.args))["error"].startswith("transient")
    assert env.state == before
    assert env.step(c.tool, dict(c.args))["status"] == "ok"
    assert env.call_budget == env.world.budget + 1


def test_distance_falls_by_one_along_the_optimal_plan():
    env = InteractiveEnv(_world(6, 4), 4, InteractiveSpec())
    expected = env.initial_distance
    for c in env.plan:
        env.step(c.tool, dict(c.args))
        expected -= 1
        assert env.steps[-1].distance_after == expected
    assert env.summary()["first_deviation_step"] is None


def test_value_links_do_not_change_ground_truth_depth():
    w = _world(8, 6)
    assert InteractiveEnv(w, 6, InteractiveSpec(value_links=1.0)).initial_distance == w.optimal_depth


# --- scripted agents ----------------------------------------------------------


class ScriptedAgent:
    """Fake client. `mode`: "perfect" (follows the optimal plan, reads tickets
    from tool results), "blind" (follows the plan, never passes tickets),
    "quit" (stops immediately). Reads the env under test from `envs[-1]`."""

    def __init__(self, envs, mode="perfect"):
        self.envs, self.mode, self.turns = envs, mode, 0

    def chat_turn(self, model, messages, tools=None, max_tokens=0, temperature=0.0):
        self.turns += 1
        env = self.envs[-1]
        done = sum(1 for s in env.steps if s.ok)
        if self.mode == "quit" or env.goal_reached or done >= len(env.plan):
            return ToolChatResult(message={"content": "DONE"}, tool_calls=(), text="DONE",
                                  tokens_in=100, tokens_out=10, latency_ms=1.0)
        call = env.plan[done]
        args = dict(call.args)
        if self.mode == "perfect" and call.tool in env.producer_for:
            for m in reversed(messages):
                if m.get("role") == "tool" and "ticket" in m["content"]:
                    args["ticket"] = json.loads(m["content"])["ticket"]
                    break
        tc = {"id": f"c{self.turns}", "type": "function",
              "function": {"name": call.tool, "arguments": json.dumps(args)}}
        return ToolChatResult(message={"content": "", "tool_calls": [tc]},
                              tool_calls=((tc["id"], call.tool, json.dumps(args)),), text="",
                              tokens_in=100, tokens_out=10, latency_ms=1.0, finish_reason="tool_calls")


def test_perfect_agent_reaches_goal_in_optimal_calls():
    env = InteractiveEnv(_world(4, 1), 1, InteractiveSpec(value_links=1.0))
    res = run_episode(ScriptedAgent([env]), "m", env)
    assert res.outcome == "goal_reached"
    assert res.summary["n_calls"] == env.world.optimal_depth
    assert res.summary["invalid_tickets"] == 0


def test_blind_agent_fails_on_value_links_only():
    env = InteractiveEnv(_world(4, 1), 1, InteractiveSpec(value_links=1.0))
    res = run_episode(ScriptedAgent([env], "blind"), "m", env)
    assert res.outcome != "goal_reached"
    assert res.summary["first_error_kind"] == "invalid_ticket"
    env0 = InteractiveEnv(_world(4, 1), 1, InteractiveSpec(value_links=0.0))
    assert run_episode(ScriptedAgent([env0], "blind"), "m", env0).outcome == "goal_reached"


def test_agent_that_stops_early_is_stopped_short():
    env = InteractiveEnv(_world(4, 1), 1, InteractiveSpec())
    assert run_episode(ScriptedAgent([env], "quit"), "m", env).outcome == "stopped_short"


def test_cost_cap_binds_mid_episode():
    env = InteractiveEnv(_world(6, 1), 1, InteractiveSpec())
    res = run_episode(ScriptedAgent([env]), "m", env, cost_fn=lambda m, i, o: 1.0,
                      may_continue=lambda c: c < 2.0)
    assert res.outcome == "cost_capped" and res.turns == 2


# --- evaluator + escalation ----------------------------------------------------


@pytest.fixture
def captured_envs(monkeypatch):
    envs = []

    class Recording(InteractiveEnv):
        def __post_init__(self):
            super().__post_init__()
            envs.append(self)

    monkeypatch.setattr(campaign_mod, "InteractiveEnv", Recording)
    return envs


def test_evaluator_persists_interactive_record(tmp_path, captured_envs):
    store = ResultsStore(tmp_path / "s.sqlite")
    ev = InteractiveEvaluator(ScriptedAgent(captured_envs), "m", store, "run-i", cost_cap_usd=10.0)
    assert ev({"plan_depth": 4.0, "value_links": 1.0}, trial_seed=1) is True
    (rec,) = store.by_run("run-i")
    assert rec.verdict == Verdict.PASS
    assert "kind=interactive" in rec.verifier_detail and "outcome=goal_reached" in rec.verifier_detail
    assert rec.latent_params["value_links"] == 1.0
    assert json.loads(rec.response_text)[0]["ok"] is True
    store.close()


def test_escalation_finds_the_value_passing_frontier_for_a_blind_agent(tmp_path, captured_envs):
    """A planner that never reads tool results passes until value links
    switch on, then fails: escalation must report a found frontier."""
    store = ResultsStore(tmp_path / "s.sqlite")
    ray = EscalationRay(dims=(RayDim("plan_depth", 3, 5, True), RayDim("value_links", 0.0, 1.0, False)))
    bp = run_interactive_breaking_point_campaign(
        ScriptedAgent(captured_envs, "blind"), store, "run-b", "m", ray=ray,
        budget=60, cost_cap=10.0, k_trials=3, parallelism=1,
    )
    assert bp.found is True
    assert bp.degradation_onset is not None
    store.close()
