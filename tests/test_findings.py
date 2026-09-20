"""Tests for the data-derived /findings page.

`findings_html` used to be a pure function over a hard-coded list of
dataclass literals: it took no store, so it could not switch between
models, could not show a strength, could not say WHERE a model failed, and
never changed when new trials landed. These tests pin the replacement --
derivation from the store, the capability map, the model switcher, the
drill-down, and the replicate action -- plus the two properties that made
the old page worth keeping: the curated causal findings survive verbatim,
and a citation whose records are gone is rendered as gone.

Zero network: replication is exercised through a scripted fake client.
"""

from __future__ import annotations

import threading

import pytest

from cartographer.confirm.replicate import (
    BASELINE_ARM_SUFFIX,
    FAILING_ARM_SUFFIX,
    arm_run_ids,
    estimate_replication_cost,
    next_replication_run_id,
    replication_evidence,
    replication_trial_count,
    run_finding_replication,
)
from cartographer.exec.client import ChatResult
from cartographer.exec.evaluator import VERIFY_SUFFIX
from cartographer.report.verdict import run_verdict
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.viewer import launch as launch_mod
from cartographer.viewer.findings import (
    CAPABILITY_KINDS,
    DIFFICULTY_DIMS,
    STRENGTH_MIN_N,
    capability_trials_html,
    cell_trials,
    derive,
    findings_html,
    heat_class,
)
from cartographer.viewer.launch import LaunchDeps, handle_replicate_now, parse_replication_config

MODEL_STRONG = "acme/strong-7b"
MODEL_WEAK = "acme/weak-7b"


@pytest.fixture(autouse=True)
def _clean_launch_state():
    launch_mod._reset_for_tests()
    yield
    launch_mod._reset_for_tests()


def _rec(
    run_id: str,
    model: str,
    verdict: Verdict,
    *,
    kind: str = "belief",
    seed: int = 0,
    dims: dict | None = None,
    latent: dict | None = None,
    detail_extra: str = "",
    response: str = "an answer",
    cost: float = 0.0002,
) -> ExperimentRecord:
    return ExperimentRecord(
        run_id=run_id,
        model_name=model,
        latent_params=latent or {"chain_length": 2.0, "coref_pressure": 0.0},
        realized_dims=dims or {"entity_state": 2.0, "coreference": 0.0},
        render_mode=RenderMode.TEMPLATE,
        task_seed=seed,
        trial_seed=seed,
        rendering_seed=seed,
        prompt_text="Where does Ana believe the key is?",
        response_text=response,
        verdict=verdict,
        verifier_detail=f"kind={kind};campaign=1{detail_extra}",
        tokens_in=40,
        tokens_out=12,
        cost_usd=cost,
        latency_ms=10.0,
    )


