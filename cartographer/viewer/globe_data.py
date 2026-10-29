"""Live data behind the globe Map (`/`) and its Findings / Model views.

`/` is a single page (`static/map.html`) that fetches `/api/map.json` and
draws everything client-side: one globe per model, a flame per province
whose length is the model's pass rate there, the capability matrix, the
per-model workspace and the trial explorer.

Every number in the payload comes from the store through the same rules as
`/findings` (`cartographer.viewer.findings`): primary records only, SCORED
trials only (PASS + FAIL), ERRORs excluded, FAILs that the resample gate
judged to be noise excluded from both numerator and denominator. A cell the
map shows and the matrix cell it opens therefore always agree.

The payload is cached and rebuilt only when the store changes (record count
or newest `created_at`), so a page load costs one cheap SQL query once the
first build is done.
"""

from __future__ import annotations

import json
import re
import threading
from collections import defaultdict

from ..store.schema import Verdict
from ..store.store import ResultsStore
from . import findings as F

# Provinces are the six flames on the globe. `anchor` places the flame on
# the globe (sphere-radius units, screen coordinates: +x right, +y down);
# `kinds` are the query kinds pooled into the province's pass rate.
PROVINCES = [
    {"id": "narrative", "short": "Narrative", "l1": "Narrative", "l2": "World model", "name": "Narrative world model",
     "kinds": ["state", "order", "belief", "adversarial_belief"], "color": "#E8A35C", "anchor": [-0.74, -0.68]},
    {"id": "tool", "short": "Tool planning", "l1": "Tool-call", "l2": "Planning", "name": "Tool-call planning",
     "kinds": ["tool_plan"], "color": "#B9D76A", "anchor": [0.93, -0.66]},
    {"id": "code_trace", "short": "Code tracing", "l1": "Code", "l2": "Tracing", "name": "Code tracing",
     "kinds": ["code_trace"], "color": "#7FB2E5", "anchor": [1.11, -0.02]},
    {"id": "code_repair", "short": "Code repair", "l1": "Code", "l2": "Repair", "name": "Code repair and writing",
     "kinds": ["code_repair", "code_write"], "color": "#EC7F6D", "anchor": [0.76, 0.66]},
    {"id": "repo", "short": "Repository", "l1": "Repository", "l2": "Fixes", "name": "Repository fixes",
     "kinds": ["repo_fix"], "color": "#7CD3B4", "anchor": [-0.42, 0.63]},
    {"id": "interactive", "short": "Interactive", "l1": "Interactive", "l2": "Execution", "name": "Interactive execution",
     "kinds": ["interactive"], "color": "#A68AEE", "anchor": [-1.09, -0.02]},
]
# Matrix / table columns: every query kind the findings derivation knows,
# plus any province kind it does not (repo_fix), in province order.
_ALL_KINDS = list(dict.fromkeys(list(F.CAPABILITY_KINDS) + [k for p in PROVINCES for k in p["kinds"]]))

_MODEL_COLORS = ["#7FB2E5", "#E8A35C", "#B9D76A", "#68B98F", "#7CD3B4", "#A68AEE", "#EC7F6D", "#D3A957"]

_CACHE: dict = {"key": None, "payload": None}
_LOCK = threading.Lock()


def vendor_of(model: str) -> str:
    m = model.lower()
    if "gemma" in m or "gemini" in m or m.startswith("google/"):
        return "google"
    if "nemotron" in m or m.startswith("nvidia/"):
        return "nvidia"
    if "gpt" in m or m.startswith("openai/"):
        return "openai"
    if "claude" in m or m.startswith("anthropic/"):
        return "anthropic"
    if "llama" in m or m.startswith("meta-llama/"):
        return "meta"
    if "mistral" in m:
        return "mistralai"
    return "other"


_ORG = {"google": "Google", "nvidia": "NVIDIA", "openai": "OpenAI", "anthropic": "Anthropic",
        "meta": "Meta", "mistralai": "Mistral AI", "other": ""}


def display_name(model: str) -> str:
    """Readable model name: `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` -> `Nemotron 3 Nano 30B`."""
    base = model.split("/", 1)[-1]
    low = base.lower()
    m = re.match(r"(?:nvidia-)?nemotron-(\d+)-(nano|super|ultra)-(\d+)b", low)
    if m:
        return f"Nemotron {m.group(1)} {m.group(2).title()} {m.group(3)}B"
    m = re.match(r"gemma-(\d+)-(\d+)b", low)
    if m:
        quant = " QAT" if "qat" in low else ""
        return f"Gemma {m.group(1)} {m.group(2)}B{quant}"
    if low.startswith("gpt-oss"):
        return base
    return base


def _store_key(store: ResultsStore):
    try:
        row = store._conn.execute("SELECT count(*), max(created_at) FROM experiments").fetchone()
        return tuple(row)
    except Exception:  # noqa: BLE001 -- a key we cannot read just disables caching
        return None


