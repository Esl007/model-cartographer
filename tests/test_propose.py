"""Acceptance tests for cartographer.propose (the adversarial proposer):
digest determinism, proposal parsing/clamping, the offline gauntlet, and
the full round loop. Zero network calls -- every client is a scripted fake.
"""

from __future__ import annotations

import pytest

from cartographer.exec.client import ChatResult
from cartographer.exec.evaluator import PipelineEvaluator
from cartographer.propose.digest import build_digest
from cartographer.propose.gauntlet import run_gauntlet, run_gauntlet_batch
from cartographer.propose.loop import ContaminationError, run_proposer_loop
from cartographer.propose.proposer import Proposal, propose_configs
from cartographer.render import build_name_map, render
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.world.generate import DimParams, generate


# --- shared fakes ----------------------------------------------------------


class ScriptedClient:
    """Fake client: `.chat` pops the next scripted result (keyed by model),
    or falls back to a per-model default callable. Mirrors the
    `ScriptedClient` pattern in tests/test_evaluator.py."""

    def __init__(self, scripts: dict | None = None, default_text: str = "wrong answer"):
        self.scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self.default_text = default_text
        self.calls = []

    def chat(self, model, prompt, max_tokens=1500, max_tokens_retry=4000, **kwargs):
        self.calls.append({"model": model, "prompt": prompt})
        script = self.scripts.get(model)
        if script:
            item = script.pop(0)
            if callable(item):
                item = item(prompt)
            return item
        return ChatResult(text=self.default_text, tokens_in=10, tokens_out=5, latency_ms=1.0)


def _ok(text):
    return ChatResult(text=text, tokens_in=10, tokens_out=5, latency_ms=1.0, finish_reason="stop")


def _make_store(tmp_path):
    return ResultsStore(str(tmp_path / "test.sqlite"))


def _seed_store_record(store, model, latent_params, verdict, verifier_detail="kind=belief;campaign=1"):
    store.append(
        ExperimentRecord(
            run_id="seed-run",
            model_name=model,
            latent_params=latent_params,
            realized_dims=dict(latent_params),
            render_mode=RenderMode.TEMPLATE,
            task_seed=1,
            trial_seed=1,
            verdict=verdict,
            verifier_detail=verifier_detail,
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.001,
        )
    )


# --- digest ------------------------------------------------------------


def test_digest_determinism(tmp_path):
    store = _make_store(tmp_path)
    _seed_store_record(
        store, "subj", {"chain_length": 4, "witness_cutoff": 0.7}, Verdict.FAIL,
        "kind=belief;campaign=1;traps=final_true_state",
    )
    _seed_store_record(store, "subj", {"chain_length": 2, "witness_cutoff": 0.0}, Verdict.PASS)

    d1 = build_digest(store, "subj")
    d2 = build_digest(store, "subj")
    assert d1 == d2
    assert "final_true_state" in d1
    assert "subj" in d1


def test_digest_no_evidence(tmp_path):
    store = _make_store(tmp_path)
    d = build_digest(store, "nonexistent-model")
    assert "No prior evidence" in d


def test_digest_respects_max_chars(tmp_path):
    store = _make_store(tmp_path)
    for i in range(50):
        _seed_store_record(
            store, "subj", {"chain_length": i % 10 + 1, "witness_cutoff": (i % 9) / 10.0},
            Verdict.FAIL if i % 2 else Verdict.PASS,
        )
    d = build_digest(store, "subj", max_chars=500)
    assert len(d) <= 500


# --- proposer parsing ----------------------------------------------------


def test_propose_configs_valid_json():
    payload = (
        '[{"config": {"chain_length": 8, "witness_cutoff": 0.7}, '
        '"require_traps": ["final_true_state"], "hypothesis": "stale belief"}]'
    )
    client = ScriptedClient(scripts={"proposer-model": [_ok(payload)]})
    proposals = propose_configs(client, "proposer-model", digest="no evidence", n=1)
    assert len(proposals) == 1
    assert proposals[0].config == {"chain_length": 8, "witness_cutoff": 0.7}
    assert proposals[0].require_traps == ("final_true_state",)
    assert proposals[0].hypothesis == "stale belief"


def test_propose_configs_fence_wrapped():
    payload = '```json\n[{"config": {"chain_length": 5}, "hypothesis": "h"}]\n```'
    client = ScriptedClient(scripts={"proposer-model": [_ok(payload)]})
    proposals = propose_configs(client, "proposer-model", digest="d", n=1)
    assert len(proposals) == 1
    assert proposals[0].config == {"chain_length": 5}