def _seed(tmp_path) -> ResultsStore:
    """A store with two models that differ on purpose.

    MODEL_STRONG: 30/30 on state, 24/25 on belief -> two strengths.
    MODEL_WEAK:   30/30 on state, 32/50 on belief. The belief failures are
                  ALL in the high-coreference region and none of them are
                  in the high-entity_state region, so "where it failed" has
                  exactly one right answer and a page that reports the
                  wrong dimension fails this fixture.
    """
    store = ResultsStore(tmp_path / "findings.sqlite")
    records = []
    n = 0

    for i in range(30):
        n += 1
        records.append(_rec("camp-state-strong-001", MODEL_STRONG, Verdict.PASS, kind="state", seed=n))
    for i in range(30):
        n += 1
        records.append(_rec("camp-state-weak-001", MODEL_WEAK, Verdict.PASS, kind="state", seed=n))

    for i in range(25):
        n += 1
        records.append(
            _rec(
                "camp-belief-strong-001", MODEL_STRONG,
                Verdict.PASS if i > 0 else Verdict.FAIL,
                seed=n,
                dims={"entity_state": 2.0, "coreference": 0.0},
            )
        )

    # WEAK, belief: 20 easy (pass) + 10 long-chain (pass, so the
    # high-entity_state region is CLEAN) + 20 high-coreference (2 pass,
    # 18 fail, so the failures live in exactly one dimension region).
    for i in range(20):
        n += 1
        records.append(
            _rec(
                "camp-belief-weak-001", MODEL_WEAK, Verdict.PASS, seed=n,
                dims={"entity_state": 2.0, "coreference": 0.0},
                latent={"chain_length": 2.0, "coref_pressure": 0.0},
            )
        )
    for i in range(10):
        n += 1
        records.append(
            _rec(
                "camp-belief-weak-001", MODEL_WEAK, Verdict.PASS, seed=n,
                dims={"entity_state": 9.0, "coreference": 0.0},
                latent={"chain_length": 9.0, "coref_pressure": 0.0},
            )
        )
    for i in range(20):
        n += 1
        records.append(
            _rec(
                "camp-belief-weak-001", MODEL_WEAK,
                Verdict.PASS if i < 2 else Verdict.FAIL,
                seed=n,
                dims={"entity_state": 2.0, "coreference": 6.0},
                latent={"chain_length": 2.0, "coref_pressure": 6.0},
                detail_extra=";adversarial=1;traps=final_true_state",
                response="the kitchen",
            )
        )
    # Two infra ERRORs that must never touch a pass rate.
    for i in range(2):
        n += 1
        records.append(
            _rec(
                "camp-belief-weak-001", MODEL_WEAK, Verdict.ERROR, seed=n,
                dims={"entity_state": 2.0, "coreference": 6.0},
                detail_extra=";error=timeout",
            )
        )
    store.append_many(records)
    return store


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def test_derive_builds_a_cell_per_model_and_capability(tmp_path):
    store = _seed(tmp_path)
    data = derive(store)

    assert [mf.model for mf in data.models] == sorted([MODEL_STRONG, MODEL_WEAK])
    expected_cells = len(CAPABILITY_KINDS) + len(DIFFICULTY_DIMS)
    for mf in data.models:
        assert len(mf.cells) == expected_cells
    store.close()


def test_scored_denominator_excludes_errors_and_matches_the_verdict_panel(tmp_path):
    """The KPI contract: /findings and report/verdict.py must divide by the
    same thing. 50 scored belief trials + 2 ERRORs -> 32/50, never 32/52."""
    store = _seed(tmp_path)
    data = derive(store)
    weak = data.by_model(MODEL_WEAK)
    belief = weak.cells[("kind", "belief")]

    assert belief.n_scored == 50
    assert belief.n_error == 2
    assert belief.n_pass == 32  # 20 easy + 10 long-chain + 2 of the hard ones
    assert belief.pass_rate == pytest.approx(32 / 50)

    verdict = run_verdict(store, "camp-belief-weak-001")
    assert verdict.stats["pass_rate"] == pytest.approx(belief.pass_rate)
    store.close()


def test_noise_classified_failures_leave_the_denominator(tmp_path):
    """A FAIL that passes on resample is sampling noise: it must drop out of
    BOTH numerator and denominator, exactly as `_verified_only` does for the
    run verdict -- otherwise the same screen shows two different rates."""
    store = _seed(tmp_path)
    weak_fails = [
        r for r in store.by_run("camp-belief-weak-001") if r.verdict == Verdict.FAIL
    ]
    noisy = weak_fails[0]
    for i in range(3):
        store.append(
            _rec(
                f"camp-belief-weak-001{VERIFY_SUFFIX}", MODEL_WEAK, Verdict.PASS,
                seed=9000 + i,
                detail_extra=f";verify=1;verify_of={noisy.record_id};sample={i}",
            )
        )

    belief = derive(store).by_model(MODEL_WEAK).cells[("kind", "belief")]
    assert belief.n_noise == 1
    assert belief.n_scored == 49
    assert belief.pass_rate == pytest.approx(32 / 49)
    assert belief.verification == "contains-noise"
    assert "sampling noise" in belief.verification_detail

    # ...and the verdict panel agrees, because both call the same classifier.
    verdict = run_verdict(store, "camp-belief-weak-001")
    assert verdict.stats["pass_rate"] == pytest.approx(belief.pass_rate)
    store.close()


