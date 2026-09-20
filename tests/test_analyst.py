"""Tests for the Evidence Analyst (Phase 9 centerpiece).

Zero network. The analyst model is a scripted fake client; Tavily is a
mocked transport. The through-line these tests defend is the credibility
rule: every number in the report comes from the deterministic evidence
pack, the audit catches an invented number and passes a faithful one, and
the literature layer degrades gracefully without a key.
"""

from __future__ import annotations

from cartographer.analyst.analyst import (
    Diagnosis,
    audit_diagnosis,
    parse_sections,
    write_diagnosis,
)
from cartographer.analyst.corroborate import (
    Corroboration,
    corroborate_findings,
    load_tavily_key,
)
from cartographer.analyst.evidence import build_evidence_pack, render_pack
from cartographer.analyst.report_page import load_report, report_body
from cartographer.exec.client import ChatResult
from cartographer.exec.keys import load_keys
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore

MODEL_STRONG = "acme/strong-7b"
MODEL_WEAK = "acme/weak-7b"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _rec(run_id, model, verdict, *, kind="belief", seed=0, dims=None, latent=None, cost=0.0002):
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
        response_text="an answer",
        verdict=verdict,
        verifier_detail=f"kind={kind};campaign=1",
        tokens_in=40,
        tokens_out=12,
        cost_usd=cost,
        latency_ms=10.0,
    )


def _seed_store(tmp_path) -> ResultsStore:
    """STRONG: 30/30 state, 25/25 belief. WEAK: 30/30 state, belief with
    high-coreference failures. Small but enough that pack rates are exact
    and hand-checkable."""
    store = ResultsStore(tmp_path / "analyst.sqlite")
    records = []
    n = 0
    for _ in range(30):
        n += 1
        records.append(_rec("camp-state-strong-001", MODEL_STRONG, Verdict.PASS, kind="state", seed=n))
    for _ in range(30):
        n += 1
        records.append(_rec("camp-state-weak-001", MODEL_WEAK, Verdict.PASS, kind="state", seed=n))
    for _ in range(25):
        n += 1
        records.append(_rec("camp-belief-strong-001", MODEL_STRONG, Verdict.PASS, seed=n))
    # WEAK belief: 20 easy pass, 10 high-coref (2 pass, 8 fail)
    for _ in range(20):
        n += 1
        records.append(_rec("camp-belief-weak-001", MODEL_WEAK, Verdict.PASS, seed=n,
                            dims={"entity_state": 2.0, "coreference": 0.0}))
    for i in range(10):
        n += 1
        records.append(_rec("camp-belief-weak-001", MODEL_WEAK,
                            Verdict.PASS if i < 2 else Verdict.FAIL, seed=n,
                            dims={"entity_state": 2.0, "coreference": 5.0},
                            latent={"chain_length": 2.0, "coref_pressure": 5.0}))
    store.append_many(records)
    return store


class FakeClient:
    """Scripted chat client. `reply` is returned as text for every call
    (or `error` set). Records the prompts it saw."""

    def __init__(self, reply="", error=None):
        self.reply = reply
        self.error = error
        self.prompts = []

    def chat(self, model, prompt, max_tokens=60, **kw):
        self.prompts.append((model, prompt))
        return ChatResult(text=self.reply, tokens_in=0, tokens_out=0,
                          latency_ms=1.0, error=self.error)


# ---------------------------------------------------------------------------
# Evidence pack: numbers match SQL by hand
# ---------------------------------------------------------------------------


def test_pack_rates_match_hand_computation(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store)

    by_model = {m["model"]: m for m in pack["models"]}
    strong = by_model[MODEL_STRONG]
    weak = by_model[MODEL_WEAK]

    # STRONG: 55 scored (30 state + 25 belief), all pass -> 100.0
    assert strong["n_scored"] == 55
    assert strong["overall_pass_rate_pct"] == 100.0

    # WEAK: 60 scored (30 state + 30 belief), 8 fail -> 52/60 = 86.7
    assert weak["n_scored"] == 60
    assert weak["overall_pass_rate_pct"] == 86.7

    # WEAK belief cell: 22/30 = 73.3
    belief_cell = next(c for c in weak["capability_grid"] if c["key"] == "belief")
    assert belief_cell["n_scored"] == 30
    assert belief_cell["n_pass"] == 22
    assert belief_cell["pass_rate_pct"] == 73.3


