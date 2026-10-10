"""Province VI campaigns (docs/province-vi-spec.md §9): the escalation
evaluator over repository episodes, the rays, and the paired graph-value
campaign with its pre-registered statistics (§8 H4).

A trial's task seed depends only on the latent key and the trial seed
(`task_seed`), never on `n_modules`, the environment switches
(`graph_tools`, `restate_*`, `tool_budget`) or the presentation dials
(look-alikes, clone, comments, opaque names). So a graph-on and a graph-off
trial with the same trial seed see byte-identical repositories, and a
`n_modules` sweep at a pinned `n_funcs` re-partitions the SAME program.
"""

from __future__ import annotations

import difflib
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields

from ..codeworld.generate import GenerationFailed, _stable_int
from ..exec.client import Client
from ..exec.pricing import cost_usd
from ..interactive.episode import run_episode
from ..schedule.certificate import certify
from ..schedule.escalate import BreakingPoint, EscalationRay, RayDim, find_breaking_point
from ..schedule.gate import ResampleGatedEvaluator
from ..store.schema import ExperimentRecord, RenderMode, Verdict
from ..store.store import ResultsStore
from .compose import RepoParams, compose
from .env import RepoEnv
from .verify import verify_repo

INT_KEYS = ("n_modules", "bug_distance", "fanout", "n_funcs", "distractor_modules", "n_bugs", "hidden_inputs",
            "n_statements", "tool_budget")
FLOAT_KEYS = ("inline_p", "bug_mix")
BOOL_KEYS = ("lookalike_names", "clone_trap", "misleading_comments", "graph_tools", "restate_symbols",
             "restate_imports", "opaque_names",
             # Evidence Fidelity Campaign conditions (outside the latent key: same program)
             "bug_local", "show_data_flow", "diff_emphasis")
KNOWN_KEYS = INT_KEYS + FLOAT_KEYS + BOOL_KEYS


def params_from_config(base: RepoParams, config: dict) -> RepoParams:
    kw = {}
    for k, v in config.items():
        if k in INT_KEYS:
            n = int(round(v))
            kw[k] = None if k == "n_funcs" and n <= 0 else n
        elif k in FLOAT_KEYS:
            kw[k] = float(v)
        elif k in BOOL_KEYS:
            kw[k] = bool(v >= 0.5)
    return RepoParams(**{**{f.name: getattr(base, f.name) for f in fields(base)}, **kw})


def task_seed(params: RepoParams, trial_seed: int) -> int:
    return _stable_int("repo-task", repr(params.latent_key()), trial_seed)


def latent_dict(params: RepoParams) -> dict:
    out = {}
    for f in fields(params):
        v = getattr(params, f.name)
        if v is None or isinstance(v, str):
            continue
        out[f.name] = float(v)
    return out


MEASURE_KEYS = ("n_calls", "n_reads", "n_searches", "n_neighbors", "n_edits", "n_test_runs", "edit_nomatch",
                "edit_nonunique", "whole_file_rewrite", "test_edit_attempts", "unknown_tool", "n_errors",
                "calls_to_first_cause_read", "files_read", "files_read_on_path", "ran_tests_after_last_edit")


