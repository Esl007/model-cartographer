"""The analyst call + the contamination audit.

`write_diagnosis(client, analyst_model, evidence_pack, corroborations=None)`
prompts an LLM (Nemotron by default in the script layer) with the rendered
evidence pack and STRICT instructions: cite only numbers present in the
pack, never invent a figure. The response is parsed into named sections
(executive summary / per-model profiles / cross-model comparison /
recommended next probes), robust to code fences and preamble.

`audit_diagnosis(diagnosis, pack)` is the product's signature honesty move:
a DETERMINISTIC post-check that extracts every numeric token the analyst
wrote (percents, bare counts, dollar amounts) and diffs each against the
set of numbers actually present in the evidence pack, at the pack's own
rounding precision. Any figure that has no support is returned as an
`UnsupportedClaim` so the report can render it visibly as an "unverified
figure" rather than letting an invented number pass silently.

Nothing in this module writes to the results store. The LLM narrates; the
numbers are the store's alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .evidence import render_pack

# Section headers we ask the analyst to emit, mapped to the parsed-dict key.
# Matching is done on a normalized (lowercased, punctuation-stripped)
# header line, so "## Executive Summary" and "EXECUTIVE SUMMARY:" both hit.
_SECTION_ORDER = [
    ("executive_summary", ("executive summary", "summary")),
    ("per_model", ("per-model profile", "per model profile", "model profiles", "per-model profiles")),
    ("cross_model", ("cross-model comparison", "cross model comparison", "comparison")),
    ("next_probes", ("recommended next probes", "next probes", "recommendations", "next steps")),
]

_SYSTEM_INSTRUCTIONS = """\
You are the Evidence Analyst for Model Cartographer, an instrument that maps \
where language models break at social/theory-of-mind reasoning. You are \
reading a DETERMINISTIC evidence pack computed straight from a measurement \
store. Write a capability diagnosis.

ABSOLUTE RULES:
1. Every number you state MUST appear in the evidence pack. Do NOT invent, \
estimate, extrapolate, or round to new figures. If you want to mention a \
rate or count, copy it from the pack exactly as written.
2. You may interpret and narrate freely, but interpretation is words, not \
new numbers. Never manufacture a statistic to support a point.
3. A finding marked evidence_resolves=false has lost its underlying rows -- \
you may mention it exists but must NOT treat its numbers as live evidence.
4. Percentages are pass rates over SCORED trials only (PASS+FAIL), infra \
errors and resample-noise failures excluded. Say "pass rate", not "accuracy".

STRUCTURE your answer with these four sections, each under its own header:

## Executive Summary
Three to five sentences: the single most important thing the evidence shows \
about these models' theory-of-mind reasoning.

