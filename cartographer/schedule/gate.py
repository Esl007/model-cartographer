"""Resample gate for escalation evaluators (closes the findings §5.4 gap for
every province, not just interactive).

`ResampleGatedEvaluator` wraps any campaign evaluator whose call shape is
`(config, trial_seed, run_suffix="") -> bool` and which exposes
`error_count` / `capped` (the interactive, tool and code evaluators all do).
Every FAIL is re-run `n` more times on the byte-identical task (same config,
same trial seed, temperature 0) BEFORE escalation sees it; reruns are stored
under `<run_id>/verify`.

Outcomes per gated FAIL:
  STABLE     -- at most `stable_threshold` of the *non-error* reruns pass; the FAIL stands.
  NOISE      -- more pass; scored as a pass for escalation.
  UNRESOLVED -- every rerun was an ERROR; the FAIL stands but is excluded
                from both stable and noise (reported as `gate_unresolved=N`).
  SKIPPED    -- the cost cap / gate rerun budget prevented reruns; the FAIL
                stands, reported as `gate_skipped=N;reason=...`.
Infrastructure outcomes (errors, cost cap) on the original call are never
resampled.

Per-call error detection: error-ness is reported per thread, not by diffing
the inner evaluator's shared `error_count` (racy at parallelism > 1). The
gate hooks the inner evaluator's `error_count` writes and records each
increment in a thread-local, so a call only sees errors raised on its own
thread.
"""

from __future__ import annotations

import threading
from typing import Any

_tls = threading.local()


def _thread_errors() -> int:
    return getattr(_tls, "errors", 0)


def _hooked_setattr(self, name, value):
    if name == "error_count":
        try:
            if value > self.__dict__.get("error_count", 0):
                _tls.errors = _thread_errors() + 1
        except TypeError:
            pass
    type(self)._gate_base_cls.__setattr__(self, name, value)


def install_error_hook(inner: Any) -> None:
    """Make every `inner.error_count` increment visible to the calling thread."""
    cls = type(inner)
    if getattr(cls, "_gate_hooked", False):
        return
    hooked = type(cls.__name__, (cls,), {
        "__setattr__": _hooked_setattr, "_gate_hooked": True,
        "_gate_base_cls": cls,
        "__module__": cls.__module__, "__qualname__": cls.__qualname__,
    })
    try:
        inner.__class__ = hooked
    except TypeError:  # __slots__ / builtin layouts: fall back to no hook
        pass


class ResampleGatedEvaluator:
    def __init__(self, inner: Any, n: int = 3, stable_threshold: float = 0.5,
                 serial: bool = False, max_reruns: int | None = None) -> None:
        self.inner, self.n, self.stable_threshold = inner, n, stable_threshold
        self.serial = serial          # run resamples one at a time (per-provider serialisation)
        self.max_reruns = max_reruns  # gate-wide rerun budget; None = unlimited
        self.stable = 0
        self.noise = 0
        self.unresolved = 0
        self.skipped = 0
        self.skip_reasons: dict[str, int] = {}
        self.reruns = 0
        self._lock = threading.Lock()
        self._serial_lock = threading.Lock()
        install_error_hook(inner)

    def __getattr__(self, name):  # spent_usd, capped, error_count, ... come from the inner evaluator
        return getattr(self.inner, name)

    def _call(self, config, trial_seed, **kw) -> tuple[bool, bool]:
        """One inner call -> (passed, was_error), error-ness from this thread only."""
        before = _thread_errors()
        ok = bool(self.inner(config, trial_seed, **kw))
        return ok, _thread_errors() > before

    def _skip(self, reason: str) -> bool:
        with self._lock:
            self.skipped += 1
            self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
        return False

    def __call__(self, config: dict[str, float], trial_seed: int) -> bool:
        ok, err = self._call(config, trial_seed)
        if ok:
            return True
        if err:
            return False  # ERROR (infra), not a model failure: nothing to resample
        if self.inner.capped:
            return self._skip("cost_cap")
        if self.max_reruns is not None:
            with self._lock:
                if self.reruns + self.n > self.max_reruns:
                    budget_out = True
                else:
                    budget_out = False
                    self.reruns += self.n
            if budget_out:
                return self._skip("gate_budget")
        if self.serial:
            with self._serial_lock:
                results = self._resample(config, trial_seed)
        else:
            results = self._resample(config, trial_seed)
        if results is None:
            return self._skip("cost_cap")
        valid = [p for p, e in results if not e]
        if not valid:
            with self._lock:
                self.unresolved += 1
            return False
        noise = sum(valid) / len(valid) > self.stable_threshold
        with self._lock:
            if noise:
                self.noise += 1
            else:
                self.stable += 1
        return noise

    def _resample(self, config, trial_seed):
        results = []
        for _ in range(self.n):
            if self.inner.capped:
                break
            results.append(self._call(config, trial_seed, run_suffix="/verify"))
        if not results:
            return None
        return results

    def note_fields(self) -> str:
        """`;gate_stable=..;gate_noise=..` plus `;gate_unresolved=N` and
        `;gate_skipped=N;reason=...` when nonzero."""
        s = f";gate_stable={self.stable};gate_noise={self.noise}"
        if self.unresolved:
            s += f";gate_unresolved={self.unresolved}"
        if self.skipped:
            reasons = ",".join(f"{k}:{v}" for k, v in sorted(self.skip_reasons.items()))
            s += f";gate_skipped={self.skipped};reason={reasons}"
        return s