def file_diff(before: dict, after: dict) -> str:
    out = []
    for p in sorted(set(before) | set(after)):
        a, b = before.get(p, ""), after.get(p, "")
        if a != b:
            out.extend(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{p}", f"b/{p}", n=2))
    return "".join(out)


# -- the evaluator ---------------------------------------------------------------------------------

class RepoFixEvaluator:
    """`(config, trial_seed, run_suffix="") -> bool` over one repository
    episode, persisting an `ExperimentRecord(kind=repo_fix)`. Config keys:
    any `RepoParams` numeric/boolean field (`KNOWN_KEYS`)."""

    def __init__(self, client: Client, model: str, store: ResultsStore, run_id: str, cost_cap_usd: float,
                 base: RepoParams | None = None, max_tokens: int = 16000,
                 cancel_event: threading.Event | None = None, protocol: str = "native",
                 context_window: int | None = None, deadline: float | None = None,
                 episode_wall_s: float | None = None, retry_max_tokens: int | None = 32000,
                 on_task=None) -> None:
        self.retry_max_tokens = retry_max_tokens
        self.on_task = on_task  # EF campaign zoo hook: on_task(env=, task=, config=) before the episode
        self.client, self.model, self.store, self.run_id = client, model, store, run_id
        self.context_window = context_window
        # wall-clock guards, checked before every turn: an absolute session deadline (epoch
        # seconds, e.g. a GPU-time budget) and a per-episode cap. Hitting either ends the
        # episode as an infrastructure stop (ERROR, `deadline_hit=1`), never a model FAIL.
        self.deadline = deadline
        self.episode_wall_s = episode_wall_s
        self.cost_cap_usd = cost_cap_usd
        self.base = base or RepoParams()
        self.max_tokens = max_tokens
        self.cancel_event = cancel_event
        self.protocol = protocol
        self.spent_usd = 0.0
        self.call_count = 0
        self.error_count = 0
        self.capped = False
        self.cancelled = False
        self.outcomes: dict = {}  # (run_suffix, trial_seed, config items) -> (verdict, signature, measures)
        self._lock = threading.Lock()

    def __call__(self, config: dict[str, float], trial_seed: int, run_suffix: str = "") -> bool:
        params = params_from_config(self.base, {k: v for k, v in config.items() if k in KNOWN_KEYS})
        seed = task_seed(params, trial_seed)
        latent = latent_dict(params)
        key = (run_suffix, trial_seed, tuple(sorted(config.items())))
        with self._lock:
            reason = None
            if self.spent_usd >= self.cost_cap_usd:
                self.capped, reason = True, "cost_capped"
            elif self.cancel_event is not None and self.cancel_event.is_set():
                self.cancelled, reason = True, "cancelled"
            elif self.deadline is not None and time.time() >= self.deadline:
                self.cancelled, reason = True, "deadline"
            if reason:
                self.error_count += 1
                self._append(latent, {}, seed, trial_seed, None, None, Verdict.ERROR,
                             f"kind=repo_fix;campaign=1;detail={reason};{reason}=1", 0, 0, 0.0, 0.0, None, run_suffix)
                self.outcomes[key] = (Verdict.ERROR, reason, {})
                return False
        try:
            task = compose(params, seed)
        except GenerationFailed as e:
            with self._lock:
                self.error_count += 1
                self._append(latent, {}, seed, trial_seed, None, str(e), Verdict.ERROR,
                             "kind=repo_fix;campaign=1;detail=generation_failed", 0, 0, 0.0, 0.0, None, run_suffix)
                self.outcomes[key] = (Verdict.ERROR, "generation_failed", {})
            return False
        env = RepoEnv(task)
        if self.on_task is not None:
            self.on_task(env=env, task=task, config=config)
        started = time.time()
        timed_out = [False]

        def may_continue(ep_cost: float) -> bool:
            now = time.time()
            if (self.deadline is not None and now >= self.deadline) or \
                    (self.episode_wall_s is not None and now - started >= self.episode_wall_s):
                timed_out[0] = True
                return False
            return self.spent_usd + ep_cost < self.cost_cap_usd

        res = run_episode(self.client, self.model, env, max_tokens=self.max_tokens, cost_fn=cost_usd,
                          may_continue=may_continue, protocol=self.protocol, context_window=self.context_window,
                          retry_max_tokens=self.retry_max_tokens, keep_texts=1500)
        v = verify_repo(task, env.files, res.outcome)
        s = env.summary()
        measures = {k: float(s[k]) for k in MEASURE_KEYS}
        detail = (f"kind=repo_fix;campaign=1;protocol={res.protocol};outcome={res.outcome};detail={v.signature}"
                  f";bug={'+'.join(task.bug_kinds)};bug_module={task.bug_module}"
                  f";edited={'+'.join(s['edited_files']) or '-'};turns={res.turns}"
                  f";protocol_version={res.protocol_version};result_rendering={res.result_rendering}"
                  f";calls_per_turn={env.max_calls_per_turn};text_stop={int(bool(env.text_stop))}"
                  f";skipped_calls={res.skipped_calls};truncated_turns={res.truncated_turns}"
                  f";max_tokens={self.max_tokens}"
                  + "".join(f";{k}={int(measures[k])}" for k in MEASURE_KEYS))
        if "fabricated" in v.extra:
            detail += f";fabricated={v.extra['fabricated']}"
        if self.context_window:
            detail += f";context_window={self.context_window}"
        if timed_out[0]:
            detail += ";deadline_hit=1"
        detail += f";wall_s={int(time.time() - started)}"
        if res.error:
            detail += f";error={res.error}"
        realized = {**task.realized, **measures, "tokens_in_total": float(res.tokens_in),
                    "truncated_turns": float(res.truncated_turns),
                    "protocol_version": float(res.protocol_version)}
        prompt = env.opening_messages()[1]["content"]
        response = json.dumps({"steps": json.loads(env.transcript()), "diff": file_diff(task.files, env.files),
                               "assistant": list(res.assistant_texts)})
        with self._lock:
            self.spent_usd += res.cost_usd
            self.call_count += res.turns
            if v.verdict == Verdict.ERROR:
                self.error_count += 1
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True
            self._append(latent, realized, seed, trial_seed, prompt, response, v.verdict, detail, res.tokens_in,
                         res.tokens_out, res.cost_usd, res.latency_ms, res.finish_reason, run_suffix)
            self.outcomes[key] = (v.verdict, v.signature, measures)
        return v.verdict == Verdict.PASS

    def _append(self, latent, realized, seed, trial_seed, prompt, response, verdict, detail, t_in, t_out, cost,
                latency, finish, run_suffix: str = "") -> None:
        self.store.append(ExperimentRecord(
            run_id=self.run_id + run_suffix, model_name=self.model, latent_params=latent, realized_dims=realized,
            render_mode=RenderMode.TEMPLATE, task_seed=seed, trial_seed=trial_seed, rendering_seed=seed,
            prompt_text=prompt, response_text=response, verdict=verdict, verifier_detail=detail,
            tokens_in=t_in, tokens_out=t_out, cost_usd=cost, latency_ms=latency, finish_reason=finish))


# -- rays -------------------------------------------------------------------------------------------

def default_repo_navigate_ray() -> EscalationRay:
    """Symptom-to-cause distance 1 -> 6 hops with the repository 4 -> 12 files
    (every hop crosses a file all along the ray); budget pinned at 20."""
    return EscalationRay(dims=(RayDim("bug_distance", 1, 6, True), RayDim("n_modules", 4, 12, True),
                               RayDim("tool_budget", 20, 20, True)))


def default_repo_lookalike_ray() -> EscalationRay:
    """Look-alike twins on; 4 -> 12 files, cause 1 -> 3 hops away."""
    return EscalationRay(dims=(RayDim("lookalike_names", 1, 1, True), RayDim("n_modules", 4, 12, True),
                               RayDim("bug_distance", 1, 3, True), RayDim("tool_budget", 20, 20, True)))


def default_repo_clone_ray() -> EscalationRay:
    """The near-miss clone on; 4 -> 12 files, cause 1 -> 3 hops away."""
    return EscalationRay(dims=(RayDim("clone_trap", 1, 1, True), RayDim("n_modules", 4, 12, True),
                               RayDim("bug_distance", 1, 3, True), RayDim("tool_budget", 20, 20, True)))


def default_repo_budget_ray() -> EscalationRay:
    """Tool budget 30 -> 6 at a cause 3 hops away in 8 files."""
    return EscalationRay(dims=(RayDim("tool_budget", 30, 6, True), RayDim("bug_distance", 3, 3, True),
                               RayDim("n_modules", 8, 8, True)))


def default_repo_control_ray() -> EscalationRay:
    """The SAME program (n_funcs pinned at 8, cause 1 hop away) spread over
    1 -> 8 files. At 1 file the task is Province V's, so the drop along this
    ray is the cost of navigation itself."""
    return EscalationRay(dims=(RayDim("n_modules", 1, 8, True), RayDim("n_funcs", 8, 8, True),
                               RayDim("bug_distance", 1, 1, True), RayDim("tool_budget", 20, 20, True)))


# -- runners -----------------------------------------------------------------------------------------

def run_repo_breaking_point_campaign(client: Client, store: ResultsStore, run_id: str, model: str,
                                     ray: EscalationRay | None = None, budget: int = 60, cost_cap: float = 1.0,
                                     k_trials: int = 5, parallelism: int = 4, max_tokens: int = 16000,
                                     cancel_event: threading.Event | None = None, protocol: str = "native",
                                     early_stop: bool = False, resample_gate: bool = False, resample_n: int = 3,
                                     base: RepoParams | None = None,
                                     context_window: int | None = None) -> BreakingPoint:
    """Escalation over repository episodes (default ray: VI.navigate)."""
    ev = RepoFixEvaluator(client, model, store, run_id, cost_cap, base=base, max_tokens=max_tokens,
                          cancel_event=cancel_event, protocol=protocol, context_window=context_window)
    gated = ResampleGatedEvaluator(ev, n=resample_n) if resample_gate else ev
    bp = find_breaking_point(gated, ray if ray is not None else default_repo_navigate_ray(), budget=budget,
                             k_trials=k_trials, parallelism=parallelism, early_stop=early_stop)
    note = f"spent=${ev.spent_usd:.4f}" + (";cost_cap_hit" if ev.capped else "")
    if resample_gate:
        note += gated.note_fields()
        bp.certificate = certify(bp, gate_stable=gated.stable, gate_noise=gated.noise)
    bp.note = f"{bp.note}; {note}" if bp.note else note
    return bp


# -- paired statistics (pre-registered, §8) -------------------------------------------------------------

def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p on the discordant counts (b: pass off / fail
    on; c: fail off / pass on)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def wilcoxon_signed_rank_less(diffs: list[float]) -> float:
    """One-sided Wilcoxon signed-rank p for H1: median(diff) < 0. Zeros are
    dropped, ties get average ranks; exact for n <= 25 (enumerated W+
    distribution over the actual ranks), normal approximation above."""
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return 1.0
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks, d) if x > 0)
    if n <= 25:
        scaled = [int(round(2 * r)) for r in ranks]  # half-ranks -> integers
        dist = {0: 1}
        for r in scaled:
            nxt = dict(dist)
            for s, c in dist.items():
                nxt[s + r] = nxt.get(s + r, 0) + c
            dist = nxt
        target = int(round(2 * w_plus))
        return sum(c for s, c in dist.items() if s <= target) / 2 ** n
    mean = n * (n + 1) / 4
    sd = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)
    z = (w_plus + 0.5 - mean) / sd
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (s[m - 1] + s[m]) / 2