def _detail_field(record, name: str) -> str:
    for part in (record.verifier_detail or "").split(";"):
        if part.startswith(name + "="):
            return part[len(name) + 1:]
    return ""


def _scored(records, verification):
    return [r for r in records if r.verdict in (Verdict.PASS, Verdict.FAIL)
            and not (r.verdict == Verdict.FAIL and verification.get(r.record_id) == "noise")]


def _cell_list(cell) -> list:
    return [cell.n_pass, cell.n_scored, cell.n_fail, cell.n_noise, cell.n_error,
            cell.n_stable, cell.n_unverified, cell.verification]


def build_payload(store: ResultsStore) -> dict:
    records = F._primary_records(store)
    verification = F._verification_map(store, records)
    data = F.derive(store)
    cuts = data.dim_cuts
    by_model: dict[str, list] = defaultdict(list)
    for r in records:
        by_model[r.model_name].append(r)
    model_ids = [m.model for m in data.models]
    # Most-measured first: the map opens on the model with the most evidence.
    model_ids.sort(key=lambda mid: -len(by_model.get(mid, [])))
    mindex = {mid: i for i, mid in enumerate(model_ids)}
    kinds_present = [k for k in _ALL_KINDS if any(F._query_kind_of(r) == k for r in records)]
    runs = sorted({r.run_id for r in records})
    run_ix = {rid: i for i, rid in enumerate(runs)}

    models, evcells, findings = [], {}, []
    for i, mid in enumerate(model_ids):
        mrecs = by_model.get(mid, [])
        mf = data.by_model(mid)
        cells = {}
        for axis, keys in (("kind", kinds_present), ("dim", F.DIFFICULTY_DIMS)):
            for key in keys:
                cell = mf.cells.get((axis, key)) if mf else None
                members = F._cell_members(mrecs, cuts, axis, key)
                if cell is None:
                    cell = F._tally(members, verification, mid, axis, key)
                if not cell.n_scored:
                    continue
                cells[key] = _cell_list(cell)
                conc = sorted(F._concentrations(members, verification, cuts), key=lambda c: c.pass_rate)[:3]
                traps: dict[str, int] = defaultdict(int)
                for r in members:
                    if r.verdict == Verdict.FAIL and verification.get(r.record_id) != "noise":
                        for t in F._traps_of(r):
                            traps[t] += 1
                kb: dict[str, list] = defaultdict(lambda: [0, 0])
                if axis == "dim":
                    for r in _scored(members, verification):
                        slot = kb[F._query_kind_of(r) or "unknown"]
                        slot[0] += 1
                        slot[1] += r.verdict == Verdict.PASS
                cfg = F._failing_config(members, verification)
                evcells[f"{mid}|{key}"] = {
                    "conc": [[c.dim, c.bucket, c.lo, c.hi, c.n_pass, c.n_scored] for c in conc],
                    "kb": [[k, v[0], v[1]] for k, v in sorted(kb.items())],
                    "traps": sorted(traps.items(), key=lambda x: -x[1])[:5],
                    "ex": [list(e) for e in F._examples(members, verification, config=cfg)],
                    "cfg": cfg,
                }
        # province rates pool the province's kinds (scored trials, noise out)
        prov = {}
        for p in PROVINCES:
            n = sum(cells[k][1] for k in p["kinds"] if k in cells)
            k_pass = sum(cells[k][0] for k in p["kinds"] if k in cells)
            if n:
                prov[p["id"]] = [k_pass, n]
        # daily scored pass rate, for the trajectory chart
        daily: dict[str, list] = defaultdict(lambda: [0, 0])
        for r in _scored(mrecs, verification):
            d = (r.created_at or "")[:10]
            if d:
                daily[d][1] += 1
                daily[d][0] += r.verdict == Verdict.PASS
        # per-run summary, for the Evaluations tab
        per_run: dict[str, list] = defaultdict(lambda: [0, 0, 0, ""])
        for r in mrecs:
            slot = per_run[r.run_id.split("/", 1)[0]]
            slot[2] += 1
            slot[3] = max(slot[3], (r.created_at or "")[:16])
        for r in _scored(mrecs, verification):
            slot = per_run[r.run_id.split("/", 1)[0]]
            slot[1] += 1
            slot[0] += r.verdict == Verdict.PASS
        run_list = sorted(([rid, v[0], v[1], v[2], v[3].replace("T", " ")] for rid, v in per_run.items()),
                          key=lambda x: x[4], reverse=True)
        dates = sorted((r.created_at or "")[:10] for r in mrecs if r.created_at)
        vend = vendor_of(mid)
        models.append({
            "id": mid, "name": mid.split("/", 1)[-1], "display": display_name(mid), "vendor": vend,
            "org": _ORG.get(vend, ""), "color": _MODEL_COLORS[i % len(_MODEL_COLORS)],
            "trials": len(mrecs), "scored": mf.n_scored if mf else 0, "pass": mf.n_pass if mf else 0,
            "cost": round(sum(r.cost_usd for r in mrecs), 4),
            "first": dates[0] if dates else "", "last": dates[-1] if dates else "",
            "cells": cells, "prov": prov, "daily": sorted([d, v[0], v[1]] for d, v in daily.items()),
            "runs": run_list[:40],
        })
        if mf:
            for f in mf.strengths + mf.weaknesses:
                c = f.cell
                findings.append({
                    "m": i, "kind": f.kind, "axis": c.axis, "key": c.key, "h": f.headline,
                    "c": _cell_list(c), "vd": c.verification_detail, "runs": list(c.run_ids)[:4],
                    "rep": f.replication_status,
                    "form": F._replicate_form_html(f, store) if f.kind == "weakness" else "",
                })

    trials = []
    for r in records:
        mask = 0
        for j, dim in enumerate(F.DIFFICULTY_DIMS):
            if dim in r.realized_dims and dim in cuts and F._bucket(float(r.realized_dims[dim]), cuts[dim]) == "high":
                mask |= 1 << j
        dims = [r.realized_dims.get(d) for d in F.DIFFICULTY_DIMS]
        resp = (r.response_text or "").strip().replace("\n", " ")
        trials.append([
            r.record_id, mindex.get(r.model_name, -1), F._query_kind_of(r) or "",
            "P" if r.verdict == Verdict.PASS else "F" if r.verdict == Verdict.FAIL else "E",
            {"stable": "s", "noise": "n", "unverified": "u"}.get(verification.get(r.record_id), ""),
            mask, [None if v is None else round(float(v), 2) for v in dims] if any(v is not None for v in dims) else None,
            resp[:72] + ("…" if len(resp) > 72 else ""), _detail_field(r, "detail")[:40], run_ix[r.run_id],
            (r.created_at or "")[:16].replace("T", " "), r.tokens_out, round(r.cost_usd, 6), r.finish_reason or "",
        ])

    newest = max((r.created_at or "" for r in records), default="")
    return {
        "generated": newest,
        "trials_total": len(records),
        "cost_total": round(sum(r.cost_usd for r in records), 2),
        "provinces": PROVINCES,
        "kinds": kinds_present,
        "dims": list(F.DIFFICULTY_DIMS),
        "dimHigh": data.dim_high_min,
        "thr": {"strength_rate": F.STRENGTH_PASS_RATE, "strength_n": F.STRENGTH_MIN_N,
                "weakness_rate": F.WEAKNESS_PASS_RATE, "weakness_n": F.WEAKNESS_MIN_N},
        "models": models,
        "findings": findings,
        "ev": {"runs": runs, "cells": evcells, "trials": trials},
    }


