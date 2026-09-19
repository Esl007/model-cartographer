"""Tests for cartographer.exec.evaluator: PipelineEvaluator and the
state/belief campaign wrappers.

All chat calls go through a scripted fake client (a plain object exposing
`.chat(model, prompt, max_tokens=...) -> ChatResult`); zero network calls.
"""

from __future__ import annotations

import threading

import pytest

from cartographer.exec.client import ChatResult
from cartographer.exec.evaluator import (
    VERIFY_SUFFIX,
    PipelineEvaluator,
    _apply_config,
    _task_seed,
    classify_all_verified_failures,
    default_token_budget,
    run_verify_failures,
)
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

    def chat(self, model, prompt, max_tokens=1500, max_tokens_retry=4000, **kwargs):
        self.calls.append((model, prompt, max_tokens))
        self.max_tokens_retries = getattr(self, "max_tokens_retries", [])
        self.max_tokens_retries.append(max_tokens_retry)
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


# --- model-aware token budgets ----------------------------------------------


def test_default_token_budget_ultra_gets_bigger_budget():
    assert default_token_budget("nvidia/Nemotron-3-Ultra-550b-a55b") == (8000, 16000)
    assert default_token_budget("NVIDIA/SOME-ULTRA-MODEL") == (8000, 16000)


def test_default_token_budget_non_ultra_keeps_defaults():
    assert default_token_budget("openai/gpt-oss-20b") == (1500, 4000)
    assert default_token_budget("nvidia/Nemotron-3-Nano-9b") == (1500, 4000)