def test_verify_and_replication_runs_never_inflate_a_capability_cell(tmp_path):
    """`*/verify` resample trials and `repl-*` arms are evidence ABOUT a
    finding, not trials of the capability. Folding them into the cell would
    let one failure vote three more times (verify) and let a deliberately
    easy baseline arm lift the rate (replication)."""
    store = _seed(tmp_path)
    before = derive(store).by_model(MODEL_WEAK).cells[("kind", "belief")]

    for i in range(10):
        store.append(_rec(f"camp-belief-weak-001{VERIFY_SUFFIX}", MODEL_WEAK, Verdict.PASS, seed=8000 + i,
                          detail_extra=f";verify=1;verify_of=nonexistent{i};sample=0"))
        store.append(_rec("repl-weak-7b-belief-001-baseline", MODEL_WEAK, Verdict.PASS, seed=8100 + i))

    after = derive(store).by_model(MODEL_WEAK).cells[("kind", "belief")]
    assert (after.n_scored, after.n_pass) == (before.n_scored, before.n_pass)
    store.close()


def test_strength_is_derived_with_its_real_numbers(tmp_path):
    store = _seed(tmp_path)
    strong = derive(store).by_model(MODEL_STRONG)

    state = [f for f in strong.strengths if f.cell.key == "state"]
    assert state, "a 30/30 state result is a finding, and used to be invisible"
    assert state[0].cell.n_scored == 30 >= STRENGTH_MIN_N
    assert "passes state queries cleanly: 30/30, no failures at all" in state[0].headline
    store.close()


def test_weakness_says_where_it_failed(tmp_path):
    store = _seed(tmp_path)
    weak = derive(store).by_model(MODEL_WEAK)
    belief = [f for f in weak.weaknesses if f.cell.key == "belief"]
    assert belief, "64% on belief must surface as a weakness"
    finding = belief[0]

    # 1. a rate, in the headline
    assert "64% pass (32/50 scored, 18 real failures)" in finding.headline
    assert "concentrated at coreference = 6, where it drops to 10% (2/20)" in finding.headline
    # 2. WHICH dimension region the failures concentrate in
    worst = finding.concentrations[0]
    assert worst.dim == "coreference" and worst.bucket == "high"
    assert worst.pass_rate == pytest.approx(2 / 20)
    assert "coreference = 6" in worst.range_text()
    # 3. which traps fired, parsed from verifier_detail
    assert ("final_true_state", 18) in finding.traps
    # 4. concrete, linkable examples
    assert len(finding.examples) == 3
    for record_id, desc in finding.examples:
        assert store.lineage(record_id)
        assert "coreference=6" in desc
    # 5. the exact config a replication has to re-run
    assert finding.failing_config == {"chain_length": 2.0, "coref_pressure": 6.0}
    store.close()


def test_verification_state_is_carried_on_every_derived_finding(tmp_path):
    store = _seed(tmp_path)
    weak = derive(store).by_model(MODEL_WEAK)
    belief = [f for f in weak.weaknesses if f.cell.key == "belief"][0]

    assert belief.cell.verification == "unverified"
    assert "18 failure(s): 18 not yet resample-checked" == belief.cell.verification_detail

    # Verify all 18 as stable -> the finding's state flips.
    for r in store.by_run("camp-belief-weak-001"):
        if r.verdict != Verdict.FAIL:
            continue
        for i in range(3):
            store.append(
                _rec(
                    f"camp-belief-weak-001{VERIFY_SUFFIX}", MODEL_WEAK, Verdict.FAIL,
                    seed=7000 + i,
                    detail_extra=f";verify=1;verify_of={r.record_id};sample={i}",
                )
            )
    belief = [
        f for f in derive(store).by_model(MODEL_WEAK).weaknesses if f.cell.key == "belief"
    ][0]
    assert belief.cell.verification == "verified-stable"
    assert "18 survived resampling" in belief.cell.verification_detail
    store.close()


