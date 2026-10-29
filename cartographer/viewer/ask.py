"""Ask the Cartographer: a live Nemotron agent over the map, on Nebius Token Factory.

Three tiers, each where it fits:

  1. Nemotron 3 Nano (thinking off, ~1 s) reads the question and names the
     models and capabilities it is about, choosing only from lists we give it.
  2. The app pulls the matching facts from the results store. Every fact is a
     number the viewer itself derived from scored trials, with a short id.
  3. Nemotron 3 Super (thinking on) answers from those facts only, citing ids.
  4. A deterministic audit checks every number in the answer against the
     facts it was given; anything not found is flagged on screen.

Separately, `explain` hands one failed trial to Nemotron 3 Ultra (serious
reasoning) together with the verified true answer and the verifier's own
trap classification, and asks why the model got it wrong.

Every call goes through `cartographer.exec.client.Client(NEBIUS, key)`. On the
public deployment the key comes from the NEBIUS_API_KEY environment variable,
and spend is capped per instance per day and rate-limited per visitor.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass

from ..exec.client import NEBIUS, Client
from ..exec.pricing import PRICES
from ..store.store import ResultsStore

NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
TIER_NAME = {NANO: "Nemotron 3 Nano", SUPER: "Nemotron 3 Super", ULTRA: "Nemotron 3 Ultra"}

KIND_LABEL = {"state": "State", "order": "Order", "belief": "Belief", "adversarial_belief": "Adversarial belief",
              "tool_plan": "Tool plan", "interactive": "Interactive", "code_trace": "Code trace",
              "code_repair": "Code repair", "code_write": "Code writing", "repo_fix": "Repository fix"}
DIM_LABEL = {"entity_state": "Entity state", "temporal": "Temporal", "coreference": "Coreference",
             "distractors": "Distractors", "context_length": "Context length", "witness_gap": "Witness gap"}

MAX_FACTS = 80


# ---------------------------------------------------------------------------
# Nebius client, budget and rate limits
# ---------------------------------------------------------------------------

class AskUnavailable(Exception):
    """A reason the agent cannot run right now, safe to show the visitor."""


_CLIENT: dict = {"c": None}
_LOCK = threading.Lock()
_SPEND = {"day": "", "usd": 0.0}
_HITS: dict[str, list[float]] = {}
DAILY_USD = float(os.environ.get("ASK_DAILY_USD", "2.0"))
PER_IP = int(os.environ.get("ASK_PER_IP_10MIN", "12"))


def _client() -> Client:
    if _CLIENT["c"] is None:
        key = os.environ.get("NEBIUS_API_KEY", "").strip()
        if not key:
            try:
                from ..exec.keys import load_keys

                key = load_keys().get("nebius", "")
            except Exception:  # noqa: BLE001 -- no key file on this machine
                key = ""
        if not key:
            raise AskUnavailable("No Nebius API key is configured for this deployment.")
        _CLIENT["c"] = Client(NEBIUS, key)
    return _CLIENT["c"]


def check_limits(ip: str) -> None:
    """Raise AskUnavailable when this visitor or this instance is over its limit."""
    now = time.time()
    with _LOCK:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if _SPEND["day"] != day:
            _SPEND.update(day=day, usd=0.0)
        if _SPEND["usd"] >= DAILY_USD:
            raise AskUnavailable("Today's live-call budget for this demo is used up. Please try again tomorrow.")
        hits = [t for t in _HITS.get(ip, []) if now - t < 600]
        if len(hits) >= PER_IP:
            raise AskUnavailable("Too many questions in ten minutes. Please wait a few minutes.")
        hits.append(now)
        _HITS[ip] = hits


def _charge(usd: float) -> None:
    with _LOCK:
        _SPEND["usd"] += usd


def _cost(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = PRICES.get(model, (0.0, 0.0))
    return tokens_in / 1e6 * p_in + tokens_out / 1e6 * p_out


def _call(model: str, prompt: str, max_tokens: int, thinking: bool, system: str | None = None) -> dict:
    """One Token Factory chat call; returns text plus the numbers the UI shows."""
    c = _client()
    body = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    if not thinking:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    url, headers, _ = c._build_request(model, prompt, max_tokens, 0.0)
    import urllib.error
    import urllib.request

    t0 = time.time()
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=240) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise AskUnavailable(f"Token Factory returned HTTP {e.code} for {TIER_NAME.get(model, model)}.") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise AskUnavailable(f"Could not reach Token Factory ({type(e).__name__}).") from None
    latency = time.time() - t0
    choice = payload["choices"][0]
    msg = choice.get("message", {})
    usage = payload.get("usage", {})
    t_in, t_out = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    cost = _cost(model, t_in, t_out)
    _charge(cost)
    reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
    return {
        "model": model, "tier": TIER_NAME.get(model, model), "text": (msg.get("content") or "").strip(),
        "latency_s": round(latency, 2), "tokens_in": t_in, "tokens_out": t_out, "cost_usd": round(cost, 6),
        "reasoning_chars": len(reasoning), "finish": choice.get("finish_reason"), "thinking": thinking,
    }


# ---------------------------------------------------------------------------
# Evidence: numbered facts derived from the store
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    id: str
    model: str          # display name
    topics: frozenset   # kinds, dims, provinces, breaking-point keys
    text: str


_FACTS: dict = {"key": None, "facts": None, "models": None, "topics": None}


def _pct(a: int, b: int) -> str:
    return f"{round(a / b * 100)}%" if b else "n/a"


def facts(store: ResultsStore) -> tuple[list[Fact], list[str], dict[str, str]]:
    """(facts, model display names, topic id -> label). Cached per store state."""
    from ..report.frontier import frontier_summaries
    from .globe_data import _store_key, display_name, payload_json

    key = _store_key(store)
    if key is not None and _FACTS["key"] == key:
        return _FACTS["facts"], _FACTS["models"], _FACTS["topics"]

    data = json.loads(payload_json(store))
    prov_of = {k: p for p in data["provinces"] for k in p["kinds"]}
    topics: dict[str, str] = {}
    out: list[Fact] = []

    def add(model: str, tops: set, text: str) -> None:
        out.append(Fact(f"F{len(out) + 1}", model, frozenset(tops), text))

    for m in data["models"]:
        name = m["display"]
        measured = []
        for p in data["provinces"]:
            cells = [m["cells"].get(k) for k in p["kinds"] if m["cells"].get(k) and m["cells"][k][1]]
            if cells:
                a, b = sum(c[0] for c in cells), sum(c[1] for c in cells)
                measured.append(a / b * 100)
                topics[p["id"]] = p["name"]
                add(name, {p["id"], *p["kinds"]}, f"{name}, {p['name']} province: {a}/{b} scored trials passed ({_pct(a, b)}).")
        if m["scored"]:
            avg = sum(measured) / len(measured) if measured else 0
            add(name, {"overall"}, f"{name} overall: {m['pass']}/{m['scored']} scored trials passed ({_pct(m['pass'], m['scored'])}); "
                                   f"province average {avg:.1f} over {len(measured)} of {len(data['provinces'])} provinces; "
                                   f"{m['trials']} trials recorded; spend ${m['cost']:.2f}.")
        for k in data["kinds"]:
            c = m["cells"].get(k)
            if c and c[1]:
                topics[k] = KIND_LABEL.get(k, k)
                prov = prov_of.get(k, {}).get("name", "")
                add(name, {k, prov_of.get(k, {}).get("id", "")}, f"{name}, {KIND_LABEL.get(k, k)} ({prov}): {c[0]}/{c[1]} scored passed ({_pct(c[0], c[1])}).")
        for d in data["dims"]:
            c = m["cells"].get(d)
            if c and c[1]:
                topics[d] = DIM_LABEL.get(d, d)
                add(name, {d, "narrative"}, f"{name}, narrative stories in the hardest third of {DIM_LABEL.get(d, d).lower()} "
                                            f"(>= {data['dimHigh'][d]:g}): {c[0]}/{c[1]} scored passed ({_pct(c[0], c[1])}).")

    for mf in frontier_summaries(store):
        name = display_name(mf.model)
        for row in mf.rows:
            if row.status == "NOT TESTED":
                continue
            topics[row.key] = row.label
            bits = [f"{name}, breaking point for '{row.label}' ({row.province}): {row.status}"]
            if row.headline:
                bits.append(row.headline.rstrip("."))
            if row.boundary:
                bits.append(f"boundary {row.boundary}")
            if row.held_through:
                bits.append(f"held through {row.held_through}")
            if row.n_scored:
                bits.append(f"{row.n_pass}/{row.n_scored} scored passed")
            if row.confidence:
                bits.append(f"confidence grade {row.confidence}")
            prov_id = next((p["id"] for p in data["provinces"] if p["name"].lower().startswith(row.province.lower()[:6])), "")
            add(name, {row.key, prov_id, "breaking"}, "; ".join(bits) + ".")

    names = [m["display"] for m in data["models"]]
    for f in data["findings"]:
        name = data["models"][f["m"]]["display"]
        h = f["h"].replace(data["models"][f["m"]]["id"], name)
        add(name, {f["key"], "findings", f["kind"]}, f"{name} {f['kind']}: {h}.")

    _FACTS.update(key=key, facts=out, models=names, topics=topics)
    return out, names, topics


# ---------------------------------------------------------------------------
# Step 1: Nano plans
# ---------------------------------------------------------------------------

def plan(store: ResultsStore, question: str) -> dict:
    question = question.strip()[:400]
    if len(question) < 3:
        raise AskUnavailable("Ask a question about the models on the map.")
    _, names, topics = facts(store)
    topic_list = "\n".join(f"- {k}: {v}" for k, v in sorted(topics.items()))
    prompt = (
        "You route questions about an LLM capability map. Pick which models and topics the question is about.\n"
        f"Models (use these exact names): {json.dumps(names)}\n"
        f"Topics (use the ids before the colon):\n{topic_list}\n"
        "- overall: overall pass rates\n- findings: listed weaknesses and strengths\n- breaking: where models break\n\n"
        f"Question: {question}\n\n"
        'Reply with JSON only: {"models": [...], "topics": [...], "intent": "compare|explain|rank|lookup"}. '
        "Use [] for models or topics when the question covers all of them."
    )
    r = _call(NANO, prompt, max_tokens=300, thinking=False)
    parsed = {}
    m = re.search(r"\{.*\}", r["text"], re.S)
    if m:
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            parsed = {}
    lower = {n.lower(): n for n in names}
    models = [lower[x.lower()] for x in parsed.get("models", []) if isinstance(x, str) and x.lower() in lower]
    tops = [x for x in parsed.get("topics", []) if isinstance(x, str) and (x in topics or x in ("overall", "findings", "breaking"))]
    intent = parsed.get("intent") if parsed.get("intent") in ("compare", "explain", "rank", "lookup") else "lookup"
    return {"question": question, "models": models, "topics": tops, "intent": intent, "call": r}


def select(store: ResultsStore, models: list[str], topics: list[str]) -> list[Fact]:
    all_facts, _, _ = facts(store)
    pick = [f for f in all_facts if (not models or f.model in models) and (not topics or f.topics & set(topics))]
    if not pick and models:  # topic too narrow for these models: keep the models' overview
        pick = [f for f in all_facts if f.model in models and ("overall" in f.topics or "breaking" in f.topics)]
    if not pick:
        pick = [f for f in all_facts if "overall" in f.topics or "breaking" in f.topics]
    # breaking points and overall first, then the rest, capped
    order = {"overall": 0, "breaking": 1}
    pick.sort(key=lambda f: min((order[t] for t in f.topics if t in order), default=2))
    return pick[:MAX_FACTS]


# ---------------------------------------------------------------------------
# Step 2: Super answers, step 3: deterministic audit
# ---------------------------------------------------------------------------

def answer(store: ResultsStore, question: str, models: list[str], topics: list[str]) -> dict:
    chosen = select(store, models, topics)
    evidence = "\n".join(f"[{f.id}] {f.text}" for f in chosen)
    prompt = (
        "You are the analyst for Model Cartographer, which measures where language models break using tasks "
        "generated from simulated worlds with deterministic ground truth (no LLM judge). "
        "Answer the question using ONLY the facts below. Cite the fact ids you use in square brackets, like [F3]. "
        "Use only numbers that appear in the facts; do not compute new ones. If the facts do not answer the question, say so. "
        "Write at most 110 words, plain sentences, no headings, no bullet points.\n\n"
        f"Facts:\n{evidence}\n\nQuestion: {question.strip()[:400]}"
    )
    r = _call(SUPER, prompt, max_tokens=4000, thinking=True)
    if not r["text"]:
        raise AskUnavailable("Nemotron 3 Super did not return an answer in its token budget. Try a narrower question.")
    return {"answer": r["text"], "facts": [{"id": f.id, "text": f.text} for f in chosen], "audit": audit(r["text"], chosen),
            "call": r}


_NAME_NUMS = re.compile(
    r"Nemotron\s*3(?:\.5)?|Nano\s*30B|Super\s*120B|Ultra\s*550B|Gemma\s*[34]|\b\d+B\b|gpt-oss-120b|\[F\d+\]|QAT|\bF\d+\b",
    re.I)
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")


def _norm(n: str) -> str:
    return n.replace(",", "").rstrip("%")


def audit(text: str, chosen: list[Fact]) -> dict:
    """Every number in the answer must appear in the facts it was given."""
    cited = set(re.findall(r"\[?(F\d+)\]?", text))
    by_id = {f.id: f for f in chosen}
    cited_nums = {_norm(n) for fid in cited if fid in by_id for n in _NUM.findall(by_id[fid].text)}
    all_nums = {_norm(n) for f in chosen for n in _NUM.findall(f.text)}
    checks = []
    for raw in _NUM.findall(_NAME_NUMS.sub(" ", text)):
        n = _norm(raw)
        status = "cited" if n in cited_nums else ("in evidence" if n in all_nums else "not found")
        checks.append({"value": raw, "status": status})
    unknown_ids = sorted(fid for fid in cited if fid not in by_id)
    return {"checks": checks, "unsupported": [c["value"] for c in checks if c["status"] == "not found"],
            "unknown_citations": unknown_ids, "cited": sorted(cited & set(by_id), key=lambda x: int(x[1:]))}


# ---------------------------------------------------------------------------
# Ultra explains one failed trial
# ---------------------------------------------------------------------------

def explain(store: ResultsStore, record_id: str) -> dict:
    from ..store.store import _row_to_record
    from .globe_data import display_name

    row = store._conn.execute("SELECT * FROM experiments WHERE record_id = ?", (record_id,)).fetchone()
    if row is None:
        raise AskUnavailable("No such trial.")
    rec = _row_to_record(row)
    if rec.verdict.name != "FAIL":
        raise AskUnavailable("Only failed trials can be explained.")
    truth, trap = None, None
    try:
        from ..report.verdict import _is_reconstructable, _regenerate, classify_belief_failure

        if _is_reconstructable(rec):
            truth = _regenerate(rec)[3].true_answer
            trap = classify_belief_failure(rec)
    except Exception:  # noqa: BLE001 -- not every task family can be regenerated
        pass
    dims = ", ".join(f"{k}={v:g}" for k, v in sorted(rec.realized_dims.items())) if rec.realized_dims else "none recorded"
    prompt = (
        f"A language model ({display_name(rec.model_name)}) failed this task in Model Cartographer. The task was generated "
        "from a simulated world, and a deterministic verifier marked the answer wrong.\n\n"
        f"TASK PROMPT:\n{(rec.prompt_text or '(not recorded)')[:7000]}\n\n"
        f"MODEL'S ANSWER:\n{(rec.response_text or '(empty)')[:2000]}\n\n"
        + (f"VERIFIED CORRECT ANSWER: {truth}\n" if truth else "")
        + f"VERIFIER DETAIL: {rec.verifier_detail or 'none'}\n"
        f"DIFFICULTY SETTINGS OF THIS TASK: {dims}\n\n"
        "In at most 120 words, explain what the task required, exactly where the model's answer went wrong (point to the "
        "sentences in the prompt that decide it), and which difficulty setting most likely caused the mistake. "
        "Plain sentences, no headings."
    )
    r = _call(ULTRA, prompt, max_tokens=12000, thinking=True)
    if not r["text"]:
        raise AskUnavailable("Nemotron 3 Ultra ran out of its reasoning budget on this trial.")
    return {"record_id": record_id, "model": display_name(rec.model_name), "truth": truth, "trap": trap,
            "answer": (rec.response_text or "").strip()[:200], "explanation": r["text"], "call": r}
