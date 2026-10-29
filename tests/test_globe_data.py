"""`/api/map.json` payload (`cartographer.viewer.globe_data`) on a synthetic store.

The globe, the capability matrix and the trial explorer all read this one
payload, so the property that matters most is agreement: every cell's
pass / scored counts must be exactly what the trial list reproduces, and a
province a model never ran in must carry no number at all (not a zero)."""

from __future__ import annotations

import json

from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.viewer import globe_data as G

A = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
B = "google/gemma-3-27b-it"


def _rec(model, kind, verdict, seed, dims=None, run="camp-x-001"):
    return ExperimentRecord(
        run_id=run, model_name=model, latent_params={"chain_length": 3.0}, realized_dims=dims or {},
        render_mode=RenderMode.TEMPLATE, task_seed=seed, trial_seed=seed, rendering_seed=seed,
        prompt_text="p", response_text="answer", verdict=verdict,
        verifier_detail=f"kind={kind};campaign=1", tokens_in=1, tokens_out=1, cost_usd=0.001, latency_ms=1.0,
        created_at=f"2026-10-0{1 + seed % 3}T12:00:00+00:00",
    )


def _store(tmp_path):
    s = ResultsStore(tmp_path / "g.sqlite")
    recs = []
    for i in range(30):  # A: narrative (belief) with a rising difficulty dial
        dims = {"entity_state": float(1 + i % 10), "context_length": float(5 + i)}
        recs.append(_rec(A, "belief", Verdict.PASS if i % 4 else Verdict.FAIL, i, dims))
    for i in range(20):  # A: tool planning
        recs.append(_rec(A, "tool_plan", Verdict.PASS if i % 2 else Verdict.FAIL, 100 + i))
    recs.append(_rec(A, "tool_plan", Verdict.ERROR, 999))  # errors never count
    for i in range(25):  # B: code tracing only
        recs.append(_rec(B, "code_trace", Verdict.PASS, 200 + i))
    s.append_many(recs)
    return s


def test_cells_agree_with_the_trial_list(tmp_path):
    s = _store(tmp_path)
    p = G.build_payload(s)
    dims = p["dims"]
    for mi, m in enumerate(p["models"]):
        for key, c in m["cells"].items():
            ts = [t for t in p["ev"]["trials"] if t[1] == mi and (
                (key in dims and (t[5] >> dims.index(key)) & 1) or (key not in dims and t[2] == key))]
            scored = [t for t in ts if t[3] == "P" or (t[3] == "F" and t[4] != "n")]
            assert len(scored) == c[1], (m["id"], key)
            assert sum(t[3] == "P" for t in scored) == c[0], (m["id"], key)
    s.close()


def test_provinces_pool_kinds_and_skip_what_was_never_run(tmp_path):
    s = _store(tmp_path)
    p = G.build_payload(s)
    a = next(m for m in p["models"] if m["id"] == A)
    b = next(m for m in p["models"] if m["id"] == B)
    assert a["prov"]["narrative"] == [22, 30]
    assert a["prov"]["tool"] == [10, 20]  # the ERROR is excluded from the denominator
    assert "code_trace" not in a["prov"] and "narrative" not in b["prov"]
    assert b["prov"]["code_trace"] == [25, 25]
    # most-measured model first: the map opens on the model with the most evidence
    assert p["models"][0]["id"] == A
    assert a["daily"] and all(n >= k for _d, k, n in a["daily"])
    s.close()


def test_payload_is_cached_until_the_store_changes(tmp_path):
    s = _store(tmp_path)
    first = G.payload_json(s)
    assert G.payload_json(s) is first  # unchanged store: the cached bytes come back
    s.append(_rec(B, "code_trace", Verdict.FAIL, 500))
    second = json.loads(G.payload_json(s))
    b = next(m for m in second["models"] if m["id"] == B)
    assert b["prov"]["code_trace"] == [25, 26]
    s.close()


def test_display_names_and_vendors():
    assert G.display_name("nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B") == "Nemotron 3 Nano 30B"
    assert G.display_name("nvidia/nemotron-3-super-120b-a12b") == "Nemotron 3 Super 120B"
    assert G.display_name("gemma-4-31b-it-qat-w4a16-ct") == "Gemma 4 31B QAT"
    assert G.display_name("google/gemma-3-27b-it") == "Gemma 3 27B"
    assert G.display_name("openai/gpt-oss-120b") == "gpt-oss-120b"
    assert G.vendor_of("gemma-4-31b-it-qat-w4a16-ct") == "google"
    assert G.vendor_of("openai/gpt-oss-120b") == "openai"
    assert G.vendor_of("nvidia/Nemotron-3-Ultra-550b-a55b") == "nvidia"
