"""Deterministic rule primitives for synthetic model pass/fail behavior.

All randomness is derived from sha256 hashing of stable inputs, so
`.evaluate(dims, trial_seed)` is a pure function: identical inputs always
produce identical outputs, with no reliance on global RNG state.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

DimVector = dict[str, float]


def _stable_float(*parts: str) -> float:
    """Map an arbitrary sequence of string parts to a float in [0, 1).

    Invariant: same `parts` always yields the same float (sha256 over a
    stable, deterministic serialization).
    """
    payload = "|".join(parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    # Use first 15 hex digits (60 bits) for ample precision without overflow.
    as_int = int(digest[:15], 16)
    return as_int / float(16**15)


def _serialize_dims(dims: DimVector) -> str:
    """Serialize dims deterministically regardless of insertion order."""
    return ",".join(f"{k}={dims.get(k, 0.0)!r}" for k in sorted(dims))


class Rule:
    """Base interface all rules implement."""

    def evaluate(self, dims: DimVector, trial_seed: int) -> bool:
        """Return True if the model passes on `dims` for `trial_seed`."""
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class ThresholdRule(Rule):
    """Fails iff dims[dim] > cutoff. Missing dim is treated as 0."""

    dim: str
    cutoff: float

    def evaluate(self, dims: DimVector, trial_seed: int) -> bool:
        return dims.get(self.dim, 0.0) <= self.cutoff

    def describe(self) -> str:
        return f"ThresholdRule(dim={self.dim!r}, cutoff={self.cutoff})"


@dataclass(frozen=True)
class LogisticRule(Rule):
    """P(fail) = 1 / (1 + exp(-slope * (x - midpoint))), drawn deterministically."""

    dim: str
    midpoint: float
    slope: float

    def _p_fail(self, dims: DimVector) -> float:
        x = dims.get(self.dim, 0.0)
        return 1.0 / (1.0 + math.exp(-self.slope * (x - self.midpoint)))

    def evaluate(self, dims: DimVector, trial_seed: int) -> bool:
        p_fail = self._p_fail(dims)
        draw = _stable_float(
            "logistic", self.describe(), _serialize_dims(dims), str(trial_seed)
        )
        return draw >= p_fail

    def describe(self) -> str:
        return f"LogisticRule(dim={self.dim!r}, midpoint={self.midpoint}, slope={self.slope})"


@dataclass(frozen=True)
class InteractionRule(Rule):
    """Fails iff ALL of dims[k] > v hold, for every (k, v) in conditions.

    Each factor exceeded alone (with all others at baseline) passes by
    construction, since a single exceeded condition does not satisfy "ALL".
    """

    conditions: dict[str, float] = field(default_factory=dict)

    def evaluate(self, dims: DimVector, trial_seed: int) -> bool:
        all_exceeded = all(
            dims.get(k, 0.0) > v for k, v in self.conditions.items()
        )
        return not all_exceeded

    def describe(self) -> str:
        cond_str = ", ".join(f"{k}>{v}" for k, v in self.conditions.items())
        return f"InteractionRule({cond_str})"


@dataclass(frozen=True)
class CompositeRule(Rule):
    """Fails if ANY sub-rule fails (logical AND of pass conditions)."""

    rules: list = field(default_factory=list)

    def evaluate(self, dims: DimVector, trial_seed: int) -> bool:
        return all(r.evaluate(dims, trial_seed) for r in self.rules)

    def describe(self) -> str:
        return f"CompositeRule([{', '.join(r.describe() for r in self.rules)}])"


@dataclass(frozen=True)
class NoiseWrapper(Rule):
    """Flips the wrapped rule's outcome with probability p_flip.

    The flip draw is salted with a distinct tag ("noiseflip") from
    LogisticRule's own draw tag ("logistic"), so the two do not correlate
    even when wrapping a LogisticRule.
    """

    rule: Rule
    p_flip: float

    def evaluate(self, dims: DimVector, trial_seed: int) -> bool:
        base = self.rule.evaluate(dims, trial_seed)
        draw = _stable_float(
            "noiseflip", self.describe(), _serialize_dims(dims), str(trial_seed)
        )
        flip = draw < self.p_flip
        return (not base) if flip else base

    def describe(self) -> str:
        return f"NoiseWrapper({self.rule.describe()}, p_flip={self.p_flip})"
