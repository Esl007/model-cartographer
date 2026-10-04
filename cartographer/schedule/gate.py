"""Resample gate for escalation evaluators (closes the findings §5.4 gap for
every province, not just interactive).

`ResampleGatedEvaluator` wraps any campaign evaluator whose call shape is
`(config, trial_seed, run_suffix="") -> bool` and which exposes
`error_count` / `capped` (the interactive, tool and code evaluators all do).
Every FAIL is re-run `n` more times on the byte-identical task (same config,
same trial seed, temperature 0) BEFORE escalation sees it; reruns are stored
under `<run_id>/verify`.

STABLE (at most `stable_threshold` of the reruns pass) -> the FAIL stands.
NOISE (more pass) -> the trial is scored as a pass for escalation, so a
sampling fluke can never become a frontier or enter decomposition. The
original FAIL record is kept (records are append-only); `stable` / `noise`
counts live on the wrapper and feed `schedule.certificate.certify`.
Infrastructure outcomes (errors, cost cap) are never resampled.
"""

from __future__ import annotations

import threading
from typing import Any


class ResampleGatedEvaluator:
    def __init__(self, inner: Any, n: int = 3, stable_threshold: float = 0.5) -> None:
        self.inner, self.n, self.stable_threshold = inner, n, stable_threshold
        self.stable = 0
        self.noise = 0
        self._lock = threading.Lock()

    def __getattr__(self, name):  # spent_usd, capped, error_count, ... come from the inner evaluator
        return getattr(self.inner, name)

    def __call__(self, config: dict[str, float], trial_seed: int) -> bool:
        errors_before = self.inner.error_count
        if self.inner(config, trial_seed):
            return True
        if self.inner.error_count > errors_before or self.inner.capped:
            return False  # ERROR (infra / cap), not a model failure: nothing to resample
        passes = sum(bool(self.inner(config, trial_seed, run_suffix="/verify")) for _ in range(self.n))
        noise = passes / self.n > self.stable_threshold
        with self._lock:
            if noise:
                self.noise += 1
            else:
                self.stable += 1
        return noise