def test_pack_store_totals_and_size(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store)
    assert pack["store_totals"]["n_models"] == 2
    assert pack["store_totals"]["n_trials"] == 115
    # cap: rendered pack stays reasonably small
    assert len(render_pack(pack)) < 20000


def test_pack_model_filter_narrows_models(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store, model=MODEL_WEAK)
    assert [m["model"] for m in pack["models"]] == [MODEL_WEAK]
    # totals still reflect the whole store
    assert pack["store_totals"]["n_models"] == 2


def test_pack_curated_findings_carry_resolution(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store)
    curated = pack["curated_findings"]
    assert curated["confirmed_or_needs_replication"]
    # These curated run_ids are NOT in our fixture store, so they must not
    # be marked as live evidence.
    for f in curated["confirmed_or_needs_replication"]:
        if f["run_ids"]:
            assert f["evidence_resolves"] is False


# ---------------------------------------------------------------------------
# Audit: catches invented numbers, passes faithful ones, rounding tolerance
# ---------------------------------------------------------------------------


def test_audit_passes_faithful_diagnosis(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store)
    # Only cite numbers that appear in the pack.
    diag = Diagnosis(
        model="m",
        raw_text="The weak model scored 86.7% overall across 60 scored trials. "
                 "Its belief pass rate is 73.3%. The strong model is at 100.0%.",
    )
    flags = audit_diagnosis(diag, pack)
    assert flags == []


def test_audit_catches_planted_invented_number(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store)
    diag = Diagnosis(
        model="m",
        raw_text="The weak model collapses to 42% on adversarial belief queries.",
    )
    flags = audit_diagnosis(diag, pack)
    assert any(f.figure == "42%" for f in flags)


def test_audit_rounding_tolerance(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store)
    # pack has 86.7; writing "87%" is within the half-point tolerance? No --
    # 87 vs 86.7 = 0.3 <= 0.5, so it should PASS.
    diag = Diagnosis(model="m", raw_text="around 87% overall")
    assert audit_diagnosis(diag, pack) == []
    # But 88% is 1.3 away -> flagged.
    diag2 = Diagnosis(model="m", raw_text="around 88% overall")
    assert any(f.figure == "88%" for f in audit_diagnosis(diag2, pack))


def test_audit_small_prose_integers_not_flagged(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store)
    diag = Diagnosis(model="m", raw_text="There are 2 models and 4 dimensions worth noting.")
    assert audit_diagnosis(diag, pack) == []


def test_audit_money_tolerance(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store)
    spend = pack["store_totals"]["spend_usd"]
    # quoting the spend rounded to a nearby value is fine; a fabricated
    # dollar figure is flagged
    diag = Diagnosis(model="m", raw_text="It cost about $999.00 to run.")
    assert any(f.kind == "money" for f in audit_diagnosis(diag, pack))
    assert spend >= 0.0  # sanity


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------


def test_parse_sections_handles_fences_and_headers():
    text = (
        "```\n"
        "## Executive Summary\n"
        "The model breaks on belief.\n"
        "## Per-Model Profile\n"
        "strong is strong.\n"
        "## Cross-Model Comparison\n"
        "they differ.\n"
        "## Recommended Next Probes\n"
        "probe more.\n"
        "```"
    )
    parsed = parse_sections(text)
    assert "breaks on belief" in parsed["executive_summary"]
    assert "strong is strong" in parsed["per_model"]
    assert "they differ" in parsed["cross_model"]
    assert "probe more" in parsed["next_probes"]


def test_parse_sections_preamble_folds_into_summary():
    text = "Here is my analysis.\n\n## Per-Model Profile\ndetails"
    parsed = parse_sections(text)
    assert "Here is my analysis" in parsed["executive_summary"]
    assert "details" in parsed["per_model"]


