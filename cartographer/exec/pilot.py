"""Pilot run: a small spread grid of tasks x pilot models, against a live provider.

`build_pilot_tasks` derives `DimParams` for a 3 (query_kind) x 3 (difficulty)
grid, `n_per_cell` seeds each. `run_pilot` renders each task, calls each
model, verifies the response, and appends one `ExperimentRecord` per
(task, model) pair to the store.
"""

from __future__ import annotations

from typing import Callable

from ..render import build_name_map, render, verify
from ..store.schema import ExperimentRecord, RenderMode, Verdict
from ..store.store import ResultsStore
from ..world.generate import DimParams, generate
from ..world.measure import measure
from .client import Client
from .pricing import cost_usd

PILOT_MODELS = ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"]

_QUERY_KINDS = ["state", "belief", "order"]

_DIFFICULTY_CELLS = {
    "easy": dict(chain_length=2, scramble=0.0, coref_pressure=0, distractor_events=2, filler_events=2),
    "medium": dict(chain_length=4, scramble=0.5, coref_pressure=2, distractor_events=6, filler_events=4),
    "hard": dict(chain_length=7, scramble=0.9, coref_pressure=4, distractor_events=12, filler_events=8),
}

# Per-query-kind overrides of the "hard" cell. state saturates models at
# chain_length=7, so it gets a much harder cell; belief/order get a bumped
# chain length to match.
_HARD_CELL_OVERRIDES: dict[str, dict] = {
    "state": dict(
        chain_length=15, scramble=0.9, coref_pressure=4,
        distractor_events=20, filler_events=8,
        distractor_similarity="near_duplicate",
    ),
    "belief": dict(chain_length=8),
    "order": dict(chain_length=8),
}


def _cell_for(query_kind: str, difficulty: str) -> dict:
    cell = dict(_DIFFICULTY_CELLS[difficulty])
    cell.setdefault("distractor_similarity", "same_category")
    if difficulty == "hard":
        cell.update(_HARD_CELL_OVERRIDES.get(query_kind, {}))
    return cell


def build_pilot_tasks(n_per_cell: int = 2) -> list[tuple[DimParams, int]]:
    """Build the spread grid: 3 query_kinds x 3 difficulty cells x n_per_cell seeds."""
    tasks: list[tuple[DimParams, int]] = []
    seed_counter = 0
    for query_kind in _QUERY_KINDS:
        for difficulty in _DIFFICULTY_CELLS:
            cell = _cell_for(query_kind, difficulty)
            for _ in range(n_per_cell):
                params = DimParams(
                    chain_length=cell["chain_length"],
                    scramble=cell["scramble"],
                    coref_pressure=cell["coref_pressure"],
                    distractor_events=cell["distractor_events"],
                    distractor_similarity=cell["distractor_similarity"],
                    filler_events=cell["filler_events"],
                    query_kind=query_kind,
                )
                seed = 1000 * (seed_counter + 1) + seed_counter
                tasks.append((params, seed))
                seed_counter += 1
    return tasks


def _difficulty_of(params: DimParams) -> str:
    for name in _DIFFICULTY_CELLS:
        cell = _cell_for(params.query_kind, name)
        if (
            params.chain_length == cell["chain_length"]
            and params.scramble == cell["scramble"]
            and params.coref_pressure == cell["coref_pressure"]
            and params.distractor_events == cell["distractor_events"]
            and params.filler_events == cell["filler_events"]
        ):
            return name
    return "unknown"


