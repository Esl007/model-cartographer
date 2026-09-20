"""Tavily literature corroboration -- the "does public research agree?" layer.

For each confirmed / needs-replication curated finding, we form one or two
focused queries from its mechanism, search Tavily's public web index, and
ask the ANALYST MODEL to read the returned snippets and classify the
relation: corroborates / contradicts / no_clear_signal. The snippet quotes
and source URLs are retained so a human can check the call -- the LLM's
verdict is narration, never a stored measurement.

This whole layer degrades gracefully: with NO Tavily key, `corroborate_findings`
returns an empty list and the report renders "literature check not run (no
Tavily key)". Responses are cached to a scratch JSON so re-renders don't
re-bill, and searches run sequentially with a small `max_results` to respect
Tavily's rate limits. The key is loaded via `load_tavily_key` and is never
logged.

Stdlib only: HTTP is `urllib`, same idiom as `cartographer/exec/client.py`.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_RELATIONS = ("corroborates", "contradicts", "no_clear_signal")


def load_tavily_key(env_path: str = "env/Nebius api.txt") -> str | None:
    """Resolve a Tavily key, env var first then the local key file.

    Order: `TAVILY_API_KEY` env var, then a `tavily`/`tvly-` line in the
    key file (via `cartographer.exec.keys.load_keys`). Returns None if
    neither is present -- callers degrade rather than fail. Never logged.
    """
    env_key = os.environ.get("TAVILY_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    try:
        from ..exec.keys import load_keys

        return load_keys(env_path).get("tavily")
    except (OSError, KeyError):
        return None


@dataclass(frozen=True)
class Corroboration:
    """One finding checked against the literature."""

    finding_name: str
    finding_status: str
    queries: list = field(default_factory=list)
    results: list = field(default_factory=list)  # list of {title,url,content}
    relation: str = "no_clear_signal"  # one of _RELATIONS
    rationale: str = ""

    def to_json_dict(self) -> dict:
        return {
            "finding_name": self.finding_name,
            "finding_status": self.finding_status,
            "queries": list(self.queries),
            "results": list(self.results),
            "relation": self.relation,
            "rationale": self.rationale,
        }


def _post_json(url: str, payload: dict, timeout: float = 20) -> tuple[int, bytes]:
    """POST JSON, same non-raising contract shape as client._post but
    simplified to (status, body). A socket timeout returns (-1, b'')."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "model-cartographer/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (socket.timeout, TimeoutError, urllib.error.URLError):
        return -1, b""


def _queries_for(finding) -> list:
    """One or two focused literature queries derived from a finding's
    mechanism. Kept deterministic (no LLM) so the cache key is stable and a
    re-render never re-bills for the same finding."""
    name = getattr(finding, "name", "") or ""
    lname = name.lower()
    base = "large language model theory of mind"
    queries = []
    if "belief" in lname or "reality" in lname or "false" in lname:
        queries.append(f"{base} false belief task failure reality bias")
    if "pronoun" in lname or "coreference" in lname or "continuity" in lname:
        queries.append(
            "large language model pronoun coreference resolution error actor tracking"
        )
    if "abstention" in lname or "trap" in lname or "adversarial" in lname:
        queries.append(
            "large language model adversarial distractor abstention answer refusal reasoning"
        )
    if not queries:
        queries.append(f"{base} social reasoning failure mode")
    return queries[:2]


