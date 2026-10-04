"""Trivialized false belief through the REAL narrative evaluator (fake client,
no spend): a template-matching reader that always answers the classic trap
location fails exactly when `full_witness` is on, its failures classify as
`stale_belief`, the resample gate re-verifies against the trivialized world,
and the record carries `full_witness=1` so it can be rebuilt."""

from __future__ import annotations

import pytest

from cartographer.exec.client import ChatResult
from cartographer.exec.evaluator import (
    PipelineEvaluator,
    _apply_config,
    _default_base,
    _regenerate_for_verify,
    _task_seed,
    default_trivial_belief_ray,
    run_breaking_point_campaign,
)
from cartographer.report.verdict import classify_belief_failure
from cartographer.schedule.gate import ResampleGatedEvaluator
from cartographer.store.schema import Verdict
from cartographer.store.store import ResultsStore
from cartographer.world.generate import generate
from cartographer.world.trivial import stale_location
from cartographer.render import build_name_map, render


class TemplateMatcher:
    """Answers the classic false-belief trap location (where the agent LAST
    saw the object before the final move), regardless of what it witnessed."""

    def __init__(self):
        self.calls = 0

    def chat(self, model, prompt, max_tokens=0, max_tokens_retry=0, **kw):
        self.calls += 1
        config, trial_seed = _pending[-1]
        params = _apply_config(_default_base("belief"), config)
        seed = _task_seed(config, trial_seed)
        world = generate(params, seed)
        nm = build_name_map(world, seed)
        loc = stale_location(world)
        text = nm.location[loc] if loc in nm.location else str(loc)
        return ChatResult(text=text, tokens_in=10, tokens_out=3, latency_ms=1.0, finish_reason="stop")


_pending: list = []


class Tracking(PipelineEvaluator):
    def __call__(self, config, trial_seed, run_suffix=""):
        _pending.append((config, trial_seed))
        return super().__call__(config, trial_seed, run_suffix)


@pytest.fixture
def store(tmp_path):
    s = ResultsStore(tmp_path / "t.sqlite")
    yield s
    s.close()


def test_template_matcher_fails_only_on_full_witness_worlds(store):
    ev = Tracking(TemplateMatcher(), "m", store, "tb", "belief", 1e9)
    base = {"chain_length": 5.0, "witness_cutoff": 0.0}
    classic = [ev(base, i) for i in range(12)]
    trivial = [ev({**base, "full_witness": 1.0}, i) for i in range(12)]
    assert all(classic)  # the trap answer IS the right answer on the classic story
    assert not any(trivial)
    recs = store.by_run("tb")
    fails = [r for r in recs if r.verdict == Verdict.FAIL]
    assert len(fails) == 12 and all(r.latent_params.get("full_witness") == 1.0 for r in fails)
    assert all(classify_belief_failure(r) == "stale_belief" for r in fails)


def test_verify_regeneration_uses_the_trivialized_world(store):
    ev = Tracking(TemplateMatcher(), "m", store, "rv", "belief", 1e9)
    ev({"chain_length": 4.0, "witness_cutoff": 0.0, "full_witness": 1.0}, 3)
    (rec,) = store.by_run("rv")
    world, nm, rendered, _ = _regenerate_for_verify(rec)
    assert rendered.prompt == rec.prompt_text  # byte-identical rebuild, trivialization included


def test_gate_and_escalation_on_the_trivial_belief_ray(store):
    ev = ResampleGatedEvaluator(Tracking(TemplateMatcher(), "m", store, "camp-trivial-belief-zoo-001", "belief", 1e9))
    from cartographer.schedule.escalate import find_breaking_point

    bp = find_breaking_point(ev, default_trivial_belief_ray(), budget=60, k_trials=5)
    # full_witness is pinned ON along the whole ray, so the baseline itself fails:
    # escalation reports a degenerate ray, the honest answer for "breaks everywhere"
    assert bp.found is False and bp.pass_curve[0][1] == 0.0
    assert ev.stable > 0 and ev.noise == 0
    assert store.by_run("camp-trivial-belief-zoo-001/verify")