# ---------------------------------------------------------------------------
# write_diagnosis end to end with a fake client
# ---------------------------------------------------------------------------


def test_write_diagnosis_parses_fake_reply(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store)
    reply = (
        "## Executive Summary\nWeak at belief.\n"
        "## Per-Model Profile\nprofile.\n"
        "## Cross-Model Comparison\ncompare.\n"
        "## Recommended Next Probes\nprobe.\n"
    )
    client = FakeClient(reply=reply)
    diag = write_diagnosis(client, "analyst-x", pack)
    assert diag.error is None
    assert "Weak at belief" in diag.executive_summary
    # the prompt actually carried the rendered pack
    assert "evidence_pack_v1" in client.prompts[0][1]


def test_write_diagnosis_surfaces_error(tmp_path):
    store = _seed_store(tmp_path)
    pack = build_evidence_pack(store)
    client = FakeClient(reply="", error="http 500")
    diag = write_diagnosis(client, "analyst-x", pack)
    assert diag.error is not None


# ---------------------------------------------------------------------------
# Tavily corroboration
# ---------------------------------------------------------------------------


def _fake_tavily_transport(results):
    def transport(url, payload):
        import json
        return 200, json.dumps({"results": results}).encode("utf-8")
    return transport


def test_corroborate_degrades_without_key():
    from cartographer.viewer.findings import FINDINGS
    out = corroborate_findings(FINDINGS, tavily_key=None)
    assert out == []


def test_corroborate_parses_and_classifies(tmp_path):
    from cartographer.viewer.findings import FINDINGS
    results = [
        {"title": "LLMs and false belief", "url": "http://x/1",
         "content": "models struggle with false belief tasks"},
    ]
    transport = _fake_tavily_transport(results)
    client = FakeClient(reply="corroborates\nThe literature agrees.")
    cache = tmp_path / "cache.json"
    out = corroborate_findings(
        FINDINGS, tavily_key="tvly-fake", client=client,
        analyst_model="analyst-x", cache_path=str(cache), transport=transport,
    )
    assert out, "expected at least one corroboration"
    assert any(c.relation == "corroborates" for c in out)
    assert all(c.results for c in out)
    # cache file was written
    assert cache.exists()


def test_corroborate_cache_avoids_second_call(tmp_path):
    from cartographer.viewer.findings import FINDINGS
    calls = {"n": 0}

    def counting_transport(url, payload):
        import json
        calls["n"] += 1
        return 200, json.dumps({"results": [{"title": "t", "url": "u", "content": "c"}]}).encode()

    client = FakeClient(reply="no_clear_signal\n")
    cache = tmp_path / "cache.json"
    corroborate_findings(FINDINGS, "tvly-fake", client=client,
                         analyst_model="a", cache_path=str(cache), transport=counting_transport)
    first = calls["n"]
    assert first > 0
    # second run: all queries cached -> no new transport calls
    corroborate_findings(FINDINGS, "tvly-fake", client=client,
                         analyst_model="a", cache_path=str(cache), transport=counting_transport)
    assert calls["n"] == first


# ---------------------------------------------------------------------------
# Keys extension is additive
# ---------------------------------------------------------------------------


def test_load_keys_extracts_tavily(tmp_path):
    p = tmp_path / "keys.txt"
    p.write_text("v1.abcdef\nsome groq gsk_ABC123\ntavily tvly-XYZ789\n", encoding="utf-8")
    keys = _load_keys_from_abs(p)
    assert keys.get("tavily") == "tvly-XYZ789"
    # the additive extraction did not disturb the existing two keys
    assert keys.get("nebius") == "v1.abcdef"
    assert keys.get("groq") == "gsk_ABC123"


