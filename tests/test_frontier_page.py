"""Capability map (`/capability-map`), globe Map (`/` + `/api/map.json`) and
discovery page (`/frontier`) on synthetic stores.

Pages are rendered both directly and through the real request handler on an
in-process server, so the route wiring in `serve.py` is covered too."""

from __future__ import annotations

import html
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from cartographer.report.frontier import SPECS, frontier_summary
from cartographer.store.schema import ExperimentRecord, RenderMode, Verdict
from cartographer.store.store import ResultsStore
from cartographer.viewer import serve as sv
from cartographer.viewer.frontier_page import capability_map_html, frontier_html

M = "nvidia/test-model"
M2 = "nvidia/other-model"
RAW_PARAMS = ("plan_depth", "id_collisions_on", "realized_plan_depth", "state_obscurity")


def _rec(run_id, verdict, seed, latent=None, realized=None, detail="", kind="tool_plan", model=M):
    return ExperimentRecord(
        run_id=run_id, model_name=model, latent_params=latent or {}, realized_dims=realized or {},
        render_mode=RenderMode.TEMPLATE, task_seed=seed, trial_seed=seed, rendering_seed=seed,
        prompt_text="p", response_text="the model answer", verdict=verdict,
        verifier_detail=f"kind={kind};campaign=1{detail}", tokens_in=1, tokens_out=1,
        cost_usd=0.0, latency_ms=1.0,
    )


def _depth_level(recs, pd, n_pass, n_fail, seed0, model=M, run="camp-tool-deep-test-001",
                 code="unknown_tool:step=3:x"):
    lat = {"plan_depth": float(pd), "id_collisions_on": 1.0}
    real = {"realized_plan_depth": float(pd + 3)}
    for i in range(n_pass):
        recs.append(_rec(run, Verdict.PASS, seed0 + i, lat, real, model=model))
    for i in range(n_fail):
        recs.append(_rec(run, Verdict.FAIL, seed0 + 100 + i, lat, real, f";detail={code}", model=model))


@pytest.fixture
def store(tmp_path):
    """M breaks on look-alike plans at 25-call plans and holds on state;
    M2 holds on look-alike plans. Everything else is NOT TESTED."""
    s = ResultsStore(tmp_path / "s.sqlite")
    recs: list = []
    _depth_level(recs, 4, 10, 0, 1)
    _depth_level(recs, 10, 9, 1, 1000)
    _depth_level(recs, 16, 4, 6, 2000)
    _depth_level(recs, 22, 1, 9, 3000)
    recs += [_rec("camp-x-001", Verdict.PASS, 5000 + i, kind="state") for i in range(30)]
    _depth_level(recs, 4, 10, 0, 7000, model=M2, run="camp-tool-deep-other-001")
    _depth_level(recs, 10, 10, 0, 8000, model=M2, run="camp-tool-deep-other-001")
    s.append_many(recs)
    yield s
    s.close()


