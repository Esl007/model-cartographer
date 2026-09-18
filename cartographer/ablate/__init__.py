"""Automated failure attribution: the manual Ultra pronoun-ablation loop,
made deterministic and repeatable.

See `cartographer/ablate/resample.py` for the noise-filter gate and
`cartographer/ablate/ablations.py` / `cartographer/ablate/attribute.py` for
the ablation catalog and causal-attribution engine.
"""

from __future__ import annotations

from .ablations import ABLATIONS, AblatedTask, available_ablations
from .attribute import AttributionReport, attribute_failure
from .resample import ResampleResult, resample_failure

__all__ = [
    "ABLATIONS",
    "AblatedTask",
    "available_ablations",
    "AttributionReport",
    "attribute_failure",
    "ResampleResult",
    "resample_failure",
]
