"""Retro-verify FAILs from historical runs: dry-run by default, live for one
run_id at a time.

Every campaign preset now auto-verifies its own FAILs when launched with
`verify="auto"` (the default) -- see `cartographer.exec.evaluator.
run_verify_failures` and docs/lab-log.md. This script exists for the runs
that predate that feature, or that were deliberately launched with
`verify="manual"` (e.g. from /launch's "Verify failures" checkbox
unchecked): it finds every run in the store that still has unverified
FAILs and, in `--dry-run` mode (the default), just lists what WOULD be
verified and an estimated call/cost count, with zero network calls.

`--live --run <run_id>` actually verifies exactly one run, live, against
the real model -- it calls `cartographer.exec.evaluator.run_verify_failures`
through the EXACT SAME code path as the viewer's "Verify failures now"
button (`cartographer.viewer.launch.handle_verify_now` /
`_verify_now_thread`), so behavior (one-campaign-at-a-time guard, cost cap,
truncation, cancellation) is identical whether triggered from the CLI or
the browser -- this script is a thin CLI wrapper around that same function,
not a second implementation.

Zero live API calls happen unless `--live` is passed explicitly. Main-
guarded so importing this module has no side effects.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cartographer.exec.client import GROQ, NEBIUS, Client  # noqa: E402
from cartographer.exec.evaluator import (  # noqa: E402
    VERIFY_SUFFIX,
    default_token_budget,
    run_verify_failures,
)
from cartographer.exec.keys import load_keys  # noqa: E402
from cartographer.exec.pricing import cost_usd  # noqa: E402
from cartographer.store.schema import Verdict  # noqa: E402
from cartographer.store.store import ResultsStore  # noqa: E402

_DEFAULT_DB_PATH = "results/campaign.sqlite"

# Providers keyed by name -- same set /launch offers.
_PROVIDERS = {"groq": GROQ, "nebius": NEBIUS}

# Verification resamples each FAIL this many times by default (matches
# run_verify_failures's own default `n`).
_DEFAULT_N = 3

# Rough per-call token estimate used ONLY for the dry-run cost estimate
# (actual cost is whatever the real calls cost -- this is a ballpark for
# planning, not a promise).
_EST_TOKENS_IN = 400
_EST_TOKENS_OUT = 150


def _all_run_ids(store: ResultsStore) -> list[str]:
    cur = store._conn.execute("SELECT DISTINCT run_id FROM experiments")
    return sorted(
        row["run_id"]
        for row in cur.fetchall()
        if not row["run_id"].endswith(VERIFY_SUFFIX)
    )


def _unverified_fails_for_run(store: ResultsStore, run_id: str) -> int:
    """Count FAILs in `run_id` that have NO verify trials at all yet
    (a cheap existence check, not the full stable/noise classification --
    dry-run only needs "how many would get verified")."""
    fails = [r for r in store.by_run(run_id) if r.verdict == Verdict.FAIL]
    if not fails:
        return 0
    verify_trials = store.by_run(f"{run_id}{VERIFY_SUFFIX}")
    verified_ids = set()
    import re as _re

    for r in verify_trials:
        m = _re.search(r"verify_of=([0-9a-fA-F]+)", r.verifier_detail or "")
        if m:
            verified_ids.add(m.group(1))
    return sum(1 for f in fails if f.record_id not in verified_ids)


def _model_of_run(store: ResultsStore, run_id: str) -> str | None:
    records = store.by_run(run_id)
    return records[0].model_name if records else None


def dry_run(db_path: str, max_verified: int) -> None:
    store = ResultsStore(db_path)
    try:
        run_ids = _all_run_ids(store)
        total_calls_est = 0
        total_cost_est = 0.0
        print(f"{'run_id':<45} {'model':<45} {'unverified FAILs':>16} {'est. calls':>10} {'est. $':>8}")
        print("-" * 130)
        any_found = False
        for run_id in run_ids:
            n_fails = _unverified_fails_for_run(store, run_id)
            if n_fails == 0:
                continue
            any_found = True
            model = _model_of_run(store, run_id) or "?"
            n_to_verify = min(n_fails, max_verified)
            est_calls = n_to_verify * _DEFAULT_N
            est_cost = 0.0
            try:
                est_cost = cost_usd(model, _EST_TOKENS_IN, _EST_TOKENS_OUT) * est_calls
            except Exception:
                pass
            total_calls_est += est_calls
            total_cost_est += est_cost
            truncated_note = f" (capped from {n_fails})" if n_fails > max_verified else ""
            print(
                f"{run_id:<45} {model:<45} {n_fails:>16} {est_calls:>10} {est_cost:>8.4f}{truncated_note}"
            )
        if not any_found:
            print("No runs with unverified FAILs found.")
        else:
            print("-" * 130)
            print(f"Total (dry-run estimate): ~{total_calls_est} calls, ~${total_cost_est:.4f}")
        print(
            "\nThis is a DRY RUN -- zero network calls were made. "
            "Use --live --run <run_id> to actually verify one run."
        )
    finally:
        store.close()


def live_verify(db_path: str, run_id: str, provider_name: str) -> None:
    """Verify one run for real, via the SAME code path the viewer's
    "Verify failures now" button uses (`cartographer.viewer.launch.
    handle_verify_now` / `_verify_now_thread`) -- this function just
    resolves the provider/key/model and calls `run_verify_failures`
    directly rather than going through the HTTP form + thread machinery
    (no browser involved from the CLI), but the underlying call is
    identical.
    """
    provider = _PROVIDERS.get(provider_name)
    if provider is None:
        print(f"unknown provider: {provider_name!r} (choices: {sorted(_PROVIDERS)})", file=sys.stderr)
        sys.exit(2)

    try:
        keys = load_keys()
    except OSError as exc:
        print(f"could not load API keys: {exc!r}", file=sys.stderr)
        sys.exit(2)
    key = keys.get(provider_name)
    if not key:
        print(f"no API key found for provider {provider_name!r}", file=sys.stderr)
        sys.exit(2)

    store = ResultsStore(db_path)
    try:
        model = _model_of_run(store, run_id)
        if model is None:
            print(f"no records found for run_id {run_id!r}", file=sys.stderr)
            sys.exit(2)

        client = Client(provider, key)
        print(f"Verifying FAILs for run {run_id!r} (model={model}) against {provider_name}...")
        result = run_verify_failures(
            client=client,
            model=model,
            store=store,
            run_id=run_id,
        )
        print(result.to_json_dict())
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=_DEFAULT_DB_PATH, help="path to the results sqlite store")
    parser.add_argument(
        "--live", action="store_true", help="actually verify (default is dry-run, zero network calls)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit no-op flag for the default mode (dry-run happens whenever --live is absent)",
    )
    parser.add_argument("--run", default=None, help="run_id to verify (required with --live)")
    parser.add_argument(
        "--provider", default="nebius", choices=sorted(_PROVIDERS), help="provider for --live"
    )
    parser.add_argument(
        "--max-verified",
        type=int,
        default=30,
        help="cap on FAILs verified per run (matches run_verify_failures's own default)",
    )
    args = parser.parse_args()

    if args.live:
        if not args.run:
            parser.error("--live requires --run <run_id>")
        live_verify(args.db, args.run, args.provider)
    else:
        dry_run(args.db, args.max_verified)


if __name__ == "__main__":
    main()
