"""Tests for cartographer.exec.evaluator: PipelineEvaluator and the
state/belief campaign wrappers.

All chat calls go through a scripted fake client (a plain object exposing
`.chat(model, prompt, max_tokens=...) -> ChatResult`); zero network calls.
"""

from __future__ import annotations

import pytest

from cartographer.exec.client import ChatResult
from cartographer.exec.evaluator import PipelineEvaluator, _apply_config, _task_seed
from cartographer.render import build_name_map, render
from cartographer.store.schema import Verdict
from cartographer.store.store import ResultsStore
from cartographer.world.generate import DimParams, generate


# --- fake client ---------------------------------------------------------


class ScriptedClient:
    """Fake client: `.chat` pops the next scripted result, or falls back to
    `default_text` (default: echoes nothing) if `script` is exhausted.
    """

    def __init__(self, script=None, default_text="wrong answer"):
        self.script = list(script) if script is not None else None
        self.default_text = default_text
        self.calls = []

    def chat(self, model, prompt, max_tokens=1500, **kwargs):
        self.calls.append((model, prompt, max_tokens))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return ChatResult(text=self.default_text, tokens_in=10, tokens_out=5, latency_ms=1.0)


def _ok(text, tokens_in=10, tokens_out=5, finish_reason="stop"):
    return ChatResult(
        text=text, tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=1.0,
        finish_reason=finish_reason,
    )


def _err(msg="http 500: boom"):
    return ChatResult(text="", tokens_in=0, tokens_out=0, latency_ms=1.0, error=msg)


# --- config mapping -------------------------------------------------------


def test_apply_config_rounds_ints_and_keeps_scramble_float():
    base = DimParams(
        chain_length=2, scramble=0.0, coref_pressure=0, distractor_events=2,
        distractor_similarity="same_category", filler_events=2, query_kind="state",
    )
    params = _apply_config(base, {"chain_length": 7.6, "scramble": 0.42, "coref_pressure": 3.4})
    assert params.chain_length == 8
    assert params.scramble == 0.42
    assert isinstance(params.scramble, float)
    assert params.coref_pressure == 3


def test_apply_config_unknown_key_raises():
    base = DimParams(
        chain_length=2, scramble=0.0, coref_pressure=0, distractor_events=2,
        distractor_similarity="same_category", filler_events=2, query_kind="state",
    )
    with pytest.raises(ValueError):
        _apply_config(base, {"bogus_dim": 1.0})


# --- determinism / trial variety ------------------------------------------


