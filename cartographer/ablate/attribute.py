"""Causal attribution: automate the manual Ultra pronoun-ablation loop.

`attribute_failure` is the end-to-end engine: given a (world, name_map,
seed) that produced a failure, it (1) resample-gates the SAME prompt first
(cheapest possible check -- see `cartographer.ablate.resample`, and the
retracted "Priya" case in docs/lab-log.md this exists to prevent), then (2)
runs every available ablation from `cartographer.ablate.ablations` `n`
times each, (3) compares each ablation's pass rate to the baseline's via a
two-proportion z-test (`cartographer.confirm.stats`), Holm-Bonferroni
corrected across the ablation family, and (4) reports which ablation(s), if
any, significantly and substantially flip the failure to a pass -- i.e.
which single surface factor the model's failure causally depends on.

Multiple ablations may all attribute (e.g. if two independent factors both
contribute); the honest "cause not isolated by current ablation catalog"
outcome is expected and reported plainly, not treated as an error, exactly
like `cartographer.schedule.escalate`'s "model exceeds generator limits"
for the escalation ray.

Everything here is deterministic given a deterministic `evaluator_factory`:
same world/name_map/seed/n/alpha in -> byte-identical report out. No LLM
judgment appears anywhere in this file -- ground truth for every ablation
comes from `cartographer.world.simulate.answer` (via
`cartographer.render.templates.render`), and `evaluator_factory` is the only
place a live model is ever called, always as a black box returning a bare
pass/fail bool against that ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..confirm.stats import holm_bonferroni, two_proportion_z_test, wilson_interval
from ..render.naming import NameMap
from ..world.model import World
from .ablations import baseline
from .resample import ResampleResult, resample_failure

# `(prompt, answer_key) -> passed` -- the only place a real model client is
# ever invoked. Despite the name (kept per the task spec this module
# implements), this is the evaluator itself, not a nullary factory that
# produces one -- callers wire in a live model client directly, tests wire
# in fakes with this exact two-argument signature.
EvaluatorFactory = Callable[[str, str], bool]

_MIN_EFFECT_SIZE = 0.3


@dataclass(frozen=True)
class AttributionReport:
    """Outcome of one causal-attribution run.

    `stable`: whether the resample gate found the original failure stable
    (pass_rate <= its threshold on repeated identical-prompt sampling). If
    False, no ablation was ever run -- `per_ablation` is empty and
    `attributed_to` is empty, by construction, not because nothing scored
    high enough.

    `per_ablation`: {name -> {"pass_rate", "n", "wilson_lo", "wilson_hi",
    "p_value", "corrected_p_threshold", "effect_size", "significant"}}.

    `attributed_to`: ablation names that are BOTH Holm-Bonferroni
    significant at the family-wise `alpha` AND have effect size > 0.3.
    Multiple entries are allowed and expected when more than one factor
    contributes.

    `note`: "cause not isolated by current ablation catalog" when nothing
    attributes despite a stable failure -- an honest negative result, not
    a bug.
    """

    stable: bool
    baseline_pass_rate: float
    per_ablation: dict[str, dict] = field(default_factory=dict)
    attributed_to: tuple[str, ...] = ()
    effect_sizes: dict[str, float] = field(default_factory=dict)
    corrected_alpha: float = 0.05
    note: str = ""
    resample: ResampleResult | None = None

    def to_json_dict(self) -> dict:
        return {
            "stable": self.stable,
            "baseline_pass_rate": self.baseline_pass_rate,
            "per_ablation": {k: dict(v) for k, v in self.per_ablation.items()},
            "attributed_to": list(self.attributed_to),
            "effect_sizes": dict(self.effect_sizes),
            "corrected_alpha": self.corrected_alpha,
            "note": self.note,
            "resample": self.resample.to_json_dict() if self.resample is not None else None,
        }


def _run_n(evaluator: EvaluatorFactory, prompt: str, answer_key: str, n: int) -> int:
    n_pass = 0
    for _ in range(n):
        n_pass += bool(evaluator(prompt, answer_key))
    return n_pass


def attribute_failure(
    evaluator_factory: EvaluatorFactory,
    world: World,
    name_map: NameMap,
    seed: int,
    n: int = 5,
    alpha: float = 0.05,
    stable_threshold: float = 0.5,
) -> AttributionReport:
    """Attribute a failure on `world` (rendered via `name_map`/`seed`) to
    one or more ablations, or report honestly that no cause was isolated.

    Stage 0 (resample gate): builds the baseline prompt and reuses
    `resample_failure` to check the SAME prompt is a stable failure before
    spending any further budget. Wraps `evaluator_factory` in a
    config-shaped adapter (`{"_prompt": ..., "_answer_key": ...}` is never
    actually read by any DimParams-consuming code -- `resample_failure`'s
    contract only inspects `config`/`task_seed` to pass through to the
    evaluator and record identity, so this is safe) so the two-argument
    (prompt, answer_key) evaluator can be reused unmodified by the
    (config, task_seed) `Evaluator` contract `resample_failure` expects.

    Stage 1+ (attribution): for each ablation available for this world,
    run it `n` times, two-proportion-z-test its pass rate against the
    baseline's (same `n` baseline runs, reused from the resample gate),
    Holm-Bonferroni correct across the family, and attribute any ablation
    that is both corrected-significant and has effect size (pass_rate
    delta) > 0.3.
    """
    baseline_task = baseline(world, name_map, seed)

    def resample_adapter(_config: dict, _task_seed: int) -> bool:
        return bool(evaluator_factory(baseline_task.prompt, baseline_task.answer_key))

    resample = resample_failure(
        resample_adapter, config={}, task_seed=seed, n=n, stable_threshold=stable_threshold
    )

    if not resample.is_stable_failure:
        return AttributionReport(
            stable=False,
            baseline_pass_rate=resample.pass_rate,
            corrected_alpha=alpha,
            note=(
                "resample gate: failure did not hold up under repeated "
                f"identical-prompt sampling (pass_rate={resample.pass_rate:.2f} "
                f"over n={resample.n}) -- likely sampling noise, not a stable "
                "capability gap. No ablation was run."
            ),
            resample=resample,
        )

    # Reuse the resample gate's own n baseline calls as the baseline arm of
    # every two-proportion test below, instead of re-calling the model.
    baseline_n_pass = resample.n_pass
    baseline_n = resample.n
    baseline_pass_rate = resample.pass_rate

    from .ablations import available_ablations

    candidates = available_ablations(world, name_map, seed)

    per_ablation: dict[str, dict] = {}
    p_values: list[float] = []
    names_in_order: list[str] = []
    for name, task in candidates.items():
        n_pass = _run_n(evaluator_factory, task.prompt, task.answer_key, n)
        pass_rate = n_pass / n if n else 0.0
        interval = wilson_interval(n_pass, n)
        p_value = two_proportion_z_test(baseline_n_pass, baseline_n, n_pass, n)
        effect_size = pass_rate - baseline_pass_rate
        per_ablation[name] = {
            "pass_rate": pass_rate,
            "n": n,
            "wilson_lo": interval.low,
            "wilson_hi": interval.high,
            "p_value": p_value,
            "effect_size": effect_size,
        }
        p_values.append(p_value)
        names_in_order.append(name)

    attributed: list[str] = []
    effect_sizes: dict[str, float] = {}
    if names_in_order:
        corrections = holm_bonferroni(p_values, alpha=alpha)
        for name, (rejected, threshold) in zip(names_in_order, corrections):
            entry = per_ablation[name]
            entry["significant"] = rejected
            entry["corrected_p_threshold"] = threshold
            effect_sizes[name] = entry["effect_size"]
            if rejected and entry["effect_size"] > _MIN_EFFECT_SIZE:
                attributed.append(name)

    note = "" if attributed else "cause not isolated by current ablation catalog"

    return AttributionReport(
        stable=True,
        baseline_pass_rate=baseline_pass_rate,
        per_ablation=per_ablation,
        attributed_to=tuple(sorted(attributed)),
        effect_sizes=effect_sizes,
        corrected_alpha=alpha,
        note=note,
        resample=resample,
    )
