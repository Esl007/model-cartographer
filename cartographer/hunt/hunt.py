"""The failure hunt (docs/spec-code-hunt.md, WP-H 2-3).

    (a) sample `n_programs` programs from a config pool, k = 1 each;
    (b) for each PASS, apply every available meaning-preserving rewrite once;
    (c) every FAIL (base or rewrite) goes through the resample gate: the
        byte-identical prompt `resample_n` more times under `<run_id>/verify`;
        stable iff at most half of the reruns pass;
    (d) each stable failure is shrunk (`shrink.shrink`, ddmin over top-level
        statements), keeping a candidate iff it fails 2 of 2 reruns under
        `<run_id>/shrink`;
    (e) the shrunk program is confirmed on 3 fresh reruns (`<run_id>/shrink`,
        `confirm=1`).

Every trial is a `kind=code_trace` record with the same detail format as
`CodeTraceEvaluator` plus `;hunt=1;stage=base|rewrite:<name>|verify|shrink`.
Hard budget in trials and in tracked dollars; when either runs out the hunt
stops cleanly and unrun work is never counted (an incomplete gate is not a
stable failure). Deterministic given (configs, seeds, model answers).

`ProgramEvaluator` is a small sibling of `CodeTraceEvaluator` that takes an
explicit (program, rendered) pair -- the hunt evaluates programs the
generator did not produce (rewrites, shrunk candidates), which the
config -> program evaluator cannot express.
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from ..codeworld.generate import GenerationFailed
from ..codeworld.measure import measure as code_measure
from ..codeworld.model import TRAP_KEYS as CODE_TRAP_KEYS
from ..codeworld.model import Program
from ..codeworld.render import render as code_render
from ..codeworld.verify import verify_answer as code_verify_answer
from ..exec.evaluator import _code_latent, build_code_task, default_token_budget
from ..exec.pricing import cost_usd
from ..store.schema import ExperimentRecord, RenderMode, Verdict
from .rewrites import REWRITES, RewriteUnavailable
from .shrink import ShrinkAborted, shrink

DEFAULT_OUT_DIR = os.path.join("results", "hunt")
BASE_TRIAL_SEED = 700_000


def _jsonable(v):
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return repr(v)


@dataclass
class TrialOutcome:
    verdict: Verdict
    detail: str  # verifier signature, e.g. "foil:value_semantics" / "correct" / "cap_hit"
    parsed: object = None
    truth: object = None


class ProgramEvaluator:
    """One model call on an explicit (program, rendered) pair, persisted as a
    `kind=code_trace` record. Cost-cap / lock / error discipline copied from
    `CodeTraceEvaluator`; additionally a hard cap on trials. `evaluate`
    returns None (and writes nothing) when the budget or the cap is gone."""

    def __init__(self, client, model: str, store, cost_cap_usd: float, budget_trials: int,
                 max_tokens: int | None = None, max_tokens_retry: int | None = None) -> None:
        self.client, self.model, self.store = client, model, store
        self.cost_cap_usd, self.budget_trials = cost_cap_usd, budget_trials
        d_max, d_retry = default_token_budget(model)
        self.max_tokens = max_tokens if max_tokens is not None else d_max
        self.max_tokens_retry = max_tokens_retry if max_tokens_retry is not None else d_retry
        if self.max_tokens_retry <= self.max_tokens:
            self.max_tokens_retry = self.max_tokens * 2
        self.spent_usd = 0.0
        self.trials_used = 0
        self.call_count = 0
        self.error_count = 0
        self.capped = False
        self.out_of_budget = False
        self._lock = threading.Lock()

    def _reserve(self) -> bool:
        with self._lock:
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True
                return False
            if self.trials_used >= self.budget_trials:
                self.out_of_budget = True
                return False
            self.trials_used += 1
            return True

    @property
    def stopped(self) -> bool:
        return self.capped or self.out_of_budget

    def _chat(self, prompt: str):
        result = self.client.chat(model=self.model, prompt=prompt, max_tokens=self.max_tokens,
                                  max_tokens_retry=self.max_tokens_retry)
        cost = cost_usd(self.model, result.tokens_in, result.tokens_out)
        with self._lock:
            self.call_count += 1
            self.spent_usd += cost
        return result, cost

    def evaluate(self, program: Program, rendered, *, run_id: str, trial_seed: int, task_seed: int,
                 rendering_seed: int, stage: str, extra: str = "") -> TrialOutcome | None:
        if not self._reserve():
            return None
        params = program.params
        realized = code_measure(program)
        traps = ";".join(f"{k}={int(getattr(params, k, False))}" for k in (*CODE_TRAP_KEYS, "opaque_literal_on"))
        traps += f";dialect_rules={len(getattr(params, 'dialect', ()))}"
        detail = f"kind=code_trace;campaign=1;query={program.query.kind}"
        result, cost = self._chat(rendered.prompt)
        if result.error is not None:
            retry, retry_cost = self._chat(rendered.prompt)
            cost += retry_cost
            result = retry
        parsed = truth = None
        if result.error is not None:
            verdict, code = Verdict.ERROR, "api_error"
            detail += f";detail=api_error;error={result.error}"
        elif result.finish_reason == "length":
            verdict, code = Verdict.ERROR, "cap_hit"  # a truncated answer says nothing about the model
            detail += ";detail=cap_hit;cap_hit=1" + (";retry_exhausted=1" if result.retried else "")
        else:
            tv = code_verify_answer(result.text, program, rendered)
            verdict, code, parsed, truth = tv.verdict, tv.detail, tv.parsed, tv.truth
            detail += f";detail={tv.detail}"
            realized["n_foil_matches"] = float(len(tv.foil_matches))
            if tv.normalised_tuple:
                detail += ";normalised=tuple"
        detail += (
            f";n_stmts={int(realized['n_statements'])};trace_len={int(realized['trace_length'])}"
            f";traps={traps};confusability={getattr(params, 'identifier_confusability', 0.0):g}"
            f";hunt=1;stage={stage}{extra}"
        )
        latent = _code_latent(params)
        record = ExperimentRecord(
            run_id=run_id, model_name=self.model, latent_params=latent,
            realized_dims={**realized, **{k: latent[k] for k in (*CODE_TRAP_KEYS, "opaque_literal_on")
                                          if k in latent}},
            render_mode=RenderMode.TEMPLATE, task_seed=task_seed, trial_seed=trial_seed,
            rendering_seed=rendering_seed, prompt_text=rendered.prompt, response_text=result.text,
            verdict=verdict, verifier_detail=detail, tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            cost_usd=cost, latency_ms=result.latency_ms, reasoning_text=result.reasoning,
            finish_reason=result.finish_reason,
        )
        with self._lock:
            if verdict == Verdict.ERROR:
                self.error_count += 1
            self.store.append(record)
            if self.spent_usd >= self.cost_cap_usd:
                self.capped = True
        return TrialOutcome(verdict, code, parsed, truth)


@dataclass
class Finding:
    kind: str  # "base" | "rewrite:<name>"
    config: dict
    trial_seed: int
    source: str
    shrunk_source: str
    n_statements_before: int  # top-level statements
    n_statements_after: int
    truth: object
    model_answer: object
    detail: str  # verifier signature of the original failure, e.g. "foil:value_semantics"
    necessary_lines: list = field(default_factory=list)
    confirm_pass_rate: float | None = None
    confirmed: bool = False  # the shrunk program failed at least half of its confirmation reruns
    task_seed: int = 0
    rendering_seed: int = 0
    gate_pass_rate: float = 0.0
    shrink_checks: int = 0
    shrink_note: str = ""
    shrunk_truth: object = None

    def to_json_dict(self) -> dict:
        d = asdict(self)
        for k in ("truth", "model_answer", "shrunk_truth"):
            d[k] = _jsonable(d[k])
        return d

    @classmethod
    def from_json_dict(cls, d: dict) -> "Finding":
        return cls(**d)


@dataclass
class HuntResult:
    run_id: str
    model: str
    n_base: int = 0  # base programs scored (PASS or FAIL)
    n_base_fail: int = 0
    n_rewrite: int = 0  # rewrites scored
    n_rewrite_fail: int = 0
    n_stable: int = 0
    n_noise: int = 0
    n_generation_failed: int = 0
    findings: list = field(default_factory=list)
    spent_usd: float = 0.0
    trials_used: int = 0
    note: str = ""
    json_path: str | None = None

    def to_json_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "findings"}
        d["findings"] = [f.to_json_dict() for f in self.findings]
        return d

    @classmethod
    def from_json_dict(cls, d: dict) -> "HuntResult":
        d = dict(d)
        d["findings"] = [Finding.from_json_dict(f) for f in d.get("findings", [])]
        return cls(**d)


@dataclass
class _Task:
    index: int
    config: dict
    trial_seed: int
    task_seed: int
    program: Program
    rendering_seed: int
    kind: str = "base"

    @property
    def rendered(self):
        return code_render(self.program, self.rendering_seed)


def _necessary_lines(program: Program, rendered, sids) -> list[str]:
    from ..codeworld.render import _stmt_lines

    out = []
    by_sid = {s.sid: s for s in program.body}
    for sid in sids:
        try:
            out.append("\n".join(_stmt_lines(by_sid[sid], rendered.name_map, 0, {})))
        except Exception:  # noqa: BLE001 - a statement type this renderer cannot print alone
            out.append(f"<statement {sid}>")
    return out


def run_hunt(client, store, run_id: str, model: str, *, configs: list[dict], n_programs: int = 40,
             rewrites=tuple(REWRITES), budget_trials: int = 300, cost_cap: float = 1.0, resample_n: int = 3,
             shrink_checks: int = 40, parallelism: int = 4, max_tokens: int | None = None,
             out_dir: str | None = DEFAULT_OUT_DIR) -> HuntResult:
    if not configs:
        raise ValueError("configs must be non-empty")
    unknown = [r for r in rewrites if r not in REWRITES]
    if unknown:
        raise ValueError(f"unknown rewrites: {unknown}")
    ev = ProgramEvaluator(client, model, store, cost_cap, budget_trials, max_tokens=max_tokens)
    res = HuntResult(run_id=run_id, model=model)
    notes: list[str] = []

    def run_many(tasks: list[_Task], stage_of) -> list:
        def one(t: _Task):
            return ev.evaluate(t.program, t.rendered, run_id=run_id, trial_seed=t.trial_seed,
                               task_seed=t.task_seed, rendering_seed=t.rendering_seed, stage=stage_of(t))
        if parallelism <= 1:
            return [one(t) for t in tasks]
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            return list(pool.map(one, tasks))

    # (a) base programs
    base: list[_Task] = []
    for i in range(n_programs):
        config = dict(configs[i % len(configs)])
        trial_seed = BASE_TRIAL_SEED + i
        try:
            _, program, _, seed = build_code_task(config, trial_seed)
        except GenerationFailed:
            res.n_generation_failed += 1
            continue
        base.append(_Task(i, config, trial_seed, seed, program, seed))
    base_out = run_many(base, lambda t: "base")
    failures: list[tuple[_Task, TrialOutcome]] = []
    passes: list[_Task] = []
    for t, o in zip(base, base_out):
        if o is None or o.verdict == Verdict.ERROR:
            continue
        res.n_base += 1
        if o.verdict == Verdict.PASS:
            passes.append(t)
        else:
            res.n_base_fail += 1
            failures.append((t, o))

    # (b) rewrites of every base PASS
    rw_tasks: list[_Task] = []
    if not ev.stopped:
        for t in passes:
            for name in rewrites:
                try:
                    out = REWRITES[name](t.program, t.trial_seed)
                except RewriteUnavailable:
                    continue
                prog, rs = out if isinstance(out, tuple) else (out, t.rendering_seed)
                rw_tasks.append(_Task(t.index, t.config, t.trial_seed, t.task_seed, prog, rs,
                                      kind=f"rewrite:{name}"))
    rw_out = run_many(rw_tasks, lambda t: t.kind) if rw_tasks else []
    for t, o in zip(rw_tasks, rw_out):
        if o is None or o.verdict == Verdict.ERROR:
            continue
        res.n_rewrite += 1
        if o.verdict == Verdict.FAIL:
            res.n_rewrite_fail += 1
            failures.append((t, o))

    # (c)-(e) gate, shrink, confirm -- sequential, in a fixed order
    for t, o in failures:
        if ev.stopped:
            notes.append("budget or cost cap reached before every failure was gated")
            break
        rendered = t.rendered
        stage_tag = f";origin={t.kind}"
        gate = [ev.evaluate(t.program, rendered, run_id=run_id + "/verify", trial_seed=t.trial_seed,
                            task_seed=t.task_seed, rendering_seed=t.rendering_seed, stage="verify",
                            extra=stage_tag) for _ in range(resample_n)]
        if any(g is None for g in gate) or any(g.verdict == Verdict.ERROR for g in gate):
            notes.append(f"gate incomplete for {t.kind} program {t.index} (budget/cap or API error)")
            continue
        n_pass = sum(g.verdict == Verdict.PASS for g in gate)
        if resample_n and n_pass / resample_n > 0.5:
            res.n_noise += 1
            continue
        res.n_stable += 1

        def still_fails(cand: Program, t=t) -> bool:
            r = code_render(cand, t.rendering_seed)
            outs = []
            for _ in range(2):
                g = ev.evaluate(cand, r, run_id=run_id + "/shrink", trial_seed=t.trial_seed,
                                task_seed=t.task_seed, rendering_seed=t.rendering_seed, stage="shrink",
                                extra=stage_tag)
                if g is None:
                    raise ShrinkAborted("budget or cost cap reached")
                outs.append(g)
                if g.verdict != Verdict.FAIL:
                    return False
            return all(g.verdict == Verdict.FAIL for g in outs)

        sr = shrink(t.program, still_fails, max_checks=shrink_checks, rendering_seed=t.rendering_seed)
        shrunk_rendered = code_render(sr.program, t.rendering_seed)
        confirm = []
        for _ in range(3):
            g = ev.evaluate(sr.program, shrunk_rendered, run_id=run_id + "/shrink", trial_seed=t.trial_seed,
                            task_seed=t.task_seed, rendering_seed=t.rendering_seed, stage="shrink",
                            extra=stage_tag + ";confirm=1")
            if g is None:
                break
            confirm.append(g)
        scored = [g for g in confirm if g.verdict != Verdict.ERROR]
        rate = (sum(g.verdict == Verdict.PASS for g in scored) / len(scored)) if scored else None
        shrink_note = "aborted (budget)" if sr.aborted else ("max_checks reached" if sr.exhausted else "")
        if len(scored) < 3:
            shrink_note = (shrink_note + "; " if shrink_note else "") + f"confirmed on {len(scored)}/3 reruns"
        from ..codeworld.interpret import answer as _answer

        res.findings.append(Finding(
            kind=t.kind, config=t.config, trial_seed=t.trial_seed, source=rendered.source,
            shrunk_source=shrunk_rendered.source, n_statements_before=len(t.program.body),
            n_statements_after=len(sr.program.body), truth=o.truth, model_answer=o.parsed, detail=o.detail,
            necessary_lines=_necessary_lines(sr.program, shrunk_rendered, sr.necessary_sids),
            confirm_pass_rate=rate, confirmed=rate is not None and len(scored) == 3 and rate <= 0.5,
            task_seed=t.task_seed, rendering_seed=t.rendering_seed, gate_pass_rate=n_pass / max(resample_n, 1),
            shrink_checks=sr.checks, shrink_note=shrink_note, shrunk_truth=_answer(sr.program),
        ))

    res.spent_usd = round(ev.spent_usd, 6)
    res.trials_used = ev.trials_used
    if ev.out_of_budget:
        notes.insert(0, f"trial budget {budget_trials} reached")
    if ev.capped:
        notes.insert(0, f"cost cap ${cost_cap:g} reached")
    if res.n_generation_failed:
        notes.append(f"{res.n_generation_failed} program(s) failed to generate")
    res.note = "; ".join(dict.fromkeys(notes))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{run_id}.json")
        res.json_path = path
        with open(path, "w") as f:
            json.dump(res.to_json_dict(), f, indent=1, default=repr)
    return res


__all__ = ["Finding", "HuntResult", "ProgramEvaluator", "TrialOutcome", "run_hunt"]
