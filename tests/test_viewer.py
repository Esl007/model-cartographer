"""Tests for the local results viewer's pure HTML builders.

No real server is started -- `index_html`, `run_html`, `record_html` are
plain functions over a `ResultsStore`, tested directly.
"""

from __future__ import annotations

from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.viewer.atlas import atlas_html
from cartographer.viewer.serve import index_html, live_html, record_html, run_html


def _seed_store(tmp_path):
    store = ResultsStore(tmp_path / "viewer_test.sqlite")

    r1 = ExperimentRecord(
        run_id="run-a",
        model_name="model-x",
        latent_params={"chain_length": 4.0},
        realized_dims={"entity_state": 4.0, "distractors": 2.0},
        render_mode=RenderMode.TEMPLATE,
        task_seed=1,
        trial_seed=1,
        verdict=Verdict.PASS,
        prompt_text="Where is the box?",
        response_text="the kitchen",
        verifier_detail="kind=state;sim=same_category",
        tokens_in=10,
        tokens_out=5,
        cost_usd=0.001,
        latency_ms=120.0,
        finish_reason="stop",
    )
    r2 = ExperimentRecord(
        run_id="run-a",
        model_name="model-x",
        latent_params={"chain_length": 4.0},
        realized_dims={"entity_state": 4.0, "distractors": 2.0},
        render_mode=RenderMode.TEMPLATE,
        task_seed=2,
        trial_seed=2,
        verdict=Verdict.FAIL,
        prompt_text="Where is the folder?",
        response_text="<script>alert('xss')</script> the basement",
        verifier_detail="kind=state;sim=unrelated",
        tokens_in=8,
        tokens_out=4,
        cost_usd=0.0008,
        latency_ms=90.0,
        finish_reason="stop",
    )
    r3 = ExperimentRecord(
        run_id="run-a",
        model_name="model-y",
        latent_params={"chain_length": 8.0},
        realized_dims={"entity_state": 8.0, "distractors": 5.0},
        render_mode=RenderMode.TEMPLATE,
        task_seed=3,
        trial_seed=3,
        verdict=Verdict.ERROR,
        prompt_text="Where does Marcus believe the box is?",
        response_text="",
        verifier_detail="kind=belief;sim=near_duplicate;error=timeout",
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        latency_ms=0.0,
        reasoning_text="I considered the last move and the earlier claim by Elena.",
        finish_reason="length",
    )
    store.append_many([r1, r2, r3])
    return store, r1, r2, r3