def payload_json(store: ResultsStore) -> bytes:
    """The cached `/api/map.json` body, rebuilt only when the store changes."""
    key = _store_key(store)
    with _LOCK:
        if key is not None and _CACHE["key"] == key and _CACHE["payload"] is not None:
            return _CACHE["payload"]
        body = json.dumps(build_payload(store), separators=(",", ":"), default=str).encode("utf-8")
        _CACHE["key"], _CACHE["payload"] = key, body
        return body


def slug(model: str) -> str:
    """The Map's URL slug for a model (`#model.<slug>.overview`), same rule as its JS."""
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


_SEARCH: dict = {"key": None, "body": None}


def search_json(store: ResultsStore, q: str = "") -> bytes:
    """`/api/search.json`: what the shared top bar on every page searches.

    With no `q`: the models (most trials first, as on the Map), the
    provinces, the trial total and the newest trial's date for the
    "Data to" readout. Cached until the store changes. With `q`: up to
    eight trial ids starting with it, so a pasted id prefix opens its record.
    """
    q = (q or "").strip().lower()
    if q:
        if not re.fullmatch(r"[0-9a-f]{4,32}", q):
            return b'{"trials":[]}'
        rows = store._conn.execute(
            "SELECT record_id, model_name, verdict FROM experiments WHERE record_id LIKE ? ORDER BY record_id LIMIT 8",
            (q + "%",),
        ).fetchall()
        trials = [{"id": r[0], "model": display_name(r[1]), "verdict": str(r[2])} for r in rows]
        return json.dumps({"trials": trials}, separators=(",", ":")).encode("utf-8")
    key = _store_key(store)
    with _LOCK:
        if key is not None and _SEARCH["key"] == key:
            return _SEARCH["body"]
        records = F._primary_records(store)  # the Map's population: no verify/replication/rejected rows
        counts: dict = defaultdict(int)
        for r in records:
            counts[r.model_name] += 1
        models = [{"id": m, "name": display_name(m), "slug": slug(m), "vendor": vendor_of(m), "trials": n}
                  for m, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        provinces = [{"id": p["id"], "name": p["short"], "color": p["color"]} for p in PROVINCES]
        newest = max((r.created_at for r in records if r.created_at), default=None)
        body = json.dumps({"models": models, "provinces": provinces, "trials": len(records), "newest": newest},
                          separators=(",", ":"), default=str).encode("utf-8")
        _SEARCH["key"], _SEARCH["body"] = key, body
        return body
