"""Tests for cartographer.report.verdict.run_verdict.

Fixtures are built by driving real `PipelineEvaluator` calls against a
scripted fake client (same idiom as tests/test_evaluator.py) so every
stored record is genuinely reconstructable (real `(latent_params,
task_seed, rendering_seed)` provenance, `created_at` stamped after the
generator-hardening cutoff) -- this lets `run_verdict` exercise its real
world-regeneration + foil-classification path, not a mocked one.

Zero network calls anywhere.
"""

from __future__ import annotations

import pytest

from cartographer.exec.evaluator import PipelineEvaluator, _apply_config, _default_base
from cartographer.exec.client import ChatResult
from cartographer.render import build_name_map, render
from cartographer.report.verdict import run_verdict
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.world.generate import generate


class ScriptedClient:
    def __init__(self, script=None, default_text="wrong answer"):
        self.script = list(script) if script is not None else None
        self.default_text = default_text
        self.calls = []

    def chat(self, model, prompt, max_tokens=1500, max_tokens_retry=4000, **kwargs):
        self.calls.append((model, prompt, max_tokens))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return ChatResult(text=self.default_text, tokens_in=10, tokens_out=5, latency_ms=1.0)


def _correct_answer_for(query_kind: str, config: dict, trial_seed: int) -> str:
    """Compute the true answer_key for a given (query_kind, config,
    trial_seed) the same way PipelineEvaluator derives its task seed --
    lets tests script a client that answers correctly on demand."""
    from cartographer.exec.evaluator import _task_seed

    base = _default_base(query_kind)
    params = _apply_config(base, config)
    seed = _task_seed(config, trial_seed)
    world = generate(params, seed)
    name_map = build_name_map(world, seed)
    rendered = render(world, name_map, seed)
    return rendered.answer_key


def _add_passing_trials(store, run_id, query_kind, model, n, config, start_seed=0):
    client_script = []
    for i in range(n):
        answer = _correct_answer_for(query_kind, config, start_seed + i)
        client_script.append(ChatResult(text=answer, tokens_in=10, tokens_out=5, latency_ms=1.0))
    client = ScriptedClient(script=client_script)
    ev = PipelineEvaluator(client, model, store, run_id, query_kind, cost_cap_usd=10.0)
    for i in range(n):
        ev(config, trial_seed=start_seed + i)


def _add_failing_trials(store, run_id, query_kind, model, n, config, start_seed=100):
    client = ScriptedClient(default_text="a place that is definitely wrong")
    ev = PipelineEvaluator(client, model, store, run_id, query_kind, cost_cap_usd=10.0)
    for i in range(n):
        ev(config, trial_seed=start_seed + i)


# --- saturated run -----------------------------------------------------------


