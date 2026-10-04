"""Copy-fidelity / ticket fabrication (docs/spec-ticket-fabrication.md):
the new interactive dims, invalid-ticket classification, the Part B
ablations, the resample gate, the rays (validated on planted agents), and
the code-tracing replication. All offline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from cartographer.ablate.interactive import TICKET_ABLATIONS, attribute_ticket_failure, fisher_one_sided
from cartographer.exec.client import ToolChatResult
from cartographer.exec.evaluator import _default_tool_base, _task_seed
from cartographer.interactive import InteractiveEnv, InteractiveSpec, choose_links, run_episode
from cartographer.interactive import campaign as campaign_mod
from cartographer.interactive.campaign import (
    TICKET_RAYS,
    InteractiveEvaluator,
    ResampleGatedEvaluator,
    run_ticket_fidelity_campaign,
)
from cartographer.interactive.env import controlled_links
from cartographer.store.schema import Verdict
from cartographer.store.store import ResultsStore
from cartographer.toolworld.generate import generate

BASE = _default_tool_base()


def _world(pd=6, seed=3):
    return generate(replace(BASE, plan_depth=pd), seed)


def _env(pd=12, seed=5, **spec):
    return InteractiveEnv(_world(pd, seed), seed, InteractiveSpec(**spec))


def _drive(env, fabricate_from_level=None, use_decoy=False, stop_on_error=True):
    """Oracle executor: follows the optimal plan, passes the right ticket
    (read from the env -- an ORACLE, not a model). Optionally fabricates a
    pattern-shaped ticket when tickets are pattern-shaped."""
    for c in env.plan:
        args = dict(c.args)
        name = env._out(c.tool)
        args = {k: env._out(v) for k, v in args.items()}
        if c.tool in env.producer_for:
            prod = env.producer_for[c.tool]
            real = env.tickets.get(prod)
            if fabricate_from_level is not None and env.spec.ticket_level >= fabricate_from_level:
                args["ticket"] = f"ticket_{prod}_12345"
            elif use_decoy and env.decoys_shown:
                args["ticket"] = sorted(env.decoys_shown)[0]
            else:
                args["ticket"] = real
        r = env.step(name, args)
        if r["status"] != "ok" and stop_on_error:
            return False
    return env.goal_reached


# --- back-compat ---------------------------------------------------------------


def test_default_spec_is_byte_identical_to_the_legacy_tickets_and_links():
    w = _world(6, 3)
    env = InteractiveEnv(w, 11, InteractiveSpec(value_links=1.0))
    assert env.links == choose_links(w, env.plan, 11, 1.0)
    for c in env.plan:
        args = dict(c.args)
        if c.tool in env.producer_for:
            args["ticket"] = env.tickets[env.producer_for[c.tool]]
        r = env.step(c.tool, args)
        if "ticket" in r:
            legacy = "tkt-" + hashlib.sha256(f"11|{c.tool}|1".encode()).hexdigest()[:6]
            assert r["ticket"] == legacy
            assert set(r) == {"status", "ticket"}  # no decoys, no restatement
            return
    pytest.fail("no ticket issued")


# --- ticket form, length, carry, live tickets -------------------------------------


@pytest.mark.parametrize("entropy,prefix", [(0.0, "tkt-"), (0.3, "tkt-"), (0.6, "ticket_"), (1.0, "ticket_")])
def test_ticket_form_moves_from_opaque_to_pattern_shaped(entropy, prefix):
    env = _env(ticket_entropy=entropy, n_live_tickets=1, carry_distance=2)
    _drive(env)
    t = next(iter(env.issued))
    assert t.startswith(prefix)
    if entropy >= 0.75:
        assert t.rsplit("_", 1)[1].isdigit()  # looks derivable; the digits are random
    if entropy < 0.25:
        assert len(t) == len("tkt-") + 6


def test_ticket_length_controls_the_hex_body():
    for n in (8, 32, 64):
        env = _env(ticket_length=n, n_live_tickets=1, carry_distance=2)
        _drive(env)
        assert len(next(iter(env.issued))) == len("tkt-") + n


@pytest.mark.parametrize("k,d", [(1, 1), (1, 6), (1, 10), (3, 4), (6, 6), (4, 2)])
def test_controlled_links_hit_the_requested_carry_and_live_count(k, d):
    hits = 0
    for seed in range(8):
        env = _env(pd=12, seed=seed, n_live_tickets=k, carry_distance=d)
        s = env.summary()
        if s["n_links"] == min(k, d):
            hits += 1
            assert s["carry_distance"] == d
            assert s["max_live_tickets"] == min(k, d)
        assert _drive(env), "the optimal plan with correct tickets must still reach the goal"
        assert env.summary()["n_calls"] == env.initial_distance
    assert hits >= 6


def test_consumer_never_appears_in_the_plan_before_its_producer():
    for seed in range(20):
        plan = _env(pd=12, seed=seed).plan
        links, pos = controlled_links(plan, seed, 6, 6)
        first = {}
        for i, c in enumerate(plan):
            first.setdefault(c.tool, i)
        for link, (p, c) in zip(links, pos):
            assert first[link.consumer] > p and plan[p].tool == link.producer and plan[c].tool == link.consumer


# --- invalid-ticket classification -------------------------------------------------


def _first_consumer_env(**spec):
    env = _env(n_live_tickets=2, carry_distance=4, **spec)
    for c in env.plan:
        if c.tool in env.producer_for:
            return env, c
        env.step(c.tool, dict(c.args))
    pytest.fail("no consumer")


@pytest.mark.parametrize("make,kind", [
    (lambda env, prod: None, "missing"),
    (lambda env, prod: "ticket_depth_flag_1_12345", "fabricated"),
    (lambda env, prod: env.tickets[prod][:-1] + ("0" if env.tickets[prod][-1] != "0" else "1"), "corrupted"),
    (lambda env, prod: next(t for t, (p, _) in env.issued.items() if p != prod), "wrong_producer"),
    (lambda env, prod: sorted(env.decoys_shown)[0], "decoy"),
])
def test_invalid_tickets_are_classified_by_what_was_submitted(make, kind):
    env, c = _first_consumer_env(decoy_tickets=2)
    prod = env.producer_for[c.tool]
    if kind == "wrong_producer" and not any(p != prod for p, _ in env.issued.values()):
        pytest.skip("only one producer issued so far on this plan")
    args = dict(c.args)
    t = make(env, prod)
    if t is not None:
        args["ticket"] = t
    r = env.step(c.tool, args)
    assert r["status"] == "error"
    assert env.steps[-1].ticket_error == kind
    assert env.summary()["ticket_errors"] == {kind: 1}
    assert json.loads(env.transcript())[-1]["ticket_error"] == kind


def test_stale_ticket_is_an_older_ticket_of_the_same_producer():
    env = _env(n_live_tickets=1, carry_distance=3)
    prod = env.links[0].producer
    env.issued["tkt-oldold"] = (prod, 1)  # as if an earlier call of the producer had issued it
    assert env.classify_ticket_error("tkt-oldold", prod) == "stale"


def test_decoys_are_labelled_plausible_and_never_the_real_ticket():
    env = _env(decoy_tickets=3, n_live_tickets=2, carry_distance=3, ticket_entropy=1.0)
    _drive(env)
    ok_results = [s.result for s in env.steps if s.ok]
    assert all({"previous_ticket", "example_ticket", "cached_ticket"} <= set(r) for r in ok_results)
    assert not (env.decoys_shown & set(env.issued))
    assert all(d.startswith("ticket_") for d in env.decoys_shown)  # same form as the real ones


# --- Part B ablations ----------------------------------------------------------------


def test_ablations_preserve_the_answer_key_and_the_task():
    for flags in ({"restate_tickets": True}, {"copy_instruction": True}, {"opaque_names": True}):
        base, abl = _env(n_live_tickets=2, carry_distance=4), _env(n_live_tickets=2, carry_distance=4, **flags)
        assert [c.tool for c in base.plan] == [c.tool for c in abl.plan]
        assert base.links == abl.links and base.initial_distance == abl.initial_distance
        assert _drive(abl)


def test_restatement_carries_the_valid_tickets_in_every_result():
    env = _env(n_live_tickets=2, carry_distance=4, restate_tickets=True)
    _drive(env)
    later = [s.result for s in env.steps if env.tickets and "valid_tickets" in s.result]
    assert later and all(isinstance(r["valid_tickets"], dict) for r in later)
    assert set(env.tickets.values()) <= set(later[-1]["valid_tickets"].values())


def test_copy_instruction_is_one_system_line():
    plain, inst = _env(), _env(copy_instruction=True)
    assert "never construct or guess" in inst.opening_messages()[0]["content"]
    assert "never construct or guess" not in plain.opening_messages()[0]["content"]
    assert plain.opening_messages()[1] == inst.opening_messages()[1]


def test_opaque_rename_hides_every_id_but_keeps_the_world_playable():
    env = _env(n_live_tickets=2, carry_distance=3, opaque_names=True, ticket_entropy=1.0)
    ids = [t.id for t in env.world.catalog] + [s.id for s in env.world.services]
    surface = json.dumps(env.tools_schema()) + json.dumps(env.opening_messages())
    assert not any(f'"{i}"' in surface for i in ids)
    assert not any(t["function"]["name"] in ids for t in env.tools_schema())
    assert _drive(env)
    assert all(not any(i in t for i in ids) for t in env.issued)  # tickets built from renamed entities
    r = env.step("not_a_tool", {})
    assert r["status"] == "error" and env.steps[-1].error_kind == "unknown_tool"


def test_ablation_switches_do_not_change_the_task_seed(tmp_path, monkeypatch):
    store = ResultsStore(tmp_path / "s.sqlite")
    agent = FabricatingAgent([], from_level=99)
    monkeypatch.setattr(campaign_mod, "InteractiveEnv", _recording(agent.envs))
    ev = InteractiveEvaluator(agent, "m", store, "r", 10.0, protocol="native")
    cfg = {"plan_depth": 8.0, "n_live_tickets": 1.0, "carry_distance": 3.0}
    ev(cfg, 4)
    ev({**cfg, "copy_instruction": 1.0}, 4)
    a, b = store.by_run("r")
    assert a.task_seed == b.task_seed == _task_seed(cfg, 4)
    assert "ablation=copy_instruction" in b.verifier_detail
    store.close()


# --- planted agents through the real evaluator -----------------------------------------


def _recording(envs):
    class Recording(InteractiveEnv):
        def __post_init__(self):
            super().__post_init__()
            envs.append(self)

    return Recording


class FabricatingAgent:
    """Perfect executor (native tool calls) that FABRICATES a pattern-shaped
    ticket whenever tickets are pattern-shaped (ticket level >= from_level),
    then gives up after the error. `flaky` makes its first attempt on each
    task fail and every rerun pass (a planted sampling fluke)."""

    def __init__(self, envs, from_level=2, rescued_by=None, flaky=False):
        self.envs, self.from_level, self.rescued_by, self.flaky = envs, from_level, rescued_by, flaky
        self.seen: dict = {}

    def chat_turn(self, model, messages, tools=None, max_tokens=0, temperature=0.0):
        env = self.envs[-1]
        done = DONE()
        if env.goal_reached or any(not s.ok for s in env.steps):
            return done
        nxt = sum(1 for s in env.steps if s.ok)
        if nxt >= len(env.plan):
            return done
        c = env.plan[nxt]
        args = {k: env._out(v) for k, v in c.args.items()}
        if c.tool in env.producer_for:
            prod = env.producer_for[c.tool]
            fabricate = env.spec.ticket_level >= self.from_level
            if self.rescued_by and getattr(env.spec, self.rescued_by):
                fabricate = False
            if self.flaky:
                key = (env.seed, tuple(c.tool for c in env.plan))
                self.seen[key] = self.seen.get(key, 0) + 1
                fabricate = self.seen[key] == 1
            args["ticket"] = f"ticket_{prod}_12345" if fabricate else env.tickets[prod]
        tc = {"id": f"c{nxt}", "type": "function", "function": {"name": env._out(c.tool), "arguments": json.dumps(args)}}
        return ToolChatResult(message={"content": "", "tool_calls": [tc]},
                              tool_calls=((tc["id"], env._out(c.tool), json.dumps(args)),), text="",
                              tokens_in=100, tokens_out=10, latency_ms=1.0, finish_reason="tool_calls")


def DONE():
    return ToolChatResult(message={"content": "DONE"}, tool_calls=(), text="DONE",
                          tokens_in=100, tokens_out=10, latency_ms=1.0, finish_reason="stop")


@pytest.fixture
def store(tmp_path):
    s = ResultsStore(tmp_path / "t.sqlite")
    yield s
    s.close()


def test_mechanism_ray_breaks_where_tickets_become_pattern_shaped(store, monkeypatch):
    agent = FabricatingAgent([], from_level=2)
    monkeypatch.setattr(campaign_mod, "InteractiveEnv", _recording(agent.envs))
    bp = run_ticket_fidelity_campaign(agent, store, "camp-ticket-mechanism-zoo-001", "m", ray="mechanism",
                                      budget=120, cost_cap=1e9, k_trials=5, parallelism=1, protocol="native")
    assert bp.found
    assert 0.5 <= bp.config_at_break["ticket_entropy"] <= 0.55  # level 2 starts at entropy 0.5
    assert bp.hypothesis is not None and bp.hypothesis.necessary_factors == ("ticket_entropy",)
    fails = [r for r in store.by_run("camp-ticket-mechanism-zoo-001") if r.verdict == Verdict.FAIL]
    assert fails and all("ticket_errors=fabricated:1" in r.verifier_detail for r in fails)
    assert all(r.realized_dims["fabricated_tickets"] == 1.0 for r in fails)
    verify = store.by_run("camp-ticket-mechanism-zoo-001/verify")
    assert len(verify) == 3 * len(fails)  # every FAIL resample-gated before escalation used it
    assert "gate_stable=" in bp.note and "gate_noise=0" in bp.note


def test_resample_gate_turns_a_planted_fluke_into_a_pass(store, monkeypatch):
    agent = FabricatingAgent([], from_level=0, flaky=True)
    monkeypatch.setattr(campaign_mod, "InteractiveEnv", _recording(agent.envs))
    inner = InteractiveEvaluator(agent, "m", store, "g", 1e9, protocol="native")
    gated = ResampleGatedEvaluator(inner, n=3)
    cfg = {"plan_depth": 8.0, "n_live_tickets": 1.0, "carry_distance": 3.0}
    assert gated(cfg, 1) is True
    assert gated.noise == 1 and gated.stable == 0
    assert [r.verdict for r in store.by_run("g")] == [Verdict.FAIL]
    assert [r.verdict for r in store.by_run("g/verify")] == [Verdict.PASS] * 3


def test_resample_gate_keeps_a_stable_failure_and_never_resamples_errors(store, monkeypatch):
    agent = FabricatingAgent([], from_level=0)
    monkeypatch.setattr(campaign_mod, "InteractiveEnv", _recording(agent.envs))
    gated = ResampleGatedEvaluator(InteractiveEvaluator(agent, "m", store, "s", 1e9, protocol="native"))
    cfg = {"plan_depth": 8.0, "n_live_tickets": 1.0, "carry_distance": 3.0}
    assert gated(cfg, 1) is False and gated.stable == 1
    capped = ResampleGatedEvaluator(InteractiveEvaluator(agent, "m", store, "c", 0.0, protocol="native"))
    assert capped(cfg, 1) is False
    assert store.by_run("c/verify") == [] and capped.stable == 0


def test_length_ray_is_flat_for_a_reader_that_copies_any_length(store, monkeypatch):
    agent = FabricatingAgent([], from_level=99)
    monkeypatch.setattr(campaign_mod, "InteractiveEnv", _recording(agent.envs))
    bp = run_ticket_fidelity_campaign(agent, store, "len", "m", ray="length", budget=50, cost_cap=1e9,
                                      k_trials=5, parallelism=1, protocol="native")
    assert not bp.found and bp.degradation_onset is None


def test_trap_ray_ends_carry_six_tickets_with_three_decoys():
    cfg = TICKET_RAYS["trap"]().config_at(1.0)
    assert cfg["n_live_tickets"] == 6 and cfg["decoy_tickets"] == 3 and cfg["carry_distance"] == 6
    env = _env(pd=12, seed=2, n_live_tickets=6, carry_distance=6, decoy_tickets=3)
    assert env.summary()["max_live_tickets"] >= 4 and _drive(env)


# --- attribution ----------------------------------------------------------------------


def test_fisher_one_sided():
    assert fisher_one_sided(6, 6, 0, 6) == pytest.approx(1 / 924)
    assert fisher_one_sided(0, 6, 6, 6) == 1.0


def test_attribution_names_the_ablation_that_rescues(store, monkeypatch):
    agent = FabricatingAgent([], from_level=2, rescued_by="copy_instruction")
    monkeypatch.setattr(campaign_mod, "InteractiveEnv", _recording(agent.envs))
    ev = InteractiveEvaluator(agent, "m", store, "a", 1e9, protocol="native")
    cfg = {"plan_depth": 8.0, "n_live_tickets": 1.0, "carry_distance": 3.0, "ticket_entropy": 1.0}
    att = attribute_ticket_failure(ev, cfg, 7)
    assert att.status == "attributed" and att.baseline_pass == 0
    assert att.rescued_by == ["copy_instruction"]
    arms = {a.name: a for a in att.arms}
    assert arms["copy_instruction"].n_pass == 6 and arms["ticket_restatement"].n_pass == 0
    assert set(arms) == set(TICKET_ABLATIONS)


def test_attribution_refuses_a_failure_that_does_not_survive_the_gate(store, monkeypatch):
    agent = FabricatingAgent([], from_level=99)
    monkeypatch.setattr(campaign_mod, "InteractiveEnv", _recording(agent.envs))
    ev = InteractiveEvaluator(agent, "m", store, "n", 1e9, protocol="native")
    att = attribute_ticket_failure(ev, {"plan_depth": 8.0, "n_live_tickets": 1.0, "carry_distance": 3.0}, 7)
    assert att.status == "noise" and att.arms == []


def test_text_protocol_episode_with_fabrication_is_classified(store):
    env = _env(n_live_tickets=1, carry_distance=2, ticket_entropy=1.0)

    class TextFabricator:
        def chat_turn(self, model, messages, tools=None, max_tokens=0, temperature=0.0):
            if any(not s.ok for s in env.steps) or env.goal_reached:
                return DONE()
            c = env.plan[sum(1 for s in env.steps if s.ok)]
            args = dict(c.args)
            if c.tool in env.producer_for:
                args["ticket"] = "ticket_made_up_99999"
            argtxt = ", ".join(f"{k}={v!r}" for k, v in args.items())
            text = f"```tool_code\n{c.tool}({argtxt})\n```"
            return ToolChatResult(message={"content": text}, tool_calls=(), text=text, tokens_in=1,
                                  tokens_out=1, latency_ms=1.0, finish_reason="stop")

    res = run_episode(TextFabricator(), "m", env, protocol="text_fallback")
    assert res.outcome == "stopped_short" and res.summary["fabricated_tickets"] == 1


# --- Part D: code-tracing replication ---------------------------------------------------


def test_opaque_literal_trap_is_load_bearing_and_python_exact():
    from cartographer.codeworld import CodeDimParams, answer, foil_answers, generate, render, run

    for i in range(200):
        p = CodeDimParams(n_statements=8 + i % 25, max_nesting=i % 3, n_variables=2 + i % 5,
                          distractor_fraction=(i % 3) * 0.2, identifier_confusability=(i % 2) * 0.6,
                          opaque_literal_on=True, aliasing_on=i % 4 == 0)
        prog = generate(p, 40_000 + i)
        truth = answer(prog)
        assert foil_answers(prog)["pattern_completion"] != truth
        r = render(prog, i)
        assert "'tkt-" in r.source
        env, out = {"__builtins__": {"range": range, "len": len, "print": lambda *a: out.append(a)}}, []
        exec(compile(r.source, "<p>", "exec"), env)  # tests only
        assert env[r.target_name] == truth == run(prog).globals[prog.query.target]
    off = generate(CodeDimParams(n_statements=12), 3)
    assert foil_answers(off)["pattern_completion"] == answer(off)


def test_pattern_completing_reader_fails_exactly_when_the_trap_is_on(tmp_path, monkeypatch):
    from cartographer.codeworld import FOILS, answer
    from cartographer.exec import evaluator as ev_mod
    from cartographer.exec.client import ChatResult
    from cartographer.exec.evaluator import CodeTraceEvaluator, default_code_copy_ray
    from cartographer.schedule.escalate import find_breaking_point

    seen = {}
    real = ev_mod.code_render
    monkeypatch.setattr(ev_mod, "code_render", lambda prog, seed, *a, **k: seen.setdefault(
        (r := real(prog, seed, *a, **k)).prompt, (prog, r))[1])

    class Completer:
        def chat(self, model, prompt, **kw):
            prog, _ = seen[prompt]
            return ChatResult(text=f"ANSWER: {answer(prog, FOILS['pattern_completion'])!r}", tokens_in=1,
                              tokens_out=1, latency_ms=1.0, finish_reason="stop")

    s = ResultsStore(tmp_path / "c.sqlite")
    ev = CodeTraceEvaluator(Completer(), "m", s, "code", 1e9)
    assert all(ev({"n_statements": 10, "opaque_literal_on": 1}, i) is False for i in range(15))
    assert all(ev({"n_statements": 10, "aliasing_on": 1}, i) is True for i in range(15))
    fails = [r for r in s.by_run("code") if r.verdict == Verdict.FAIL]
    assert all("foil:" in r.verifier_detail and "pattern_completion" in r.verifier_detail for r in fails)
    assert all("opaque_literal_on=1" in r.verifier_detail for r in fails)
    bp = find_breaking_point(ev, default_code_copy_ray(), budget=120, k_trials=5)
    assert bp.found and bp.hypothesis.necessary_factors == ("opaque_literal_on",)
    s.close()