@dataclass
class GraphValueResult:
    """Paired graph-on vs graph-off outcomes (VI.graph_value, H4)."""

    pairs: int = 0
    pass_off: int = 0
    pass_on: int = 0
    b: int = 0  # pass off, fail on
    c: int = 0  # fail off, pass on
    mcnemar_p: float = 1.0
    median_cfcr_off: float | None = None
    median_cfcr_on: float | None = None
    ratio: float | None = None
    wilcoxon_p: float = 1.0
    h4_supported: bool = False
    per_level: dict = field(default_factory=dict)
    note: str = ""

    def to_json_dict(self) -> dict:
        return asdict(self)


def paired_graph_stats(pairs: list[tuple], tool_budget: int) -> GraphValueResult:
    """`pairs`: [(level, (pass_off, cfcr_off), (pass_on, cfcr_on)), ...];
    `cfcr` = calls_to_first_cause_read, -1 (never read) censored to
    tool_budget + 1. H4 holds iff median(on) <= 0.7 x median(off) AND the
    one-sided Wilcoxon p < 0.05."""
    def cens(x):
        return float(tool_budget + 1) if x is None or x < 0 else float(x)

    r = GraphValueResult(pairs=len(pairs))
    offs, ons, diffs = [], [], []
    by_level: dict = {}
    for level, (p_off, c_off), (p_on, c_on) in pairs:
        r.pass_off += int(p_off)
        r.pass_on += int(p_on)
        r.b += int(p_off and not p_on)
        r.c += int(p_on and not p_off)
        offs.append(cens(c_off))
        ons.append(cens(c_on))
        diffs.append(cens(c_on) - cens(c_off))
        lv = by_level.setdefault(str(level), {"pairs": 0, "pass_off": 0, "pass_on": 0, "cfcr_off": [], "cfcr_on": []})
        lv["pairs"] += 1
        lv["pass_off"] += int(p_off)
        lv["pass_on"] += int(p_on)
        lv["cfcr_off"].append(cens(c_off))
        lv["cfcr_on"].append(cens(c_on))
    r.mcnemar_p = mcnemar_exact(r.b, r.c)
    r.median_cfcr_off, r.median_cfcr_on = _median(offs), _median(ons)
    if r.median_cfcr_off:
        r.ratio = r.median_cfcr_on / r.median_cfcr_off
    r.wilcoxon_p = wilcoxon_signed_rank_less(diffs)
    r.h4_supported = bool(r.ratio is not None and r.ratio <= 0.7 and r.wilcoxon_p < 0.05)
    r.per_level = {k: {"pairs": v["pairs"], "pass_off": v["pass_off"], "pass_on": v["pass_on"],
                       "median_cfcr_off": _median(v["cfcr_off"]), "median_cfcr_on": _median(v["cfcr_on"])}
                   for k, v in by_level.items()}
    r.note = (f"pairs={r.pairs};pass_off={r.pass_off};pass_on={r.pass_on};mcnemar_b={r.b};mcnemar_c={r.c}"
              f";mcnemar_p={r.mcnemar_p:.4g};median_cfcr_off={r.median_cfcr_off};median_cfcr_on={r.median_cfcr_on}"
              f";ratio={'' if r.ratio is None else f'{r.ratio:.3f}'};wilcoxon_p={r.wilcoxon_p:.4g}"
              f";h4_supported={int(r.h4_supported)}")
    return r