## Per-Model Profile
For each model: strengths, weaknesses, and exactly where it breaks (which \
query kind or difficulty region, with the pack's numbers).

## Cross-Model Comparison
How the models differ -- tier discrimination, shared vs distinct failure \
modes.

## Recommended Next Probes
Concrete next experiments the evidence motivates.
"""


@dataclass(frozen=True)
class Diagnosis:
    """The analyst's written diagnosis, parsed into sections."""

    model: str
    raw_text: str
    executive_summary: str = ""
    per_model: str = ""
    cross_model: str = ""
    next_probes: str = ""
    error: str | None = None

    def sections(self) -> list[tuple[str, str]]:
        """(title, body) in canonical order, skipping empty bodies."""
        titles = {
            "executive_summary": "Executive Summary",
            "per_model": "Per-Model Profile",
            "cross_model": "Cross-Model Comparison",
            "next_probes": "Recommended Next Probes",
        }
        out = []
        for key, title in titles.items():
            body = getattr(self, key)
            if body.strip():
                out.append((title, body.strip()))
        return out

    def to_json_dict(self) -> dict:
        return {
            "model": self.model,
            "raw_text": self.raw_text,
            "executive_summary": self.executive_summary,
            "per_model": self.per_model,
            "cross_model": self.cross_model,
            "next_probes": self.next_probes,
            "error": self.error,
        }


@dataclass(frozen=True)
class UnsupportedClaim:
    """One numeric token the analyst stated that the evidence pack does not
    support -- rendered as an 'unverified figure' flag."""

    figure: str  # the token exactly as written, e.g. "88%" or "$0.42"
    normalized: float  # its numeric value
    kind: str  # "percent" | "money" | "count"
    context: str  # a short window of surrounding text

    def to_json_dict(self) -> dict:
        return {
            "figure": self.figure,
            "normalized": self.normalized,
            "kind": self.kind,
            "context": self.context,
        }


def _normalize_header(line: str) -> str:
    line = line.strip().lstrip("#").strip()
    line = line.strip("*").strip()
    line = line.rstrip(":").strip()
    line = re.sub(r"[^a-z0-9 \-]", "", line.lower())
    return line.strip()


def _strip_fences(text: str) -> str:
    """Remove a leading ```...``` fence wrapper if the model wrapped the
    whole answer in one; leaves inline code alone."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # drop the first fence line and a trailing fence if present
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines)
    return text


def parse_sections(text: str) -> dict:
    """Split analyst prose into the four canonical sections, robust to
    fences, preamble before the first header, and header styling (markdown
    #, bold, ALL CAPS, trailing colon). Text before the first recognized
    header folds into the executive summary so nothing is dropped."""
    text = _strip_fences(text)
    lines = text.splitlines()

    # header alias -> section key
    alias_to_key: dict[str, str] = {}
    for key, aliases in _SECTION_ORDER:
        for a in aliases:
            alias_to_key[a] = key

    sections: dict[str, list[str]] = {key: [] for key, _ in _SECTION_ORDER}
    preamble: list[str] = []
    current: str | None = None

    for line in lines:
        norm = _normalize_header(line)
        # A header line is short and matches an alias exactly (or is that
        # alias followed by nothing meaningful).
        matched_key = alias_to_key.get(norm)
        if matched_key is None and norm:
            # tolerate "1. executive summary" style numbering
            norm2 = re.sub(r"^\d+[\.\)]\s*", "", norm)
            matched_key = alias_to_key.get(norm2)
        is_headerish = line.strip().startswith("#") or (
            len(line.strip()) <= 60 and matched_key is not None
        )
        if matched_key is not None and is_headerish:
            current = matched_key
            continue
        if current is None:
            preamble.append(line)
        else:
            sections[current].append(line)

    if preamble and not sections["executive_summary"]:
        sections["executive_summary"] = preamble

    return {key: "\n".join(v).strip() for key, v in sections.items()}


def write_diagnosis(
    client,
    analyst_model: str,
    evidence_pack: dict,
    corroborations=None,
    max_tokens: int = 3000,
) -> Diagnosis:
    """Prompt the analyst model with the rendered evidence pack and parse
    its answer into a `Diagnosis`. `client` is any object with a
    `chat(model, prompt, max_tokens=...) -> result` where `result.text` is
    the reply and `result.error` is None on success (the project's
    `cartographer.exec.client.Client` satisfies this; tests pass a fake).

    `corroborations`, if given, is appended to the prompt as read-only
    context (the analyst may reference it in prose) but is NOT part of the
    number-bearing evidence and is never audited against.
    """
    rendered = render_pack(evidence_pack)
    prompt_parts = [
        _SYSTEM_INSTRUCTIONS,
        "",
        "=== EVIDENCE PACK (the ONLY source of numbers you may cite) ===",
        rendered,
    ]
    if corroborations:
        prompt_parts += [
            "",
            "=== LITERATURE CONTEXT (narration only, contains no store numbers) ===",
            _render_corroborations_for_prompt(corroborations),
        ]
    prompt_parts += [
        "",
        "Write the diagnosis now, using ONLY numbers found in the evidence pack.",
    ]
    prompt = "\n".join(prompt_parts)

    result = client.chat(analyst_model, prompt, max_tokens=max_tokens)
    error = getattr(result, "error", None)
    text = getattr(result, "text", "") or ""
    if error is not None:
        return Diagnosis(model=analyst_model, raw_text=text, error=str(error))
    if not text.strip():
        return Diagnosis(
            model=analyst_model, raw_text=text, error="analyst returned empty text"
        )

    parsed = parse_sections(text)
    return Diagnosis(
        model=analyst_model,
        raw_text=text,
        executive_summary=parsed["executive_summary"],
        per_model=parsed["per_model"],
        cross_model=parsed["cross_model"],
        next_probes=parsed["next_probes"],
    )


def _render_corroborations_for_prompt(corroborations) -> str:
    lines = []
    for c in corroborations:
        finding_name = getattr(c, "finding", None)
        if hasattr(finding_name, "name"):
            finding_name = finding_name.name
        lines.append(f"- finding: {finding_name} (relation: {getattr(c, 'relation', '?')})")
        for res in getattr(c, "results", [])[:3]:
            title = res.get("title", "") if isinstance(res, dict) else ""
            lines.append(f"    * {title}")
    return "\n".join(lines) or "(none)"


# ---------------------------------------------------------------------------
# The contamination audit.
# ---------------------------------------------------------------------------

# A number token: an optional $, digits with optional thousands separators
# and decimal, and an optional trailing %.
_NUMBER_RE = re.compile(
    r"(?P<money>\$)?(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?P<pct>\s*%)?"
)

# Small integers are ubiquitous prose ("the four dims", "3 sentences",
# "two models") and are not the kind of figure the audit is meant to catch
# (a fabricated *count of trials* is what matters). We only audit bare
# integers at or above this value; percents and dollar amounts are always
# audited regardless of magnitude, and any integer that literally appears
# in the pack is fine at any size.
_MIN_AUDITED_BARE_INT = 10


def _collect_pack_numbers(pack: dict) -> tuple[set[float], set[float], set[float]]:
    """Every number present in the pack, split into (percents, money,
    counts) buckets. Percents are stored rounded to one decimal to match
    `evidence._round_rate`; a claim is checked against the bucket that
    matches how it was written, with a small rounding tolerance."""
    percents: set[float] = set()
    money: set[float] = set()
    counts: set[float] = set()

    def walk(node, key_hint: str = ""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list):
            for v in node:
                walk(v, key_hint)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            val = float(node)
            kl = key_hint.lower()
            if "pct" in kl or "rate" in kl or "percent" in kl:
                percents.add(round(val, 1))
            elif "usd" in kl or "spend" in kl or "cost" in kl or "money" in kl:
                money.add(round(val, 4))
                money.add(round(val, 2))
            else:
                counts.add(val)
                # A count is also legitimately quotable as a percent-looking
                # figure only if it coincides; keep it out of the percent set.
        elif isinstance(node, str):
            # numbers embedded in headline strings (e.g. "61% pass
            # (224/369 scored)") are legitimate pack numbers to cite.
            for m in _NUMBER_RE.finditer(node):
                raw = m.group("num").replace(",", "")
                try:
                    val = float(raw)
                except ValueError:
                    continue
                if m.group("pct"):
                    percents.add(round(val, 1))
                elif m.group("money"):
                    money.add(round(val, 4))
                    money.add(round(val, 2))
                else:
                    counts.add(val)

    walk(pack)
    return percents, money, counts


def _matches(value: float, allowed: set[float], tol: float) -> bool:
    return any(abs(value - a) <= tol for a in allowed)


def audit_diagnosis(diagnosis: "Diagnosis", pack: dict) -> list:
    """Return every numeric figure in the diagnosis prose that the evidence
    pack does not support -- the deterministic contamination check.

    Matching precision mirrors the pack's own rounding: a written "61%" is
    supported by a pack value of 60.7 (tolerance 0.5 on percents), a written
    "$0.42" by a pack spend of 0.4231 (tolerance 0.005 on money, since the
    pack rounds spend to 4 decimals and the analyst may quote 2), and a
    written count must appear exactly (integer counts don't get rounded).
    """
    percents, money, counts = _collect_pack_numbers(pack)
    text = diagnosis.raw_text or ""

    flags: list[UnsupportedClaim] = []
    for m in _NUMBER_RE.finditer(text):
        raw = m.group("num").replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        is_pct = bool(m.group("pct"))
        is_money = bool(m.group("money"))

        if is_pct:
            if _matches(value, percents, 0.5):
                continue
            kind = "percent"
            allowed_ok = False
        elif is_money:
            if _matches(value, money, 0.005):
                continue
            kind = "money"
            allowed_ok = False
        else:
            # bare integer/decimal: supported if it appears as any count,
            # or coincides with a percent/money value the analyst wrote
            # without the sign (common: "passes 61 of ..." vs percents).
            if (
                _matches(value, counts, 1e-9)
                or _matches(value, percents, 0.5)
                or _matches(value, money, 0.005)
            ):
                continue
            # small prose integers are not audited
            if value < _MIN_AUDITED_BARE_INT and float(value).is_integer():
                continue
            kind = "count"
            allowed_ok = False

        if allowed_ok:
            continue

        start = max(0, m.start() - 32)
        end = min(len(text), m.end() + 32)
        context = " ".join(text[start:end].split())
        figure = m.group(0).strip()
        flags.append(
            UnsupportedClaim(
                figure=figure, normalized=value, kind=kind, context=context
            )
        )
    return flags
