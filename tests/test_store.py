"""Acceptance tests for the Phase 0.5 results store."""

from __future__ import annotations

import json

import pytest

from cartographer.store import (
    ExperimentRecord,
    InstrumentedSyntheticRunner,
    Mutation,
    RenderMode,
    ResultsStore,
    Verdict,
)
from cartographer.synthetic import ZOO
from cartographer.synthetic.recovery import bisection_strategy


def _full_record(**overrides) -> ExperimentRecord:
    fields = dict(
        run_id="run-1",
        model_name="clean_threshold",
        latent_params={"temporal": 5.0},
        realized_dims={"temporal": 5.2},
        render_mode=RenderMode.NEMOTRON,
        task_seed=1,
        trial_seed=2,
        verdict=Verdict.PASS,
        wave_id=3,
        parent_id="parent-abc",
        mutation=Mutation(factor="temporal", from_value=4.0, to_value=5.0),
        renderer_model="nemotron-70b",
        rendering_seed=9,
        prompt_text="prompt",
        response_text="response",
        verifier_detail="details",
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.05,
        latency_ms=123.4,
    )
    fields.update(overrides)
    return ExperimentRecord(**fields)


def test_round_trip(tmp_path) -> None:
    store = ResultsStore(tmp_path / "db.sqlite3")
    record = _full_record()
    store.append(record)
    fetched = store.by_run("run-1")
    assert len(fetched) == 1
    assert fetched[0].to_json_dict() == record.to_json_dict()


def test_append_only(tmp_path) -> None:
    store = ResultsStore(tmp_path / "db.sqlite3")
    record = _full_record()
    store.append(record)
    dup = _full_record()
    object.__setattr__(dup, "record_id", record.record_id)
    with pytest.raises(Exception):
        store.append(dup)
    assert not hasattr(store, "update")
    assert not hasattr(store, "delete")


def test_lineage(tmp_path) -> None:
    store = ResultsStore(tmp_path / "db.sqlite3")
    root = _full_record(parent_id=None, mutation=None)
    a = _full_record(
        parent_id=root.record_id,
        mutation=Mutation("temporal", 5.0, 6.0),
    )
    b = _full_record(
        parent_id=a.record_id,
        mutation=Mutation("temporal", 6.0, 7.0),
    )
    c = _full_record(
        parent_id=b.record_id,
        mutation=Mutation("temporal", 7.0, 8.0),
    )
    store.append_many([root, a, b, c])

    chain = store.lineage(c.record_id)
    assert [r.record_id for r in chain] == [
        root.record_id,
        a.record_id,
        b.record_id,
        c.record_id,
    ]
    assert chain[0].mutation is None
    assert chain[1].mutation == Mutation("temporal", 5.0, 6.0)
    assert chain[2].mutation == Mutation("temporal", 6.0, 7.0)
    assert chain[3].mutation == Mutation("temporal", 7.0, 8.0)


def test_dim_region_query(tmp_path) -> None:
    store = ResultsStore(tmp_path / "db.sqlite3")
    records = []
    expected_ids = []
    for i in range(20):
        temporal = float(i)
        record = _full_record(
            run_id="run-region",
            realized_dims={"temporal": temporal, "distractors": 1.0},
            mutation=None,
            parent_id=None,
        )
        records.append(record)
        if 4 <= temporal <= 8:
            expected_ids.append(record.record_id)
    store.append_many(records)

    result = store.in_dim_region({"temporal": (4, 8)}, run_id="run-region")
    assert sorted(r.record_id for r in result) == sorted(expected_ids)


def test_cost_summary(tmp_path) -> None:
    store = ResultsStore(tmp_path / "db.sqlite3")
    records = [
        _full_record(
            run_id="run-cost",
            verdict=Verdict.PASS,
            cost_usd=1.0,
            tokens_in=10,
            tokens_out=5,
            mutation=None,
            parent_id=None,
        ),
        _full_record(
            run_id="run-cost",
            verdict=Verdict.FAIL,
            cost_usd=2.5,
            tokens_in=20,
            tokens_out=8,
            mutation=None,
            parent_id=None,
        ),
        _full_record(
            run_id="run-cost",
            verdict=Verdict.PASS,
            cost_usd=0.5,
            tokens_in=1,
            tokens_out=1,
            mutation=None,
            parent_id=None,
        ),
    ]
    store.append_many(records)

    summary = store.cost_summary(run_id="run-cost")
    assert summary["total_cost_usd"] == pytest.approx(4.0)
    assert summary["total_tokens_in"] == 31
    assert summary["total_tokens_out"] == 14
    assert summary["n_records"] == 3
    assert summary["n_by_verdict"] == {"PASS": 2, "FAIL": 1}


def test_jsonl_export(tmp_path) -> None:
    store = ResultsStore(tmp_path / "db.sqlite3")
    records = [
        _full_record(run_id="run-export", mutation=None, parent_id=None)
        for _ in range(5)
    ]
    store.append_many(records)

    out_path = tmp_path / "export.jsonl"
    store.export_jsonl(out_path, run_id="run-export")

    lines = out_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 5
    parsed = [ExperimentRecord.from_json_dict(json.loads(line)) for line in lines]
    expected_ids = sorted(r.record_id for r in records)
    assert sorted(r.record_id for r in parsed) == expected_ids
    by_id = {r.record_id: r for r in records}
    for parsed_record in parsed:
        assert parsed_record.to_json_dict() == by_id[parsed_record.record_id].to_json_dict()


def test_end_to_end_replay(tmp_path) -> None:
    store = ResultsStore(tmp_path / "db.sqlite3")
    model = ZOO["clean_threshold"]
    run_id = "run-replay"
    runner = InstrumentedSyntheticRunner(store=store, model=model, run_id=run_id)

    strategy = bisection_strategy()
    estimate = strategy(runner, "temporal", (0.0, 20.0), 20)

    records = store.by_run(run_id)
    assert len(records) == 20

    for record in records:
        replayed = model.evaluate(record.realized_dims, record.trial_seed)
        expected_verdict = Verdict.PASS if replayed else Verdict.FAIL
        assert record.verdict == expected_verdict

    assert abs(estimate - 6.0) < 0.1