def test_a_cell_with_no_failures_is_not_called_verified(tmp_path):
    """30/30 is a real strength, but nothing was ever resampled there --
    labelling it "verified-stable" would claim an audit that never ran."""
    store = _seed(tmp_path)
    clean = derive(store).by_model(MODEL_STRONG).cells[("kind", "state")]

    assert clean.n_fail == 0 and clean.n_noise == 0
    assert clean.verification == "no-failures"
    assert clean.verification_detail == "no failures recorded here -- nothing to re-sample"
    assert 'class="status-tag verif-no-failures"' in findings_html(store)
    store.close()


def test_empty_store_derives_nothing_without_crashing(tmp_path):
    store = ResultsStore(tmp_path / "empty.sqlite")
    data = derive(store)
    assert data.models == [] and data.n_trials == 0
    page = findings_html(store)
    assert "No trials in this store yet" in page
    store.close()


def test_heat_ramp_is_monotone_and_has_an_empty_state():
    assert heat_class(None) == "heat-none"
    assert heat_class(0.0) == "heat-1"
    assert heat_class(0.6) == "heat-2"
    assert heat_class(0.8) == "heat-3"
    assert heat_class(0.93) == "heat-4"
    assert heat_class(1.0) == "heat-5"


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_capability_map_has_a_row_per_model_and_clickable_cells(tmp_path):
    store = _seed(tmp_path)
    page = findings_html(store)

    assert 'id="capability-map"' in page
    assert page.count('<tr class="cap-row"') + page.count('<tr class="cap-row ') == 2
    n_cells = len(CAPABILITY_KINDS) + len(DIFFICULTY_DIMS)
    assert f"2 models &times; {n_cells} capabilities = {2 * n_cells} cells" in page
    # Every populated cell is a link into the trials behind it.
    assert f'href="/trials?model=acme%2Fweak-7b&axis=kind&key=belief"' in page
    # A capability with no trials renders as an honest empty cell, not 0%.
    assert 'class="cap-block heat-none"' in page
    store.close()


def test_model_switcher_renders_every_model_and_honours_the_query_param(tmp_path):
    store = _seed(tmp_path)

    all_page = findings_html(store)
    assert 'data-model-tab=""' in all_page and ">All models<" in all_page
    assert f'href="/findings?model=acme%2Fweak-7b"' in all_page
    # All-models view: both sections rendered AND visible.
    assert all_page.count('<section class="model-findings"') == 2

    one = findings_html(store, model=MODEL_WEAK)
    # The other model's section is still in the DOM (so the client-side
    # switcher costs zero round-trips) but server-hidden, so the page is
    # correct with JavaScript disabled.
    assert f'data-model="{MODEL_STRONG}" style="display:none;"' in one
    assert f'data-model="{MODEL_WEAK}">' in one
    assert '<tr class="cap-row cap-row-selected"' in one

    # An unknown model falls back to the all-models view rather than 404ing
    # or rendering an empty page.
    assert findings_html(store, model="acme/never-run") == all_page
    store.close()


def test_page_shows_both_positives_and_negatives(tmp_path):
    store = _seed(tmp_path)
    page = findings_html(store)

    assert "What it does well" in page and "Where it fails" in page
    assert 'class="card finding-strength"' in page
    assert 'class="card finding-weakness"' in page
    assert "Derived strengths" in page and "Derived weaknesses" in page
    store.close()


def test_weakness_card_offers_replication_with_a_cost_estimate(tmp_path):
    store = _seed(tmp_path)
    page = findings_html(store)

    assert 'action="/replicate"' in page
    assert "Replicate on fresh seeds" in page
    # The estimate is derived from this model's OWN recorded per-trial cost,
    # and the trial count is stated, so the user knows what they're buying.
    est = estimate_replication_cost(store, MODEL_WEAK)
    assert f"{est.n_trials} fresh-seed trials" in page
    assert f"~${est.est_cost_usd:.4f}" in page
    # The exact config being re-run is named, not implied.
    assert 'name="cfg_coref_pressure" value="6"' in page
    store.close()


def test_curated_findings_survive_and_are_labelled_as_curated(tmp_path):
    store = _seed(tmp_path)
    page = findings_html(store)

    assert "Documented findings" in page
    assert "curated &middot; not auto-derived" in page
    assert "pronoun" in page.lower() and "Ultra" in page
    assert "Retractions" in page and "RETRACTED" in page
    store.close()