def test_ultra_model_id_gets_bigger_budgets_in_practice(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")
    ev = PipelineEvaluator(
        client, "nvidia/Nemotron-3-Ultra-550b-a55b", store, "run1", "state", cost_cap_usd=10.0,
    )
    assert ev.max_tokens == 8000
    assert ev.max_tokens_retry == 16000

    ev({"chain_length": 2}, trial_seed=1)
    assert client.calls[0][2] == 8000  # max_tokens actually sent
    assert client.max_tokens_retries[0] == 16000
    store.close()


def test_non_ultra_model_keeps_default_budgets(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")
    ev = PipelineEvaluator(client, "openai/gpt-oss-20b", store, "run1", "state", cost_cap_usd=10.0)
    assert ev.max_tokens == 1500
    assert ev.max_tokens_retry == 4000
    store.close()


def test_explicit_max_tokens_override_flows_through(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")
    ev = PipelineEvaluator(
        client, "nvidia/Nemotron-3-Ultra-550b-a55b", store, "run1", "state", cost_cap_usd=10.0,
        max_tokens=3000,
    )
    # Override wins over the ultra heuristic; retry budget derives from it
    # (double, since no explicit max_tokens_retry override was given).
    assert ev.max_tokens == 3000
    assert ev.max_tokens_retry == 6000

    ev({"chain_length": 2}, trial_seed=1)
    assert client.calls[0][2] == 3000
    store.close()


def test_explicit_max_tokens_and_retry_override_both_flow_through(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")
    ev = PipelineEvaluator(
        client, "m", store, "run1", "state", cost_cap_usd=10.0,
        max_tokens=2000, max_tokens_retry=5000,
    )
    assert ev.max_tokens == 2000
    assert ev.max_tokens_retry == 5000
    store.close()


def test_persistent_cap_hit_marks_retry_exhausted(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    # `retried=True` simulates Client.chat's own retry path having already
    # fired and still coming back length-capped -- a persistent cap.
    client = ScriptedClient(
        script=[ChatResult(
            text="", tokens_in=10, tokens_out=4000, latency_ms=1.0,
            finish_reason="length", retried=True,
        )]
    )
    ev = PipelineEvaluator(client, "m", store, "run1", "state", cost_cap_usd=10.0)
    ok = ev({"chain_length": 2}, trial_seed=1)
    assert ok is False

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "db.sqlite3"))
    detail = conn.execute(
        "SELECT verifier_detail FROM experiments WHERE run_id = ?", ("run1",)
    ).fetchone()[0]
    conn.close()
    store.close()

    assert "cap_hit=1" in detail
    assert "retry_exhausted=1" in detail


def test_transient_cap_hit_without_retry_has_no_exhausted_marker(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    # retried=False (default): the length-cap happened but Client.chat's
    # retry path never fired (e.g. some text already came back), so this
    # is a one-off, not a persistent exhaustion.
    client = ScriptedClient(
        script=[ChatResult(
            text="partial", tokens_in=10, tokens_out=1500, latency_ms=1.0,
            finish_reason="length",
        )]
    )
    ev = PipelineEvaluator(client, "m", store, "run1", "state", cost_cap_usd=10.0)
    ev({"chain_length": 2}, trial_seed=1)

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "db.sqlite3"))
    detail = conn.execute(
        "SELECT verifier_detail FROM experiments WHERE run_id = ?", ("run1",)
    ).fetchone()[0]
    conn.close()
    store.close()

    assert "cap_hit=1" in detail
    assert "retry_exhausted=1" not in detail


# --- cooperative cancellation ------------------------------------------------


def test_cancel_event_set_before_call_short_circuits(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")
    cancel_event = threading.Event()
    cancel_event.set()
    ev = PipelineEvaluator(
        client, "m", store, "run1", "state", cost_cap_usd=10.0, cancel_event=cancel_event,
    )

    ok = ev({"chain_length": 2}, trial_seed=1)

    assert ok is False
    assert ev.cancelled is True
    assert len(client.calls) == 0  # no API call at all
    store.close()


def test_cancel_event_mid_run_stops_further_calls(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")
    cancel_event = threading.Event()
    ev = PipelineEvaluator(
        client, "m", store, "run1", "state", cost_cap_usd=10.0, cancel_event=cancel_event,
    )

    ev({"chain_length": 2}, trial_seed=1)
    assert len(client.calls) == 1

    cancel_event.set()
    ok = ev({"chain_length": 2}, trial_seed=2)

    assert ok is False
    assert len(client.calls) == 1  # no new call after cancellation
    store.close()


def test_cancelled_record_has_marker_and_no_prompt(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")
    cancel_event = threading.Event()
    cancel_event.set()
    ev = PipelineEvaluator(
        client, "m", store, "run1", "state", cost_cap_usd=10.0, cancel_event=cancel_event,
    )
    ev({"chain_length": 2}, trial_seed=1)

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "db.sqlite3"))
    detail, cost, prompt_text = conn.execute(
        "SELECT verifier_detail, cost_usd, prompt_text FROM experiments WHERE run_id = ?",
        ("run1",),
    ).fetchone()
    conn.close()
    store.close()

    assert "cancelled=1" in detail
    assert cost == 0.0
    assert prompt_text is None


def test_no_cancel_event_behaves_as_before(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")
    ev = PipelineEvaluator(client, "m", store, "run1", "state", cost_cap_usd=10.0)
    ev({"chain_length": 2}, trial_seed=1)
    assert len(client.calls) == 1
    assert ev.cancelled is False
    store.close()


# --- adversarial mode -------------------------------------------------------


def _adversarial_belief_base() -> DimParams:
    return DimParams(
        chain_length=4, scramble=0.3, coref_pressure=1, distractor_events=2,
        distractor_similarity="same_category", filler_events=2, query_kind="belief",
        witness_cutoff=0.5,
    )


def _adversarial_rendered(base: DimParams, config: dict, trial_seed: int, n_candidates: int = 40):
    """Mirrors what PipelineEvaluator does internally in adversarial mode:
    derive the task seed, run the adversarial search from it, and render
    the winning world -- so a test can script the client with the correct
    expected answer for whichever world adversarial mode actually picks."""
    from cartographer.world.search import search_adversarial_seeds

    params = _apply_config(base, config)
    task_seed = _task_seed(config, trial_seed)
    winners = search_adversarial_seeds(params, task_seed, n_candidates=n_candidates, top_k=1)
    seed, profile = winners[0]
    world = generate(params, seed)
    name_map = build_name_map(world, seed)
    rendered = render(world, name_map, seed)
    return seed, profile, rendered


def test_adversarial_mode_records_traps_and_foil_score(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    base = _adversarial_belief_base()
    config = {"chain_length": 4, "witness_cutoff": 0.5}
    seed, profile, rendered = _adversarial_rendered(base, config, trial_seed=3)

    client = ScriptedClient(script=[_ok(rendered.answer_key)])
    ev = PipelineEvaluator(
        client, "m", store, "run1", "belief", cost_cap_usd=10.0, base=base, adversarial=True,
    )
    ok = ev(config, trial_seed=3)

    assert ok is True
    records = store.by_run("run1")
    assert len(records) == 1
    record = records[0]
    assert record.task_seed == seed  # the chosen adversarial seed, not the plain task seed
    assert ";adversarial=1" in record.verifier_detail
    assert f"traps={','.join(profile.trap_kinds)}" in record.verifier_detail
    assert "foil_score=" in record.verifier_detail
    assert record.realized_dims["foil_divergence"] == float(profile.n_diverging)
    store.close()


def test_adversarial_mode_off_by_default(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    base = _adversarial_belief_base()
    ev = PipelineEvaluator(ScriptedClient(), "m", store, "run1", "belief", cost_cap_usd=10.0, base=base)
    assert ev.adversarial is False
    ev({"chain_length": 4}, trial_seed=1)
    record = store.by_run("run1")[0]
    assert "adversarial=1" not in (record.verifier_detail or "")
    assert "foil_divergence" not in record.realized_dims
    store.close()


def test_adversarial_mode_deterministic_seed_choice(tmp_path):
    """Same config/trial_seed under adversarial mode picks the same world
    both times (determinism carries through the search)."""
    store = ResultsStore(tmp_path / "db.sqlite3")
    base = _adversarial_belief_base()
    config = {"chain_length": 4, "witness_cutoff": 0.5}

    client = ScriptedClient(default_text="wrong answer entirely")
    ev = PipelineEvaluator(
        client, "m", store, "run1", "belief", cost_cap_usd=10.0, base=base, adversarial=True,
    )
    ev(config, trial_seed=9)
    ev(config, trial_seed=9)

    prompts = [c[1] for c in client.calls]
    assert prompts[0] == prompts[1]
    store.close()


# --- full_map campaign ------------------------------------------------------


from cartographer.exec.evaluator import FullMapResult, StageSummary, run_full_map_campaign  # noqa: E402


def test_full_map_runs_all_five_stages_under_child_run_ids(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    # default_text always "wrong" -- every trial FAILs, but that's fine, we
    # only care that every stage ran and persisted records under its own
    # child run_id.
    client = ScriptedClient(default_text="definitely not the right answer")

    result = run_full_map_campaign(
        client, store, "camp-full-map-test-001", "fake/model",
        budget=300, cost_cap=0.5,
    )

    assert isinstance(result, FullMapResult)
    stages_seen = [s.stage for s in result.stages]
    assert stages_seen == ["state", "order", "belief", "adversarial_belief", "breaking_point"]

    for stage in stages_seen:
        child_run_id = f"camp-full-map-test-001/{stage}"
        records = store.by_run(child_run_id)
        assert records, f"stage {stage} recorded no records"
        assert all(r.run_id == child_run_id for r in records)

    assert result.total_records == sum(s.n_records for s in result.stages)
    store.close()


def test_full_map_budget_partitioned_across_stages(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")

    budget = 300
    result = run_full_map_campaign(
        client, store, "camp-full-map-budget-001", "fake/model",
        budget=budget, cost_cap=0.5,
    )

    # No single stage should have consumed the entire shared budget --
    # each stage's own trial count should roughly track its target share
    # (state gets the smallest slice, belief/adversarial the largest).
    by_stage = {s.stage: s.n_records for s in result.stages}
    assert by_stage["state"] > 0
    assert by_stage["belief"] >= by_stage["state"]
    # Total records across all stages must not blow past the shared budget
    # by more than a small per-stage rounding slop.
    assert sum(by_stage.values()) <= budget + len(by_stage)
    store.close()


def test_full_map_cost_cap_exhaustion_continues_to_next_stage(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    # A priced model + a near-zero overall cost_cap: the first stage(s)
    # will exhaust their tiny per-stage cap almost immediately, but the
    # battery must still reach every later stage (as zero-cost/zero-call
    # capped records, per PipelineEvaluator._record_capped).
    client = ScriptedClient(default_text="wrong")

    result = run_full_map_campaign(
        client, store, "camp-full-map-tinycap-001", "openai/gpt-oss-20b",
        budget=300, cost_cap=0.0000001,
    )

    stages_seen = [s.stage for s in result.stages]
    assert stages_seen == ["state", "order", "belief", "adversarial_belief", "breaking_point"]
    # Every stage still produced at least one persisted record (even if it's
    # just a cost_capped marker record) -- the battery didn't stop early.
    for stage in stages_seen:
        assert store.by_run(f"camp-full-map-tinycap-001/{stage}")
    store.close()


def test_full_map_cancellation_stops_cleanly(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")
    cancel_event = threading.Event()
    cancel_event.set()  # already cancelled before the battery even starts

    result = run_full_map_campaign(
        client, store, "camp-full-map-cancel-001", "fake/model",
        budget=30, cost_cap=0.05, cancel_event=cancel_event,
    )

    assert result.cancelled is True
    # Cancelled before the first stage even starts -- no stages ran.
    assert result.stages == []
    store.close()


def test_full_map_aggregates_spend_and_records_correctly(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")

    result = run_full_map_campaign(
        client, store, "camp-full-map-agg-001", "fake/model",
        budget=300, cost_cap=0.5,
    )

    # total_spend_usd/total_records must equal the sum over stages (no
    # double counting, no dropped stages).
    assert result.total_spend_usd == pytest.approx(sum(s.spend_usd for s in result.stages))
    assert result.total_records == sum(s.n_records for s in result.stages)
    for s in result.stages:
        assert isinstance(s, StageSummary)
        assert s.run_id == f"camp-full-map-agg-001/{s.stage}"
    store.close()


def test_full_map_breaking_point_seeds_from_belief_boundaries(tmp_path, monkeypatch):
    """If the belief stage's CampaignState reports a boundary on some dim,
    the breaking_point stage's ray should be re-seeded near it (base_value
    moved off the generator default) rather than using the plain default
    ray -- verified via the stage note recording which path was taken."""
    store = ResultsStore(tmp_path / "db.sqlite3")

    # A client that always fails so run_belief_campaign's scheduler is
    # virtually guaranteed to record SOME pass/fail flip (baseline configs
    # are the easiest, later a lot are "wrong"); to make this deterministic
    # instead of relying on real generation, we monkeypatch run_belief_campaign
    # to return a CampaignState with an explicit boundary.
    import cartographer.exec.evaluator as ev_mod
    from cartographer.schedule.scheduler import CampaignState

    fake_state = CampaignState(boundaries={"chain_length": (5.0, 4.0, 6.0)})

    class _FakeEvaluator:
        spent_usd = 0.01
        error_count = 0

    fake_state.evaluator = _FakeEvaluator()  # type: ignore[attr-defined]

    def _fake_run_belief_campaign(client, store, run_id, model, **kwargs):
        # Still write at least one record so store.by_run(child) is non-empty.
        from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict

        store.append(
            ExperimentRecord(
                run_id=run_id, model_name=model, latent_params={}, realized_dims={},
                render_mode=RenderMode.TEMPLATE, task_seed=1, trial_seed=1,
                verdict=Verdict.PASS,
            )
        )
        return fake_state

    monkeypatch.setattr(ev_mod, "run_belief_campaign", _fake_run_belief_campaign)

    client = ScriptedClient(default_text="wrong")
    result = ev_mod.run_full_map_campaign(
        client, store, "camp-full-map-seed-001", "fake/model",
        budget=300, cost_cap=0.5,
    )

    bp_summary = next(s for s in result.stages if s.stage == "breaking_point")
    assert "re-seeded near belief boundaries" in bp_summary.note
    assert "chain_length" in bp_summary.note
    store.close()


# --- run_verify_failures ---------------------------------------------------


def _make_fail_record(store, model="fake/model", run_id="run-verify-001", trial_seed=1):
    """Run one real PipelineEvaluator trial against a client that always
    answers wrong, so it's guaranteed to persist exactly one FAIL record
    with a real stored prompt_text -- the exact thing run_verify_failures
    replays. Returns the FAIL ExperimentRecord."""
    before_ids = {r.record_id for r in store.by_run(run_id)}
    client = ScriptedClient(default_text="definitely the wrong answer")
    ev = PipelineEvaluator(client, model, store, run_id, "state", cost_cap_usd=10.0)
    ev({"chain_length": 2}, trial_seed=trial_seed)
    records = store.by_run(run_id)
    new_fails = [
        r for r in records if r.verdict == Verdict.FAIL and r.record_id not in before_ids
    ]
    assert len(new_fails) == 1, f"expected exactly one NEW FAIL, got {[r.verdict for r in records]}"
    return new_fails[0]


def _true_answer_for(record):
    """Recompute the correct answer text for a stored record via the same
    regeneration idiom evaluator.py itself uses."""
    from cartographer.exec.evaluator import _apply_config, _default_base
    from cartographer.world.foils import foil_divergence

    base = _default_base("state")
    params = _apply_config(base, record.latent_params)
    world = generate(params, record.task_seed)
    name_map = build_name_map(world, record.rendering_seed)
    profile = foil_divergence(world, name_map)
    return profile.true_answer


def test_verify_failures_classifies_noise_when_resamples_mostly_pass(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    run_id = "run-verify-noise"
    original = _make_fail_record(store, run_id=run_id)
    correct = _true_answer_for(original)

    # 3 resamples, all correct -> pass_rate = 3/4 = 0.75 > 0.5 -> noise.
    resample_client = ScriptedClient(script=[
        ChatResult(text=correct, tokens_in=10, tokens_out=5, latency_ms=1.0, finish_reason="stop"),
        ChatResult(text=correct, tokens_in=10, tokens_out=5, latency_ms=1.0, finish_reason="stop"),
        ChatResult(text=correct, tokens_in=10, tokens_out=5, latency_ms=1.0, finish_reason="stop"),
    ])

    result = run_verify_failures(resample_client, "fake/model", store, run_id, n=3)

    assert result.n_fails_total == 1
    assert result.n_verified == 1
    assert result.n_noise == 1
    assert result.n_stable == 0
    assert resample_client.calls  # actually called the client

    # Verify trials were persisted under f"{run_id}/verify", linked to the
    # original record_id.
    verify_records = store.by_run(f"{run_id}{VERIFY_SUFFIX}")
    assert len(verify_records) == 3
    for r in verify_records:
        assert f"verify_of={original.record_id}" in (r.verifier_detail or "")
    assert sum(1 for r in verify_records if r.verdict == Verdict.PASS) == 3

    # Original FAIL record is untouched (append-only invariant).
    reread = [r for r in store.by_run(run_id) if r.record_id == original.record_id][0]
    assert reread.verdict == Verdict.FAIL
    assert reread.response_text == original.response_text

    classification = classify_all_verified_failures(store, run_id)
    assert classification[original.record_id].status == "noise"
    store.close()


def test_verify_failures_classifies_stable_when_resamples_mostly_fail(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    run_id = "run-verify-stable"
    original = _make_fail_record(store, run_id=run_id)

    # 3 resamples, all wrong -> pass_rate = 0/4 = 0.0 <= 0.5 -> stable.
    resample_client = ScriptedClient(default_text="still wrong")

    result = run_verify_failures(resample_client, "fake/model", store, run_id, n=3)

    assert result.n_verified == 1
    assert result.n_stable == 1
    assert result.n_noise == 0

    classification = classify_all_verified_failures(store, run_id)
    assert classification[original.record_id].status == "stable"
    assert classification[original.record_id].n_samples == 3
    assert classification[original.record_id].n_pass == 0
    store.close()


def test_verify_failures_threshold_boundary_exactly_half_is_stable(tmp_path):
    """pass_rate == stable_threshold (0.5) counts as stable (<=, not <)."""
    store = ResultsStore(tmp_path / "db.sqlite3")
    run_id = "run-verify-boundary"
    original = _make_fail_record(store, run_id=run_id)
    correct = _true_answer_for(original)

    # n=3 resamples: 1 correct, 2 wrong -> pass_rate = (0+1)/4 = 0.25 -> stable
    # (use n=1 with 1 correct to hit exactly 1/2 = 0.5): original FAIL (1
    # fail) + 1 resample pass = 1 pass out of 2 total = 0.5 exactly.
    resample_client = ScriptedClient(script=[
        ChatResult(text=correct, tokens_in=10, tokens_out=5, latency_ms=1.0, finish_reason="stop"),
    ])

    result = run_verify_failures(resample_client, "fake/model", store, run_id, n=1, stable_threshold=0.5)

    assert result.n_stable == 1
    assert result.n_noise == 0
    classification = classify_all_verified_failures(store, run_id)
    assert classification[original.record_id].pass_rate == pytest.approx(0.5)
    assert classification[original.record_id].status == "stable"
    store.close()


def test_verify_failures_skips_passes_never_resamples_them(tmp_path):
    """A run with only PASS records should never call the client at all --
    verification is FAIL-only, by construction."""
    store = ResultsStore(tmp_path / "db.sqlite3")
    run_id = "run-verify-allpass"

    # A client that always answers correctly relative to whatever the true
    # state ends up being isn't reliable to script generically, so instead
    # directly persist a PASS record.
    from cartographer.store.schema import ExperimentRecord, RenderMode

    store.append(
        ExperimentRecord(
            run_id=run_id, model_name="fake/model", latent_params={}, realized_dims={},
            render_mode=RenderMode.TEMPLATE, task_seed=1, trial_seed=1,
            verdict=Verdict.PASS, prompt_text="does not matter",
        )
    )

    resample_client = ScriptedClient(default_text="anything")
    result = run_verify_failures(resample_client, "fake/model", store, run_id, n=3)

    assert result.n_fails_total == 0
    assert result.n_verified == 0
    assert resample_client.calls == []
    store.close()


def test_verify_failures_cost_cap_slice_stops_early(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    run_id = "run-verify-costcap"
    _make_fail_record(store, run_id=run_id, model="openai/gpt-oss-120b")

    resample_client = ScriptedClient(default_text="still wrong")
    # A cost_cap_slice of exactly 0.0 must stop before any resample call is made.
    result = run_verify_failures(
        resample_client, "openai/gpt-oss-120b", store, run_id, n=3, cost_cap_slice=0.0
    )

    assert result.n_verified == 0
    assert resample_client.calls == []
    store.close()


def test_verify_failures_max_verified_truncates_and_logs(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    run_id = "run-verify-truncate"
    for i in range(3):
        _make_fail_record(store, run_id=run_id, trial_seed=100 + i)

    resample_client = ScriptedClient(default_text="still wrong")
    result = run_verify_failures(
        resample_client, "fake/model", store, run_id, n=1, max_verified=2
    )

    assert result.n_fails_total == 3
    assert result.n_verified == 2
    assert result.truncated is True
    assert any("truncated" in note for note in result.notes)
    store.close()


def test_verify_failures_cancel_event_stops_cleanly(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    run_id = "run-verify-cancel"
    for i in range(3):
        _make_fail_record(store, run_id=run_id, trial_seed=200 + i)

    cancel_event = threading.Event()
    cancel_event.set()  # already cancelled before verification starts

    resample_client = ScriptedClient(default_text="still wrong")
    result = run_verify_failures(
        resample_client, "fake/model", store, run_id, n=3, cancel_event=cancel_event
    )

    assert result.cancelled is True
    assert result.n_verified == 0
    assert resample_client.calls == []
    store.close()


def test_campaign_verify_manual_mode_spends_zero_verification_calls(tmp_path):
    """run_state_campaign(..., verify="manual") must record FAILs as usual
    but never call run_verify_failures -- zero extra API calls beyond the
    main scheduler loop."""
    import cartographer.exec.evaluator as ev_mod

    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")

    verify_calls = []
    real_run_verify_failures = ev_mod.run_verify_failures

    def _spy(*args, **kwargs):
        verify_calls.append((args, kwargs))
        return real_run_verify_failures(*args, **kwargs)

    import unittest.mock as mock

    with mock.patch.object(ev_mod, "run_verify_failures", side_effect=_spy):
        ev_mod.run_state_campaign(
            client, store, "camp-state-manual-001", "fake/model",
            budget=10, cost_cap=0.05, verify="manual",
        )

    assert verify_calls == []
    # No f"{run_id}/verify" records exist either.
    assert store.by_run("camp-state-manual-001/verify") == []
    store.close()


def test_campaign_verify_auto_mode_calls_verify_failures(tmp_path):
    """The inverse check: verify="auto" (the default) actually invokes
    verification when the main loop produced at least one FAIL."""
    import cartographer.exec.evaluator as ev_mod

    store = ResultsStore(tmp_path / "db.sqlite3")
    client = ScriptedClient(default_text="wrong")

    state = ev_mod.run_state_campaign(
        client, store, "camp-state-auto-001", "fake/model",
        budget=10, cost_cap=0.5, verify="auto",
    )

    assert any("verify_failures:" in note for note in state.notes)
    store.close()


def test_run_verify_failures_invalid_mode_raises():
    import cartographer.exec.evaluator as ev_mod

    store = None
    with pytest.raises(ValueError):
        ev_mod._run_campaign_with_verification(
            "state", ev_mod._state_scheduler_config, ScriptedClient(), store,
            "run", "model", 10, 0.5, None, None, None, verify="bogus",
        )
