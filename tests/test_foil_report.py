"""Tests for scripts/foil_report.py: builds a small synthetic results store
(rather than touching the real results/campaign.sqlite) and asserts the
classification/report logic works end to end. Zero network."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from cartographer.exec.evaluator import _apply_config, _default_base
from cartographer.render import build_name_map, render
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.world.generate import generate

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "foil_report.py"


def _load_foil_report_module():
    spec = importlib.util.spec_from_file_location("foil_report", _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["foil_report"] = module
    spec.loader.exec_module(module)
    return module


foil_report = _load_foil_report_module()


def _make_record(run_id, model, latent_params, task_seed, response_text, verdict=Verdict.FAIL):
    base = _default_base("belief")
    params = _apply_config(base, latent_params)
    world = generate(params, task_seed)
    name_map = build_name_map(world, task_seed)
    rendered = render(world, name_map, task_seed)
    return ExperimentRecord(
        run_id=run_id,
        model_name=model,
        latent_params=latent_params,
        realized_dims={},
        render_mode=RenderMode.TEMPLATE,
        task_seed=task_seed,
        trial_seed=0,
        rendering_seed=task_seed,
        prompt_text=rendered.prompt,
        response_text=response_text,
        verdict=verdict,
        verifier_detail="kind=belief;campaign=1",
        tokens_in=10,
        tokens_out=5,
        cost_usd=0.0,
        latency_ms=1.0,
    )


def test_classify_fail_matches_final_true_state(tmp_path):
    latent_params = {
        "chain_length": 5.0, "scramble": 0.0, "coref_pressure": 0.0,
        "distractor_events": 0.0, "filler_events": 1.0, "witness_cutoff": 0.8,
    }
    # Find a seed producing a final_true_state trap.
    task_seed = None
    for seed in range(30):
        base = _default_base("belief")
        params = _apply_config(base, latent_params)
        world = generate(params, seed)
        name_map = build_name_map(world, seed)
        from cartographer.world.foils import foil_divergence

        profile = foil_divergence(world, name_map)
        if "final_true_state" in profile.trap_kinds:
            task_seed = seed
            wrong_answer = profile.foils["final_true_state"]
            break
    assert task_seed is not None

    record = _make_record(
        "run-x", "model/a", latent_params, task_seed, response_text=wrong_answer
    )
    trap_kind, reconstructed = foil_report.classify_fail(record)
    assert trap_kind == "final_true_state"
    assert reconstructed is True


def test_classify_fail_unclassified_when_response_matches_nothing(tmp_path):
    latent_params = {
        "chain_length": 5.0, "scramble": 0.0, "coref_pressure": 0.0,
        "distractor_events": 0.0, "filler_events": 1.0, "witness_cutoff": 0.8,
    }
    record = _make_record(
        "run-x", "model/a", latent_params, task_seed=1,
        response_text="somewhere that is definitely not in this story at all",
    )
    trap_kind, _ = foil_report.classify_fail(record)
    assert trap_kind is None


def test_build_report_end_to_end(tmp_path):
    db_path = tmp_path / "synthetic.sqlite"
    store = ResultsStore(db_path)

    latent_params = {
        "chain_length": 5.0, "scramble": 0.0, "coref_pressure": 0.0,
        "distractor_events": 0.0, "filler_events": 1.0, "witness_cutoff": 0.8,
    }
    from cartographer.world.foils import foil_divergence

    trapping_seed = None
    for seed in range(30):
        base = _default_base("belief")
        params = _apply_config(base, latent_params)
        world = generate(params, seed)
        name_map = build_name_map(world, seed)
        profile = foil_divergence(world, name_map)
        if "final_true_state" in profile.trap_kinds:
            trapping_seed = seed
            wrong_answer = profile.foils["final_true_state"]
            break
    assert trapping_seed is not None

    record = _make_record(
        "run-synth", "acme/tiny-model", latent_params, trapping_seed, response_text=wrong_answer
    )
    store.append(record)
    store.close()

    report = foil_report.build_report(db_path=db_path, run_ids=["run-synth"])
    assert report["per_model_totals"]["acme/tiny-model"] == 1
    assert report["per_model"]["acme/tiny-model"]["final_true_state"] == 1
    assert report["per_model_reconstructed"]["acme/tiny-model"] == 1

    # print_report must not raise on this report.
    foil_report.print_report(report)


def test_build_report_empty_run_id_is_harmless(tmp_path):
    db_path = tmp_path / "empty.sqlite"
    ResultsStore(db_path).close()
    report = foil_report.build_report(db_path=db_path, run_ids=["does-not-exist"])
    assert report["per_model_totals"] == {}
    foil_report.print_report(report)  # must not raise on an empty report