def test_dangling_curated_run_ids_are_shown_as_gone_not_linked(tmp_path):
    """~1,180 records were destroyed on 2026-09-19, so several documented
    run_ids no longer resolve. A dead link that looks alive overstates the
    evidence on hand."""
    store = _seed(tmp_path)
    page = findings_html(store)

    assert "evidence no longer in store" in page
    assert 'class="mono dangling">camp-belief-nemotron-nano-001<' in page
    assert 'href="/run/camp-belief-nemotron-nano-001"' not in page

    # Put that run back and the citation becomes a working link again.
    store.append(_rec("camp-belief-nemotron-nano-001", MODEL_STRONG, Verdict.PASS, seed=5555))
    page2 = findings_html(store)
    assert 'href="/run/camp-belief-nemotron-nano-001"' in page2
    store.close()


# ---------------------------------------------------------------------------
# Drill-down
# ---------------------------------------------------------------------------


def test_cell_drilldown_agrees_with_the_cell_it_came_from(tmp_path):
    store = _seed(tmp_path)
    cell = derive(store).by_model(MODEL_WEAK).cells[("dim", "coreference")]
    drilled, members, _verification = cell_trials(store, MODEL_WEAK, "dim", "coreference")

    assert (drilled.n_scored, drilled.n_pass) == (cell.n_scored, cell.n_pass)
    page = capability_trials_html(
        store, {"model": MODEL_WEAK, "axis": "dim", "key": "coreference"}
    )
    assert f"{cell.n_pass:,}/{cell.n_scored:,} scored" in page
    assert all(f"/record/{r.record_id}" in page for r in members[:5])
    store.close()


def test_cell_drilldown_rejects_a_nonexistent_selection(tmp_path):
    store = _seed(tmp_path)
    page = capability_trials_html(store, {"model": "", "axis": "kind", "key": "belief"})
    assert "does not exist" in page
    store.close()


# ---------------------------------------------------------------------------
# Replication
# ---------------------------------------------------------------------------


class _ScriptedClient:
    """Fake client: answers `hard_answer` whenever the prompt came from the
    hard config and `easy_answer` otherwise. No network."""

    def __init__(self, text="the kitchen"):
        self.text = text
        self.calls = []

    def chat(self, model, prompt, max_tokens=1500, max_tokens_retry=4000, **kwargs):
        self.calls.append(prompt)
        return ChatResult(text=self.text, tokens_in=30, tokens_out=8, latency_ms=1.0)


def test_estimate_uses_this_models_own_observed_cost(tmp_path):
    store = _seed(tmp_path)
    est = estimate_replication_cost(store, MODEL_WEAK)

    assert est.n_trials == replication_trial_count()
    assert est.mean_trial_cost_usd == pytest.approx(0.0002)
    assert est.est_cost_usd == pytest.approx(0.0002 * est.n_trials)
    assert "mean observed cost/trial" in est.human()

    # A model with no history gets an honest "cannot estimate", not a zero.
    unknown = estimate_replication_cost(store, "acme/never-run")
    assert unknown.est_cost_usd is None
    assert "no prior trials" in unknown.human()
    store.close()


def test_replication_writes_two_fresh_seed_arms_and_updates_the_finding(tmp_path):
    store = _seed(tmp_path)
    client = _ScriptedClient()

    outcome = run_finding_replication(
        client=client,
        model=MODEL_WEAK,
        store=store,
        query_kind="belief",
        failing_config={"chain_length": 2.0, "coref_pressure": 6.0},
        k_trials=2,
        n_fresh_seeds=1,
        cost_cap=1.0,
    )

    failing_run, baseline_run = arm_run_ids(outcome.base_run_id)
    assert failing_run.endswith(FAILING_ARM_SUFFIX)
    assert baseline_run.endswith(BASELINE_ARM_SUFFIX)
    assert len(store.by_run(failing_run)) == 2
    assert len(store.by_run(baseline_run)) == 2
    assert outcome.n_trials_spent == 4

    evidence = replication_evidence(store, MODEL_WEAK, "belief")
    assert evidence.n_runs == 1
    assert evidence.failing_n == 2 and evidence.baseline_n == 2

    # The finding on the page now reports the fresh evidence rather than
    # "not replicated" -- this is the "so that it gets updated" contract.
    belief = [
        f for f in derive(store).by_model(MODEL_WEAK).weaknesses if f.cell.key == "belief"
    ][0]
    assert belief.replication.has_evidence
    assert belief.replication_status in ("replicated", "replication-disagrees")
    assert "fresh seeds" in findings_html(store)
    store.close()


