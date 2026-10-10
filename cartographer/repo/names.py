"""Names for the repository world (docs/province-vi-spec.md §4.2).

Module names, function names and their look-alike / clone variants. Every
choice is drawn from `_Rng` (sha256), so names are a deterministic function
of the seed. Module names never collide with the standard library or common
third-party packages (an exported repository must import its own modules,
not those), and function names (compound `verb_noun`) never collide with the
single-word local-variable pools `codeworld.render` uses, so a local can
never shadow a function it calls.
"""

from __future__ import annotations

import hashlib

from ..codeworld.generate import _Rng

MODULE_WORDS = (
    "pricing", "inventory", "ledger", "billing", "reporting", "shipping", "orders", "accounts", "metrics",
    "scheduling", "routing", "rules", "discounts", "taxes", "payroll", "grading", "scoring", "ranking",
    "budgeting", "forecast", "ingest", "audit", "quotas", "rewards", "utils", "helpers",
)
VERBS = (
    "compute", "apply", "merge", "count", "adjust", "collect", "summarize", "normalize", "rank", "score",
    "measure", "combine", "build", "estimate", "update", "resolve", "allocate", "reconcile", "aggregate",
    "derive",
)
NOUNS = (
    "total", "discount", "balance", "items", "weights", "offset", "margin", "quota", "budget", "levels",
    "codes", "tags", "rows", "batch", "rate", "limit", "window", "bonus", "credit", "reserve", "penalty",
    "fees", "units", "stock",
)
CLONE_SUFFIXES = ("legacy", "v1", "old", "draft")
ENTRY_FUNCTION = "solve"


def module_names(n: int, seed: int) -> list[str]:
    """`n` distinct module names in draw order (index 0 = entry module)."""
    rng = _Rng("repo-modules", seed)
    pool = list(MODULE_WORDS)
    out = []
    for _ in range(n):
        if not pool:
            raise ValueError(f"at most {len(MODULE_WORDS)} modules")
        w = rng.choice(pool)
        pool.remove(w)
        out.append(w)
    return out


def function_names(n: int, seed: int, taken=()) -> list[str]:
    """`n` distinct `verb_noun` names, none in `taken`."""
    rng = _Rng("repo-functions", seed)
    used = set(taken)
    out = []
    while len(out) < n:
        w = f"{rng.choice(VERBS)}_{rng.choice(NOUNS)}"
        if w not in used:
            used.add(w)
            out.append(w)
    return out


def _past(verb: str) -> str:
    return verb + ("d" if verb.endswith("e") else "ed")


def lookalike_names(name: str) -> list[str]:
    """Spellings of a `verb_noun` name at edit distance 1-2: noun plural
    toggled, verb with `s`, verb in the past tense, camelCase. Never equal to
    `name`."""
    verb, _, noun = name.partition("_")
    out = [f"{verb}_{noun[:-1] if noun.endswith('s') else noun + 's'}", f"{verb}s_{noun}"]
    if len(_past(verb)) - len(verb) <= 2:
        out.append(f"{_past(verb)}_{noun}")
    out.append(verb + noun[:1].upper() + noun[1:])
    return [w for w in out if w != name and edit_distance(w, name) <= 2]


def clone_name(name: str, seed: int) -> str:
    return f"{name}_{_Rng('repo-clone-name', seed).choice(CLONE_SUFFIXES)}"


def opaque(kind: str, name: str, seed: int) -> str:
    """A stable opaque identifier (`m_3f9a` / `fn_c21e`) for the `opaque_names` ablation."""
    h = hashlib.sha256(f"{kind}|{name}|{seed}".encode()).hexdigest()[:4]
    return f"{'m' if kind == 'module' else 'fn'}_{h}"


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
