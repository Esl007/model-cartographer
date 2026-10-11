"""Province III protocol v3 (docs/spec-iii-protocol-v3-and-belief-power.md Task 1)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cartographer.exec.client import ToolChatResult
from cartographer.exec.evaluator import _apply_tool_config, _default_tool_base
from cartographer.interactive.campaign import InteractiveEvaluator
from cartographer.interactive.env import InteractiveEnv, InteractiveSpec
from cartographer.interactive.episode import V3_TEXT_STOP, protocol_version_of, run_episode
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.toolworld.generate import generate


def _env(seed=3):
    w = generate(_apply_tool_config(_default_tool_base(), {"plan_depth": 6.0}), seed)
    return InteractiveEnv(w, seed, InteractiveSpec(carry_distance=2, n_live_tickets=1))


class Blind:
    """Writes the whole plan as calls in ONE message (the v2 artefact), then stops."""

    def __init__(self, envs, capped=False):
        self.envs, self.turn, self.stops, self.capped = envs, 0, None, capped

    def chat_turn(self, model, messages, tools=None, max_tokens=0, temperature=0.0, stop=None):
        self.turn += 1
        self.stops = stop
        env = self.envs[-1]
        if self.turn > 1:
            return ToolChatResult(message={"content": "DONE"}, tool_calls=(), text="DONE", tokens_in=1,
                                  tokens_out=1, latency_ms=1.0, finish_reason="stop")
        blocks = []
        for c in env.plan:
            args = ", ".join(f"{k}={str(v)!r}" for k, v in c.args.items())
            blocks.append(f"```tool_code\n{c.tool}({args})\n```")
        text = "\n".join(blocks) + ("\nLet me now trace everything by hand ..." if self.capped else "")
        return ToolChatResult(message={"content": text}, tool_calls=(), text=text, tokens_in=1, tokens_out=1,
                              latency_ms=1.0, finish_reason="length" if self.capped else "stop")


def test_v2_default_executes_every_call_in_a_message():
    env = _env()
    res = run_episode(Blind([env]), "m", env, protocol="text_fallback")
    assert res.protocol_version == 2 and len(env.steps) == len(env.plan) > 1
    assert res.calls_written_first_turn == len(env.plan)


def test_v3_executes_one_call_per_turn_and_sends_stop_strings():
    env = _env()
    agent = Blind([env])
    res = run_episode(agent, "m", env, protocol="text_fallback", turn_discipline=True)
    assert res.protocol_version == 3 and len(env.steps) == 1
    assert res.skipped_calls == len(env.plan) - 1 and res.calls_written_first_turn == len(env.plan)
    assert agent.stops == list(V3_TEXT_STOP)


def test_v3_capped_turn_with_a_complete_call_executes_it():
    env = _env()
    res = run_episode(Blind([env], capped=True), "m", env, protocol="text_fallback", turn_discipline=True,
                      retry_max_tokens=None)
    assert res.outcome != "cap_hit" and res.truncated_turns == 1 and len(env.steps) == 1


def test_v3_leaves_native_protocol_alone():
    env = _env()
    res = run_episode(Blind([env]), "m", env, protocol="native", turn_discipline=True)
    assert res.protocol_version == 3 and env.steps == []  # text calls are not parsed on the native path


def test_evaluator_stamps_v3_and_the_process_measures(tmp_path):
    store = ResultsStore(str(tmp_path / "s.sqlite"))
    envs = []
    ev = InteractiveEvaluator(Blind(envs), "m", store, "camp-interactive-x-v3-001", 1.0, protocol="text_fallback",
                              protocol_version=3, on_task=lambda env, config: envs.append(env))
    ev({"plan_depth": 6.0, "value_links": 1.0}, 0)
    r = store.by_run("camp-interactive-x-v3-001")[0]
    assert protocol_version_of(r) == 3
    for key in ("turns_total=", "calls_written_first_turn=", "skipped_calls="):
        assert key in r.verifier_detail
    with pytest.raises(ValueError):
        InteractiveEvaluator(Blind([]), "m", store, "x", 1.0, protocol_version=4)


def test_frontier_never_pools_v2_and_v3():
    from cartographer.report import frontier

    specs = {s.key: s for s in frontier.SPECS}
    base = ExperimentRecord(run_id="camp-interactive-gemma4qat-v3-001", model_name="m", latent_params={},
                            realized_dims={}, render_mode=RenderMode.TEMPLATE, task_seed=0, trial_seed=0,
                            verdict=Verdict.PASS, verifier_detail="kind=interactive;campaign=1;protocol_version=3")
    assert frontier._matches(specs["interactive_v3"], base) and not frontier._matches(specs["interactive"], base)
    v2 = replace(base, run_id="camp-interactive-gemma4qat-001", verifier_detail="kind=interactive;campaign=1")
    assert frontier._matches(specs["interactive"], v2) and not frontier._matches(specs["interactive_v3"], v2)
    ef3 = replace(base, run_id="camp-ef-tickets-x-002/B")
    assert frontier._matches(specs["ef_tickets_v3"], ef3) and not frontier._matches(specs["ef_tickets"], ef3)


def test_ef_summary_refuses_mixed_protocol_versions(tmp_path):
    from cartographer.campaigns.evidence_fidelity import summarize_family

    store = ResultsStore(str(tmp_path / "m.sqlite"))
    for arm, v in (("A", 2), ("B", 3)):
        store.append(ExperimentRecord(run_id=f"camp-ef-tickets-x-001/{arm}", model_name="m", latent_params={},
                                      realized_dims={}, render_mode=RenderMode.TEMPLATE, task_seed=0, trial_seed=0,
                                      verdict=Verdict.FAIL,
                                      verifier_detail=f"kind=interactive;campaign=1;protocol_version={v}"))
    with pytest.raises(ValueError):
        summarize_family(store, "camp-ef-tickets-x-001", "tickets")