def run_paired_graph(evaluator, levels=(4, 8), n_seeds: int = 10, tool_budget: int = 20, bug_distance: int = 3,
                     n_funcs: int | None = None, parallelism: int = 4, seed_base: int = 0) -> GraphValueResult:
    """Each (level, seed) runs twice on the identical repository: graph tools
    off under run suffix `/graph_off`, on under `/graph_on`. `evaluator` is a
    RepoFixEvaluator or a zoo.ZooEvaluator."""
    jobs = []
    for level in levels:
        for i in range(n_seeds):
            cfg = {"n_modules": float(level), "bug_distance": float(min(bug_distance, level - 1)),
                   "tool_budget": float(tool_budget)}
            if n_funcs:
                cfg["n_funcs"] = float(n_funcs)
            jobs.append((level, seed_base + i, cfg))

    def one(job, graph):
        level, ts, cfg = job
        c = {**cfg, "graph_tools": float(graph)}
        ok = evaluator(c, ts, run_suffix="/graph_on" if graph else "/graph_off")
        return ok, _cfcr(evaluator, c, ts, "/graph_on" if graph else "/graph_off")

    def pair(job):  # both arms in one worker, so a run cut short leaves whole pairs
        return one(job, 0), one(job, 1)

    with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
        results = list(pool.map(pair, jobs))
    complete = [(j[0], o, n) for j, (o, n) in zip(jobs, results) if _scored(evaluator, j, 0) and _scored(evaluator, j, 1)]
    return paired_graph_stats(complete, tool_budget)