def test_saturated_run_says_did_not_challenge(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    _add_passing_trials(store, "run-sat", "state", "fake/model", 8, {"chain_length": 2})

    verdict = run_verdict(store, "run-sat")

    assert "did not challenge" in verdict.headline
    assert verdict.stats["pass_rate"] == pytest.approx(1.0)
    store.close()


# --- boundary run --------------------------------------------------------


def test_boundary_run_reports_pass_and_fail_split(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    _add_passing_trials(store, "run-mixed", "state", "fake/model", 5, {"chain_length": 2}, start_seed=0)
    _add_failing_trials(store, "run-mixed", "state", "fake/model", 5, {"chain_length": 2}, start_seed=100)

    verdict = run_verdict(store, "run-mixed")

    assert verdict.stats["n_pass"] == 5
    assert verdict.stats["n_fail"] == 5
    assert "50%" in verdict.headline
    assert len(verdict.notable_failures) > 0
    store.close()


# --- planted belief -> reality collapse failures ------------------------


def test_belief_collapse_failure_named_in_verdict(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    # Failing belief trials: the model always answers with something that
    # doesn't match the true belief. We can't force the exact
    # final_true_state foil deterministically without inspecting the world,
    # so instead assert on the general shape: belief FAILs get classified
    # and named in plain English (either the collapse phrasing or the
    # generic shortcut-match phrasing), not left silent.
    _add_failing_trials(
        store, "run-belief-fail", "belief", "fake/model", 6,
        {"chain_length": 4, "witness_cutoff": 0.3}, start_seed=200,
    )

    verdict = run_verdict(store, "run-belief-fail")

    assert verdict.stats["n_fail"] == 6
    joined = " ".join(verdict.paragraphs)
    assert "belief failure" in joined or "belief FAIL" in joined
    store.close()


def test_belief_collapse_wording_when_final_true_state_dominates(tmp_path):
    """Directly exercises the "collapsed her perspective into reality"
    wording by constructing records whose response text is exactly the
    world's true final location (the final_true_state foil) -- the
    dominant real-world failure mode documented in docs/lab-log.md."""
    from cartographer.exec.evaluator import _task_seed
    from cartographer.world.foils import foil_divergence

    store = ResultsStore(tmp_path / "db.sqlite3")
    query_kind = "belief"
    config = {"chain_length": 5, "witness_cutoff": 0.6, "coref_pressure": 2}
    base = _default_base(query_kind)
    params = _apply_config(base, config)

    n_planted = 0
    seed_i = 0
    script = []
    seeds_used = []
    while n_planted < 5 and seed_i < 200:
        seed = _task_seed(config, seed_i)
        world = generate(params, seed)
        name_map = build_name_map(world, seed)
        profile = foil_divergence(world, name_map)
        if profile.foils.get("final_true_state") and profile.foils["final_true_state"] != profile.true_answer:
            script.append(
                ChatResult(
                    text=profile.foils["final_true_state"], tokens_in=10, tokens_out=5, latency_ms=1.0,
                )
            )
            seeds_used.append(seed_i)
            n_planted += 1
        seed_i += 1

    assert n_planted == 5, "generator didn't produce enough final_true_state-diverging worlds to test with"

    client = ScriptedClient(script=list(script))
    ev = PipelineEvaluator(client, "fake/model", store, "run-collapse", query_kind, cost_cap_usd=10.0)
    for s in seeds_used:
        ev(config, trial_seed=s)

    verdict = run_verdict(store, "run-collapse")
    joined = " ".join(verdict.paragraphs)
    assert "believe" in joined.lower() or "collapsed" in joined.lower()
    store.close()


# --- empty run -------------------------------------------------------------


def test_empty_run_is_graceful(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    verdict = run_verdict(store, "run-does-not-exist")
    assert "no records" in verdict.headline.lower() or "no records" in " ".join(verdict.paragraphs).lower()
    assert verdict.stats.get("n_records") == 0
    store.close()


def test_all_error_run_is_graceful(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    for i in range(3):
        store.append(
            ExperimentRecord(
                run_id="run-all-error",
                model_name="fake/model",
                latent_params={},
                realized_dims={},
                render_mode=RenderMode.TEMPLATE,
                task_seed=i,
                trial_seed=i,
                verdict=Verdict.ERROR,
                verifier_detail="kind=state;campaign=1;error=timeout",
            )
        )
    verdict = run_verdict(store, "run-all-error")
    assert "error" in verdict.headline.lower()
    assert verdict.stats["n_records"] == 3
    store.close()


# --- state/order runs need no foils -----------------------------------------


def test_state_run_uses_simpler_copy_no_foil_language(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    _add_passing_trials(store, "run-state-simple", "state", "fake/model", 3, {"chain_length": 2}, start_seed=0)
    _add_failing_trials(store, "run-state-simple", "state", "fake/model", 3, {"chain_length": 2}, start_seed=100)

    verdict = run_verdict(store, "run-state-simple")
    joined = " ".join(verdict.paragraphs).lower()
    assert "believe" not in joined
    assert "collapsed" not in joined
    store.close()


# --- full_map aggregation ---------------------------------------------------


def test_full_map_verdict_aggregates_child_stages(tmp_path):
    store = ResultsStore(tmp_path / "db.sqlite3")
    parent = "camp-full-map-verdict-001"
    _add_passing_trials(store, f"{parent}/state", "state", "fake/model", 4, {"chain_length": 2})
    _add_passing_trials(store, f"{parent}/order", "order", "fake/model", 4, {"chain_length": 2})
    _add_failing_trials(store, f"{parent}/belief", "belief", "fake/model", 4, {"chain_length": 3}, start_seed=50)

    verdict = run_verdict(store, parent)

    assert "state" in verdict.stats["stages"]
    assert "order" in verdict.stats["stages"]
    assert "belief" in verdict.stats["stages"]
    assert verdict.stats["stages"]["belief"]["pass_rate"] == pytest.approx(0.0)
    assert "belief" in verdict.headline.lower() or "weak province" in verdict.headline.lower()
    store.close()


# --- verification (Task 2): verified counts + noise exclusion ------------


def test_verdict_no_verification_data_shows_caution_line(tmp_path):
    """A run with FAILs but zero verify trials (old run, or verify="manual")
    must say "failures not yet verified" and point at the action, not
    silently report raw counts as if they were trustworthy."""
    store = ResultsStore(tmp_path / "db.sqlite3")
    _add_passing_trials(store, "run-unverified", "state", "fake/model", 3, {"chain_length": 2}, start_seed=0)
    _add_failing_trials(store, "run-unverified", "state", "fake/model", 2, {"chain_length": 2}, start_seed=100)

    verdict = run_verdict(store, "run-unverified")
    joined = " ".join(verdict.paragraphs)

    assert "not yet verified" in joined
    assert "Verify failures" in joined
    assert "scripts/verify_past_failures.py" in joined
    store.close()


def test_verdict_mixed_stable_and_noise_reports_verified_counts(tmp_path):
    """The exact copy the coordinator specified: N failures recorded, M
    survived verification, K were noise and excluded."""
    from cartographer.exec.evaluator import VERIFY_SUFFIX
    from cartographer.store.schema import ExperimentRecord, RenderMode

    store = ResultsStore(tmp_path / "db.sqlite3")
    run_id = "run-mixed-verify"
    _add_passing_trials(store, run_id, "state", "fake/model", 2, {"chain_length": 2}, start_seed=0)
    # 9 FAILs total: 6 stable (all-wrong resamples), 3 noise (all-correct resamples).
    _add_failing_trials(store, run_id, "state", "fake/model", 9, {"chain_length": 2}, start_seed=200)

    fails = [r for r in store.by_run(run_id) if r.verdict == Verdict.FAIL]
    assert len(fails) == 9
    stable_fails, noise_fails = fails[:6], fails[6:]

    def _append_verify_trial(original, verdict, i):
        store.append(
            ExperimentRecord(
                run_id=f"{run_id}{VERIFY_SUFFIX}",
                model_name="fake/model",
                latent_params=dict(original.latent_params),
                realized_dims=dict(original.realized_dims),
                render_mode=RenderMode.TEMPLATE,
                task_seed=original.task_seed,
                trial_seed=original.trial_seed,
                rendering_seed=original.rendering_seed,
                prompt_text=original.prompt_text,
                response_text="resample response",
                verdict=verdict,
                verifier_detail=f"kind=state;campaign=1;verify=1;verify_of={original.record_id};sample={i}",
            )
        )

    for original in stable_fails:
        for i in range(3):
            _append_verify_trial(original, Verdict.FAIL, i)  # all wrong -> stable
    for original in noise_fails:
        for i in range(3):
            _append_verify_trial(original, Verdict.PASS, i)  # all correct -> noise

    verdict = run_verdict(store, run_id)
    joined = " ".join(verdict.paragraphs)

    assert "9 failures recorded" in joined
    assert "6 survived verification" in joined
    assert "3 were sampling noise and are excluded from findings" in joined

    # Verified stats must reflect verified counts (noise excluded from the
    # failure tally used for pass_rate -- 2 PASS + 6 stable FAIL = 8
    # scored, pass_rate = 2/8 = 0.25), and notable_failures must never
    # include a noise-classified record.
    assert verdict.stats["pass_rate"] == pytest.approx(2 / 8)
    noise_ids = {r.record_id for r in noise_fails}
    assert all(f.record_id not in noise_ids for f in verdict.notable_failures)
    store.close()
