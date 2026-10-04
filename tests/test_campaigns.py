"""cartographer.campaigns: registry coverage of every legacy campaign name,
run-id templates vs frontier.SPECS regexes, evidence tiers, the pass^k
consistency campaign, and the generated catalog. Zero network."""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import random
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cartographer.campaigns import tiers as T
from cartographer.campaigns.consistency import run_consistency_campaign
from cartographer.campaigns.registry import (
    PROVINCES, REGISTRY, by_province, catalog_markdown, next_run_id, resolve, write_catalog,
)
from cartographer.report import frontier
from cartographer.schedule.escalate import EscalationRay
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore

ROOT = Path(__file__).resolve().parents[1]
M = "nvidia/test-model"


def _stages() -> dict:
    spec = importlib.util.spec_from_file_location("_rap", ROOT / "scripts" / "run_all_provinces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.STAGES


def _owners(alias: str) -> list[str]:
    return [s.id for s in REGISTRY.values() if alias in s.legacy_aliases]


# --- registry coverage ---------------------------------------------------------


def test_every_launch_preset_resolves_to_exactly_one_id():
    from cartographer.viewer.launch import CAMPAIGN_PRESETS

    for preset in CAMPAIGN_PRESETS:
        owners = _owners(preset)
        assert len(owners) == 1, (preset, owners)
        assert resolve(preset).id == owners[0]


def test_every_script_stage_resolves_to_exactly_one_id():
    for stage in _stages():
        owners = _owners(stage)
        assert len(owners) == 1, (stage, owners)
        assert resolve(stage).id == owners[0]


def test_ticket_rays_resolve_and_use_the_script_rays():
    from cartographer.interactive.campaign import TICKET_RAYS, run_ticket_fidelity_campaign

    for ray in TICKET_RAYS:
        spec = resolve(f"ticket_{ray}")
        assert spec.id == f"III.ticket_{ray}"
        assert spec.ray_factory is TICKET_RAYS[ray]
        assert spec.runner is run_ticket_fidelity_campaign


def test_ids_unique_well_formed_and_complete():
    ids = list(REGISTRY)
    assert len(ids) == len(set(ids))
    for cid, s in REGISTRY.items():
        assert s.id == cid
        assert re.fullmatch(r"(I|II|III|IV|X)\.[a-z_]+", cid), cid
        assert cid.split(".")[0] == s.province and s.province in PROVINCES
        assert s.evidence_kind in ("axis", "rate", "battery")
        assert s.question.endswith("?") and s.stressor
        assert resolve(cid) is s
    aliases = [a for s in REGISTRY.values() for a in s.legacy_aliases]
    assert len(aliases) == len(set(aliases))
    required = {"I.state", "I.belief", "I.order", "I.adversarial_belief", "I.breaking_point", "II.plan_length",
                "II.plan_length_lookalike", "II.traps", "II.catalog_size", "II.impossible", "III.values",
                "III.lookalike", "III.fault", "III.ticket_mechanism", "III.ticket_trap", "III.ticket_length",
                "IV.lookalike", "IV.traps", "IV.length", "IV.copy", "X.full_map", "X.consistency"}
    assert required <= set(ids)
    assert sum(len(v) for v in by_province().values()) == len(ids)
    with pytest.raises(KeyError):
        resolve("no_such_campaign")


def test_runners_callable_with_campaign_signature_and_rays_build():
    for s in REGISTRY.values():
        assert callable(s.runner), s.id
        first = list(inspect.signature(s.runner).parameters)[:4]
        assert first == ["client", "store", "run_id", "model"], (s.id, first)
        params = inspect.signature(s.runner).parameters
        for key in s.default_kwargs:
            assert key in params, (s.id, key)  # registry defaults never crash a runner
        if s.ray_factory is not None:
            assert "ray" in params, s.id
            assert isinstance(s.ray_factory(), EscalationRay), s.id


def test_run_id_templates_match_declared_frontier_rows():
    specs = {sp.key: sp for sp in frontier.SPECS}
    for s in REGISTRY.values():
        for key in s.frontier_rows:
            assert key in specs, (s.id, key)
        if not s.frontier_rows:
            continue

        def feeds(label: str) -> bool:
            rid = s.run_id_template.format(label=label, n=1)
            return all(not specs[k].runs or any(re.search(p, rid) for p in specs[k].runs) for k in s.frontier_rows)

        if s.frontier_labels:
            # The whitelist note must be true: "x" is NOT picked up, the listed labels are.
            assert not feeds("x"), s.id
            assert all(feeds(lab) for lab in s.frontier_labels), s.id
        else:
            assert feeds("x"), (s.id, s.run_id_template)


# --- tiers -----------------------------------------------------------------------


def test_tiers_monotone_and_scout_not_reportable():
    sc, st, rg = T.TIERS["scout"], T.TIERS["standard"], T.TIERS["rigorous"]
    assert sc.k_trials < st.k_trials < rg.k_trials
    assert sc.budget_scale < st.budget_scale <= rg.budget_scale == 1.0
    assert sc.cost_scale < st.cost_scale <= rg.cost_scale == 1.0
    assert rg.cost_scale / sc.cost_scale >= 2.5  # ~3x cheaper
    assert not sc.reportable and sc.grade_ceiling == "C"
    assert st.reportable and rg.reportable and rg.grade_ceiling == "A"
    assert sc.early_stop and not rg.early_stop


def test_apply_tier_only_passes_declared_kwargs(monkeypatch):
    def narrow(client, store, run_id, model, budget=100):
        return None

    spec = dataclasses.replace(REGISTRY["IV.traps"], runner=narrow)  # defaults carry k_trials, cost_cap, ...
    assert T.apply_tier(spec, "scout", {}) == {"budget": 70}  # 200 x 0.35

    def gated(client, store, run_id, model, budget=50, cost_cap=1.0, k_trials=7, resample_gate=True,
              early_stop=False):
        return None

    bare = dataclasses.replace(REGISTRY["IV.traps"], runner=gated, default_kwargs={})
    monkeypatch.setattr(T, "find_breaking_point_accepts", lambda name: False)
    kw = T.apply_tier(bare, "scout")
    assert kw == {"budget": 18, "cost_cap": 0.35, "k_trials": 3, "resample_gate": False}
    monkeypatch.setattr(T, "find_breaking_point_accepts", lambda name: True)
    assert T.apply_tier(bare, "rigorous")["early_stop"] is False
    assert T.apply_tier(bare, "scout")["early_stop"] is True


def test_apply_tier_user_choices_win_and_verify_maps_the_gate():
    spec = REGISTRY["IV.lookalike"]
    # the code runner declares resample_gate / early_stop, so the tier passes them directly
    assert T.apply_tier(spec, "scout")["resample_gate"] is False
    assert T.apply_tier(spec, "standard")["resample_gate"] is True
    assert T.apply_tier(spec, "standard")["early_stop"] is True
    assert T.apply_tier(spec, "rigorous")["early_stop"] is False
    # a runner that only knows `verify` gets the gate mapped onto it
    sweep = REGISTRY["II.sweep"]
    assert T.apply_tier(sweep, "scout")["verify"] == "manual"
    assert T.apply_tier(sweep, "standard")["verify"] == "auto"
    kw = T.apply_tier(spec, "scout", {"budget": 999, "cost_cap": 0.9, "resample_gate": True, "k_trials": 9})
    assert (kw["budget"], kw["cost_cap"], kw["resample_gate"], kw["k_trials"]) == (999, 0.9, True, 9)
    # pass^k repeat count defines the metric: never moved by the tier.
    assert T.apply_tier(REGISTRY["X.consistency"], "scout")["k"] == 5


# --- consistency (pass^k) --------------------------------------------------------


class FlakyEvaluator:
    """Every task passes each attempt independently with probability p."""

    def __init__(self, p: float, seed: int = 0):
        self.p, self.rng, self.lock, self.calls = p, random.Random(seed), threading.Lock(), []

    def __call__(self, config, trial_seed):
        with self.lock:
            self.calls.append((dict(config), trial_seed))
            return self.rng.random() < self.p


class DeterministicEvaluator:
    """A task either always passes or always fails."""

    def __call__(self, config, trial_seed):
        return trial_seed % 3 != 0


def test_consistency_flaky_model_pass_k_collapses():
    ev = FlakyEvaluator(0.7, seed=11)
    res = run_consistency_campaign(None, None, "camp-consistency-t-001", M, evaluator_factory=lambda rid: ev,
                                   config={"plan_depth": 11}, m_tasks=80, k=5, parallelism=1, side_file="")
    assert 0.62 <= res.pass1 <= 0.78
    assert res.pass_k < res.pass1 - 0.35  # 0.7**5 = 0.17
    assert res.flaky_tasks and res.tasks_complete == 80 and res.n_scored == 400
    seeds = [s for _, s in ev.calls]
    assert all(seeds.count(s) == 5 for s in set(seeds))  # each task repeated exactly k times
    assert all(c == {"plan_depth": 11} for c, _ in ev.calls)  # same config every repeat
    assert res.wilson_passk[0] <= res.pass_k <= res.wilson_passk[1]


def test_consistency_deterministic_model_pass_k_equals_pass1(tmp_path):
    side = tmp_path / "c.json"
    res = run_consistency_campaign(None, None, "camp-consistency-t-002", M,
                                   evaluator_factory=lambda rid: DeterministicEvaluator(),
                                   config={"plan_depth": 11}, m_tasks=12, k=5, parallelism=4, side_file=str(side))
    assert res.pass_k == pytest.approx(res.pass1)
    assert res.flaky_tasks == []
    assert sorted(n for _, n in res.per_task) == sorted(0 if s % 3 == 0 else 5 for s in range(1, 13))
    assert side.exists() and '"pass_k"' in side.read_text()


def test_consistency_cost_cap_trials_are_unscored_not_failures():
    class Capping:
        capped = False

        def __init__(self):
            self.n = 0

        def __call__(self, config, trial_seed):
            self.n += 1
            if self.n > 10:
                self.capped = True
                return False
            return True

    res = run_consistency_campaign(None, None, "camp-consistency-t-003", M, evaluator_factory=lambda rid: Capping(),
                                   config={}, m_tasks=5, k=5, parallelism=1, side_file="")
    assert res.pass1 == 1.0 and res.pass_k == 1.0 and res.tasks_complete == 2 and res.capped


def _rec(run_id, verdict, trial_seed, latent):
    return ExperimentRecord(
        run_id=run_id, model_name=M, latent_params=latent, realized_dims={}, render_mode=RenderMode.TEMPLATE,
        task_seed=trial_seed, trial_seed=trial_seed, rendering_seed=trial_seed, prompt_text="p",
        response_text="r", verdict=verdict, verifier_detail="kind=tool_plan;campaign=1", tokens_in=1,
        tokens_out=1, cost_usd=0.0, latency_ms=1.0,
    )


def test_consistency_reads_store_verdicts_and_feeds_frontier(tmp_path):
    store = ResultsStore(tmp_path / "s.sqlite")
    cfg = {"plan_depth": 11.0, "id_collisions_on": 1.0}

    def factory(run_id):
        count = {}

        def ev(config, trial_seed):
            i = count[trial_seed] = count.get(trial_seed, 0) + 1
            verdict = Verdict.ERROR if (trial_seed == 1 and i == 1) else (
                Verdict.PASS if (trial_seed % 2 or i < 5) else Verdict.FAIL)
            store.append(_rec(run_id, verdict, trial_seed, dict(config)))
            return verdict == Verdict.PASS

        return ev

    run_id = "camp-consistency-t-001"
    res = run_consistency_campaign(None, store, run_id, M, evaluator_factory=factory, config=cfg, m_tasks=6, k=5,
                                   parallelism=1, side_file="")
    assert res.n_unscored == 1 and res.n_scored == 29  # the ERROR repeat is not a failure
    assert res.tasks_complete == 5  # task 1 has only 4 scored repeats
    assert res.flaky_tasks == [2, 4, 6] and res.pass_k == pytest.approx(2 / 5)
    row = next(r for r in frontier.frontier_summary(store, M).rows if r.key == "consistency")
    assert row.status != "NOT TESTED" and (row.n_pass, row.n_scored) == (26, 29)
    store.close()


# --- run ids, catalog, script ------------------------------------------------------


def test_next_run_id_increments_and_pins(tmp_path):
    db = tmp_path / "s.sqlite"
    assert next_run_id(REGISTRY["III.values"], "gemma", str(db)) == "camp-interactive-gemma-001"
    store = ResultsStore(db)
    store.append_many([_rec(rid, Verdict.PASS, 1, {}) for rid in
                       ("camp-interactive-gemma-001", "camp-interactive-gemma-003",
                        "camp-interactive-lookalike-gemma-007", "camp-interactive-gemma-003/verify")])
    store.close()
    assert next_run_id(REGISTRY["III.values"], "gemma", str(db)) == "camp-interactive-gemma-004"
    assert next_run_id(REGISTRY["III.lookalike"], "gemma", str(db)) == "camp-interactive-lookalike-gemma-008"
    assert next_run_id(REGISTRY["II.plan_length"], "gemma", str(db)) == "camp-tool-depth-gemma-003"
    con = sqlite3.connect(db)
    assert con.execute("select count(*) from experiments").fetchone()[0] == 4  # read-only scan
    con.close()


def test_catalog_mentions_every_id_and_tier(tmp_path):
    path = write_catalog(str(tmp_path / "catalog.md"))
    text = Path(path).read_text()
    assert text == catalog_markdown()
    for cid, s in REGISTRY.items():
        assert f"`{cid}`" in text
        for a in s.legacy_aliases:
            assert f"`{a}`" in text
    for tier in T.TIERS:
        assert f"`{tier}`" in text
    committed = ROOT / "docs" / "campaign-catalog.md"
    if committed.exists():
        assert committed.read_text() == text, "docs/campaign-catalog.md is stale: re-run --write-catalog"


def test_run_campaign_script_list_and_dry_run(tmp_path):
    from cartographer.exec.pricing import PRICES

    script = str(ROOT / "scripts" / "run_campaign.py")
    out = subprocess.run([sys.executable, script, "--list"], capture_output=True, text=True, check=True).stdout
    assert all(cid in out for cid in REGISTRY)
    model = next(iter(PRICES))
    dry = subprocess.run([sys.executable, script, "--campaign", "code_traps", "--tier", "scout", "--model", model,
                          "--label", "x", "--dry-run", "--db", str(tmp_path / "none.sqlite")],
                         capture_output=True, text=True, check=True).stdout
    line = next(ln for ln in dry.splitlines() if ln.startswith("DONE "))
    assert line.startswith("DONE IV.traps ") and '"camp-code-traps-x-001"' in line and '"reportable": false' in line
    bad = subprocess.run([sys.executable, script, "--campaign", "IV.traps", "--model", "not/a-priced-model",
                          "--label", "x", "--dry-run"], capture_output=True, text=True)
    assert bad.returncode != 0 and "pricing.py" in bad.stderr
