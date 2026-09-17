"""Bridge between Phase 0 synthetic models and the Phase 0.5 results store."""

from __future__ import annotations

from dataclasses import dataclass

from cartographer.synthetic.models import SyntheticModel

from .schema import ExperimentRecord, Mutation, RenderMode, Verdict
from .store import ResultsStore


def record_synthetic_eval(
    model: SyntheticModel,
    dims: dict[str, float],
    trial_seed: int,
    run_id: str,
    wave_id: int = 0,
    parent_id: str | None = None,
    mutation: Mutation | None = None,
) -> ExperimentRecord:
    """Evaluate `model` on `dims` and package the outcome as an `ExperimentRecord`.

    latent_params and realized_dims are identical here since synthetic
    models have no rendering step to introduce drift between the two.
    """
    ok = model.evaluate(dims, trial_seed)
    return ExperimentRecord(
        run_id=run_id,
        wave_id=wave_id,
        parent_id=parent_id,
        mutation=mutation,
        model_name=model.name,
        latent_params=dict(dims),
        realized_dims=dict(dims),
        render_mode=RenderMode.SYNTHETIC,
        renderer_model=None,
        task_seed=trial_seed,
        rendering_seed=0,
        trial_seed=trial_seed,
        prompt_text=None,
        response_text=None,
        verdict=Verdict.PASS if ok else Verdict.FAIL,
        verifier_detail=model.ground_truth.describe(),
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        latency_ms=0.0,
    )


@dataclass
class InstrumentedSyntheticRunner:
    """Wraps a `SyntheticModel` so every `.evaluate` call is logged to a store.

    Duck-type compatible with `SyntheticModel` (same `.evaluate(dims,
    trial_seed) -> bool` signature) so it can be passed directly to Phase 0
    probe strategies without modification.
    """

    store: ResultsStore
    model: SyntheticModel
    run_id: str
    wave_id: int = 0
    parent_id: str | None = None
    mutation: Mutation | None = None

    def evaluate(self, dims: dict[str, float], trial_seed: int) -> bool:
        record = record_synthetic_eval(
            self.model,
            dims,
            trial_seed,
            self.run_id,
            wave_id=self.wave_id,
            parent_id=self.parent_id,
            mutation=self.mutation,
        )
        self.store.append(record)
        return record.verdict is Verdict.PASS

    @property
    def name(self) -> str:
        return self.model.name

    @property
    def ground_truth(self):
        return self.model.ground_truth
