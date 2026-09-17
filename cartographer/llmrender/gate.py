"""Phase 4 mandatory fidelity gate: an LLM-rendered story is only accepted
if an independent extractor model, reading ONLY the story, recovers a fact
list that deterministically matches the world's ground truth.

This is the enforcement mechanism that makes Nemotron-style natural-
language rendering trustworthy for eval: a hallucination or omission in the
rendered story is caught before the story is ever shown to a subject model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..render.naming import NameMap
from ..store.schema import ExperimentRecord, RenderMode, Verdict
from ..store.store import ResultsStore
from ..world.measure import measure
from ..world.model import World
from .extract import extract_facts_tokens
from .facts import facts_match, world_facts
from .renderer import render_llm_tokens


@dataclass(frozen=True)
class GateResult:
    story: str | None
    accepted: bool
    attempts: int
    violations: list[str] = field(default_factory=list)
    renderer_model: str = ""
    extractor_model: str = ""
    fell_back_to_template: bool = False
    tokens_spent: int = 0


def gated_render(
    world: World,
    name_map: NameMap,
    client,
    renderer_model: str,
    extractor_model: str,
    subject_model: str,
    seed: int,
    max_attempts: int = 3,
) -> GateResult:
    """Render `world` via `renderer_model`, verify fidelity via
    `extractor_model`, and retry up to `max_attempts` times.

    HARD GUARD (contamination): `renderer_model` and `extractor_model` must
    never be the same model as `subject_model` under evaluation, and
    `extractor_model` must never equal `renderer_model` — otherwise a model
    could "verify" its own hallucinations (or the subject's own rendering
    bias could leak into what's judged faithful), silently invalidating the
    fidelity gate's independence guarantee. This is checked eagerly, before
    any API calls are made.
    """
    if renderer_model == subject_model:
        raise ValueError(
            f"contamination guard: renderer_model {renderer_model!r} must not "
            f"equal subject_model {subject_model!r}"
        )
    if extractor_model == subject_model:
        raise ValueError(
            f"contamination guard: extractor_model {extractor_model!r} must not "
            f"equal subject_model {subject_model!r}"
        )
    if extractor_model == renderer_model:
        raise ValueError(
            f"contamination guard: extractor_model {extractor_model!r} must not "
            f"equal renderer_model {renderer_model!r}"
        )

    expected = world_facts(world, name_map)
    all_violations: list[str] = []
    tokens_spent = 0

    for attempt in range(1, max_attempts + 1):
        attempt_seed = seed + attempt - 1
        temperature = min(0.7 + 0.1 * (attempt - 1), 1.0)

        story, render_tokens = render_llm_tokens(
            world, name_map, client, renderer_model, seed=attempt_seed, temperature=temperature
        )
        tokens_spent += render_tokens
        if story is None:
            all_violations.append(f"attempt {attempt}: renderer call failed")
            continue

        extracted, extract_tokens = extract_facts_tokens(story, client, extractor_model)
        tokens_spent += extract_tokens
        if extracted is None:
            all_violations.append(f"attempt {attempt}: extractor call failed or unparseable")
            continue

        ok, violations = facts_match(expected, extracted)
        all_violations.extend(f"attempt {attempt}: {v}" for v in violations)

        if ok:
            return GateResult(
                story=story,
                accepted=True,
                attempts=attempt,
                violations=all_violations,
                renderer_model=renderer_model,
                extractor_model=extractor_model,
                fell_back_to_template=False,
                tokens_spent=tokens_spent,
            )

    return GateResult(
        story=None,
        accepted=False,
        attempts=max_attempts,
        violations=all_violations,
        renderer_model=renderer_model,
        extractor_model=extractor_model,
        fell_back_to_template=True,
        tokens_spent=tokens_spent,
    )


def record_gate_rejection(
    store: ResultsStore,
    run_id: str,
    world: World,
    name_map: NameMap,
    gate_result: GateResult,
    task_seed: int,
) -> ExperimentRecord:
    """Persist a fidelity-gate exhaustion as data: rejections are as
    informative as acceptances, so they get their own verdict and are
    appended to the store rather than silently discarded."""
    detail = ";".join(gate_result.violations)[:500]
    record = ExperimentRecord(
        run_id=run_id,
        model_name=gate_result.renderer_model,
        latent_params={},
        realized_dims=measure(world),
        render_mode=RenderMode.NEMOTRON,
        task_seed=task_seed,
        trial_seed=task_seed,
        verdict=Verdict.REJECTED_BY_FIDELITY_GATE,
        renderer_model=gate_result.renderer_model,
        prompt_text=gate_result.story,
        verifier_detail=detail,
    )
    store.append(record)
    return record
