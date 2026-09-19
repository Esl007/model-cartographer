"""Deterministic plain-text evidence digest built from a `ResultsStore`, for
one subject model, that the proposer LLM reads before proposing new configs.

Pure and deterministic given the store's contents at call time: same rows in
-> same digest out (records are sorted by `created_at`/`record_id`, matching
`ResultsStore.by_model`'s own ordering, so re-running the digest on an
unchanged store always reproduces byte-identical text). No API calls.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..store.schema import ExperimentRecord, Verdict

# Latent-param keys worth summarizing per-config in "recent failures" /
# "configs that passed" sections -- mirrors PipelineEvaluator's
# `latent_params` dict (see cartographer/exec/evaluator.py) so the digest's
# config shorthand round-trips cleanly against what a proposer would submit.
_CONFIG_KEYS = (
    "chain_length",
    "scramble",
    "coref_pressure",
    "distractor_events",
    "filler_events",
    "witness_cutoff",
)


def _fmt_config(latent_params: dict[str, float]) -> str:
    parts = []
    for key in _CONFIG_KEYS:
        if key in latent_params:
            value = latent_params[key]
            if key in ("scramble", "witness_cutoff"):
                parts.append(f"{key}={value:.2f}")
            else:
                parts.append(f"{key}={int(round(value))}")
    return "{" + ", ".join(parts) + "}"


def _query_kind(record: ExperimentRecord) -> str:
    detail = record.verifier_detail or ""
    for part in detail.split(";"):
        if part.startswith("kind="):
            return part[len("kind=") :]
    return "unknown"


def _traps(record: ExperimentRecord) -> tuple[str, ...]:
    detail = record.verifier_detail or ""
    for part in detail.split(";"):
        if part.startswith("traps="):
            raw = part[len("traps=") :]
            return tuple(t for t in raw.split(",") if t)
    return ()


def build_digest(store, subject_model: str, max_chars: int = 4000) -> str:
    """Summarize everything known about `subject_model` from `store`, most
    recent and most informative first, capped at `max_chars`.

    Sections, in order (each skipped if empty):
      1. Per-query-kind pass rates.
      2. Per-dim pass rates in tested regions (coarse buckets over the
         realized value of each `_CONFIG_KEYS` dim, across all query kinds).
      3. Confirmed trap signatures (`traps=` tokens parsed out of
         `verifier_detail` on FAIL records, i.e. adversarial-search hits
         that the subject model actually fell for).
      4. Configs of recent failures (latest FAIL records' `latent_params`).
      5. Configs that were tried and passed (latest PASS records'
         `latent_params`) -- so the proposer avoids re-spending budget on
         known-safe regions.
    """
    records = store.by_model(subject_model)
    if not records:
        return f"No prior evidence for subject model {subject_model!r}."

    lines: list[str] = [f"Evidence digest for subject model {subject_model!r}:"]

    # 1. Per-query-kind pass rates.
    by_kind: dict[str, list[ExperimentRecord]] = defaultdict(list)
    for r in records:
        by_kind[_query_kind(r)].append(r)
    lines.append("")
    lines.append("Pass rates by query kind:")
    for kind in sorted(by_kind):
        kind_records = by_kind[kind]
        scored = [r for r in kind_records if r.verdict in (Verdict.PASS, Verdict.FAIL)]
        n_pass = sum(1 for r in scored if r.verdict == Verdict.PASS)
        n = len(scored)
        rate = (n_pass / n) if n else 0.0
        lines.append(f"  {kind}: {n_pass}/{n} pass ({rate:.0%}), n={len(kind_records)} total incl. errors")

    # 2. Per-dim pass rates in tested regions (coarse buckets: low/mid/high
    # tercile of each dim's observed realized value range).
    lines.append("")
    lines.append("Pass rates by dimension region (tercile of observed range):")
    dim_values: dict[str, list[float]] = defaultdict(list)
    for r in records:
        for key in _CONFIG_KEYS:
            if key in r.realized_dims:
                dim_values[key].append(r.realized_dims[key])
    for dim in sorted(dim_values):
        values = dim_values[dim]
        lo, hi = min(values), max(values)
        if hi <= lo:
            continue
        third = (hi - lo) / 3.0
        buckets = {"low": [0, 0], "mid": [0, 0], "high": [0, 0]}  # [n_pass, n_total]
        for r in records:
            v = r.realized_dims.get(dim)
            if v is None or r.verdict not in (Verdict.PASS, Verdict.FAIL):
                continue
            if v <= lo + third:
                bucket = "low"
            elif v <= lo + 2 * third:
                bucket = "mid"
            else:
                bucket = "high"
            buckets[bucket][1] += 1
            if r.verdict == Verdict.PASS:
                buckets[bucket][0] += 1
        summary = ", ".join(
            f"{b}={n_pass}/{n_tot}" for b, (n_pass, n_tot) in buckets.items() if n_tot > 0
        )
        if summary:
            lines.append(f"  {dim} [{lo:.2f}..{hi:.2f}]: {summary}")

    # 3. Confirmed trap signatures: traps= tokens on FAIL records.
    trap_counts: Counter[str] = Counter()
    for r in records:
        if r.verdict == Verdict.FAIL:
            for t in _traps(r):
                trap_counts[t] += 1
    if trap_counts:
        lines.append("")
        lines.append("Confirmed trap signatures (from FAILs, most common first):")
        for trap, count in trap_counts.most_common():
            lines.append(f"  {trap}: {count} confirmed fail(s)")

    # 4. Configs of recent failures (most recent first).
    fails = [r for r in records if r.verdict == Verdict.FAIL]
    fails.sort(key=lambda r: r.created_at, reverse=True)
    if fails:
        lines.append("")
        lines.append("Recent FAIL configs (most recent first):")
        for r in fails[:12]:
            kind = _query_kind(r)
            traps = _traps(r)
            trap_note = f" traps={','.join(traps)}" if traps else ""
            lines.append(f"  kind={kind} {_fmt_config(r.latent_params)}{trap_note}")

    # 5. Configs tried and passed (most recent first) -- avoid re-spending.
    passes = [r for r in records if r.verdict == Verdict.PASS]
    passes.sort(key=lambda r: r.created_at, reverse=True)
    if passes:
        lines.append("")
        lines.append("Recent PASS configs (tried, did NOT fail -- avoid repeating verbatim):")
        for r in passes[:12]:
            kind = _query_kind(r)
            lines.append(f"  kind={kind} {_fmt_config(r.latent_params)}")

    digest = "\n".join(lines)
    if len(digest) > max_chars:
        digest = digest[: max_chars - 20].rstrip() + "\n...[truncated]"
    return digest