def _cache_load(cache_path: str | None) -> dict:
    if not cache_path:
        return {}
    p = Path(cache_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cache_store(cache_path: str | None, cache: dict) -> None:
    if not cache_path:
        return
    try:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(
            json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        pass


def _tavily_search(
    query: str,
    tavily_key: str,
    cache: dict,
    max_results: int = 5,
    transport=_post_json,
) -> list:
    """Search Tavily for one query, using `cache` (query -> results) to
    avoid re-billing. Returns a list of {title,url,content} dicts; on any
    error returns whatever the cache holds, else an empty list."""
    if query in cache:
        return cache[query]
    payload = {
        "api_key": tavily_key,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
    }
    status, raw = transport(_TAVILY_SEARCH_URL, payload)
    if status < 200 or status >= 300:
        return []
    try:
        data = json.loads(raw)
        results = [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "content": str(r.get("content", ""))[:500],
            }
            for r in data.get("results", [])
        ]
    except (ValueError, TypeError, AttributeError):
        return []
    cache[query] = results
    return results


def _classify_relation(client, analyst_model: str, finding, results: list) -> tuple[str, str]:
    """Ask the analyst model whether the snippets corroborate, contradict,
    or give no clear signal for the finding's mechanism. Returns
    (relation, rationale). This is narration-layer, so LLM use is fine; the
    snippets are retained separately for human checking."""
    if not results:
        return "no_clear_signal", "no search results returned"
    snippet_block = "\n".join(
        f"- {r.get('title','')}: {r.get('content','')}" for r in results[:5]
    )
    mechanism = getattr(finding, "mechanism", "") or ""
    name = getattr(finding, "name", "") or ""
    prompt = (
        "A measurement instrument found this failure mode in a language model:\n\n"
        f"FINDING: {name}\nMECHANISM: {mechanism}\n\n"
        "Here are public literature snippets from a web search:\n"
        f"{snippet_block}\n\n"
        "Does the public literature CORROBORATE, CONTRADICT, or give NO CLEAR "
        "SIGNAL about this specific failure mechanism? Answer on the first line "
        "with exactly one of: corroborates / contradicts / no_clear_signal. "
        "Then one sentence of rationale.\n"
        "Do not invent findings not present in the snippets."
    )
    result = client.chat(analyst_model, prompt, max_tokens=200)
    if getattr(result, "error", None) is not None:
        return "no_clear_signal", f"classification unavailable ({result.error})"
    text = (getattr(result, "text", "") or "").strip()
    first = text.splitlines()[0].strip().lower() if text else ""
    relation = "no_clear_signal"
    for r in _RELATIONS:
        if r in first:
            relation = r
            break
    rationale = " ".join(text.splitlines()[1:]).strip() or text
    return relation, rationale


def corroborate_findings(
    findings,
    tavily_key: str | None,
    client=None,
    analyst_model: str = "",
    cache_path: str | None = None,
    max_results: int = 5,
    transport=_post_json,
) -> list:
    """Corroborate each confirmed / needs-replication finding against the
    literature.

    With NO `tavily_key`, returns [] -- the caller renders "literature check
    not run (no Tavily key)". `client`/`analyst_model` drive the relation
    classification (an LLM reading the snippets); if omitted, relations are
    left as "no_clear_signal" but the raw results are still returned so a
    human can read them. Searches run sequentially (rate-limit-friendly) and
    reuse `cache_path` across re-renders.
    """
    if not tavily_key:
        return []

    eligible = [
        f
        for f in findings
        if getattr(f, "status", "") in ("confirmed", "needs-replication")
    ]

    cache = _cache_load(cache_path)
    out: list[Corroboration] = []
    for f in eligible:
        queries = _queries_for(f)
        merged: list = []
        seen_urls: set[str] = set()
        for q in queries:
            for r in _tavily_search(q, tavily_key, cache, max_results, transport):
                url = r.get("url", "")
                if url and url in seen_urls:
                    continue
                seen_urls.add(url)
                merged.append(r)

        if client is not None and analyst_model:
            relation, rationale = _classify_relation(client, analyst_model, f, merged)
        else:
            relation, rationale = "no_clear_signal", "relation not classified (no client)"

        out.append(
            Corroboration(
                finding_name=getattr(f, "name", ""),
                finding_status=getattr(f, "status", ""),
                queries=queries,
                results=merged,
                relation=relation,
                rationale=rationale,
            )
        )

    _cache_store(cache_path, cache)
    return out
