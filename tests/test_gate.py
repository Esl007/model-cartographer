"""Section D: resample-gate accounting (per-call errors, unresolved, skipped)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from cartographer.schedule.gate import ResampleGatedEvaluator


class Planted:
    """Scripted inner evaluator. behaviour[seed] = (first, resamples) where each
    outcome is 'P', 'F' or 'E' (ERROR bumps the shared error_count)."""

    def __init__(self, behaviour, cap_after=None):
        self.behaviour = behaviour
        self.error_count = 0
        self.calls = 0
        self.cap_after = cap_after
        self._lock = threading.Lock()
        self._seen = {}

    @property
    def capped(self):
        return self.cap_after is not None and self.calls >= self.cap_after

    def __call__(self, config, trial_seed, run_suffix=""):
        with self._lock:
            self.calls += 1
            i = self._seen.get(trial_seed, 0)
            self._seen[trial_seed] = i + 1
        first, resamples = self.behaviour[trial_seed]
        out = first if i == 0 else resamples[(i - 1) % len(resamples)]
        time.sleep(0.002)  # interleave threads
        if out == "E":
            with self._lock:
                self.error_count += 1
            return False
        return out == "P"


def test_concurrent_errors_counted_per_call():
    beh = {}
    for s in range(0, 40):
        kind = s % 4
        beh[s] = {0: ("E", "P"), 1: ("F", "FFF"), 2: ("F", "PPP"), 3: ("F", "EEE")}[kind]
    inner = Planted(beh)
    ev = ResampleGatedEvaluator(inner, n=3)
    with ThreadPoolExecutor(4) as pool:
        out = list(pool.map(lambda s: ev({}, s), range(40)))
    assert (ev.stable, ev.noise, ev.unresolved, ev.skipped) == (10, 10, 10, 0)
    assert out == [s % 4 == 2 for s in range(40)]
    note = ev.note_fields()
    assert ";gate_stable=10;gate_noise=10;gate_unresolved=10" in note
    assert "gate_skipped" not in note
    # original ERRORs were never resampled: 10 + 30*(1+3)
    assert inner.calls == 10 + 30 * 4


def test_all_error_resamples_unresolved():
    ev = ResampleGatedEvaluator(Planted({0: ("F", "EEE")}), n=3)
    assert ev({}, 0) is False
    assert (ev.stable, ev.noise, ev.unresolved) == (0, 0, 1)
    assert "gate_unresolved=1" in ev.note_fields()


def test_partial_errors_judged_on_valid_reruns():
    ev = ResampleGatedEvaluator(Planted({0: ("F", "EPP")}), n=3)
    assert ev({}, 0) is True and ev.noise == 1


def test_cost_cap_emits_skipped():
    ev = ResampleGatedEvaluator(Planted({0: ("F", "PPP"), 1: ("F", "PPP")}, cap_after=1), n=3)
    assert ev({}, 0) is False
    assert (ev.stable, ev.noise, ev.skipped) == (0, 0, 1)
    assert ev.note_fields().endswith(";gate_skipped=1;reason=cost_cap:1")


def test_gate_budget_emits_skipped():
    ev = ResampleGatedEvaluator(Planted({s: ("F", "FFF") for s in range(3)}), n=3, max_reruns=3)
    for s in range(3):
        ev({}, s)
    assert (ev.stable, ev.skipped) == (1, 2)
    assert ";gate_skipped=2;reason=gate_budget:2" in ev.note_fields()


def test_serial_runs_resamples_one_at_a_time():
    active, peak = [0], [0]
    lock = threading.Lock()

    class Probe(Planted):
        def __call__(self, config, trial_seed, run_suffix=""):
            if run_suffix:
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
            try:
                return super().__call__(config, trial_seed, run_suffix)
            finally:
                if run_suffix:
                    with lock:
                        active[0] -= 1

    ev = ResampleGatedEvaluator(Probe({s: ("F", "FFF") for s in range(8)}), n=3, serial=True)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda s: ev({}, s), range(8)))
    assert peak[0] == 1 and ev.stable == 8


def test_default_note_unchanged():
    ev = ResampleGatedEvaluator(Planted({}), n=3)
    assert ev.note_fields() == ";gate_stable=0;gate_noise=0"
