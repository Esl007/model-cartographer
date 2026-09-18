"""Automate the manual Ultra pronoun-ablation loop against real campaign
history: load `results/campaign.sqlite` READ-ONLY, find post-fix belief
FAILs, regenerate each failing world from its stored seed/params, and run
`cartographer.ablate.attribute.attribute_failure` against it.

Default mode is `--dry-run` (the actual default -- no flag needed): no live
model calls are made. It prints, per failing record, the resample-gate plan
and which ablations from `cartographer.ablate.ablations.ABLATIONS` are
available for that specific world, i.e. exactly what WOULD be probed if run
live. This lets you sanity-check the plan (how many worlds, how many calls
that implies, which ablations apply) before spending any budget.

`--live` wires in a real `cartographer.exec.client.Client` and actually
calls `attribute_failure` against it -- but this script does NOT invoke
that path unless `--live` is passed explicitly on the command line; running
this script with no arguments is fully offline. `--model` overrides which
model to probe (default: attribute against the same model_name that
produced the failing record). `--n` controls resample/ablation trial count
(default 5, per `attribute_failure`'s own default). `--budget` caps how
many failing records are processed (records are LIFO-ish: most recent
FAILs first) so a live run can't accidentally re-probe an entire history's
worth of failures in one go.

Zero network calls anywhere unless `--live` is passed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `cartographer` importable when this script is run directly
# (`python3 scripts/attribute_failures.py`) from the repo root, mirroring
# the other scripts/ entries in this repo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cartographer.ablate.ablations import available_ablations, baseline
from cartographer.ablate.attribute import attribute_failure
from cartographer.render.naming import build_name_map
from cartographer.render.templates import render
from cartographer.store.schema import ExperimentRecord, Verdict
from cartographer.store.store import ResultsStore
from cartographer.world.generate import DimParams, generate

_DEFAULT_STORE_PATH = "results/campaign.sqlite"

# Config keys that map onto DimParams fields, mirroring
# cartographer/exec/evaluator.py's _INT_KEYS / _FLOAT_KEYS -- this script
# reads the same `latent_params` shape PipelineEvaluator writes.
_INT_KEYS = {"chain_length", "coref_pressure", "distractor_events", "filler_events"}
_FLOAT_KEYS = {"scramble", "witness_cutoff"}


def _params_from_record(record: ExperimentRecord) -> DimParams:
    """Rebuild the exact `DimParams` a stored record was generated from.

    Unlike `cartographer.ablate.ablations`'s removal ablations (which
    operate on an already-built `World` and therefore never need to
    reconstruct `DimParams`), this script has the actual `latent_params`
    dict `PipelineEvaluator` persisted -- so reconstruction here is exact,
    not a lossy post-hoc guess.
    """
    lp = record.latent_params
    overrides: dict[str, object] = {}
    for key, value in lp.items():
        if key in _INT_KEYS:
            overrides[key] = int(round(value))
        elif key in _FLOAT_KEYS:
            overrides[key] = float(value)
    query_kind = "belief"
    for token in (record.verifier_detail or "").split(";"):
        if token.startswith("kind="):
            query_kind = token.split("=", 1)[1]
    return DimParams(
        chain_length=int(overrides.get("chain_length", 2)),
        scramble=float(overrides.get("scramble", 0.0)),
        coref_pressure=int(overrides.get("coref_pressure", 0)),
        distractor_events=int(overrides.get("distractor_events", 2)),
        distractor_similarity="same_category",
        filler_events=int(overrides.get("filler_events", 2)),
        query_kind=query_kind,
        witness_cutoff=float(overrides.get("witness_cutoff", 0.0)),
    )


def _belief_fail_records(store: ResultsStore, model: str | None) -> list[ExperimentRecord]:
    """Every stored FAIL record for `query_kind=belief`, newest first.

    Uses `ResultsStore._all()` (read-only; no writes anywhere in this
    script) since there is no dedicated verdict-filtered query on the
    public API -- the same pattern `ResultsStore.export_jsonl` itself uses
    internally for a full dump.
    """
    records = store._all()
    out = []
    for r in records:
        if r.verdict != Verdict.FAIL:
            continue
        kind = None
        for token in (r.verifier_detail or "").split(";"):
            if token.startswith("kind="):
                kind = token.split("=", 1)[1]
        if kind != "belief":
            continue
        if model is not None and r.model_name != model:
            continue
        out.append(r)
    out.sort(key=lambda r: r.created_at, reverse=True)
    return out


def _plan_for_record(record: ExperimentRecord, n: int) -> dict:
    params = _params_from_record(record)
    seed = record.task_seed
    world = generate(params, seed)
    name_map = build_name_map(world, seed)
    baseline_task = baseline(world, name_map, seed)
    candidates = available_ablations(world, name_map, seed)

    answer_matches = baseline_task.answer_key == render(world, name_map, seed).answer_key
    n_calls = n  # resample gate
    n_calls += len(candidates) * n  # one arm per available ablation

    return {
        "record_id": record.record_id,
        "model_name": record.model_name,
        "task_seed": seed,
        "params": params,
        "baseline_answer_key": baseline_task.answer_key,
        "answer_key_reproduced": answer_matches,
        "available_ablations": sorted(candidates.keys()),
        "unavailable_ablations": sorted(
            set(k for k in _ALL_ABLATION_NAMES if k not in candidates)
        ),
        "estimated_calls_if_live": n_calls,
    }


from cartographer.ablate.ablations import ABLATIONS as _ALL_ABLATIONS  # noqa: E402

_ALL_ABLATION_NAMES = [n for n in _ALL_ABLATIONS if n != "baseline"]


def _print_dry_run_plan(plans: list[dict]) -> None:
    total_calls = sum(p["estimated_calls_if_live"] for p in plans)
    print(f"Dry run: {len(plans)} belief FAIL record(s) selected, "
          f"{total_calls} model calls WOULD be made in --live mode.\n")
    for p in plans:
        print(f"record {p['record_id'][:12]}  model={p['model_name']}  seed={p['task_seed']}")
        print(f"    params: {p['params']}")
        print(f"    baseline answer_key reproduced from stored seed: {p['answer_key_reproduced']}")
        print(f"    available ablations: {', '.join(p['available_ablations']) or '(none)'}")
        if p["unavailable_ablations"]:
            print(f"    unavailable for this world: {', '.join(p['unavailable_ablations'])}")
        print(f"    estimated calls if --live: {p['estimated_calls_if_live']}")
        print()


def _run_live(plans: list[dict], records: list[ExperimentRecord], model_override: str | None, n: int) -> None:
    """Wire a real `cartographer.exec.client.Client` and actually call
    `attribute_failure` for each planned record. Implemented so `--live`
    is functional, but this function is never invoked unless the caller
    passes `--live` explicitly -- see `main()`.
    """
    from cartographer.exec.client import NEBIUS, Client
    from cartographer.exec.keys import load_keys
    from cartographer.render.templates import RenderedTask
    from cartographer.render.verify import verify

    keys = load_keys()
    provider = NEBIUS
    client = Client(provider=provider, api_key=keys[provider.name])

    for plan, record in zip(plans, records):
        model = model_override or record.model_name
        params = plan["params"]
        seed = record.task_seed
        world = generate(params, seed)
        name_map = build_name_map(world, seed)
        query_kind = world.query.kind

        def evaluator_factory(prompt: str, answer_key: str, _model=model) -> bool:
            """The only place in this script a live model call happens.
            `verify()` grades against a `RenderedTask`-shaped object; since
            an ablation's prompt/answer_key already carry everything
            `verify()` needs except `query_kind` (which is invariant across
            every ablation of one world -- ablations never change what
            kind of question is asked, only its phrasing/support), a
            minimal shim carrying just that is enough, without
            re-rendering.
            """
            result = client.chat(model=_model, prompt=prompt, max_tokens=1500, max_tokens_retry=4000)
            if result.error is not None or not result.text:
                return False
            shim = RenderedTask(prompt=prompt, answer_key=answer_key, meta={"query_kind": query_kind})
            return verify(result.text, shim, name_map) == Verdict.PASS

        report = attribute_failure(evaluator_factory, world, name_map, seed, n=n)
        print(f"record {record.record_id[:12]} model={model}: {report.to_json_dict()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=_DEFAULT_STORE_PATH, help="Path to campaign.sqlite (read-only).")
    parser.add_argument("--model", default=None, help="Restrict to one model_name (default: every model).")
    parser.add_argument("--n", type=int, default=5, help="Trials per resample/ablation arm (default 5).")
    parser.add_argument("--budget", type=int, default=10, help="Max FAIL records to process (newest first).")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually call a live model client. Without this flag (the default), nothing but "
        "local sqlite reads and pure-Python world regeneration happens.",
    )
    args = parser.parse_args()

    store_path = Path(args.store)
    if not store_path.exists():
        print(f"no store found at {store_path} -- nothing to do.")
        return

    store = ResultsStore(store_path)
    try:
        records = _belief_fail_records(store, args.model)[: args.budget]
        if not records:
            print("no belief FAIL records found matching the given filters.")
            return
        plans = [_plan_for_record(r, args.n) for r in records]

        if args.live:
            _run_live(plans, records, args.model, args.n)
        else:
            _print_dry_run_plan(plans)
    finally:
        store.close()


if __name__ == "__main__":
    main()