def test_same_config_and_trial_seed_replays_identical_prompt(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient()
    ev = PipelineEvaluator(client, "m", store, "run1", "state", cost_cap_usd=10.0)

    config = {"chain_length": 4}
    ev(config, trial_seed=42)
    ev(config, trial_seed=42)

    prompts = [c[1] for c in client.calls]
    assert prompts[0] == prompts[1]
    store.close()


def test_different_trial_seeds_produce_different_prompts(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient()
    ev = PipelineEvaluator(client, "m", store, "run1", "state", cost_cap_usd=10.0)

    config = {"chain_length": 4}
    ev(config, trial_seed=1)
    ev(config, trial_seed=2)

    prompts = [c[1] for c in client.calls]
    assert prompts[0] != prompts[1]
    store.close()


def test_task_seed_is_deterministic_function_of_config_and_trial_seed():
    s1 = _task_seed({"chain_length": 4, "scramble": 0.5}, 7)
    s2 = _task_seed({"scramble": 0.5, "chain_length": 4}, 7)  # different order
    s3 = _task_seed({"chain_length": 4, "scramble": 0.5}, 8)
    assert s1 == s2
    assert s1 != s3


# --- pass/fail wiring ------------------------------------------------------


def _render_world(base: DimParams, config: dict, trial_seed: int):
    params = _apply_config(base, config)
    seed = _task_seed(config, trial_seed)
    world = generate(params, seed)
    name_map = build_name_map(world, seed)
    rendered = render(world, name_map, seed)
    return rendered


def test_pass_wiring_records_pass_verdict(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    base = DimParams(
        chain_length=2, scramble=0.0, coref_pressure=0, distractor_events=2,
        distractor_similarity="same_category", filler_events=2, query_kind="state",
    )
    config = {"chain_length": 2}
    rendered = _render_world(base, config, trial_seed=5)

    client = ScriptedClient(script=[_ok(rendered.answer_key)])
    ev = PipelineEvaluator(client, "m", store, "run1", "state", cost_cap_usd=10.0, base=base)
    ok = ev(config, trial_seed=5)

    assert ok is True
    store.close()


def test_fail_wiring_records_fail_verdict(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    base = DimParams(
        chain_length=2, scramble=0.0, coref_pressure=0, distractor_events=2,
        distractor_similarity="same_category", filler_events=2, query_kind="state",
    )
    config = {"chain_length": 2}

    client = ScriptedClient(script=[_ok("definitely the wrong place")])
    ev = PipelineEvaluator(client, "m", store, "run1", "state", cost_cap_usd=10.0, base=base)
    ok = ev(config, trial_seed=5)

    assert ok is False
    store.close()


# --- error retry -----------------------------------------------------------


def test_error_then_success_retries_once_and_uses_second_result(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    base = DimParams(
        chain_length=2, scramble=0.0, coref_pressure=0, distractor_events=2,
        distractor_similarity="same_category", filler_events=2, query_kind="state",
    )
    config = {"chain_length": 2}
    rendered = _render_world(base, config, trial_seed=5)

    client = ScriptedClient(script=[_err(), _ok(rendered.answer_key)])
    ev = PipelineEvaluator(client, "m", store, "run1", "state", cost_cap_usd=10.0, base=base)
    ok = ev(config, trial_seed=5)

    assert len(client.calls) == 2
    assert ok is True
    assert ev.error_count == 0
    store.close()


def test_persistent_error_returns_false_and_increments_error_count(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(script=[_err(), _err()])
    ev = PipelineEvaluator(client, "m", store, "run1", "state", cost_cap_usd=10.0)

    ok = ev({"chain_length": 2}, trial_seed=5)

    assert ok is False
    assert ev.error_count == 1
    assert len(client.calls) == 2
    store.close()


# --- cost cap ----------------------------------------------------------


def test_cost_cap_stops_further_calls(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    # Each call costs something nonzero for a priced model.
    client = ScriptedClient(default_text="wrong")
    ev = PipelineEvaluator(
        client, "openai/gpt-oss-20b", store, "run1", "state", cost_cap_usd=0.000001,
    )

    # First call: not capped yet (spent starts at 0), goes through, then trips the cap.
    ev({"chain_length": 2}, trial_seed=1)
    assert ev.spent_usd > 0
    assert ev.capped is True
    calls_after_first = len(client.calls)

    ok2 = ev({"chain_length": 2}, trial_seed=2)
    assert ok2 is False
    assert len(client.calls) == calls_after_first  # no new .chat call
    store.close()


# --- record completeness ----------------------------------------------------


def test_record_completeness_campaign_marker_and_fields(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    base = DimParams(
        chain_length=2, scramble=0.0, coref_pressure=0, distractor_events=2,
        distractor_similarity="same_category", filler_events=2, query_kind="belief",
    )
    config = {"chain_length": 2}
    client = ScriptedClient(script=[_ok("some answer", finish_reason="stop")])
    ev = PipelineEvaluator(client, "m", store, "run1", "belief", cost_cap_usd=10.0, base=base)
    ev(config, trial_seed=9)

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "db.sqlite3"))
    row = conn.execute(
        "SELECT verifier_detail, realized_dims, reasoning_text, finish_reason, response_text "
        "FROM experiments WHERE run_id = ?",
        ("run1",),
    ).fetchone()
    conn.close()
    store.close()

    assert row is not None
    verifier_detail, realized_dims_json, reasoning_text, finish_reason, response_text = row
    assert "campaign=1" in verifier_detail
    assert "kind=belief" in verifier_detail
    assert realized_dims_json != "{}"
    assert finish_reason == "stop"
    assert response_text == "some answer"


def test_cost_capped_record_has_marker_and_no_prompt(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")
    ev = PipelineEvaluator(
        client, "openai/gpt-oss-20b", store, "run1", "state", cost_cap_usd=0.000001,
    )
    ev({"chain_length": 2}, trial_seed=1)  # trips the cap
    ev({"chain_length": 2}, trial_seed=2)  # capped, zero-cost

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "db.sqlite3"))
    rows = conn.execute(
        "SELECT verifier_detail, cost_usd FROM experiments WHERE run_id = ? ORDER BY created_at",
        ("run1",),
    ).fetchall()
    conn.close()
    store.close()

    assert len(rows) == 2
    last_detail, last_cost = rows[-1]
    assert "cost_capped=1" in last_detail
    assert last_cost == 0.0


def test_full_suite_call_count_and_verdict_tracking(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    base = DimParams(
        chain_length=2, scramble=0.0, coref_pressure=0, distractor_events=2,
        distractor_similarity="same_category", filler_events=2, query_kind="state",
    )
    config = {"chain_length": 2}
    rendered = _render_world(base, config, trial_seed=1)
    client = ScriptedClient(script=[_ok(rendered.answer_key)])
    ev = PipelineEvaluator(client, "m", store, "run1", "state", cost_cap_usd=10.0, base=base)

    ev(config, trial_seed=1)
    assert ev.call_count == 1
    assert ev.error_count == 0
    store.close()
