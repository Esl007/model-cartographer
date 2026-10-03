"""frontier_summary: statuses, boundaries in native units, causes, exemplars,
hygiene inheritance -- on synthetic stores with known answers."""

from __future__ import annotations

from cartographer.report.frontier import frontier_summary
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore

M = "nvidia/test-model"


def _rec(run_id, verdict, seed, latent=None, realized=None, detail="", kind="tool_plan"):
    return ExperimentRecord(
        run_id=run_id, model_name=M, latent_params=latent or {}, realized_dims=realized or {},
        render_mode=RenderMode.TEMPLATE, task_seed=seed, trial_seed=seed, rendering_seed=seed,
        prompt_text="p", response_text="r", verdict=verdict,
        verifier_detail=f"kind={kind};campaign=1{detail}", tokens_in=1, tokens_out=1,
        cost_usd=0.0, latency_ms=1.0,
    )


def _depth_level(recs, pd, n_pass, n_fail, seed0, run="camp-tool-deep-test-001", code="unknown_tool:step=3:x"):
    lat = {"plan_depth": float(pd), "id_collisions_on": 1.0}
    real = {"realized_plan_depth": float(pd + 3)}
    for i in range(n_pass):
        recs.append(_rec(run, Verdict.PASS, seed0 + i, lat, real))
    for i in range(n_fail):
        recs.append(_rec(run, Verdict.FAIL, seed0 + 100 + i, lat, real, f";detail={code}"))


def _row(store, key):
    return next(r for r in frontier_summary(store, M).rows if r.key == key)


def test_axis_row_breaks_with_boundary_in_native_units(tmp_path):
    store = ResultsStore(tmp_path / "s.sqlite")
    recs: list = []
    _depth_level(recs, 4, 10, 0, 1)
    _depth_level(recs, 10, 9, 1, 1000)
    _depth_level(recs, 16, 4, 6, 2000)
    _depth_level(recs, 22, 1, 9, 3000)
    store.append_many(recs)
    row = _row(store, "plan_length_lookalike")
    assert row.status == "BREAKS"
    assert row.boundary == "25-call plans"
    assert row.held_through == "13-call plans"
    assert (row.n_pass, row.n_scored) == (1, 10)
    assert row.cause_code == "unknown_tool" and row.cause_share == 1.0
    assert row.exemplar["detail"].startswith("unknown_tool")
    # 19/20 before vs 1/10 at the break: overwhelming, but grade A also needs
    # >= 15 trials at/after the boundary, so this is B.
    assert row.confidence == "B" and row.p_value < 0.001
    assert [c["label"] for c in row.curve] == ["7-call plans", "13-call plans", "19-call plans", "25-call plans"]
    store.close()


def test_axis_row_holds_and_reports_ceiling(tmp_path):
    store = ResultsStore(tmp_path / "s.sqlite")
    recs: list = []
    _depth_level(recs, 4, 10, 0, 1)
    _depth_level(recs, 16, 9, 1, 500)
    store.append_many(recs)
    row = _row(store, "plan_length_lookalike")
    assert row.status == "HOLDS" and row.held_through == "19-call plans"
    store.close()


def test_rate_rows_are_statistical_not_strict(tmp_path):
    store = ResultsStore(tmp_path / "s.sqlite")
    recs = [_rec("camp-x-001", Verdict.PASS, i, kind="state") for i in range(30)]
    recs += [_rec("camp-x-001", Verdict.PASS, 100 + i, kind="belief") for i in range(90)]
    recs += [_rec("camp-x-001", Verdict.FAIL, 300 + i, kind="belief") for i in range(10)]
    store.append_many(recs)
    assert _row(store, "state").status == "HOLDS"  # 30/30: CI dips below 0.9 but is not below 0.95
    assert _row(store, "belief").status == "DEGRADES"  # 90/100: CI upper < 0.95
    store.close()


def test_untested_and_too_few_are_distinct(tmp_path):
    store = ResultsStore(tmp_path / "s.sqlite")
    store.append_many([_rec("camp-x-001", Verdict.PASS, 1, kind="order")])
    assert _row(store, "order").headline == "Too few trials to call"
    assert _row(store, "catalog_size").headline == "Not tested yet"
    assert _row(store, "consistency").status == "NOT TESTED"
    store.close()


def test_trap_count_falls_back_to_verifier_detail(tmp_path):
    """Pre-provenance-fix records carry trap settings only in verifier_detail."""
    store = ResultsStore(tmp_path / "s.sqlite")
    on = ";traps=poison_traps=1;dishonest_decoys_on=1;implicit_preconditions_on=1;zero_slack_on=1;id_collisions_on=1"
    recs = [_rec("camp-tool_bp-super-001", Verdict.PASS, i, {"plan_depth": 3.0}, {}, on) for i in range(6)]
    store.append_many(recs)
    row = _row(store, "traps")
    assert row.held_through == "5 of 5 traps active"
    store.close()


def test_invalidated_runs_never_reach_the_summary(tmp_path):
    from cartographer.viewer.findings import INVALIDATED_RUNS

    store = ResultsStore(tmp_path / "s.sqlite")
    bad = next(iter(INVALIDATED_RUNS))
    store.append_many([_rec(bad, Verdict.FAIL, i, kind="belief") for i in range(20)])
    assert _row(store, "belief").status == "NOT TESTED"
    store.close()


def test_verdict_sentence_names_the_break(tmp_path):
    store = ResultsStore(tmp_path / "s.sqlite")
    recs: list = []
    _depth_level(recs, 4, 10, 0, 1)
    _depth_level(recs, 22, 1, 9, 3000)
    store.append_many(recs)
    v = frontier_summary(store, M).verdict
    assert "breaks on long plans with look-alike tool names (25-call plans)" in v
    store.close()
