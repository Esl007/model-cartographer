"""Evidence Fidelity Campaign acceptance (docs/spec-evidence-fidelity-campaign.md §6-§7)."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from cartographer.campaigns.evidence_fidelity import (
    ARM_INDEX, FAMILIES, hypothesis_summary, holm, run_ef_family, wilcoxon_two_sided,
)
from cartographer.campaigns.evidence_zoo import EXPECTED, POLICIES, ZooClient, hook
from cartographer.store.schema import Verdict
from cartographer.store.store import ResultsStore


@pytest.fixture(scope="module")
def zoo_store(tmp_path_factory):
    """Every family x every policy, 4 paired seeds each (one shared store)."""
    store = ResultsStore(str(tmp_path_factory.mktemp("ef") / "zoo.sqlite"))
    results = {}
    for fam in FAMILIES:
        for pol in POLICIES:
            rid = f"camp-ef-{fam}-z{pol[:4]}-001"
            results[(fam, pol)] = run_ef_family(ZooClient(pol), store, rid, "zoo", fam, n_trials=4, parallelism=2,
                                                on_task=hook(fam))
    return store, results


# -- §6 zoo separations ----------------------------------------------------------------------------

@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_zoo_rescue_patterns(zoo_store, family):
    store, results = zoo_store
    rid = f"camp-ef-{family}-zprio-001"
    conflict = [r.trial_seed for r in store.by_run(f"{rid}/B") if r.verdict == Verdict.FAIL]
    assert len(conflict) >= 3, "too few conflict seeds to check a pattern"
    for pol in POLICIES:
        res = results[(family, pol)]
        for arm in res.arms:
            recs = {r.trial_seed: r for r in store.by_run(f"camp-ef-{family}-z{pol[:4]}-001/{arm}")}
            for s in conflict:
                r = recs[s]
                if r.verdict == Verdict.ERROR:
                    continue
                assert (r.verdict == Verdict.PASS) == EXPECTED[pol][arm], (family, pol, arm, s, r.verifier_detail)


@pytest.mark.parametrize("family", sorted(FAMILIES))
def test_mechanism_signature_follows_the_failures(zoo_store, family):
    _store, results = zoo_store
    prior = results[(family, "prior_follower")]
    perfect = results[(family, "evidence_follower")]
    assert prior.arms["B"].signature_mean and prior.arms["B"].signature_mean > 0
    assert all(st.signature_mean == 0 for st in perfect.arms.values())


def test_all_arms_of_a_seed_share_one_task(zoo_store):
    store, _ = zoo_store
    for fam in FAMILIES:
        seeds = {}
        for arm, extra in FAMILIES[fam].arms.items():
            if extra is None:
                continue
            for r in store.by_run(f"camp-ef-{fam}-zevid-001/{arm}"):
                seeds.setdefault(r.trial_seed, set()).add(r.task_seed)
        assert seeds and all(len(v) == 1 for v in seeds.values()), fam


def test_records_carry_condition_and_evidence_covariates(zoo_store):
    store, _ = zoo_store
    for fam in FAMILIES:
        for arm, extra in FAMILIES[fam].arms.items():
            if extra is None:
                continue
            for r in store.by_run(f"camp-ef-{fam}-zevid-001/{arm}"):
                assert r.realized_dims["condition"] == ARM_INDEX[arm]
                assert "evidence_span" in r.realized_dims and "evidence_turns" in r.realized_dims
    # restating the evidence before the question brings it nearer the decision point
    b = [r.realized_dims["evidence_span"] for r in store.by_run("camp-ef-belief-zevid-001/B")]
    c = [r.realized_dims["evidence_span"] for r in store.by_run("camp-ef-belief-zevid-001/C")]
    assert sum(c) / len(c) < sum(b) / len(b)


def test_control_gate_and_contrasts(zoo_store):
    _store, results = zoo_store
    for fam in FAMILIES:
        assert results[(fam, "prior_follower")].control_ok is True
        res = results[(fam, "retrieval_limited")]
        cC = next((c for c in res.contrasts if c.arm == "C"), None)
        cD = next(c for c in res.contrasts if c.arm == "D")
        if cC is not None:
            assert cC.c >= 3 and cC.b == 0
        assert cD.c == 0


def test_h_ef4_separates_the_two_families_on_planted_data(tmp_path):
    store = ResultsStore(str(tmp_path / "t.sqlite"))
    t = run_ef_family(ZooClient("retrieval_limited"), store, "camp-ef-transfer-z-001", "zoo", "tickets", n_trials=8,
                      arms=("B", "C", "X"), parallelism=2, prefix="/tickets", on_task=hook("tickets"))
    r = run_ef_family(ZooClient("retrieval_limited"), store, "camp-ef-transfer-z-001", "zoo", "repair", n_trials=8,
                      arms=("B", "C", "X"), parallelism=2, prefix="/repair", on_task=hook("repair"))
    assert hypothesis_summary({"tickets": t, "repair": r})["H-EF4"]["verdict"] == "two families"


# -- answer-key preservation (each new transform) -----------------------------------------------------

def _belief_world(seed):
    from cartographer.exec.evaluator import _apply_config, _default_base
    from cartographer.render.naming import build_name_map
    from cartographer.render.templates import render
    from cartographer.world.generate import generate

    w = generate(_apply_config(_default_base("belief"), FAMILIES["belief"].base), seed)
    nm = build_name_map(w, seed)
    return w, nm, render(w, nm, seed)


def test_belief_restatement_and_emphasis_carry_no_new_information():
    from cartographer.ablate.ablations import _split_story_sentences, pronoun_suppression
    from cartographer.ablate.evidence import (
        EvidenceUnavailable, belief_evidence_sentences, witness_emphasis, witness_restatement,
    )

    checked = 0
    for seed in range(30):
        w, nm, r = _belief_world(seed)
        try:
            hits = belief_evidence_sentences(w, r)
        except EvidenceUnavailable:
            continue
        checked += 1
        plain = _split_story_sentences(pronoun_suppression(w, nm, seed).prompt)
        c = witness_restatement(w, nm, r, seed)
        recall = c.split("\n\nRecall:\n", 1)[1].rsplit("\n\n", 1)[0].split("\n")
        assert recall == [plain[i] for i in hits]  # verbatim copies of story sentences, names for pronouns
        assert c.startswith(r.prompt.rsplit("\n\n", 1)[0]) and c.endswith(r.prompt.rsplit("\n\n", 1)[1])
        d = witness_emphasis(w, nm, r, seed)
        assert re.sub(r'Note: "(.*?)"', r"\1", d) == r.prompt  # D is formatting only
    assert checked >= 20


def test_tool_id_presentations_change_text_only():
    from cartographer.exec.evaluator import _default_tool_base, _apply_tool_config
    from cartographer.toolworld.generate import generate
    from cartographer.toolworld.mutate import id_collision
    from cartographer.toolworld.render import render

    for seed in range(10):
        w = id_collision(generate(_apply_tool_config(_default_tool_base(), {"plan_depth": 10.0}), seed), seed)
        base = render(w, seed).prompt
        assert render(w, seed, restate_ids=False, ids_emphasis=False).prompt == base
        c = render(w, seed, restate_ids=True).prompt
        assert c.startswith(base) and all(t.id in c[len(base):] for t in w.catalog)
        d = render(w, seed, ids_emphasis=True).prompt
        assert re.sub(r"- id: `([^`]+)`", r"- id: \1", d) == base


def test_code_presentations_change_comments_only():
    from cartographer.exec.evaluator import build_code_task

    for ts in range(10):
        cfg = {**FAMILIES["comments"].base, "misleading_comments": 2.0}
        _, prog_b, rb, _ = build_code_task(cfg, ts, pair_keys=("misleading_comments", "neutral_comments"))
        _, prog_d, rd, _ = build_code_task({**cfg, "line_emphasis": 1.0}, ts,
                                           pair_keys=("misleading_comments", "neutral_comments"))
        _, prog_e, re_, _ = build_code_task({**cfg, "prior_emphasis": 1.0}, ts,
                                            pair_keys=("misleading_comments", "neutral_comments"))
        assert prog_b.body == prog_d.body == prog_e.body
        assert rd.source.replace(" <<<", "") == rb.source
        strip = lambda src: [ln.split("  #")[0] for ln in src.splitlines()]  # noqa: E731
        assert strip(re_.source) == strip(rb.source)


def test_ticket_conditions_keep_the_world_and_accept_delimiters():
    from cartographer.exec.evaluator import _apply_tool_config, _default_tool_base
    from cartographer.interactive.env import InteractiveEnv, InteractiveSpec
    from cartographer.toolworld.generate import generate

    w = generate(_apply_tool_config(_default_tool_base(), {"plan_depth": 8.0}), 3)
    base = InteractiveEnv(w, 3, InteractiveSpec(carry_distance=3, n_live_tickets=1))
    emph = InteractiveEnv(w, 3, InteractiveSpec(carry_distance=3, n_live_tickets=1, ticket_emphasis=True))
    guess = InteractiveEnv(w, 3, InteractiveSpec(carry_distance=3, n_live_tickets=1, ticket_guessable=True))
    assert [c.tool for c in base.plan] == [c.tool for c in emph.plan] == [c.tool for c in guess.plan]
    assert base.links == emph.links
    for env, wrap in ((emph, True), (emph, False)):
        for c in env.plan:
            args = {k: str(v) for k, v in c.args.items()}
            if c.tool in env.producer_for:
                t = env.tickets[env.producer_for[c.tool]]
                args["ticket"] = f"<<{t}>>" if wrap else t
            out = env.step(c.tool, args)
            if "ticket" in out:
                assert out["ticket"].startswith("<<") and out["ticket"].endswith(">>")
        assert env.goal_reached
        emph = InteractiveEnv(w, 3, InteractiveSpec(carry_distance=3, n_live_tickets=1, ticket_emphasis=True))


def test_repair_conditions_keep_the_program():
    from cartographer.repo.compose import RepoParams, compose
    from cartographer.repo.env import RepoEnv

    p = RepoParams(n_modules=4, bug_distance=2)
    for seed in range(6):
        b = compose(p, seed)
        for flags in ({"show_data_flow": True}, {"diff_emphasis": True}, {"restate_symbols": True}):
            assert compose(replace(p, **flags), seed).files == b.files
        a = compose(replace(p, bug_local=True), seed)
        assert a.reference.body == b.reference.body and a.module_of == b.module_of
        assert a.bug_module == a.entry_module and a.carrier == "solve"
        flow = RepoEnv(compose(replace(p, show_data_flow=True), seed)).run_tests_text()
        assert f"-> returns {b.visible_wrong!r}" in flow.split("Data flow for", 1)[1].split("\n")[1]
    with pytest.raises(ValueError):
        RepoParams(bug_local=True, clone_trap=True)


# -- legacy behaviour unchanged ----------------------------------------------------------------------

def test_legacy_seeds_and_renders_unchanged():
    from cartographer.codeworld.generate import generate as cgen
    from cartographer.codeworld.render import render as crender
    from cartographer.exec.evaluator import _seed_config, _task_seed
    from cartographer.repo.compose import RepoParams

    cfg = {"plan_depth": 10.0, "id_collisions_on": 1.0, "witness_cutoff": 0.3}
    assert _seed_config(cfg) == cfg and _task_seed(_seed_config(cfg), 5) == _task_seed(cfg, 5)
    p = RepoParams()
    assert replace(p, bug_local=True, show_data_flow=True, diff_emphasis=True).latent_key() == p.latent_key()
    from cartographer.exec.evaluator import _apply_code_config, _default_code_base

    prog = cgen(_apply_code_config(_default_code_base(), {"misleading_comments": 2.0}), 7)
    assert crender(prog, 7).prompt == crender(prog, 7, emphasis="").prompt


def test_narrative_evaluator_prompt_unchanged_without_ef_keys(tmp_path):
    from cartographer.exec.client import ChatResult
    from cartographer.exec.evaluator import PipelineEvaluator, _apply_config, _task_seed
    from cartographer.render.naming import build_name_map
    from cartographer.render.templates import render
    from cartographer.world.generate import generate

    seen = []

    class C:
        def chat(self, model, prompt, max_tokens=0, max_tokens_retry=0):
            seen.append(prompt)
            return ChatResult(text="x", tokens_in=1, tokens_out=1, latency_ms=1.0, finish_reason="stop")

    store = ResultsStore(str(tmp_path / "n.sqlite"))
    ev = PipelineEvaluator(C(), "m", store, "r", "belief", 1.0)
    cfg = {"chain_length": 8.0, "witness_cutoff": 0.0}
    ev(cfg, 4)
    seed = _task_seed(cfg, 4)
    w = generate(_apply_config(ev.base, cfg), seed)
    assert seen[0] == render(w, build_name_map(w, seed), seed).prompt


# -- statistics and wiring -------------------------------------------------------------------------------

def test_holm_and_wilcoxon():
    assert holm([0.01, 0.04, 0.03]) == [0.03, 0.06, 0.06]
    assert wilcoxon_two_sided([0, 0, 0]) == 1.0
    assert wilcoxon_two_sided([-1] * 8) == pytest.approx(2 / 256)


def test_registry_and_frontier_isolation(tmp_path):
    from cartographer.campaigns.registry import REGISTRY, catalog_markdown
    from cartographer.report import frontier
    from cartographer.store.schema import ExperimentRecord, RenderMode

    ids = [k for k in REGISTRY if k.startswith("EF.")]
    assert set(ids) == {"EF.belief", "EF.tools", "EF.tickets", "EF.comments", "EF.repair", "EF.transfer"}
    assert all(i in catalog_markdown() for i in ids)
    rec = ExperimentRecord(run_id="camp-ef-belief-x-001/B", model_name="m", latent_params={}, realized_dims={},
                           render_mode=RenderMode.TEMPLATE, task_seed=0, trial_seed=0, verdict=Verdict.PASS,
                           verifier_detail="kind=belief;campaign=1")
    specs = {s.key: s for s in frontier.SPECS}
    assert frontier._matches(specs["ef_belief"], rec) and not frontier._matches(specs["belief"], rec)
    assert not frontier._matches(specs["ef_belief"], replace(rec, run_id="camp-ef-belief-x-001/A"))
