"""Pure string-based grading of a model's response against a `RenderedTask`.

No model calls, no network I/O — `verify` only inspects the response string,
the rendered task's `answer_key`, and the `NameMap` used to render it.
"""

from __future__ import annotations

import re
import string

from ..store.schema import Verdict
from .naming import NameMap
from .templates import RenderedTask

_ARTICLES = {"the", "a", "an"}
_UNKNOWN_PHRASES = ["unknown", "no belief", "doesn't know", "does not know", "don't know"]


def _normalize(text: str) -> str:
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    tokens = [t for t in text.split() if t not in _ARTICLES]
    return " ".join(tokens)


def _all_location_names(name_map: NameMap) -> list[str]:
    return [_normalize(n) for n in name_map.location.values()]


def _contains_phrase(normalized_response: str, phrase: str) -> bool:
    norm_phrase = _normalize(phrase)
    return norm_phrase in normalized_response


def verify(response: str, rendered: RenderedTask, name_map: NameMap) -> Verdict:
    if response is None or not response.strip():
        return Verdict.ERROR

    normalized_response = _normalize(response)
    query_kind = rendered.meta.get("query_kind")

    if query_kind == "order":
        has_first = bool(re.search(r"\bfirst\b", normalized_response))
        has_second = bool(re.search(r"\bsecond\b", normalized_response))
        if has_first == has_second:
            return Verdict.FAIL
        said = "first" if has_first else "second"
        return Verdict.PASS if said == rendered.answer_key else Verdict.FAIL

    if query_kind in ("state", "belief"):
        location_names = _all_location_names(name_map)

        if rendered.answer_key == "unknown":
            mentions_any_location = any(
                _contains_phrase(normalized_response, loc) for loc in location_names
            )
            mentions_unknown = any(
                _contains_phrase(normalized_response, phrase) for phrase in _UNKNOWN_PHRASES
            )
            if mentions_unknown and not mentions_any_location:
                return Verdict.PASS
            return Verdict.FAIL

        expected_norm = _normalize(rendered.answer_key)
        mentioned_locations = {
            loc for loc in location_names if _contains_phrase(normalized_response, loc)
        }
        if expected_norm in mentioned_locations and len(mentioned_locations) == 1:
            return Verdict.PASS
        return Verdict.FAIL

    return Verdict.FAIL
