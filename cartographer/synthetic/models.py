"""Synthetic model zoo: named wrappers around ground-truth rules."""

from __future__ import annotations

from dataclasses import dataclass

from .rules import (
    CompositeRule,
    DimVector,
    InteractionRule,
    LogisticRule,
    NoiseWrapper,
    Rule,
    ThresholdRule,
)


@dataclass(frozen=True)
class SyntheticModel:
    """A named synthetic model whose pass/fail behavior is fixed by `rule`."""

    name: str
    rule: Rule

    def evaluate(self, dims: DimVector, trial_seed: int) -> bool:
        return self.rule.evaluate(dims, trial_seed)

    @property
    def ground_truth(self) -> Rule:
        """Expose the underlying rule (the ground truth to be recovered)."""
        return self.rule


_pairwise_interaction_rule = InteractionRule({"temporal": 4, "coreference": 3})

ZOO: dict[str, SyntheticModel] = {
    "clean_threshold": SyntheticModel(
        "clean_threshold", ThresholdRule("temporal", 6)
    ),
    "soft_boundary": SyntheticModel(
        "soft_boundary", LogisticRule("temporal", midpoint=6, slope=1.5)
    ),
    "pairwise_interaction": SyntheticModel(
        "pairwise_interaction", _pairwise_interaction_rule
    ),
    "three_way_interaction": SyntheticModel(
        "three_way_interaction",
        InteractionRule({"temporal": 4, "coreference": 3, "distractors": 30}),
    ),
    "no_interaction_control": SyntheticModel(
        "no_interaction_control",
        CompositeRule(
            [ThresholdRule("temporal", 8), ThresholdRule("distractors", 60)]
        ),
    ),
    "noisy_threshold": SyntheticModel(
        "noisy_threshold",
        NoiseWrapper(ThresholdRule("temporal", 6), p_flip=0.15),
    ),
    "noisy_interaction": SyntheticModel(
        "noisy_interaction",
        NoiseWrapper(_pairwise_interaction_rule, p_flip=0.1),
    ),
    "multi_soft": SyntheticModel(
        "multi_soft",
        CompositeRule(
            [
                LogisticRule("temporal", midpoint=6, slope=1.5),
                LogisticRule("distractors", midpoint=40, slope=0.3),
            ]
        ),
    ),
}