def test_index_html_shows_verdict_tallies(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = index_html(store)

    assert "run-a" in page
    assert '<a href="/run/run-a">run-a</a>' in page
    # 3 total records, 1 pass, 1 fail, 1 error
    assert ">3<" in page
    assert ">1<" in page  # appears for each of pass/fail/error counts


def test_run_html_lists_records_with_links(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = run_html(store, "run-a")

    assert "model-x" in page
    assert "model-y" in page
    assert f"/record/{r1.record_id}" in page
    assert f"/record/{r2.record_id}" in page
    assert f"/record/{r3.record_id}" in page
    assert "PASS" in page
    assert "FAIL" in page
    assert "ERROR" in page
    # query kind parsed from verifier_detail
    assert "state" in page
    assert "belief" in page


def test_run_html_unknown_run_id_no_records(tmp_path):
    store, *_ = _seed_store(tmp_path)
    page = run_html(store, "does-not-exist")
    assert "No records found" in page


def test_record_html_escapes_script_in_response_text(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = record_html(store, r2.record_id)

    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "alert(" in page  # content still present, just escaped


def test_record_html_contains_reasoning_inside_details(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = record_html(store, r3.record_id)

    assert "<details>" in page
    assert "Model reasoning" in page
    assert "I considered the last move" in page
    # reasoning text should appear between the <details> tags
    details_start = page.index("<details>")
    details_end = page.index("</details>")
    assert details_start < page.index("I considered the last move") < details_end


def test_record_html_no_reasoning_shows_placeholder(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = record_html(store, r1.record_id)

    assert "no reasoning captured" in page
    assert "<details>" not in page


def test_record_html_shows_realized_and_latent_tables(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = record_html(store, r1.record_id)

    assert "entity_state" in page
    assert "chain_length" in page


def test_record_html_unknown_record_id(tmp_path):
    store, *_ = _seed_store(tmp_path)
    page = record_html(store, "not-a-real-id")
    assert "Not found" in page


# ---------------------------------------------------------------------------
# /live -- live campaign monitor
# ---------------------------------------------------------------------------


def _live_times(age_s: float) -> list[str]:
    """Three timestamps 30s apart whose NEWEST is `age_s` seconds old."""
    from datetime import datetime, timedelta, timezone

    newest = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return [
        (newest - timedelta(seconds=60)).isoformat(),
        (newest - timedelta(seconds=30)).isoformat(),
        newest.isoformat(),
    ]


def _seed_live_store(tmp_path, run_id="camp-belief-test-001", newest_age_s=10.0):
    store = ResultsStore(tmp_path / "live_test.sqlite")
    records = []
    times = _live_times(newest_age_s)
    verdicts = [Verdict.PASS, Verdict.FAIL, Verdict.PASS]
    for i, (t, v) in enumerate(zip(times, verdicts)):
        records.append(
            ExperimentRecord(
                run_id=run_id,
                model_name="model-x",
                latent_params={"chain_length": 4.0},
                realized_dims={"chain_length": float(2 + i)},
                render_mode=RenderMode.TEMPLATE,
                task_seed=i,
                trial_seed=i,
                verdict=v,
                verifier_detail="kind=belief;campaign=1",
                tokens_in=10,
                tokens_out=5,
                cost_usd=0.001,
                latency_ms=100.0,
                finish_reason="stop",
                created_at=t,
            )
        )
    store.append_many(records)
    return store, records


def test_live_html_shows_progress_and_tallies(tmp_path):
    store, records = _seed_live_store(tmp_path)
    page = live_html(store)

    assert "camp-belief-test-001" in page
    # 3 records / 700 budget = 0.4% (rounds to 0.4)
    assert "0.4%" in page
    assert "PASS" in page
    assert "FAIL" in page


def test_live_html_newest_first_ordering(tmp_path):
    store, records = _seed_live_store(tmp_path)
    page = live_html(store)

    # newest record's timestamp should appear before the oldest's.
    newest_ts = records[-1].created_at
    oldest_ts = records[0].created_at
    assert page.index(newest_ts) < page.index(oldest_ts)


def test_live_html_unknown_prefix_no_crash(tmp_path):
    store, _ = _seed_live_store(tmp_path, run_id="mystery-run-001")
    page = live_html(store)

    assert "mystery-run-001" in page
    assert "3 records" in page


def test_live_html_no_records_graceful(tmp_path):
    store = ResultsStore(tmp_path / "empty_live.sqlite")
    page = live_html(store)
    assert "No records found" in page


def test_live_html_recent_records_render_live_state_with_eta(tmp_path):
    store, _ = _seed_live_store(tmp_path, newest_age_s=10.0)
    page = live_html(store)

    assert "RUN COMPLETE" not in page
    assert "ETA:" in page
    assert 'http-equiv="refresh"' in page


def test_live_html_stale_records_render_complete_state(tmp_path):
    # newest record is 10 minutes old -> the run is finished, not stuck.
    store, _ = _seed_live_store(tmp_path, newest_age_s=600.0)
    page = live_html(store)

    assert "RUN COMPLETE" in page
    assert "finished at 3 calls" in page
    assert "ETA:" not in page
    # full-width green bar, tallies kept, no auto-refresh.
    assert "width:100%" in page and "#157a15" in page
    assert "PASS: 2" in page and "FAIL: 1" in page
    assert 'http-equiv="refresh"' not in page


def test_live_html_stale_unknown_budget_still_complete(tmp_path):
    store, _ = _seed_live_store(tmp_path, run_id="mystery-run-002", newest_age_s=600.0)
    page = live_html(store)
    assert "RUN COMPLETE" in page
    assert "finished at 3 calls" in page


# ---------------------------------------------------------------------------
# /atlas -- cross-model comparison
# ---------------------------------------------------------------------------


def _seed_atlas_store(tmp_path):
    store = ResultsStore(tmp_path / "atlas_test.sqlite")
    records = []
    # Model A: passes at every chain_length level.
    for i, cl in enumerate([2.0, 6.0, 10.0, 14.0, 18.0]):
        records.append(
            ExperimentRecord(
                run_id="camp-belief-modelA-001",
                model_name="nvidia/Model-A",
                latent_params={"chain_length": cl},
                realized_dims={"chain_length": cl, "scramble": 0.0},
                render_mode=RenderMode.TEMPLATE,
                task_seed=i,
                trial_seed=i,
                verdict=Verdict.PASS,
                verifier_detail="kind=belief;campaign=1",
                tokens_in=5,
                tokens_out=5,
                cost_usd=0.002,
                latency_ms=50.0,
                finish_reason="stop",
            )
        )
    # Model B: passes at low chain_length, fails at high chain_length.
    for i, cl in enumerate([2.0, 6.0, 10.0, 14.0, 18.0]):
        verdict = Verdict.PASS if cl < 10.0 else Verdict.FAIL
        records.append(
            ExperimentRecord(
                run_id="camp-belief-modelB-001",
                model_name="nvidia/Model-B",
                latent_params={"chain_length": cl},
                realized_dims={"chain_length": cl, "scramble": 0.0},
                render_mode=RenderMode.TEMPLATE,
                task_seed=100 + i,
                trial_seed=100 + i,
                verdict=verdict,
                verifier_detail="kind=belief;campaign=1",
                tokens_in=5,
                tokens_out=5,
                cost_usd=0.003,
                finish_reason="stop",
                latency_ms=50.0,
            )
        )
    store.append_many(records)
    return store, records


def test_atlas_html_shows_models_svg_and_summary(tmp_path):
    store, records = _seed_atlas_store(tmp_path)
    page = atlas_html(store)

    assert "Model-A" in page
    assert "Model-B" in page
    assert "<svg" in page
    # summary table rows: 100% pass rate for A, 40% for B (2/5 pass: cl<10)
    assert "100.0%" in page
    assert "40.0%" in page


def test_atlas_html_empty_db_graceful(tmp_path):
    store = ResultsStore(tmp_path / "empty_atlas.sqlite")
    page = atlas_html(store)
    assert "No campaign data" in page
