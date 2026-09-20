"""Load provider API keys from a local, gitignored key file.

Keys must never be logged, printed, or echoed anywhere: not in exceptions,
not in `repr()` of any object that holds one, not in test output. Callers
that need to display state should redact key material explicitly.
"""

from __future__ import annotations

import re
from pathlib import Path

_GROQ_KEY_RE = re.compile(r"gsk_[A-Za-z0-9_]+")
# Tavily search keys are conventionally prefixed "tvly-".
_TAVILY_KEY_RE = re.compile(r"tvly-[A-Za-z0-9_\-]+")


def load_keys(path: str = "env/Nebius api.txt") -> dict[str, str]:
    """Parse the Nebius/Groq/Tavily key file relative to the repo root.

    Line 1 is the Nebius key (starts with "v1."). Any line containing
    "groq" (case-insensitive) holds the Groq key, extracted via regex. Any
    line containing "tavily" (case-insensitive) or a "tvly-" token holds
    the Tavily search key. Missing keys are simply omitted from the
    returned dict.
    """
    repo_root = Path(__file__).resolve().parents[2]
    key_path = repo_root / path
    text = key_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    keys: dict[str, str] = {}

    if lines:
        first_line = lines[0].strip()
        if first_line.startswith("v1."):
            keys["nebius"] = first_line

    for line in lines:
        if "groq" in line.lower():
            match = _GROQ_KEY_RE.search(line)
            if match:
                keys["groq"] = match.group(0)
                break

    for line in lines:
        lower = line.lower()
        if "tavily" in lower or "tvly-" in lower:
            match = _TAVILY_KEY_RE.search(line)
            if match:
                keys["tavily"] = match.group(0)
                break

    return keys