def test_load_tavily_key_prefers_env_var(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-FROMENV")
    assert load_tavily_key() == "tvly-FROMENV"


def test_load_tavily_key_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    # point at a key file with no tavily line
    missing = tmp_path / "nope.txt"
    assert load_tavily_key(str(missing)) is None


def test_load_keys_still_loads_nebius_and_groq(tmp_path):
    # Existing behavior unchanged: a file with only nebius+groq yields those
    # two and no tavily.
    fake = tmp_path / "env" / "k.txt"
    fake.parent.mkdir()
    fake.write_text("v1.NEBIUSKEY\nline with groq gsk_GGGG\n", encoding="utf-8")
    keys = _load_keys_from_abs(fake)
    assert keys.get("nebius") == "v1.NEBIUSKEY"
    assert keys.get("groq") == "gsk_GGGG"
    assert "tavily" not in keys


def _load_keys_from_abs(path):
    """Mirror load_keys' parse on an absolute path (the real function
    resolves relative to repo root, which a tmp file isn't under)."""
    import cartographer.exec.keys as keys_mod
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    keys = {}
    if lines and lines[0].strip().startswith("v1."):
        keys["nebius"] = lines[0].strip()
    for line in lines:
        if "groq" in line.lower():
            m = keys_mod._GROQ_KEY_RE.search(line)
            if m:
                keys["groq"] = m.group(0)
                break
    for line in lines:
        if "tavily" in line.lower() or "tvly-" in line.lower():
            m = keys_mod._TAVILY_KEY_RE.search(line)
            if m:
                keys["tavily"] = m.group(0)
                break
    return keys


# ---------------------------------------------------------------------------
# Read-only store guarantee
# ---------------------------------------------------------------------------


def test_read_only_store_refuses_append(tmp_path):
    # create then reopen read-only
    path = tmp_path / "ro.sqlite"
    store = ResultsStore(path)
    store.append(_rec("r", MODEL_WEAK, Verdict.PASS, seed=1))
    store.close()

    ro = ResultsStore.open_read_only(path)
    try:
        assert len(ro.by_run("r")) == 1
        import pytest
        with pytest.raises(RuntimeError):
            ro.append(_rec("r", MODEL_WEAK, Verdict.PASS, seed=2))
    finally:
        ro.close()


# ---------------------------------------------------------------------------
# /report page rendering
# ---------------------------------------------------------------------------


def test_report_body_empty_state():
    body = report_body(None)
    assert "No report yet" in body
    assert "write_report.py" in body


def _sample_report(flags=None, corroborations=None):
    return {
        "meta": {"generated_at": "2026-09-20T00:00:00+00:00", "analyst_model": "analyst-x"},
        "store_totals": {"n_trials": 115, "n_scored": 100, "spend_usd": 0.02},
        "diagnosis": {
            "model": "analyst-x",
            "raw_text": "full text",
            "sections": [["Executive Summary", "Weak at belief."],
                         ["Per-Model Profile", "profile"]],
            "error": None,
        },
        "audit_flags": flags or [],
        "corroborations": corroborations,
        "literature": {} if corroborations else {"note": "literature check not run (no Tavily key)"},
    }


def test_report_body_renders_all_sections_clean():
    body = report_body(_sample_report())
    assert "Capability diagnosis" in body
    assert "Weak at belief" in body
    assert "Contamination audit" in body
    assert "audit clean" in body or "clean" in body
    assert "literature check not run" in body
    assert "How this was made" in body


def test_report_body_flags_are_prominent():
    flags = [{"figure": "42%", "normalized": 42.0, "kind": "percent",
              "context": "collapses to 42% on adversarial"}]
    body = report_body(_sample_report(flags=flags))
    assert "unverified figure" in body
    assert "42%" in body


def test_report_body_renders_corroborations():
    corr = [{
        "finding_name": "Belief collapse",
        "finding_status": "confirmed",
        "queries": ["llm false belief"],
        "results": [{"title": "Paper A", "url": "http://x/a", "content": "snippet"}],
        "relation": "corroborates",
        "rationale": "agrees",
    }]
    body = report_body(_sample_report(corroborations=corr))
    assert "Belief collapse" in body
    assert "http://x/a" in body
    assert "corroborates" in body


def test_load_report_missing_file_returns_none(tmp_path):
    assert load_report(tmp_path / "nope.json") is None
