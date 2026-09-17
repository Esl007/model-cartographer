"""Canonical experiment record schema for Phase 0.5.

Every probe/eval run against a model (synthetic, template-rendered, or
Nemotron-rendered) is captured as one `ExperimentRecord`. Analysis code
reads `realized_dims`, never `latent_params` (the knob settings requested,
which may diverge from what was actually measured/rendered).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto


class Verdict(Enum):
    PASS = auto()
    FAIL = auto()
    ERROR = auto()
    REJECTED_BY_FIDELITY_GATE = auto()


class RenderMode(Enum):
    SYNTHETIC = auto()
    TEMPLATE = auto()
    NEMOTRON = auto()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Mutation:
    """A single factor change applied when deriving a record from its parent."""

    factor: str
    from_value: float | str | None
    to_value: float | str | None

    def to_json_dict(self) -> dict:
        return {
            "factor": self.factor,
            "from_value": self.from_value,
            "to_value": self.to_value,
        }

    @classmethod
    def from_json_dict(cls, data: dict) -> "Mutation":
        return cls(
            factor=data["factor"],
            from_value=data["from_value"],
            to_value=data["to_value"],
        )


@dataclass(frozen=True)
class ExperimentRecord:
    """One immutable observation: a model evaluated at a point, with provenance.

    `record_id` is generated once at construction and never reused.
    `latent_params` are the intended knob settings; `realized_dims` are the
    measured actual values and are what analysis must read.
    """

    run_id: str
    model_name: str
    latent_params: dict[str, float]
    realized_dims: dict[str, float]
    render_mode: RenderMode
    task_seed: int
    trial_seed: int
    verdict: Verdict
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    wave_id: int = 0
    parent_id: str | None = None
    mutation: Mutation | None = None
    renderer_model: str | None = None
    rendering_seed: int = 0
    prompt_text: str | None = None
    response_text: str | None = None
    verifier_detail: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    created_at: str = field(default_factory=_utc_now_iso)

    def to_json_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "run_id": self.run_id,
            "wave_id": self.wave_id,
            "parent_id": self.parent_id,
            "mutation": self.mutation.to_json_dict() if self.mutation else None,
            "model_name": self.model_name,
            "latent_params": dict(self.latent_params),
            "realized_dims": dict(self.realized_dims),
            "render_mode": self.render_mode.name,
            "renderer_model": self.renderer_model,
            "task_seed": self.task_seed,
            "rendering_seed": self.rendering_seed,
            "trial_seed": self.trial_seed,
            "prompt_text": self.prompt_text,
            "response_text": self.response_text,
            "verdict": self.verdict.name,
            "verifier_detail": self.verifier_detail,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json_dict(cls, data: dict) -> "ExperimentRecord":
        return cls(
            record_id=data["record_id"],
            run_id=data["run_id"],
            wave_id=data["wave_id"],
            parent_id=data["parent_id"],
            mutation=(
                Mutation.from_json_dict(data["mutation"])
                if data["mutation"] is not None
                else None
            ),
            model_name=data["model_name"],
            latent_params=dict(data["latent_params"]),
            realized_dims=dict(data["realized_dims"]),
            render_mode=RenderMode[data["render_mode"]],
            renderer_model=data["renderer_model"],
            task_seed=data["task_seed"],
            rendering_seed=data["rendering_seed"],
            trial_seed=data["trial_seed"],
            prompt_text=data["prompt_text"],
            response_text=data["response_text"],
            verdict=Verdict[data["verdict"]],
            verifier_detail=data["verifier_detail"],
            tokens_in=data["tokens_in"],
            tokens_out=data["tokens_out"],
            cost_usd=data["cost_usd"],
            latency_ms=data["latency_ms"],
            created_at=data["created_at"],
        )
