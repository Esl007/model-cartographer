"""Fact extraction: ask an "extractor" model to parse a rendered story back
into the same structured fact schema used by `facts.world_facts`, so the
fidelity gate can compare the two deterministically.
"""

from __future__ import annotations

import json

from ..exec.client import Client

EXTRACT_PROMPT_TEMPLATE = """You will receive a short story. Extract every movement, transfer, and \
tell event mentioned in the story into a JSON array, one object per event, \
using EXACTLY this schema:

- move: {{"time": "8:04", "type": "move", "actor": "<name>", "object": "<name>", "destination": "<name>"}}
- transfer: {{"time": "8:05", "type": "transfer", "giver": "<name>", "object": "<name>", "receiver": "<name>"}}
- tell: {{"time": "8:06", "type": "tell", "speaker": "<name>", "listener": "<name>", "object": "<name>", "claimed_location": "<name>"}}

Example output:
[
  {{"time": "8:04", "type": "move", "actor": "Elena", "object": "the blue box", "destination": "the kitchen"}},
  {{"time": "8:06", "type": "tell", "speaker": "Marcus", "listener": "Elena", "object": "the blue box", "claimed_location": "the lobby"}}
]

Use the exact time stated in the story for each event's "time" field. \
Output ONLY the JSON array — no prose, no explanation, no code fences.

Story:
{story}
"""


def extract_facts_tokens(
    story: str, client: Client, extractor_model: str
) -> tuple[list[dict] | None, int]:
    """Like `extract_facts`, but also reports total tokens spent on the call
    (0 if the call itself errored before any usage was billed)."""
    prompt = EXTRACT_PROMPT_TEMPLATE.format(story=story)
    result = client.chat(
        model=extractor_model,
        prompt=prompt,
        max_tokens=2000,
        temperature=0.0,
    )
    tokens = result.tokens_in + result.tokens_out
    if result.error is not None:
        return None, tokens

    text = result.text
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None, tokens
    candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None, tokens
    if not isinstance(parsed, list):
        return None, tokens
    if not all(isinstance(item, dict) for item in parsed):
        return None, tokens
    return parsed, tokens


def extract_facts(story: str, client: Client, extractor_model: str) -> list[dict] | None:
    """Ask `extractor_model` to parse `story` into a fact list. Returns None
    on client error or unparseable output."""
    parsed, _tokens = extract_facts_tokens(story, client, extractor_model)
    return parsed
