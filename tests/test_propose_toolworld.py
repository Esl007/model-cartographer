"""Tests for the tool-province adversarial proposer (propose/toolworld.py):
offline pieces only -- clamping, gauntlet determinism, digest -- no API."""

from __future__ import annotations

import pytest

from cartographer.propose.proposer import Proposal
from cartographer.propose.toolworld import (
    ToolGauntletResult,
    build_tool_digest,
    clamp_tool_config,
    run_tool_gauntlet,
)
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore


def test_clamp_tool_config_clamps_and_drops_unknown():
    cfg = clamp_tool_config(
        {
            "plan_depth": 99,
            "poison_traps": 1,
            "state_obscurity": -3.0,
            "bogus_key": 5,
        }
    )
    assert cfg == {
        "plan_depth": 6.0,
        "poison_traps": 1.0,
        "state_obscurity": 0.0,
    }


def test_clamp_tool_config_rejects_non_numeric():
    assert clamp_tool_config({"plan_depth": "deep"}) is None
    assert clamp_tool_config({"poison_traps": True}) is None


def test_tool_gauntlet_rejects_empty_config():
    result = run_tool_gauntlet(Proposal(config={"bogus": 1}), base_seed=7)
    assert not result.accepted
    assert result.reason == "invalid_config"


def test_tool_gauntlet_deterministic():
    proposal = Proposal(
        config={"plan_depth": 5, "poison_traps": 1, "zero_slack_on": 1}
    )
    a = run_tool_gauntlet(proposal, base_seed=42)
    b = run_tool_gauntlet(proposal, base_seed=42)
    assert a.accepted == b.accepted
    assert a.seed == b.seed
    assert a.greedy_fail_rate == b.greedy_fail_rate
    assert a.random_median_fail_rate == b.random_median_fail_rate


def test_tool_gauntlet_trap_heavy_config_beats_trapless():
    # A max-trap config should have a greedy-fail rate at least as high as a
    # completely trapless baseline config at the same seeds.
    trapless = run_tool_gauntlet(
        Proposal(config={"plan_depth": 3}), base_seed=11
    )
    trapped = run_tool_gauntlet(
        Proposal(
            config={
                "plan_depth": 5,
                "poison_traps": 1,
                "dishonest_decoys_on": 1,
                "zero_slack_on": 1,
                "id_collisions_on": 1,
            }
        ),
        base_seed=11,
    )
    assert isinstance(trapped, ToolGauntletResult)
    assert trapped.greedy_fail_rate >= trapless.greedy_fail_rate


def _tool_record(run_id: str, verdict: Verdict, **latent) -> ExperimentRecord:
    base = {
        "plan_depth": 3.0,
        "decoy_tools": 2.0,
        "precondition_chain": 1.0,
        "state_obscurity": 0.0,
        "irreversibility": 0.0,
        "parallel_goals": 1.0,
    }
    base.update(latent)
    return ExperimentRecord(
        run_id=run_id,
        model_name="test-model",
        latent_params=base,
        realized_dims={},
        render_mode=RenderMode.TEMPLATE,
        task_seed=1,
        trial_seed=1,
        rendering_seed=1,
        prompt_text="p",
        response_text="r",
        verdict=verdict,
        verifier_detail="kind=tool_plan;campaign=1;detail=goal_not_reached",
        tokens_in=1,
        tokens_out=1,
        cost_usd=0.0,
        latency_ms=1,
    )


def test_build_tool_digest_deterministic_and_filters_kind(tmp_path):
    store = ResultsStore(str(tmp_path / "digest.sqlite"))
    store.append(_tool_record("r1", Verdict.PASS))
    store.append(_tool_record("r1", Verdict.FAIL, poison_traps=1.0))
    a = build_tool_digest(store, "test-model")
    b = build_tool_digest(store, "test-model")
    assert a == b
    assert "1/2 pass" in a
    assert "poison_traps=1: 0/1 pass" in a
    assert "goal_not_reached" in a
    store.close()


def test_build_tool_digest_no_evidence(tmp_path):
    store = ResultsStore(str(tmp_path / "empty.sqlite"))
    assert "No prior tool-planning evidence" in build_tool_digest(store, "nope")
    store.close()