def _text(page: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def _ribbon_item(page: str, slug: str) -> str:
    m = re.search(rf'<li class="fm-ev[^"]*" data-ev="{slug}">(.*?)</li>', page, re.S)
    assert m, slug
    return m.group(1)


# ---------------------------------------------------------------------------
# A. Capability map
# ---------------------------------------------------------------------------


def test_map_renders_every_row_including_not_tested(store):
    page = capability_map_html(store, model=M)
    rows = re.findall(r'<a class="fm-row[^"]*" href="[^"]*" data-cap="([^"]+)" data-status="([^"]+)"', page)
    assert sorted(k for k, _ in rows) == sorted(s.key for s in SPECS)
    status = dict(rows)
    assert status["plan_length_lookalike"] == "BREAKS"
    assert status["state"] == "HOLDS"
    assert status["catalog_size"] == "NOT TESTED"
    assert page.count('class="fm-row is-untested"') == sum(1 for s in status.values() if s == "NOT TESTED")
    # every row links to its discovery page
    assert "/frontier?model=nvidia%2Ftest-model&amp;cap=plan_length_lookalike" in page


def test_map_shows_frontier_data_not_invented_text(store):
    mf = frontier_summary(store, M)
    row = next(r for r in mf.rows if r.key == "plan_length_lookalike")
    page = capability_map_html(store, model=M)
    text = _text(page)
    assert row.headline in text and row.boundary in text  # "25-call plans"
    assert "grade B" in text
    # the verdict sentence is shown as labelled lines: Breaks (n) / Slips (n) / Holds
    assert "Breaks (1) long plans with look-alike tool names (25-call plans)" in text
    assert "Holds on 1 of 2 capabilities tested." in text
    # status chip = icon + word, never colour alone
    assert re.search(r'class="fm-status heat-1" data-status="BREAKS"><svg[^>]*>.*?</svg>BREAKS', page)


def test_map_groups_by_province_in_specs_order(store):
    page = capability_map_html(store, model=M)
    provinces = list(dict.fromkeys(s.province for s in SPECS))
    positions = [page.index(f'aria-label="{html.escape(p)}"') for p in provinces]
    assert positions == sorted(positions)


def test_map_strip_marks_the_boundary(store):
    page = capability_map_html(store, model=M)
    row = re.search(r'data-cap="plan_length_lookalike".*?</a>', page, re.S).group(0)
    cells = re.findall(r'<span class="fm-cell ([^"]*)" title="([^"]*)"', row)
    assert len(cells) == 4
    boundary = [t for c, t in cells if "fm-boundary" in c]
    assert boundary == ["25-call plans: 1/10 pass (10%)"]


def test_compare_view_stacks_every_model_on_a_shared_axis(store):
    page = capability_map_html(store, compare=True)
    block = re.search(r'<div class="fm-cmp" data-cap="plan_length_lookalike">.*?(?=<div class="fm-cmp" |</section>)',
                      page, re.S).group(0)
    assert f'data-model="{M}"' in block and f'data-model="{M2}"' in block
    strips = {
        model: re.search(r'<span class="fm-strip".*?</span></span>', chunk, re.S).group(0)
        for model, chunk in re.findall(r'data-model="([^"]+)"(.*?)</a>', block, re.S)
    }
    # M2 was tested at 2 of the 4 levels: same 4 columns, 2 of them gaps
    assert {m: s.count("fm-cell") for m, s in strips.items()} == {M: 4, M2: 4}
    assert strips[M2].count("fm-gap") == 2 and strips[M].count("fm-gap") == 0


def test_unknown_model_on_map_is_an_empty_state(store):
    page = capability_map_html(store, model="no-such-model")
    assert "No model called" in page and "no-such-model" in page
    assert 'class="fm-row' not in page


def test_map_on_an_empty_store(tmp_path):
    s = ResultsStore(tmp_path / "empty.sqlite")
    page = capability_map_html(s)
    assert "nothing to map" in page
    s.close()


def test_no_raw_parameter_names_or_run_ids_on_the_map(store):
    for page in (capability_map_html(store, model=M), capability_map_html(store, compare=True)):
        text = _text(page)
        for raw in RAW_PARAMS:
            assert raw not in text
        assert "camp-" not in page


# ---------------------------------------------------------------------------
# B. Discovery page
# ---------------------------------------------------------------------------


def test_frontier_page_renders_chart_cause_exemplar_and_ribbon(store):
    row = next(r for r in frontier_summary(store, M).rows if r.key == "plan_length_lookalike")
    page = frontier_html(store, model=M, cap="plan_length_lookalike")
    text = _text(page)
    # chart: one point per level, boundary drawn and labelled
    assert '<svg viewBox="0 0 760 340"' in page
    assert page.count('<circle class="pt') == len(row.curve) == 4
    assert 'class="bnd"' in page and "Boundary: breaks at 25-call plans" in text
    for c in row.curve:
        assert f'{c["n_pass"]}/{c["n_scored"]}' in text and c["label"] in text
    # finding vs trial result are two different objects
    assert re.search(r"Finding:\s+BREAKS\s+· grade B\s+\(research conclusion\)", text)
    assert "Trial result: FAIL (one model answer)" in text
    # cause
    assert "Why it breaks" in text and row.cause in text
    assert "in 100% of failures" in text
    # exemplar links
    assert f'/record/{row.exemplar["record_id"]}' in page
    assert f'/run/{row.exemplar["run_id"]}' in page
    # ribbon
    assert "deterministic simulation" in text.lower()
    assert "Wilson 95% interval" in text and "p = " in text
    assert "Grade B: moderate evidence" in text
    assert "10 scored trials at the boundary: small sample" in text
    # raw evidence, secondary
    assert "/trials?model=nvidia%2Ftest-model&amp;axis=kind&amp;key=tool_plan" in page
    assert ">camp-tool-deep-test-001</a>" in page


def test_ribbon_never_ticks_fresh_seed_or_replication(store):
    for model, cap in ((M, "plan_length_lookalike"), (M, "state"), (M2, "plan_length_lookalike")):
        page = frontier_html(store, model=model, cap=cap)
        for slug in ("fresh-seed", "replication"):
            item = _ribbon_item(page, slug)
            assert "Not run yet" in item
            assert "&#10003;" not in item and "✓" not in item and "✔" not in item


def test_ribbon_ticks_only_what_the_row_carries(store):
    page = frontier_html(store, model=M, cap="plan_length_lookalike")
    assert "&#10003;" in _ribbon_item(page, "ground-truth")
    assert "&#10003;" in _ribbon_item(page, "statistics")  # grade B
    assert "&#10003;" not in _ribbon_item(page, "sample-size")  # 10 < 15: flagged, not ticked


def test_exemplar_shows_the_dominant_cause(tmp_path):
    """frontier.py now picks an example failure of the dominant cause, so the
    example never contradicts the cause sentence above it."""
    s = ResultsStore(tmp_path / "s.sqlite")
    recs: list = []
    _depth_level(recs, 4, 10, 0, 1)
    # first failure at the boundary is a precondition violation, the rest are look-alikes
    _depth_level(recs, 22, 1, 1, 3000, code="precondition_violation:step=1:y")
    _depth_level(recs, 22, 0, 8, 4000)
    s.append_many(recs)
    page = frontier_html(s, model=M, cap="plan_length_lookalike")
    assert "failed a different check from the dominant cause" not in page
    assert "unknown_tool" in page
    s.close()


def test_not_tested_row_is_a_clear_empty_state(store):
    page = frontier_html(store, model=M, cap="catalog_size")
    assert "Not tested yet" in page
    assert 'data-ev="' not in page  # no evidence ribbon for no evidence


@pytest.mark.parametrize("model,cap,needle", [
    ("no-such-model", "plan_length_lookalike", "No model called"),
    (M, "no_such_cap", "No capability called"),
    (None, None, "No model chosen"),
])
def test_unknown_model_or_cap_is_an_empty_state(store, model, cap, needle):
    page = frontier_html(store, model=model, cap=cap)
    assert "Capability not found" in page and needle in page


def test_no_raw_parameter_names_on_discovery_page(store):
    text = _text(frontier_html(store, model=M, cap="plan_length_lookalike"))
    for raw in RAW_PARAMS:
        assert raw not in text


# ---------------------------------------------------------------------------
# Routes and navigation, through the real handler
# ---------------------------------------------------------------------------


@pytest.fixture
def server(store):
    handler = type("H", (sv._ViewerHandler,), {"store": store, "launch_deps": None,
                                               "log_message": lambda *a: None})
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(base: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base + path) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_routes(server):
    status, home = _get(server, "/")
    assert status == 200 and "<title>Model Cartographer · Map</title>" in home
    assert "/api/map.json" in home  # the globe draws itself from the live payload
    status, page = _get(server, "/capability-map")
    assert status == 200 and "<h1>Breaking points</h1>" in page
    status, page = _get(server, "/capability-map?model=test-model")  # short name works too
    assert status == 200 and 'data-cap="plan_length_lookalike" data-status="BREAKS"' in page
    status, page = _get(server, "/capability-map?compare=1")
    assert status == 200 and 'class="fm-cmp"' in page
    assert 'href="/?' not in page  # no link still points at the old root map
    status, page = _get(server, "/frontier?model=nvidia%2Ftest-model&cap=plan_length_lookalike")
    assert status == 200 and "Why it breaks" in page
    for bad in ("/frontier?model=nope&cap=state", "/frontier?model=nvidia%2Ftest-model&cap=nope"):
        status, page = _get(server, bad)
        assert status == 200 and "Capability not found" in page
    status, page = _get(server, "/frontier")  # no model: straight to Breaking points
    assert status == 200 and "<h1>Breaking points</h1>" in page


def test_overview_redirects_to_the_map(server):
    # the old Console repeated the Map with its own counts; the Map is home now
    status, page = _get(server, "/overview")
    assert status == 200 and "<title>Model Cartographer · Map</title>" in page


def test_every_page_uses_the_maps_sidebar(server):
    """Server-rendered pages and the globe Map share one sidebar: same
    labels in the same order, so moving between them never changes the frame."""
    from pathlib import Path

    map_page = (Path(__file__).resolve().parents[1] / "cartographer/viewer/static/map.html").read_text()
    map_labels = re.findall(r'<button class="nav-item[^"]*" data-nav="[^"]+"><svg.*?</svg>([^<]+)</button>', map_page)
    for path in ("/capability-map", "/runs", "/atlas", "/coverage", "/report", "/launch", "/live"):
        _status, page = _get(server, path)
        items = re.findall(r'<a class="nav-item[^"]*"(?: aria-current="page")? href="([^"]+)"><svg.*?</svg>([^<]+)</a>', page)
        assert [label for _h, label in items] == map_labels, path
        assert items[0] == ("/", "Map")
        assert 'id="mcQ"' in page and 'id="mcEpoch"' in page  # the Map's command bar
    _status, page = _get(server, "/capability-map")
    assert 'class="nav-item on" aria-current="page" href="/capability-map"' in page


def test_search_api_feeds_the_command_bar(server):
    status, raw = _get(server, "/api/search.json")
    assert status == 200
    data = json.loads(raw)
    assert [p["id"] for p in data["provinces"]][:2] == ["narrative", "tool"]
    assert data["trials"] > 0 and data["newest"]
    assert all(m["slug"] and m["name"] for m in data["models"])
    _status, raw = _get(server, "/api/search.json?q=zz")  # not an id prefix: no trial lookup
    assert json.loads(raw) == {"trials": []}


def test_map_api_serves_the_live_payload(server):
    status, raw = _get(server, "/api/map.json")
    assert status == 200
    data = json.loads(raw)
    by_id = {m["id"]: m for m in data["models"]}
    assert set(by_id) == {M, M2}
    # M: 30 state passes (narrative province) + 40 tool-plan trials, 26 of them passes
    assert by_id[M]["prov"]["narrative"] == [30, 30]
    assert by_id[M]["prov"]["tool"] == [24, 40]
    assert "narrative" not in by_id[M2]["prov"]  # never measured there, so no number at all
    assert by_id[M]["display"] and by_id[M]["vendor"] == "nvidia"
    assert {p["id"] for p in data["provinces"]} == {"narrative", "tool", "code_trace", "code_repair", "repo", "interactive"}