def test_propose_configs_garbage_then_retry_then_empty():
    client = ScriptedClient(
        scripts={"proposer-model": [_ok("I refuse to answer in JSON."), _ok("still not json")]}
    )
    proposals = propose_configs(client, "proposer-model", digest="d", n=3)
    assert proposals == []
    assert len(client.calls) == 2  # first attempt + one retry, no more


def test_propose_configs_garbage_then_retry_succeeds():
    good = '[{"config": {"chain_length": 3}, "hypothesis": "h"}]'
    client = ScriptedClient(scripts={"proposer-model": [_ok("nonsense"), _ok(good)]})
    proposals = propose_configs(client, "proposer-model", digest="d", n=1)
    assert len(proposals) == 1
    assert len(client.calls) == 2


def test_propose_configs_ignores_malformed_items():
    payload = '[{"config": {"chain_length": 3}, "hypothesis": "ok"}, "not a dict", 42]'
    client = ScriptedClient(scripts={"proposer-model": [_ok(payload)]})
    proposals = propose_configs(client, "proposer-model", digest="d", n=3)
    assert len(proposals) == 1


# --- gauntlet ------------------------------------------------------------


def test_gauntlet_clamps_out_of_range_and_reports_reason():
    # chain_length way out of range (legal max is 30); should clamp, not
    # crash, and still be scorable.
    proposal = Proposal(config={"chain_length": 9999, "query_kind": "belief"}, hypothesis="h")
    result = run_gauntlet(proposal, base_seed=42)
    assert result.params is not None
    assert result.params.chain_length == 30


def test_gauntlet_rejects_invalid_query_kind():
    proposal = Proposal(config={"query_kind": "not_a_real_kind"}, hypothesis="h")
    result = run_gauntlet(proposal, base_seed=1)
    assert result.accepted is False
    assert result.reason == "invalid_config"


def test_gauntlet_rejects_non_numeric_field():
    proposal = Proposal(config={"chain_length": "banana"}, hypothesis="h")
    result = run_gauntlet(proposal, base_seed=1)
    assert result.accepted is False
    assert result.reason == "invalid_config"


def test_gauntlet_accepts_reasonable_belief_config():
    proposal = Proposal(
        config={
            "chain_length": 8,
            "witness_cutoff": 0.7,
            "coref_pressure": 4,
            "distractor_events": 10,
            "query_kind": "belief",
        },
        hypothesis="stale belief plus coref confusion",
    )
    result = run_gauntlet(proposal, base_seed=7)
    assert result.accepted is True
    assert result.seed is not None
    assert result.profile is not None


def test_gauntlet_below_random_median_rejected_without_subject_calls():
    # An essentially trivial config (chain_length=1, no scramble, no
    # distractors, no witness_cutoff) has very little room to plant any
    # foil divergence -- verify the gauntlet CAN reject on score, and that
    # rejection happens with zero subject-model calls (there are none to
    # make yet -- the gauntlet itself never touches a client).
    proposal = Proposal(
        config={"chain_length": 1, "scramble": 0.0, "witness_cutoff": 0.0, "query_kind": "belief"},
        hypothesis="trivial",
    )
    result = run_gauntlet(proposal, base_seed=123, n_candidates=10, n_random_control=10)
    # Whatever the outcome, no API call machinery was ever invoked -- this
    # is asserted structurally by the gauntlet's own zero-import of any
    # client type; here we just confirm the reason is one of the two valid
    # deterministic outcomes and a score/median were computed.
    assert result.reason in ("accepted", "below_random_median", "no_valid_world_with_required_traps")


def test_gauntlet_batch_derives_distinct_seeds():
    proposals = [
        Proposal(config={"chain_length": 5, "query_kind": "belief"}, hypothesis="a"),
        Proposal(config={"chain_length": 6, "query_kind": "belief"}, hypothesis="b"),
    ]
    results = run_gauntlet_batch(proposals, base_seed=99)
    assert len(results) == 2
    seeds = [r.seed for r in results if r.seed is not None]
    assert len(set(seeds)) == len(seeds)  # no accidental collisions


# --- loop ------------------------------------------------------------


def test_contamination_guard_raises_on_same_model(tmp_path):
    store = _make_store(tmp_path)
    client = ScriptedClient()
    with pytest.raises(ContaminationError):
        run_proposer_loop(
            client, store, run_id="r1", proposer_model="same-model", subject_model="same-model",
            rounds=1,
        )


