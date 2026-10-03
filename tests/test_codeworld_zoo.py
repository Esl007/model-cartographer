"""Province IV synthetic zoo (spec §11): planted fake readers, each a perfect
reader with one wrong belief, run through the REAL `CodeTraceEvaluator` and
`find_breaking_point`. The instrument must break each fake on exactly its
planted trap and attribute the break to it, before any live spend."""

from __future__ import annotations

import pytest

from cartographer.codeworld import (
    FOILS,
    TRAP_FOILS,
    CodeDimParams,
    answer,
    apply_ablation,
    generate,
    measure,
    render,
    run,
    verify_answer,
)
from cartographer.codeworld.generate import GenerationFailed
from cartographer.exec import evaluator as ev_mod
from cartographer.exec.client import ChatResult
from cartographer.exec.evaluator import (
    CodeTraceEvaluator,
    build_code_task,
    default_code_length_ray,
    default_code_lookalike_ray,
    default_code_trap_ray,
    run_code_breaking_point_campaign,
)
from cartographer.schedule.escalate import find_breaking_point
from cartographer.store.schema import Verdict
from cartographer.store.store import ResultsStore

# --- the fakes: (program, rendered) -> answer value -----------------------------


def perfect(prog, rendered):
    return answer(prog)


def foil_reader(foil):
    return lambda prog, rendered: answer(prog, FOILS[foil])


def sibling_confuser(prog, rendered):
    twin = rendered.lookalike_of(prog, prog.query.target)
    return run(prog).globals[twin] if twin is not None else answer(prog)


def length_limited(limit):
    def read(prog, rendered):
        truth = answer(prog)
        if measure(prog)["trace_length"] <= limit:
            return truth
        blind = answer(prog, FOILS["all_branches"])
        if blind != truth:
            return blind
        return truth + 1 if isinstance(truth, int) else truth + [0]  # a guaranteed miss
    return read


FOIL_FAKES = {trap: foil for trap, foil in TRAP_FOILS.items()}


class ZooClient:
    """Fake chat client. `CodeTraceEvaluator` renders the program; the zoo
    fixture records prompt -> (program, rendered) so the fake can read the
    program it was shown with its one wrong belief."""

    def __init__(self, reader, seen: dict, finish_reason: str = "stop"):
        self.reader, self.seen, self.finish_reason = reader, seen, finish_reason
        self.calls = 0

    def chat(self, model, prompt, max_tokens=0, temperature=0.0, retry_on_length=True, max_tokens_retry=0):
        self.calls += 1
        prog, rendered = self.seen[prompt]
        text = f"Tracing...\nANSWER: {self.reader(prog, rendered)!r}"
        return ChatResult(text=text, tokens_in=500, tokens_out=800, latency_ms=1.0,
                          finish_reason=self.finish_reason)


@pytest.fixture
def seen(monkeypatch):
    seen: dict = {}
    real = ev_mod.code_render

    def recording(program, seed, *a, **kw):
        r = real(program, seed, *a, **kw)
        seen[r.prompt] = (program, r)
        return r

    monkeypatch.setattr(ev_mod, "code_render", recording)
    return seen


@pytest.fixture
def store(tmp_path):
    s = ResultsStore(tmp_path / "zoo.sqlite")
    yield s
    s.close()


def _evaluator(reader, seen, store, run_id="zoo", **kw):
    return CodeTraceEvaluator(ZooClient(reader, seen, **kw), "zoo-model", store, run_id, cost_cap_usd=1e9)


# --- each fake breaks on exactly its trap ----------------------------------------


@pytest.mark.parametrize("trap,foil", sorted(FOIL_FAKES.items()))
def test_each_planted_misreader_fails_exactly_on_its_trap(trap, foil, seen, store):
    ev = _evaluator(foil_reader(foil), seen, store)
    others = [k for k in TRAP_FOILS if k != trap]
    on = [ev({"n_statements": 8 + i % 20, trap: 1, others[i % 5]: i % 2}, i) for i in range(25)]
    off = [ev({"n_statements": 8 + i % 20, trap: 0, others[i % 5]: 1, "max_nesting": i % 3}, i) for i in range(25)]
    assert not any(on), f"{foil} passed with {trap} on"
    assert all(off), f"{foil} failed with {trap} off"
    fails = [r for r in store.by_run("zoo") if r.verdict == Verdict.FAIL]
    assert len(fails) == 25
    assert all(f"detail=foil:" in r.verifier_detail and foil in r.verifier_detail for r in fails)
    assert not any("detail=wrong_value" in r.verifier_detail for r in fails)


def test_records_carry_kind_params_and_realised_dims(seen, store):
    ev = _evaluator(foil_reader("value_semantics"), seen, store)
    ev({"n_statements": 12, "aliasing_on": 1, "identifier_confusability": 0.5}, 3)
    (r,) = store.by_run("zoo")
    assert r.verifier_detail.startswith("kind=code_trace;campaign=1;query=final_value;detail=")
    assert "traps=aliasing_on=1;late_binding_on=0" in r.verifier_detail
    assert "confusability=0.5" in r.verifier_detail
    assert r.latent_params["aliasing_on"] == 1.0 and r.latent_params["n_statements"] == 12.0
    for k in ("trace_length", "n_live_aliases", "n_foil_matches", "identifier_confusability"):
        assert k in r.realized_dims
    assert r.cost_usd == 0.0 or r.cost_usd >= 0  # unknown model -> $0
    assert r.prompt_text and "```python" in r.prompt_text


