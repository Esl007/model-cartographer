"""Proposal protocol: ask a proposer LLM for candidate generator CONFIGS
(never answers) it believes a subject model will fail on.

The proposer's output is a JSON array of objects with a `config` dict (a
subset of `cartographer.world.generate.DimParams` fields), an optional
`require_traps` list (foil-vocabulary names from
`cartographer.world.foils.foil_answers`), and a one-line `hypothesis`
string explaining why the proposer thinks the subject will fail there.

Ground truth is never touched here -- `propose_configs` only calls the LLM
and parses text; validating/clamping/scoring configs against the real
generator happens in `gauntlet.py`, entirely offline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Exact legal ranges, read from cartographer/world/generate.py's DimParams
# and evaluator.py's _GENERATOR_LIMITS (the validated hard ceilings).
_SCHEMA_TEXT = """\
Config schema (all fields optional; omitted fields use generator defaults):
  chain_length: int, 1..30        (length of the causal move chain)
  scramble: float, 0.0..1.0       (fraction of narration-order transpositions)
  coref_pressure: int, 0..8       (extra same-gender filler actors, pronoun pressure)
  distractor_events: int, 0..40   (irrelevant/near-duplicate object moves)
  distractor_similarity: one of "unrelated" | "same_entities" | "same_category" | "near_duplicate"
  filler_events: int, 0..60       (narrative padding events)
  query_kind: one of "state" | "belief" | "order"
  witness_cutoff: float, 0.0..0.9 (belief-only: how early the query agent's
    witnessing of the chain stops relative to the object's continued movement;
    0.0 = agent witnesses up to the second-to-last move, 0.9 = agent only
    witnesses the very first move)

Optional require_traps: a list drawn from this vocabulary (belief-query foil
names from cartographer/world/foils.py) naming which specific trap(s) the
proposed config's world must plant to be worth testing:
  final_true_state, last_mentioned_location, last_witnessed_by_anyone,
  query_agent_own_last_move, most_frequent_location
"""

PROPOSAL_PROMPT = """\
You are an ADVERSARIAL TASK PROPOSER. Your job is to propose QUESTIONS
(generator configurations), never answers. You never see or produce ground
truth -- a separate deterministic simulator computes the correct answer for
whatever world your config generates. You are rewarded only when a config
you propose causes the SUBJECT MODEL to be measurably wrong on real trials.
A config that turns out easy for the subject model earns you nothing;
proposing a config you cannot back up with a real hypothesis wastes your
own budget, so think before proposing.

{digest}

{schema}

Propose {n} candidate configs you believe the subject model will FAIL on,
each with a one-line hypothesis explaining the specific reasoning failure
you expect (e.g. "long witness_cutoff plus high coref_pressure should make
the agent's stale belief collide with pronoun-confused actors"). Prefer
regions NOT already covered by the PASS configs in the digest above, and
build on any confirmed trap signatures or FAIL configs you see.

Respond with ONLY a JSON array, no prose before or after, in this exact
shape:
[
  {{"config": {{"chain_length": 8, "witness_cutoff": 0.7, "query_kind": "belief"}},
    "require_traps": ["final_true_state"],
    "hypothesis": "one-line reasoning"}},
  ...
]
"""

_JSON_ONLY_NUDGE = (
    "Your previous response could not be parsed as JSON. Respond with ONLY "
    "the JSON array this time -- no markdown fences, no prose, no reasoning "
    "preamble. Just the array."
)


@dataclass(frozen=True)
class Proposal:
    config: dict
    require_traps: tuple[str, ...] = ()
    hypothesis: str = ""
    raw: str = ""


def _strip_fences_and_preamble(text: str) -> str:
    """Best-effort extraction of a JSON array substring from `text`: strips
    ```json ... ``` fences if present, else finds the first '[' through the
    matching last ']' in the text (tolerates reasoning preamble before/after
    the array, common with reasoning-trace models)."""
    stripped = text.strip()

    fence_match = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    start = stripped.find("[")
    end = stripped.rfind("]")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]

    return stripped


def _parse_proposals(text: str) -> list[Proposal] | None:
    """Parse `text` into a list of `Proposal`s. Returns None (not []) on
    parse failure, so the caller can distinguish "malformed, please retry"
    from "parsed to an empty list" (a valid, if useless, response)."""
    candidate = _strip_fences_and_preamble(text)
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list):
        return None

    proposals: list[Proposal] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        config = item.get("config")
        if not isinstance(config, dict):
            continue
        require_traps = item.get("require_traps") or []
        if not isinstance(require_traps, list):
            require_traps = []
        hypothesis = item.get("hypothesis", "")
        if not isinstance(hypothesis, str):
            hypothesis = str(hypothesis)
        proposals.append(
            Proposal(
                config=config,
                require_traps=tuple(str(t) for t in require_traps),
                hypothesis=hypothesis,
                raw=json.dumps(item),
            )
        )
    return proposals


def propose_configs(
    client,
    proposer_model: str,
    digest: str,
    n: int = 6,
    seed: int = 0,
    max_tokens: int = 2000,
) -> list[Proposal]:
    """Call `proposer_model` via `client.chat(...)` (same `Client` interface
    as `cartographer.exec.client.Client`) for `n` candidate proposals.

    Robust JSON parsing: strips code fences / reasoning preamble; on parse
    failure, retries ONCE with a "JSON only" nudge appended; if that also
    fails to parse, returns `[]` (never raises -- a failed proposer round
    should degrade to "no proposals," not crash the loop).
    """
    prompt = PROPOSAL_PROMPT.format(digest=digest, schema=_SCHEMA_TEXT, n=n)
    result = client.chat(model=proposer_model, prompt=prompt, max_tokens=max_tokens)
    if result.error is None:
        parsed = _parse_proposals(result.text)
        if parsed is not None:
            return parsed

    retry_prompt = prompt + "\n\n" + _JSON_ONLY_NUDGE
    retry = client.chat(model=proposer_model, prompt=retry_prompt, max_tokens=max_tokens)
    if retry.error is None:
        parsed = _parse_proposals(retry.text)
        if parsed is not None:
            return parsed

    return []
