"""Tests for the local results viewer's pure HTML builders.

No real server is started -- `index_html`, `run_html`, `record_html` are
plain functions over a `ResultsStore`, tested directly.
"""

from __future__ import annotations

import re
import threading

import pytest

from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.viewer import launch as launch_mod
from cartographer.viewer.atlas import atlas_html
from cartographer.viewer.serve import (
    _format_eta,
    _recent_rate_per_min,
    index_html,
    live_html,
    record_html,
    run_html,
    runs_html,
)


@pytest.fixture(autouse=True)
def _clean_launch_state_for_viewer_tests():
    launch_mod._reset_for_tests()
    yield
    launch_mod._reset_for_tests()


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


def test_runs_html_shows_verdict_tallies(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = runs_html(store)

    assert "run-a" in page
    assert '<a href="/run/run-a">run-a</a>' in page
    # 3 total records, 1 pass, 1 fail, 1 error (fail+error combined into
    # the redesigned table's single "pass rate" pill: 1/3 = 33%)
    assert ">3<" in page
    assert "33%" in page


def test_index_html_overview_shows_summary_and_quick_links(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = index_html(store)

    # plain-English description + live summary cards
    assert "Overview" in page
    assert "Total trials" in page
    assert "Total spend" in page
    assert ">3<" in page  # total trials count
    # quick links to every other section
    for href in ("/findings", "/coverage", "/runs", "/live", "/atlas", "/launch"):
        assert f'href="{href}"' in page


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


def test_run_html_shows_unverified_status_and_verify_now_action(tmp_path):
    """Task 2: FAIL rows show a verification status, and an unverified run
    gets a "Verify failures now" action pointing at the /verify route."""
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = run_html(store, "run-a")

    assert "unverified" in page
    assert "Verify" in page and "failure" in page.lower()
    assert 'action="/verify"' in page
    assert f'name="run_id" value="run-a"' in page


def test_run_html_shows_verified_stable_and_noise_status(tmp_path):
    from cartographer.exec.evaluator import VERIFY_SUFFIX

    store, r1, r2, r3 = _seed_store(tmp_path)
    # r2 is the FAIL record; give it 3 verify trials, all still FAIL -> stable.
    for i in range(3):
        store.append(
            ExperimentRecord(
                run_id=f"run-a{VERIFY_SUFFIX}",
                model_name="model-x",
                latent_params=dict(r2.latent_params),
                realized_dims=dict(r2.realized_dims),
                render_mode=RenderMode.TEMPLATE,
                task_seed=r2.task_seed,
                trial_seed=r2.trial_seed,
                prompt_text=r2.prompt_text,
                response_text="still wrong",
                verdict=Verdict.FAIL,
                verifier_detail=f"kind=state;campaign=1;verify=1;verify_of={r2.record_id};sample={i}",
            )
        )

    page = run_html(store, "run-a")
    assert "verified-stable" in page
    # No unverified FAILs remain -- the verify-now action must not render.
    assert 'action="/verify"' not in page


def test_run_html_unknown_run_id_no_records(tmp_path):
    store, *_ = _seed_store(tmp_path)
    page = run_html(store, "does-not-exist")
    assert "No records found" in page


def test_record_html_escapes_script_in_response_text(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = record_html(store, r2.record_id)

    # The shell now ships ONE trusted inline <script> (the chart
    # interaction layer, serve._INSTRUMENT_JS). Strip the shell's own
    # script blocks before asserting, so this test keeps checking exactly
    # what it always checked -- that attacker-controlled response text can
    # never introduce a tag -- instead of silently degrading into "the app
    # contains no JavaScript", which is a different (and now false) claim.
    outside_trusted_scripts = re.sub(r"<script>.*?</script>", "", page, flags=re.DOTALL)
    assert "<script>" not in outside_trusted_scripts
    assert "&lt;script&gt;" in page
    assert "alert(" in page  # content still present, just escaped
    # And specifically: the injected payload is never live markup.
    assert "<script>alert(" not in page


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
    # full-width green complete-state bar, tallies kept, no auto-refresh.
    assert "width:100%" in page and "progress-fill complete" in page
    assert '<div class="stat-num">2</div><div class="stat-label">Pass</div>' in page
    assert '<div class="stat-num">1</div><div class="stat-label">Fail</div>' in page
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


# ---------------------------------------------------------------------------
# TASK 1a -- /live must prefer the actively registered run_id, even with
# zero records, over the newest-record run from an older campaign.
# ---------------------------------------------------------------------------


def test_live_html_prefers_active_run_with_zero_records(tmp_path):
    # An OLDER campaign has records in the store...
    store, _ = _seed_live_store(tmp_path, run_id="camp-belief-old-001", newest_age_s=10.0)
    # ...but a NEW campaign was just registered as the active run and has
    # not written any records yet (world gen / first API call latency).
    launch_mod._CURRENT_RUN = {"run_id": "camp-state-new-002", "thread": None}
    try:
        page = live_html(store)
    finally:
        launch_mod._reset_for_tests()

    assert "camp-state-new-002" in page
    assert "camp-belief-old-001" not in page
    assert "starting" in page.lower()
    assert "0 records" in page


def _append_records(store, run_id, age_s, verdicts=(Verdict.PASS, Verdict.FAIL, Verdict.PASS)):
    """Like `_seed_live_store` but appends into an EXISTING store instead
    of creating its own -- `_seed_live_store` always targets the same
    "live_test.sqlite" filename under `tmp_path`, so calling it twice for
    one test double-connects to (and, if records are re-appended, double-
    inserts into) the same physical file. Building fresh `ExperimentRecord`
    objects directly avoids that."""
    records = []
    for i, (t, v) in enumerate(zip(_live_times(age_s), verdicts)):
        r = ExperimentRecord(
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
        store.append(r)
        records.append(r)
    return records


def test_live_html_full_map_parent_aggregates_stage_children(tmp_path):
    """Regression: a full_map PARENT run_id has no records of its own --
    every trial lives under f"{run_id}/{stage}" child run_ids. Before the
    fix, `live_html` queried the parent id verbatim and showed
    "starting... 0 records" for the campaign's entire real duration even
    though it was actively running and spending money (user report:
    "the launch screen is stuck ... even though the runs are finished").
    """
    parent = "camp-full_map-model-x-001"
    store, _ = _seed_live_store(tmp_path, run_id=f"{parent}/state", newest_age_s=10.0)
    _append_records(store, f"{parent}/belief", age_s=5.0)

    launch_mod._CURRENT_RUN = {"run_id": parent, "thread": None}
    try:
        page = live_html(store)
    finally:
        launch_mod._reset_for_tests()

    assert not ("starting" in page.lower() and "0 records" in page)
    assert 'class="stage-track"' in page
    assert f'/run/{parent}' in page


def test_live_html_shows_verify_in_progress_panel(tmp_path):
    """Regression: "Verify failures now" writes resample trials under
    f"{run_id}/verify" -- never under the original run_id -- so watching
    /live during a verify-now pass showed the OLD, static, already-complete
    campaign numbers with no sign anything was happening (user report:
    "verifying also shows the same screen but the actual process are
    working. The UI is not").
    """
    from cartographer.exec.evaluator import VERIFY_SUFFIX

    run_id = "camp-belief-model-x-001"
    store, _ = _seed_live_store(tmp_path, run_id=run_id, newest_age_s=600.0)
    _append_records(
        store,
        f"{run_id}{VERIFY_SUFFIX}",
        age_s=2.0,
        verdicts=(Verdict.PASS, Verdict.FAIL),
    )

    launch_mod._CURRENT_RUN = {"run_id": run_id, "thread": None}
    try:
        page = live_html(store)
    finally:
        launch_mod._reset_for_tests()

    assert "Verifying failures" in page
    assert "resamples so far" in page or "resample so far" in page


def test_live_html_falls_back_to_newest_record_when_no_active_run(tmp_path):
    store, _ = _seed_live_store(tmp_path, run_id="camp-belief-test-001", newest_age_s=10.0)
    assert launch_mod.current_run() is None
    page = live_html(store)
    assert "camp-belief-test-001" in page


# ---------------------------------------------------------------------------
# TASK 1b -- ETA must be computed over a recent window, show "estimating..."
# on small samples, and never appear for a completed run.
# ---------------------------------------------------------------------------


def _make_records_at(times: list[str]) -> list[ExperimentRecord]:
    out = []
    for i, t in enumerate(times):
        out.append(
            ExperimentRecord(
                run_id="camp-belief-eta-001",
                model_name="model-x",
                latent_params={},
                realized_dims={"chain_length": float(i)},
                render_mode=RenderMode.TEMPLATE,
                task_seed=i,
                trial_seed=i,
                verdict=Verdict.PASS,
                verifier_detail="kind=belief;campaign=1",
                tokens_in=1,
                tokens_out=1,
                cost_usd=0.0,
                latency_ms=1.0,
                finish_reason="stop",
                created_at=t,
            )
        )
    return out


def test_recent_rate_uses_recent_window_not_whole_run_average(tmp_path):
    from datetime import datetime, timedelta, timezone

    # A long stall (10 min) followed by a fast recent burst (1s apart).
    # The whole-run average would be poisoned by the stall; the recent
    # window should reflect the fast burst instead.
    base = datetime.now(timezone.utc) - timedelta(minutes=15)
    times = [(base + timedelta(seconds=i)).isoformat() for i in range(3)]  # fast start
    stall_start = base + timedelta(seconds=3)
    stall_end = stall_start + timedelta(minutes=10)
    times.append(stall_end.isoformat())
    # Fast recent burst: 20 records, 2s apart, right after the stall.
    for i in range(1, 21):
        times.append((stall_end + timedelta(seconds=2 * i)).isoformat())

    records = _make_records_at(times)
    rate, n = _recent_rate_per_min(records)
    assert rate is not None
    # Fast burst is 2s/record => 30/min, not the ~1.4/min whole-run average.
    assert rate > 15.0
    assert n >= 10


def test_format_eta_estimating_on_small_sample():
    assert _format_eta(remaining=50, rate_per_min=5.0, sample_n=3) == "estimating..."


def test_format_eta_normal_case():
    eta = _format_eta(remaining=50, rate_per_min=10.0, sample_n=25)
    assert "at most" in eta
    assert "5.0 min" in eta


def test_format_eta_caps_huge_projection():
    eta = _format_eta(remaining=10000, rate_per_min=1.0, sample_n=25)
    assert "&gt; 2h" in eta


def test_live_html_completed_run_never_shows_eta(tmp_path):
    store, _ = _seed_live_store(tmp_path, run_id="camp-belief-done-001", newest_age_s=600.0)
    page = live_html(store)
    assert "RUN COMPLETE" in page
    assert "ETA:" not in page


def test_live_html_small_sample_shows_estimating(tmp_path):
    # Only 3 records -- well under the min-sample threshold for a real
    # rate, so the ETA line should read "estimating..." rather than a
    # number.
    store, _ = _seed_live_store(tmp_path, run_id="camp-belief-small-001", newest_age_s=5.0)
    page = live_html(store)
    assert "estimating" in page


# ---------------------------------------------------------------------------
# TASK 2 -- stop button: only renders when a campaign is actively
# registered, and flips to a "stopping..." state once its cancel_event is
# set.
# ---------------------------------------------------------------------------

_STOP_TEST_RUN_ID = "camp-state-active-001"


def test_live_html_no_stop_button_when_no_active_run(tmp_path):
    store, _ = _seed_live_store(tmp_path, run_id="camp-belief-idle-001", newest_age_s=10.0)
    assert launch_mod.current_run() is None
    page = live_html(store)
    assert "Stop campaign" not in page
    assert "stopping" not in page.lower()


def test_live_html_shows_stop_button_when_active(tmp_path):
    store, _ = _seed_live_store(tmp_path, run_id=_STOP_TEST_RUN_ID, newest_age_s=1.0)
    cancel_event = threading.Event()
    launch_mod._CURRENT_RUN = {
        "run_id": _STOP_TEST_RUN_ID, "thread": None, "cancel_event": cancel_event,
    }
    try:
        page = live_html(store)
    finally:
        launch_mod._reset_for_tests()

    assert "Stop campaign" in page
    assert 'action="/stop"' in page


def test_live_html_shows_stopping_state_once_cancel_event_set(tmp_path):
    store, _ = _seed_live_store(tmp_path, run_id=_STOP_TEST_RUN_ID, newest_age_s=1.0)
    cancel_event = threading.Event()
    cancel_event.set()
    launch_mod._CURRENT_RUN = {
        "run_id": _STOP_TEST_RUN_ID, "thread": None, "cancel_event": cancel_event,
    }
    try:
        page = live_html(store)
    finally:
        launch_mod._reset_for_tests()

    assert "stopping" in page.lower()
    assert "Stop campaign" not in page


def test_live_html_zero_records_active_run_shows_stop_button(tmp_path):
    cancel_event = threading.Event()
    launch_mod._CURRENT_RUN = {
        "run_id": "camp-state-fresh-003", "thread": None, "cancel_event": cancel_event,
    }
    store = ResultsStore(tmp_path / "empty.sqlite")
    try:
        page = live_html(store)
    finally:
        launch_mod._reset_for_tests()

    assert "starting" in page.lower()
    assert "Stop campaign" in page


def test_request_stop_sets_event_and_returns_true():
    cancel_event = threading.Event()
    launch_mod._CURRENT_RUN = {"run_id": "camp-x-001", "thread": None, "cancel_event": cancel_event}
    try:
        assert launch_mod.request_stop() is True
        assert cancel_event.is_set()
        assert launch_mod.is_stopping() is True
    finally:
        launch_mod._reset_for_tests()


def test_request_stop_no_active_run_returns_false():
    assert launch_mod.current_run() is None
    assert launch_mod.request_stop() is False


def test_current_run_never_leaks_cancel_event():
    cancel_event = threading.Event()
    launch_mod._CURRENT_RUN = {"run_id": "camp-x-002", "thread": None, "cancel_event": cancel_event}
    try:
        run = launch_mod.current_run()
        assert "cancel_event" not in run
    finally:
        launch_mod._reset_for_tests()


# ---------------------------------------------------------------------------
# Information-architecture redesign: persistent nav, /findings, /coverage,
# /runs split out of the old index, and trap kinds surfaced on run/record
# detail pages.
# ---------------------------------------------------------------------------

_NAV_LABELS = ["Overview", "Findings", "Report", "Coverage", "Runs", "Live", "Atlas", "Launch"]


def test_nav_present_on_every_page(tmp_path):
    from cartographer.viewer.coverage import coverage_html
    from cartographer.viewer.findings import findings_html
    from cartographer.viewer.serve import report_html

    store, r1, r2, r3 = _seed_store(tmp_path)
    pages = [
        index_html(store),
        runs_html(store),
        findings_html(store),
        report_html(store),
        coverage_html(),
        run_html(store, "run-a"),
        record_html(store, r1.record_id),
        live_html(store),
        atlas_html(store),
    ]
    for page in pages:
        for label in _NAV_LABELS:
            assert label in page


def test_report_page_empty_state_when_no_report(tmp_path, monkeypatch):
    """With no generated sidecar, /report renders inside the shell and shows
    the empty state pointing at the generator script."""
    import cartographer.analyst.report_page as rp
    from cartographer.viewer.serve import report_html

    monkeypatch.setattr(rp, "_DEFAULT_SIDECAR", tmp_path / "no-report.json")
    store, _r1, _r2, _r3 = _seed_store(tmp_path)
    page = report_html(store)
    assert "Capability report" in page
    assert "No report yet" in page
    assert "write_report.py" in page
    # rendered inside the shell
    assert "Model Cartographer" in page


def test_findings_page_lists_the_four_modes(tmp_path):
    """The curated 'Documented findings' section survives the move to a
    data-derived page: these are causal results (a pronoun ablation, a
    renderer-bug retraction, a strategic escalation-vs-traps comparison)
    that no aggregate over pass/fail columns can reproduce, so they are
    still rendered verbatim -- now in their own labelled section beneath
    the derived capability map."""
    from cartographer.viewer.findings import findings_html

    store, _r1, _r2, _r3 = _seed_store(tmp_path)
    page = findings_html(store)
    assert "Findings" in page
    assert "Documented findings" in page
    assert "curated &middot; not auto-derived" in page
    # belief -> reality collapse (witness-rendering bug, now corrected)
    assert "collapse" in page.lower()
    # Nano continuity-of-observation
    assert "continuity" in page.lower() and "Nano" in page
    # Ultra protagonist-continuity pronoun bias
    assert "pronoun" in page.lower() and "Ultra" in page
    # Trap-pressure abstention: originally attributed to Ultra
    # (needs-replication), replicated 2026-09-20 and found to actually
    # belong to Super instead -- confirmed, not needs-replication.
    assert "abstention" in page.lower() or "abstain" in page.lower()
    assert "confirmed" in page.lower()
    # strategic result: escalation failed, trap-selection succeeded
    assert "trap" in page.lower()
    # retractions kept, collapsed
    assert "Retractions" in page
    assert "RETRACTED" in page


def test_coverage_page_lists_dims_and_foils():
    from cartographer.viewer.coverage import coverage_html

    page = coverage_html()
    assert "Coverage" in page
    for dim in (
        "chain_length", "scramble", "coref_pressure",
        "distractor_events", "filler_events", "witness_cutoff",
    ):
        assert dim in page
    for foil in (
        "final_true_state", "last_mentioned_location", "last_witnessed_by_anyone",
        "query_agent_own_last_move", "most_frequent_location",
    ):
        assert foil in page
    assert "Not yet covered" in page


def test_run_html_shows_trap_kinds_for_adversarial_records(tmp_path):
    store = ResultsStore(tmp_path / "adv.sqlite")
    rec = ExperimentRecord(
        run_id="camp-adversarial_belief-modelx-001",
        model_name="model-x",
        latent_params={"chain_length": 2.0},
        realized_dims={"chain_length": 2.0},
        render_mode=RenderMode.TEMPLATE,
        task_seed=1,
        trial_seed=1,
        verdict=Verdict.FAIL,
        verifier_detail="kind=belief;campaign=1;adversarial=1;traps=final_true_state,last_mentioned_location",
        tokens_in=5,
        tokens_out=5,
        cost_usd=0.001,
        latency_ms=50.0,
        finish_reason="stop",
    )
    store.append_many([rec])
    page = run_html(store, rec.run_id)
    assert "final_true_state" in page
    assert "last_mentioned_location" in page

    record_page = record_html(store, rec.record_id)
    assert "final_true_state" in record_page


# --- verdict panel wiring ----------------------------------------------------


def test_run_detail_page_contains_verdict_panel_marker(tmp_path):
    """run_html must render the "what this run revealed" panel at the top
    (see cartographer.report.verdict / cartographer.viewer.serve.verdict_panel_html)."""
    store, _, _, _ = _seed_store(tmp_path)
    page = run_html(store, "run-a")
    assert 'data-testid="verdict-panel"' in page
    # The verdict panel comes before the raw per-trial table in the body.
    assert page.index('data-testid="verdict-panel"') < page.index("<table>")


def test_run_detail_page_verdict_panel_graceful_when_empty(tmp_path):
    store = ResultsStore(tmp_path / "empty.sqlite")
    page = run_html(store, "no-such-run")
    assert "No records found." in page
    store.close()


# ---------------------------------------------------------------------------
# /atlas is an INSTRUMENT, not a picture: hover readout, per-model toggles,
# a real selector, and a drill-down per cell.
#
# Background these assert against: the previous atlas filtered to records
# that varied exactly one dim from a per-model baseline, which on real
# adaptive-campaign data admitted 3-8 records per (model, dim) and binned
# them to ONE x value -- a polyline with a single point, i.e. nothing.
# Bucketing by quantile over every record of a query kind is what makes
# the cells below exist at all, so these tests pin that down too.
# ---------------------------------------------------------------------------


def _atlas_pane(tmp_path):
    from cartographer.viewer import atlas as atlas_mod

    store, _records = _seed_atlas_store(tmp_path)
    records = atlas_mod._campaign_records(store)
    menu = atlas_mod._menu(records)
    panes = atlas_mod._all_panes(records, menu)
    return store, atlas_mod, panes


def test_atlas_buckets_use_every_record_not_a_baseline_slice(tmp_path):
    """Every campaign record of the selected kind lands in a cell."""
    store, atlas_mod, panes = _atlas_pane(tmp_path)
    pane = panes[("belief", "chain_length")]
    binned = sum(c.n_scored + c.n_error for c in pane.cells.values())
    assert binned == pane.n_records == 10  # 5 Model-A + 5 Model-B
    store.close()


def test_atlas_every_cell_has_a_tooltip_and_a_drilldown(tmp_path):
    store, _records = _seed_atlas_store(tmp_path)
    page = atlas_html(store, kind="belief", dim="chain_length")

    n_cells = page.count('class="heat-cell"')
    assert n_cells >= 2
    # Each cell is a link into its own trials AND carries a hover readout.
    assert page.count('href="/trials?') >= n_cells
    assert page.count("data-tip=") >= n_cells
    # ...and a native <title>, so the readout survives with JS off.
    assert "<title>" in page
    store.close()


def test_atlas_legend_offers_a_toggle_for_every_model(tmp_path):
    store, _records = _seed_atlas_store(tmp_path)
    page = atlas_html(store, kind="belief", dim="chain_length")

    for model in ("Model-A", "Model-B"):
        assert f'data-toggle-model="{model}"' in page
        # Series are tagged so a toggle has something to hide.
        assert f'data-series="{model}"' in page
    assert 'aria-pressed="true"' in page
    store.close()


def test_atlas_selectors_are_real_links_and_honour_query_params(tmp_path):
    """Every view state has a URL -- the page needs no JS to change what
    is plotted."""
    store, _records = _seed_atlas_store(tmp_path)
    page = atlas_html(store)
    assert "kind=belief" in page
    assert "dim=chain_length" in page
    assert 'class="seg-item' in page

    picked = atlas_html(store, kind="belief", dim="scramble")
    assert "belief / scramble" in picked
    assert 'aria-current="true"' in picked

    # Garbage params fall back to the default pane rather than 500ing.
    junk = atlas_html(store, kind="nope", dim="also-nope")
    assert "Capability map" in junk
    store.close()


def test_atlas_focal_colour_marks_the_model_that_breaks_first(tmp_path):
    """Focal-point discipline: hue goes to the model with the lowest
    fitted frontier, not to every series."""
    store, atlas_mod, panes = _atlas_pane(tmp_path)
    pane = panes[("belief", "chain_length")]
    # Model-A passes everywhere (no crossing -> no fit); Model-B breaks.
    assert "Model-A" not in pane.fits
    assert atlas_mod._focal_model(pane) == "Model-B"
    store.close()


def test_atlas_survives_one_model_one_bucket_and_empty_selections(tmp_path):
    from cartographer.viewer import atlas as atlas_mod

    store = ResultsStore(tmp_path / "thin_atlas.sqlite")
    store.append_many([
        ExperimentRecord(
            run_id="camp-belief-solo-001",
            model_name="solo/Only-Model",
            latent_params={"chain_length": 3.0},
            realized_dims={"chain_length": 3.0},
            render_mode=RenderMode.TEMPLATE,
            task_seed=1,
            trial_seed=1,
            verdict=Verdict.PASS,
            verifier_detail="kind=belief;campaign=1",
            tokens_in=1,
            tokens_out=1,
            cost_usd=0.0,
            latency_ms=1.0,
            finish_reason="stop",
        )
    ])
    page = atlas_html(store)
    assert "Only-Model" in page
    assert "Capability map" in page
    # One model, one bucket, one point: no polyline is drawn, but the page
    # still renders a cell and does not raise.
    assert page.count('class="heat-cell"') == 1
    # ...and no legend: the toggle handler refuses to hide the last
    # visible series, so a lone toggle could never act. A button that
    # cannot act is the unfinished-interface tell. Asserted on the BUTTON
    # markup, not the class name -- the stylesheet and the inline JS both
    # mention `legend-toggle` / `data-toggle-model` on every page.
    assert '<button type="button" class="legend-toggle"' not in page

    empty_pane = atlas_mod._build_pane(atlas_mod._campaign_records(store), "belief", "nope")
    assert empty_pane is None
    store.close()


# ---------------------------------------------------------------------------
# /trials -- the drill-down an atlas cell opens.
# ---------------------------------------------------------------------------


def test_trials_page_lists_the_records_behind_one_cell(tmp_path):
    from cartographer.viewer.atlas import _campaign_records, _build_pane, trials_html

    store, _records = _seed_atlas_store(tmp_path)
    pane = _build_pane(_campaign_records(store), "belief", "chain_length")
    last = len(pane.buckets) - 1

    page = trials_html(
        store, {"kind": "belief", "dim": "chain_length", "b": str(last), "model": "Model-B"}
    )
    assert "Trials" in page
    assert "Model-B" in page
    assert "Model-A" not in page.split("Trials in this cell")[-1]
    # Every listed trial links to its own transcript.
    assert "/record/" in page
    # The cell's own pass rate is restated as a bar, not a bare number.
    assert 'class="databar-fill"' in page
    store.close()


def test_trials_page_rejects_a_bogus_slice_gracefully(tmp_path):
    from cartographer.viewer.atlas import trials_html

    store, _records = _seed_atlas_store(tmp_path)
    page = trials_html(store, {"kind": "belief", "dim": "chain_length", "b": "999"})
    assert "No such selection" in page
    page2 = trials_html(store, {})
    assert "No such selection" in page2
    store.close()


# ---------------------------------------------------------------------------
# Visual encoding: a rate is a bar + a number, not a bare number.
# ---------------------------------------------------------------------------


def test_rate_bar_width_tracks_the_rate_and_clamps(tmp_path):
    from cartographer.viewer.serve import _rate_bar

    assert "width:81.0%" in _rate_bar(81.0)
    assert "width:0.0%" in _rate_bar(-5.0)       # clamped low
    assert "width:100.0%" in _rate_bar(140.0)    # clamped high
    assert "width:0.0%" in _rate_bar(float("nan"))
    assert "width:0.0%" in _rate_bar("not a number")


def test_rate_bar_colours_only_below_the_threshold():
    from cartographer.viewer.serve import _RATE_WARN_THRESHOLD, _rate_bar

    hot = _rate_bar(_RATE_WARN_THRESHOLD - 1)
    cool = _rate_bar(_RATE_WARN_THRESHOLD + 1)
    assert 'class="databar below"' in hot
    assert 'class="databar below"' not in cool
    # An explicit "magnitude only" bar is never coloured, either way.
    assert "below" not in _rate_bar(5.0, threshold=None)
    assert "full" not in _rate_bar(100.0, threshold=None)


def test_sparkline_refuses_to_imply_a_trend_from_one_point():
    from cartographer.viewer.serve import _sparkline, _verdict_seq_rates

    assert _sparkline([]) == ""
    assert _sparkline([0.5]) == ""
    assert "<svg" in _sparkline([1.0, 0.5, 0.0])
    # Fewer than four scored trials is not a trend.
    assert _verdict_seq_rates(["PASS", "FAIL"]) == []
    rates = _verdict_seq_rates(["PASS"] * 4 + ["FAIL"] * 4)
    assert rates[0] == 1.0 and rates[-1] == 0.0


def test_runs_table_renders_bars_and_status_chips(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = runs_html(store)

    assert 'class="databar-fill"' in page
    assert "33%" in page  # the exact number survives alongside the bar
    assert 'class="chip ' in page
    store.close()


def test_run_table_marks_fail_and_error_rows_at_the_row_edge(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = run_html(store, "run-a")

    # run-a is 1 PASS / 1 unverified FAIL / 1 ERROR.
    assert '<tr class="row-sev row-sev-unverified">' in page
    assert '<tr class="row-sev row-sev-error">' in page
    # PASS rows get no edge -- an edge on every row means nothing.
    # (Counted on the ROWS, not the page: the stylesheet naturally
    # mentions the class names too.)
    assert page.count('<tr class="row-sev') == 2
    # Query kind is a chip now, not a bare cell.
    assert 'class="chip chip-muted">state<' in page
    store.close()


def test_record_page_chips_categorical_metadata(tmp_path):
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = record_html(store, r1.record_id)

    assert 'class="chip-set"' in page
    assert 'class="chip chip-muted">template<' in page   # render mode
    assert 'class="chip chip-muted">state<' in page      # query kind
    # The raw verifier_detail is still there -- chips summarize, not hide.
    assert "kind=state;sim=same_category" in page
    store.close()


def test_shell_ships_the_interaction_layer_inline_and_offline(tmp_path):
    """All JS is inline in the served HTML: no CDN, works from a local
    server with no network."""
    store, r1, r2, r3 = _seed_store(tmp_path)
    page = atlas_html(store)

    assert "<script>" in page and 'src=' not in page.split("<script>")[1]
    assert "data-toggle-model" in page
    # prefers-reduced-motion is respected for the one transition added.
    assert "prefers-reduced-motion" in page
    store.close()