def test_a_cancelled_replication_is_not_reported_as_disagreement(tmp_path):
    """Observed live on 2026-09-19: a replication cancelled mid-flight
    records its remaining trials as ERRORs with no API call, leaving the
    baseline arm with zero SCORED trials. Reporting that as "replication
    disagrees" would turn a cancelled job into evidence against the
    finding."""
    store = _seed(tmp_path)
    store.append(_rec("repl-weak-7b-belief-001-failing", MODEL_WEAK, Verdict.PASS, seed=6001))
    store.append(_rec("repl-weak-7b-belief-001-failing", MODEL_WEAK, Verdict.PASS, seed=6002))
    for i in range(8):
        store.append(_rec("repl-weak-7b-belief-001-baseline", MODEL_WEAK, Verdict.ERROR,
                          seed=6100 + i, detail_extra=";cancelled=1"))

    evidence = replication_evidence(store, MODEL_WEAK, "belief")
    assert evidence.failing_n == 2 and evidence.baseline_n == 0

    belief = [
        f for f in derive(store).by_model(MODEL_WEAK).weaknesses if f.cell.key == "belief"
    ][0]
    assert belief.replication_status == "replication-incomplete"
    page = findings_html(store)
    assert "cancelled or hit its cost cap" in page
    assert "replication disagrees" not in page
    store.close()


def test_replication_run_ids_do_not_collide(tmp_path):
    store = _seed(tmp_path)
    first = next_replication_run_id(store, MODEL_WEAK, "belief")
    store.append(_rec(first + FAILING_ARM_SUFFIX, MODEL_WEAK, Verdict.FAIL, seed=1))
    second = next_replication_run_id(store, MODEL_WEAK, "belief")
    assert first != second and second.endswith("-002")
    store.close()


def test_parse_replication_config_drops_unknown_knobs():
    form = {
        "cfg_chain_length": "9", "cfg_coref_pressure": "6",
        "cfg_not_a_knob": "3", "cfg_scramble": "bogus", "provider": "nebius",
    }
    assert parse_replication_config(form) == {"chain_length": 9.0, "coref_pressure": 6.0}


def test_handle_replicate_now_requires_a_key_and_a_config(tmp_path):
    db_path = str(tmp_path / "r.sqlite")
    ResultsStore(db_path).close()
    deps = LaunchDeps(db_path=db_path, client_factory=lambda *a, **k: _ScriptedClient(), key_loader=lambda: {})

    started, payload = handle_replicate_now(
        {"provider": "nebius", "model": MODEL_WEAK, "cfg_chain_length": "9"}, deps
    )
    assert not started and "no api key" in payload.lower()

    started, payload = handle_replicate_now(
        {"provider": "nebius", "api_key": "k", "model": MODEL_WEAK}, deps
    )
    assert not started and "no failing configuration" in payload.lower()


def test_handle_replicate_now_takes_the_one_campaign_slot(tmp_path):
    """A replication is a live campaign: it must honour the same
    one-at-a-time guard as /launch and /verify."""
    launch_mod._CURRENT_RUN = {"run_id": "camp-busy-001", "thread": None, "cancel_event": threading.Event()}
    try:
        deps = LaunchDeps(db_path=str(tmp_path / "busy.sqlite"), key_loader=lambda: {"nebius": "k"})
        started, payload = handle_replicate_now(
            {"provider": "nebius", "model": MODEL_WEAK, "cfg_chain_length": "9"}, deps
        )
        assert not started
        assert "campaign in progress" in payload.lower()
    finally:
        launch_mod._reset_for_tests()