def test_truncated_answers_are_errors_not_failures(seen, store):
    ev = _evaluator(perfect, seen, store, finish_reason="length")
    assert ev({"n_statements": 8}, 1) is False
    (r,) = store.by_run("zoo")
    assert r.verdict == Verdict.ERROR and "cap_hit=1" in r.verifier_detail


def test_generation_failure_is_an_error_record(seen, store, monkeypatch):
    def boom(params, seed, *a, **kw):
        raise GenerationFailed("planted")

    monkeypatch.setattr(ev_mod, "code_generate", boom)
    ev = _evaluator(perfect, seen, store)
    assert ev({"n_statements": 8}, 1) is False
    (r,) = store.by_run("zoo")
    assert r.verdict == Verdict.ERROR and "generation_failed=1" in r.verifier_detail


def test_cost_cap_stops_spending(seen, store, monkeypatch):
    monkeypatch.setattr(ev_mod, "cost_usd", lambda m, i, o: 0.01)
    client = ZooClient(perfect, seen)
    ev = CodeTraceEvaluator(client, "zoo-model", store, "zoo", cost_cap_usd=0.025)
    results = [ev({"n_statements": 8}, i) for i in range(6)]
    assert client.calls == 3 and results[:3] == [True] * 3 and not any(results[3:])
    assert sum("cost_capped=1" in (r.verifier_detail or "") for r in store.by_run("zoo")) == 3


# --- escalation finds and attributes the planted break ------------------------------


def test_escalation_attributes_the_break_to_aliasing(seen, store):
    ev = _evaluator(foil_reader("value_semantics"), seen, store)
    bp = find_breaking_point(ev, default_code_trap_ray(), budget=300, k_trials=7)
    assert bp.found
    assert bp.hypothesis is not None
    assert bp.hypothesis.necessary_factors == ("aliasing_on",)
    assert "aliasing_on" in bp.hypothesis.factors


def test_perfect_reader_never_breaks(seen, store):
    for ray in (default_code_lookalike_ray(), default_code_trap_ray()):
        bp = find_breaking_point(_evaluator(perfect, seen, store), ray, budget=120, k_trials=5)
        assert not bp.found and bp.degradation_onset is None


def test_sibling_confuser_breaks_where_the_target_first_gets_a_lookalike(seen, store):
    ev = _evaluator(sibling_confuser, seen, store)
    bp = find_breaking_point(ev, default_code_lookalike_ray(), budget=300, k_trials=7)
    assert bp.found
    # n_variables defaults to 3, so the target gets a twin once round(c * 3) >= 1, i.e. c >= 1/6.
    # The bisect bracket is the sharp signal here: on a perfect 100% -> 0% step
    # the logistic t* is biased low (separable data), so it is not asserted.
    assert 1 / 6 <= bp.config_at_break["identifier_confusability"] <= 1 / 6 + 0.035
    # causal check: the same programs with descriptive names are answered correctly
    fails = [r for r in store.by_run("zoo") if r.verdict == Verdict.FAIL]
    assert fails and all("detail=confused_identifier:" in r.verifier_detail for r in fails)
    rescued = 0
    for r in fails[:15]:
        _, prog, rendered, _ = build_code_task(
            {"n_statements": r.latent_params["n_statements"],
             "identifier_confusability": r.latent_params["identifier_confusability"]}, r.trial_seed)
        _, plain = apply_ablation(prog, "rename_descriptive")
        text = f"ANSWER: {sibling_confuser(prog, plain)!r}"
        rescued += verify_answer(text, prog, plain).verdict == Verdict.PASS
    assert rescued == min(15, len(fails))


def test_length_limited_reader_breaks_where_traces_cross_its_limit(seen, store):
    ev = _evaluator(length_limited(40), seen, store)
    ray = default_code_length_ray()
    bp = find_breaking_point(ev, ray, budget=300, k_trials=7)
    assert bp.found

    def median_trace(t):
        cfg = ray.config_at(min(max(t, 0.0), 1.0))
        lens = sorted(measure(build_code_task(cfg, s)[1])["trace_length"] for s in range(15))
        return lens[len(lens) // 2]

    assert median_trace(bp.t_star - 0.15) <= 40 < median_trace(bp.t_star + 0.15)


def test_campaign_runner_wires_the_default_ray(seen, store):
    bp = run_code_breaking_point_campaign(ZooClient(perfect, seen), store, "zoo-camp", "zoo-model",
                                          budget=40, cost_cap=1.0, k_trials=4, parallelism=2)
    assert not bp.found and "spent=$" in bp.note
    assert all("kind=code_trace" in r.verifier_detail for r in store.by_run("zoo-camp"))