def run_pilot(
    client: Client,
    store: ResultsStore,
    run_id: str,
    models: list[str] = PILOT_MODELS,
    n_per_cell: int = 2,
    progress: Callable[[str], None] = print,
) -> dict:
    tasks = build_pilot_tasks(n_per_cell)

    per_model_counts: dict[str, dict[str, int]] = {
        m: {"PASS": 0, "FAIL": 0, "ERROR": 0, "REJECTED_BY_FIDELITY_GATE": 0} for m in models
    }
    per_cell_counts: dict[str, dict[str, int]] = {}

    n_total = len(tasks) * len(models)
    n_done = 0

    for params, seed in tasks:
        world = generate(params, seed)
        name_map = build_name_map(world, seed)
        rendered = render(world, name_map, seed)
        realized_dims = measure(world)
        difficulty = _difficulty_of(params)
        cell_key = f"{params.query_kind}:{difficulty}"
        per_cell_counts.setdefault(cell_key, {"PASS": 0, "FAIL": 0, "ERROR": 0, "REJECTED_BY_FIDELITY_GATE": 0})

        for model in models:
            result = client.chat(model=model, prompt=rendered.prompt, max_tokens=1500)

            if result.error is not None:
                verdict = Verdict.ERROR
            else:
                verdict = verify(result.text, rendered, name_map)

            verifier_detail = f"kind={params.query_kind};sim={params.distractor_similarity};difficulty={difficulty}"
            if result.error:
                verifier_detail += f";error={result.error}"
            if result.finish_reason == "length":
                verdict = Verdict.ERROR
                verifier_detail += ";cap_hit=1"

            cost = cost_usd(model, result.tokens_in, result.tokens_out)

            record = ExperimentRecord(
                run_id=run_id,
                model_name=model,
                latent_params={
                    "chain_length": float(params.chain_length),
                    "scramble": float(params.scramble),
                    "coref_pressure": float(params.coref_pressure),
                    "distractor_events": float(params.distractor_events),
                    "filler_events": float(params.filler_events),
                },
                realized_dims=realized_dims,
                render_mode=RenderMode.TEMPLATE,
                task_seed=seed,
                trial_seed=seed,
                rendering_seed=seed,
                prompt_text=rendered.prompt,
                response_text=result.text,
                verdict=verdict,
                verifier_detail=verifier_detail,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=cost,
                latency_ms=result.latency_ms,
                reasoning_text=result.reasoning,
                finish_reason=result.finish_reason,
            )
            store.append(record)

            per_model_counts[model][verdict.name] += 1
            per_cell_counts[cell_key][verdict.name] += 1

            n_done += 1
            progress(f"[{n_done}/{n_total}] {model} {cell_key} -> {verdict.name}")

    cost_summary = store.cost_summary(run_id=run_id)

    per_cell_pass_rate: dict[str, float] = {}
    for cell_key, counts in per_cell_counts.items():
        n = sum(counts.values())
        per_cell_pass_rate[cell_key] = counts["PASS"] / n if n > 0 else 0.0

    return {
        "run_id": run_id,
        "n_tasks": len(tasks),
        "n_models": len(models),
        "n_records": n_done,
        "per_model_counts": per_model_counts,
        "per_cell_pass_rate": per_cell_pass_rate,
        "total_cost_usd": cost_summary["total_cost_usd"],
        "total_tokens_in": cost_summary["total_tokens_in"],
        "total_tokens_out": cost_summary["total_tokens_out"],
    }


def main() -> None:
    import os

    from .client import GROQ
    from .keys import load_keys

    keys = load_keys()
    api_key = keys["groq"]
    client = Client(GROQ, api_key)

    os.makedirs("results", exist_ok=True)
    store = ResultsStore("results/pilot.sqlite")

    summary = run_pilot(client, store, run_id="pilot-groq-004")

    print(f"run_id: {summary['run_id']}")
    print(f"tasks: {summary['n_tasks']}  models: {summary['n_models']}  records: {summary['n_records']}")
    print("per-model verdict counts:")
    for model, counts in summary["per_model_counts"].items():
        print(f"  {model}: {counts}")
    print("per-cell pass rate:")
    for cell, rate in summary["per_cell_pass_rate"].items():
        print(f"  {cell}: {rate:.2f}")
    print(f"total cost: ${summary['total_cost_usd']:.4f}")
    print(f"total tokens in/out: {summary['total_tokens_in']}/{summary['total_tokens_out']}")

    store.close()


if __name__ == "__main__":
    main()