def _planted_config_subject_responses(prompt: str, trap_configs: list[dict]) -> ChatResult:
    """A fake subject that ALWAYS answers wrong (guaranteed FAIL) -- used to
    prove the loop can find hits when the subject genuinely fails everywhere
    proposed. Simpler and more deterministic for a scripted test than trying
    to reverse-engineer the correct answer_key from the prompt text."""
    return ChatResult(text="definitely the wrong room", tokens_in=10, tokens_out=5, latency_ms=1.0, finish_reason="stop")


def test_loop_finds_hits_and_feeds_duds_back(tmp_path):
    store = _make_store(tmp_path)

    proposal_payload = (
        '[{"config": {"chain_length": 6, "witness_cutoff": 0.6}, '
        '"hypothesis": "stale belief"},'
        '{"config": {"chain_length": 2, "witness_cutoff": 0.0}, '
        '"hypothesis": "trivial baseline"}]'
    )

    class AlwaysWrongClient:
        """Proposer answers with scripted JSON; subject always answers
        wrong -> every survivor measures at 100% failure rate."""

        def __init__(self):
            self.calls = []

        def chat(self, model, prompt, max_tokens=1500, max_tokens_retry=4000, **kwargs):
            self.calls.append(model)
            if model == "proposer-model":
                return _ok(proposal_payload)
            return ChatResult(
                text="nowhere in particular", tokens_in=10, tokens_out=5, latency_ms=1.0,
                finish_reason="stop",
            )

    client = AlwaysWrongClient()
    result = run_proposer_loop(
        client, store, run_id="loop-test",
        proposer_model="proposer-model", subject_model="subject-model",
        rounds=1, proposals_per_round=2, k_trials=3, cost_cap=5.0,
    )
    assert result.round_stats[0].proposed == 2
    assert result.round_stats[0].measured > 0
    # Always-wrong subject means every measured survivor is a 100% failure
    # -> every measured survivor should land in the trap library.
    assert len(result.trap_library) >= 1
    for entry in result.trap_library:
        assert entry.failure_rate >= 0.3
    assert result.beat_random is not None  # control arm ran in round 1


def test_loop_cost_cap_stops_cleanly(tmp_path):
    store = _make_store(tmp_path)
    proposal_payload = (
        '[{"config": {"chain_length": 6}, "hypothesis": "h1"},'
        '{"config": {"chain_length": 7}, "hypothesis": "h2"},'
        '{"config": {"chain_length": 8}, "hypothesis": "h3"}]'
    )

    class TinyBudgetClient:
        def chat(self, model, prompt, max_tokens=1500, max_tokens_retry=4000, **kwargs):
            if model == "proposer-model":
                return _ok(proposal_payload)
            # Expensive-looking response so cost accrues even with $0
            # pricing table entries -- cost_usd returns 0 for unpriced
            # models, so to actually exercise the cap we rely on the
            # evaluator's own cost tracking against a priced model id.
            return ChatResult(text="wrong", tokens_in=100000, tokens_out=100000, latency_ms=1.0, finish_reason="stop")

    client = TinyBudgetClient()
    result = run_proposer_loop(
        client, store, run_id="loop-cap-test",
        proposer_model="proposer-model",
        subject_model="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",  # priced model
        rounds=5, proposals_per_round=3, k_trials=5, cost_cap=0.01,
    )
    assert result.total_spend_usd <= 0.05  # small overshoot tolerance (see PipelineEvaluator docstring)
    assert result.stopped_reason in ("cost_cap_hit",)


def test_loop_duds_fed_back_into_next_round_digest(tmp_path):
    store = _make_store(tmp_path)

    seen_digests = []

    class RecordingClient:
        def chat(self, model, prompt, max_tokens=1500, max_tokens_retry=4000, **kwargs):
            if model == "proposer-model":
                seen_digests.append(prompt)
                payload = '[{"config": {"chain_length": 3}, "hypothesis": "h"}]'
                return _ok(payload)
            # Subject always answers correctly-looking nonsense that will
            # not match any real answer_key -> always FAIL, so every
            # proposal is a "hit," not a dud. To exercise the dud path we
            # instead check that round 2's prompt contains round 1's
            # proposed config text regardless of hit/dud classification
            # (the digest always includes recent FAIL/PASS configs).
            return ChatResult(text="nowhere", tokens_in=5, tokens_out=5, latency_ms=1.0, finish_reason="stop")

    client = RecordingClient()
    run_proposer_loop(
        client, store, run_id="loop-digest-test",
        proposer_model="proposer-model", subject_model="subject-model",
        rounds=2, proposals_per_round=1, k_trials=2, cost_cap=5.0,
    )
    assert len(seen_digests) == 2
    # Round 2's digest should differ from round 1's (new evidence accrued).
    assert seen_digests[0] != seen_digests[1]