def _scored(evaluator, job, graph: int) -> bool:
    """False when that arm ended as ERROR (infrastructure, deadline, cancelled):
    such a pair is dropped from the paired statistics, never counted as a FAIL."""
    outcomes = getattr(evaluator, "outcomes", None)
    if outcomes is None:
        return True
    _level, ts, cfg = job
    c = {**cfg, "graph_tools": float(graph)}
    hit = outcomes.get(("/graph_on" if graph else "/graph_off", ts, tuple(sorted(c.items()))))
    return hit is not None and hit[0] != Verdict.ERROR


def _cfcr(evaluator, config: dict, trial_seed: int, suffix: str):
    outcomes = getattr(evaluator, "outcomes", None)
    if outcomes is not None:
        hit = outcomes.get((suffix, trial_seed, tuple(sorted(config.items()))))
        return None if hit is None else hit[2].get("calls_to_first_cause_read")
    for cfg, ts, sfx, _v, s in reversed(getattr(evaluator, "records", [])):  # ZooEvaluator
        if ts == trial_seed and sfx == suffix and cfg == config:
            return s.get("calls_to_first_cause_read")
    return None


def run_graph_value_campaign(client: Client, store: ResultsStore, run_id: str, model: str, levels=(4, 8),
                             n_seeds: int = 10, tool_budget: int = 20, bug_distance: int = 3,
                             n_funcs: int | None = None, cost_cap: float = 2.0, parallelism: int = 4,
                             max_tokens: int = 16000, cancel_event: threading.Event | None = None,
                             protocol: str = "native", base: RepoParams | None = None,
                             context_window: int | None = None) -> GraphValueResult:
    """VI.graph_value: paired graph-on / graph-off episodes on identical
    repositories, with the §8 H4 statistics in the result's note."""
    ev = RepoFixEvaluator(client, model, store, run_id, cost_cap, base=base, max_tokens=max_tokens,
                          cancel_event=cancel_event, protocol=protocol, context_window=context_window)
    res = run_paired_graph(ev, levels, n_seeds, tool_budget, bug_distance, n_funcs, parallelism)
    res.note += f";spent=${ev.spent_usd:.4f}" + (";cost_cap_hit" if ev.capped else "")
    return res


__all__ = ["RepoFixEvaluator", "params_from_config", "task_seed", "run_repo_breaking_point_campaign",
           "run_graph_value_campaign", "run_paired_graph", "paired_graph_stats", "mcnemar_exact",
           "wilcoxon_signed_rank_less", "default_repo_navigate_ray", "default_repo_lookalike_ray",
           "default_repo_clone_ray", "default_repo_budget_ray", "default_repo_control_ray", "GraphValueResult"]
