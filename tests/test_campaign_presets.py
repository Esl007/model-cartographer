"""Tests for the new campaign wiring in cartographer.exec.evaluator:
witness_cutoff in the belief campaign, the order campaign preset, and the
breaking-point campaign. All chat calls go through scripted fakes; zero
network.
"""

from __future__ import annotations

import json
import sqlite3

from cartographer.exec.client import ChatResult
from cartographer.exec.evaluator import (
    PipelineEvaluator,
    _apply_config,
    _belief_scheduler_config,
    _default_base,
    _order_scheduler_config,
    default_belief_ray,
    run_breaking_point_campaign,
    run_order_campaign,
)
from cartographer.store.store import ResultsStore


class ScriptedClient:
    """Fake client returning a fixed (wrong) answer for every chat call."""

    def __init__(self, text="definitely the wrong place"):
        self.text = text
        self.calls = []

    def chat(self, model, prompt, max_tokens=1500, **kwargs):
        self.calls.append((model, prompt, max_tokens))
        return ChatResult(
            text=self.text, tokens_in=10, tokens_out=5, latency_ms=1.0,
            finish_reason="stop",
        )


# --- Task 1: witness_cutoff in the belief campaign --------------------------


def test_belief_config_sweeps_witness_cutoff():
    cfg = _belief_scheduler_config(budget=100)
    by_name = {d.name: d for d in cfg.dims}
    assert "witness_cutoff" in by_name
    spec = by_name["witness_cutoff"]
    assert spec.lo == 0.0
    assert spec.hi == 0.9
    assert spec.is_int is False
    assert cfg.baseline["witness_cutoff"] == 0.0
    assert cfg.ceiling["witness_cutoff"] == 0.9


def test_apply_config_accepts_witness_cutoff_as_float():
    base = _default_base("belief")
    params = _apply_config(base, {"witness_cutoff": 0.45})
    assert params.witness_cutoff == 0.45
    assert isinstance(params.witness_cutoff, float)


def test_witness_cutoff_flows_into_dimparams_and_records(tmp_path):
    db = tmp_path / "db.sqlite3"
    store = ResultsStore(db)
    client = ScriptedClient()
    ev = PipelineEvaluator(client, "m", store, "run-wc", "belief", cost_cap_usd=10.0)

    ev({"witness_cutoff": 0.6}, trial_seed=1)
    ev({"chain_length": 4}, trial_seed=2)
    store.close()

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT latent_params FROM experiments WHERE run_id = ? ORDER BY created_at",
        ("run-wc",),
    ).fetchall()
    conn.close()

    assert len(rows) == 2
    lp0 = json.loads(rows[0][0])
    lp1 = json.loads(rows[1][0])
    assert lp0["witness_cutoff"] == 0.6
    assert lp1["witness_cutoff"] == 0.0  # baseline default recorded explicitly


# --- Task 2: order campaign preset ------------------------------------------


def test_order_config_dims_and_ranges():
    cfg = _order_scheduler_config(budget=400)
    specs = {d.name: (d.lo, d.hi, d.is_int) for d in cfg.dims}
    assert specs == {
        "chain_length": (2, 20, True),
        "scramble": (0.0, 1.0, False),
        "distractor_events": (2, 30, True),
        "filler_events": (2, 40, True),
    }
    assert cfg.k_trials == 5
    assert cfg.baseline == {d.name: d.lo for d in cfg.dims}
    assert cfg.ceiling == {d.name: d.hi for d in cfg.dims}


def test_run_order_campaign_mocked(tmp_path):
    db = tmp_path / "db.sqlite3"
    store = ResultsStore(db)
    client = ScriptedClient()
    state = run_order_campaign(
        client, store, run_id="run-order", model="m", budget=25,
        cost_cap=10.0, parallelism=1,
    )
    store.close()
    assert state.budget_spent == 25

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT verifier_detail, latent_params FROM experiments WHERE run_id = ?",
        ("run-order",),
    ).fetchall()
    conn.close()

    assert len(rows) == 25
    assert all("kind=order" in detail for detail, _ in rows)
    # Explore is walking chain_length first, holding others at order baseline.
    chains = {json.loads(lp)["chain_length"] for _, lp in rows}
    assert len(chains) == 5  # 5 explore levels probed


def test_launch_presets_and_runner_dispatch_include_new_campaigns():
    from cartographer.viewer.launch import _RUNNERS, CAMPAIGN_PRESETS

    assert CAMPAIGN_PRESETS["order"] == {"budget": 400, "cost_cap": 0.5}
    assert CAMPAIGN_PRESETS["breaking_point"] == {"budget": 350, "cost_cap": 0.75}
    assert _RUNNERS["order"] is run_order_campaign
    assert _RUNNERS["breaking_point"] is run_breaking_point_campaign
    assert set(CAMPAIGN_PRESETS) == set(_RUNNERS)


# --- Task 3 wiring: breaking-point campaign ---------------------------------


def test_default_belief_ray_covers_all_six_generator_dims():
    ray = default_belief_ray()
    limits = {d.name: (d.base_value, d.limit_value, d.is_int) for d in ray.dims}
    assert limits == {
        "chain_length": (2, 25, True),
        "scramble": (0.0, 1.0, False),
        "coref_pressure": (0, 8, True),
        "distractor_events": (2, 40, True),
        "filler_events": (2, 60, True),
        "witness_cutoff": (0.0, 0.9, False),
    }


def test_run_breaking_point_campaign_mocked_all_fail(tmp_path):
    # A client that always answers wrongly fails even at t=0: the campaign
    # must return the honest degenerate result, having persisted its probes.
    db = tmp_path / "db.sqlite3"
    store = ResultsStore(db)
    client = ScriptedClient()
    bp = run_breaking_point_campaign(
        client, store, run_id="run-bp", model="m", budget=50, cost_cap=10.0,
    )
    store.close()

    assert bp.found is False
    assert "degenerate" in bp.note
    assert bp.budget_spent == 7  # one k_trials=7 probe at t=0

    conn = sqlite3.connect(str(db))
    n, = conn.execute(
        "SELECT COUNT(*) FROM experiments WHERE run_id = ?", ("run-bp",)
    ).fetchone()
    conn.close()
    assert n == 7
