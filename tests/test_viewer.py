"""Tests for the local results viewer's pure HTML builders.

No real server is started -- `index_html`, `run_html`, `record_html` are
plain functions over a `ResultsStore`, tested directly.
"""

from __future__ import annotations

from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.viewer.serve import index_html, record_html, run_html


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
