"""The Evidence Fidelity Campaign (docs/spec-evidence-fidelity-campaign.md):
a paired condition grid over existing provinces, not a new province.

Every task comes from an existing generator through an existing evaluator.
The campaign contributes the condition arms -- A agree, B conflict, C salience
(the critical fact restated before the decision point), D presentation (the
fact made louder where it already is), plus the transfer arm X and Province
IV's exploratory E -- all on the SAME trial seed (run suffixes `/A`, `/B`, ...
on one run id), and the paired statistics that compare them.

Pairing: every condition key is either an existing dim named in the family's
`pair_keys` (left out of the task seed by the evaluator) or a new campaign key
that no task seed ever includes; so all arms of one trial seed see one world /
program / repository. Primary response = the family's mechanism-signature
rate per scored episode; secondary = pass rate. Paired tests: exact McNemar
on pass, Wilcoxon signed-rank (two-sided) on the signature, Holm within the
family. Optional resample gate on every FAIL (reruns under `/<arm>/verify`;
a FAIL most of whose valid reruns pass is scored PASS, one with no valid
rerun is `unresolved` and leaves the analysis).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from typing import Callable

from ..confirm.stats import wilson_interval
from ..repo.evaluator import mcnemar_exact, wilcoxon_signed_rank_less
from ..store.schema import Verdict

ARM_ORDER = ("A", "B", "C", "D", "X", "E")
ARM_MEANING = {
    "A": "agree (the prior's answer is correct)",
    "B": "conflict (evidence contradicts the prior)",
    "C": "salience (the fact restated before the decision point)",
    "D": "presentation (the fact flagged in place, nothing added)",
    "X": "transfer (the OTHER family's kind of C)",
    "E": "prior emphasis (exploratory, Province IV only)",
}


def _detail(r) -> dict:
    out = {}
    for part in (r.verifier_detail or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out.setdefault(k, v)
    return out


# -- per-family mechanism signatures (value per scored episode) -------------------------------------

def _sig_belief(r) -> float:
    if r.verdict != Verdict.FAIL:
        return 0.0
    from ..report.verdict import classify_belief_failure

    return 1.0 if classify_belief_failure(r) == "final_true_state" else 0.0


def _sig_tools(r) -> float:
    return 1.0 if r.verdict == Verdict.FAIL and _detail(r).get("detail", "").startswith("unknown_tool") else 0.0


def _sig_tickets(r) -> float:
    return float((r.realized_dims or {}).get("fabricated_tickets", 0.0))


def _sig_comments(r) -> float:
    return 1.0 if r.verdict == Verdict.FAIL and "comment_value" in _detail(r).get("detail", "") else 0.0


_REPAIR_SIGS = ("wrong_site", "wrong_site_file", "visible_fixed_hidden_broken")


def _sig_repair(r) -> float:
    return 1.0 if r.verdict == Verdict.FAIL and _detail(r).get("detail") in _REPAIR_SIGS else 0.0


# -- families -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Family:
    key: str
    province: str
    mechanism: str  # "info" | "causal" | "dual" (§2's candidate assignment, under test)
    carriage: str  # "carried" | "in_view" (§5 H-EF1 grouping)
    signature_name: str
    base: dict
    arms: dict  # arm -> extra config, or None where the arm is N/A by construction
    signature: Callable = field(repr=False, default=None)
    record_kind: str = ""


FAMILIES: dict[str, Family] = {
    "belief": Family(
        "belief", "I", "dual", "carried", "final_true_state",
        base={"chain_length": 8.0, "witness_cutoff": 0.0},
        arms={"A": {"full_witness": 1.0}, "B": {}, "C": {"witness_restatement": 1.0},
              "D": {"witness_emphasis": 1.0}},
        signature=_sig_belief, record_kind="belief"),
    "tools": Family(
        "tools", "II", "info", "carried", "unknown_tool",
        base={"plan_depth": 10.0},
        arms={"A": {}, "B": {"id_collisions_on": 1.0},
              "C": {"id_collisions_on": 1.0, "restate_ids": 1.0},
              "D": {"id_collisions_on": 1.0, "ids_emphasis": 1.0}},
        signature=_sig_tools, record_kind="tool_plan"),
    "tickets": Family(
        "tickets", "III", "info", "carried", "fabricated_tickets",
        base={"plan_depth": 8.0, "ticket_entropy": 0.0, "carry_distance": 3.0, "n_live_tickets": 1.0},
        arms={"A": {"ticket_guessable": 1.0}, "B": {}, "C": {"restate_tickets": 1.0},
              "D": {"ticket_emphasis": 1.0}, "X": {"restate_chain": 1.0}},
        signature=_sig_tickets, record_kind="interactive"),
    "comments": Family(
        "comments", "IV", "info", "in_view", "comment_value",
        base={"n_statements": 12.0},
        arms={"A": {"neutral_comments": 2.0}, "B": {"misleading_comments": 2.0}, "C": None,
              "D": {"misleading_comments": 2.0, "line_emphasis": 1.0},
              "E": {"misleading_comments": 2.0, "prior_emphasis": 1.0}},
        signature=_sig_comments, record_kind="code_trace"),
    "repair": Family(
        "repair", "VI", "causal", "carried", "wrong_site*+visible_fixed_hidden_broken",
        base={"n_modules": 4.0, "bug_distance": 2.0, "tool_budget": 20.0},
        arms={"A": {"bug_local": 1.0}, "B": {}, "C": {"show_data_flow": 1.0}, "D": {"diff_emphasis": 1.0},
              "X": {"restate_symbols": 1.0}},
        signature=_sig_repair, record_kind="repo_fix"),
}
PAIR_KEYS = {"belief": ("full_witness",), "tools": ("id_collisions_on",),
             "comments": ("misleading_comments", "neutral_comments")}


def make_evaluator(family: str, client, model: str, store, run_id: str, cost_cap: float, protocol: str = "native",
                   on_task=None, **kwargs):
    """The family's EXISTING evaluator, with its pair keys and the zoo hook."""
    from ..exec import evaluator as E

    if family == "belief":
        return E.PipelineEvaluator(client, model, store, run_id, "belief", cost_cap, pair_keys=PAIR_KEYS[family],
                                   on_task=on_task, **kwargs)
    if family == "tools":
        return E.ToolBreakingPointEvaluator(client, model, store, run_id, cost_cap, pair_keys=PAIR_KEYS[family],
                                            on_task=on_task, **kwargs)
    if family == "comments":
        return E.CodeTraceEvaluator(client, model, store, run_id, cost_cap, pair_keys=PAIR_KEYS[family],
                                    on_task=on_task, **kwargs)
    if family == "tickets":
        from ..interactive.campaign import InteractiveEvaluator

        return InteractiveEvaluator(client, model, store, run_id, cost_cap, protocol=protocol, on_task=on_task,
                                    **kwargs)
    if family == "repair":
        from ..repo.evaluator import RepoFixEvaluator

        return RepoFixEvaluator(client, model, store, run_id, cost_cap, protocol=protocol, on_task=on_task, **kwargs)
    raise KeyError(family)


# -- realized covariates (§2): condition, evidence_span, evidence_turns ------------------------------------

ARM_INDEX = {a: float(i) for i, a in enumerate(ARM_ORDER)}
_cov = threading.local()


def _span_after(prompt: str, needles) -> float:
    """Estimated tokens (chars / 4) from the end of the LAST occurrence of any needle to the end of
    the prompt (the decision point of a single-shot task); -1 if no needle occurs."""
    ends = [prompt.rfind(n) + len(n) for n in needles if n and prompt.rfind(n) >= 0]
    return round((len(prompt) - max(ends)) / 4.0, 1) if ends else -1.0


def _covariates(family: str, kw: dict) -> dict:
    try:
        if family == "belief":
            from ..ablate.ablations import _split_story_sentences, pronoun_suppression
            from ..ablate.evidence import belief_evidence_sentences

            rendered, world = kw["rendered"], kw["world"]
            hits = belief_evidence_sentences(world, rendered)
            plain = _split_story_sentences(rendered.prompt)
            return {"evidence_span": _span_after(kw["prompt"], [plain[i] for i in hits]), "evidence_turns": 0.0}
        if family == "tools":
            from ..interactive.env import optimal_plan

            ids = [c.tool for c in (optimal_plan(kw["world"], max(kw["world"].budget, 1)) or [])]
            return {"evidence_span": _span_after(kw["prompt"], ids), "evidence_turns": 0.0}
        if family == "comments":
            lines = [ln for ln in kw["rendered"].source.splitlines() if "#" in ln]
            return {"evidence_span": _span_after(kw["prompt"], lines), "evidence_turns": 0.0}
        if family == "tickets":
            env = kw["env"]
            return {"evidence_span": -1.0, "evidence_turns": float(env.spec.carry_distance)}
        if family == "repair":
            t = kw["task"]
            return {"evidence_span": -1.0, "evidence_turns": float(t.bug_module != t.entry_module)}
    except Exception:  # a covariate must never break a trial
        pass
    return {"evidence_span": -1.0, "evidence_turns": -1.0}


def _chained_hook(family: str, inner):
    def on_task(**kw):
        _cov.values = _covariates(family, kw)
        if inner is not None:
            inner(**kw)
    return on_task


class _StampingStore:
    """Delegates to the real store; every appended record gains `condition` (from its run suffix) and
    the evidence covariates computed for the task this thread just built. Append-only stays true:
    the record is stamped BEFORE it is written, never rewritten."""

    def __init__(self, store, prefix: str) -> None:
        self._store, self._prefix = store, prefix

    def __getattr__(self, name):
        return getattr(self._store, name)

    def append(self, record) -> None:
        suffix = record.run_id.split(self._prefix + "/", 1)[-1] if self._prefix else record.run_id.rsplit("/", 2)[-1]
        arm = next((a for a in ARM_ORDER if suffix == a or suffix.startswith(a + "/")), None)
        if arm is None:
            parts = record.run_id.split("/")
            arm = next((p for p in parts if p in ARM_INDEX), None)
        extra = {"condition": ARM_INDEX.get(arm, -1.0), **getattr(_cov, "values", {})}
        _cov.values = {}
        self._store.append(replace(record, realized_dims={**(record.realized_dims or {}), **extra}))


# -- results --------------------------------------------------------------------------------------

@dataclass
class ArmStats:
    arm: str
    meaning: str
    n_records: int = 0
    n_scored: int = 0
    n_pass: int = 0
    n_error: int = 0
    n_unresolved: int = 0
    pass_rate: float | None = None
    pass_ci: tuple | None = None
    signature_mean: float | None = None
    signature_ci: tuple | None = None  # Wilson, when the signature is 0/1


@dataclass
class Contrast:
    arm: str
    vs: str
    pairs: int
    b: int  # pass in `vs`, fail in `arm`
    c: int  # fail in `vs`, pass in `arm`
    mcnemar_p: float
    signature_diff_mean: float | None
    wilcoxon_p: float
    holm_p_pass: float = 1.0
    holm_p_signature: float = 1.0


@dataclass
class FamilyResult:
    family: str
    province: str
    mechanism: str
    carriage: str
    signature_name: str
    run_id: str
    arms: dict = field(default_factory=dict)  # arm -> ArmStats
    contrasts: list = field(default_factory=list)
    control_ok: bool | None = None  # §5: A pass rate >= 0.8
    gate: dict = field(default_factory=dict)
    spent_usd: float = 0.0
    note: str = ""

    def to_json_dict(self) -> dict:
        d = asdict(self)
        return d


# -- statistics -----------------------------------------------------------------------------------

def wilcoxon_two_sided(diffs: list[float]) -> float:
    if not any(diffs):
        return 1.0
    lo = wilcoxon_signed_rank_less(diffs)
    hi = wilcoxon_signed_rank_less([-x for x in diffs])
    return min(1.0, 2 * min(lo, hi))


def holm(pvals: list[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values, in input order."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    out = [1.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvals[i]))
        out[i] = running
    return out


def _wilson(k: int, n: int):
    if not n:
        return None
    w = wilson_interval(k, n)
    return (round(w.low, 4), round(w.high, 4))


# -- the runner -------------------------------------------------------------------------------------

def _gate_outcome(store, run_id: str, prefix: str, arm: str, ts: int):
    """PASS (noise) / FAIL (stable) / None (unresolved) from a FAIL's reruns."""
    reruns = [r for r in store.by_run(f"{run_id}{prefix}/{arm}/verify") if r.trial_seed == ts]
    valid = [r for r in reruns if r.verdict != Verdict.ERROR]
    if not valid:
        return None
    passes = sum(r.verdict == Verdict.PASS for r in valid)
    return Verdict.PASS if passes * 2 > len(valid) else Verdict.FAIL


def summarize_family(store, run_id: str, family: str, prefix: str = "", gated: dict | None = None,
                     arms: tuple | None = None) -> FamilyResult:
    """Per-arm rates and paired contrasts (each arm vs B) from the store."""
    fam = FAMILIES[family]
    arms = tuple(a for a in (arms or ARM_ORDER) if a in fam.arms and fam.arms[a] is not None)
    res = FamilyResult(fam.key, fam.province, fam.mechanism, fam.carriage, fam.signature_name, run_id + prefix)
    per: dict[str, dict[int, tuple]] = {}  # arm -> trial seed -> (passed, signature) for scored episodes
    for arm in arms:
        recs = {}
        for r in store.by_run(f"{run_id}{prefix}/{arm}"):
            recs[r.trial_seed] = r  # the last record per seed
        st = ArmStats(arm, ARM_MEANING[arm], n_records=len(recs))
        scored: dict[int, tuple] = {}
        for ts, r in recs.items():
            if r.verdict == Verdict.ERROR:
                st.n_error += 1
                continue
            verdict = r.verdict
            if verdict == Verdict.FAIL and gated is not None and (arm, ts) in gated:
                verdict = gated[(arm, ts)]
                if verdict is None:
                    st.n_unresolved += 1
                    continue
            scored[ts] = (verdict == Verdict.PASS, fam.signature(r) if verdict == Verdict.FAIL else 0.0)
        st.n_scored = len(scored)
        st.n_pass = sum(1 for p, _ in scored.values() if p)
        if scored:
            st.pass_rate = round(st.n_pass / st.n_scored, 4)
            st.pass_ci = _wilson(st.n_pass, st.n_scored)
            sigs = [s for _, s in scored.values()]
            st.signature_mean = round(sum(sigs) / len(sigs), 4)
            if all(s in (0.0, 1.0) for s in sigs):
                st.signature_ci = _wilson(int(sum(sigs)), len(sigs))
        res.arms[arm] = st
        per[arm] = scored
    if "B" in per:
        for arm in arms:
            if arm == "B":
                continue
            seeds = sorted(set(per[arm]) & set(per["B"]))
            b = sum(1 for s in seeds if per["B"][s][0] and not per[arm][s][0])
            c = sum(1 for s in seeds if per[arm][s][0] and not per["B"][s][0])
            diffs = [per[arm][s][1] - per["B"][s][1] for s in seeds]
            res.contrasts.append(Contrast(arm, "B", len(seeds), b, c, round(mcnemar_exact(b, c), 6),
                                          round(sum(diffs) / len(diffs), 4) if diffs else None,
                                          round(wilcoxon_two_sided(diffs), 6)))
        adj = holm([x.mcnemar_p for x in res.contrasts] + [x.wilcoxon_p for x in res.contrasts])
        n = len(res.contrasts)
        for i, x in enumerate(res.contrasts):
            x.holm_p_pass, x.holm_p_signature = round(adj[i], 6), round(adj[n + i], 6)
    a = res.arms.get("A")
    res.control_ok = None if a is None or a.pass_rate is None else a.pass_rate >= 0.8
    res.note = "; ".join(
        f"{arm}: pass {st.n_pass}/{st.n_scored}, {fam.signature_name} {st.signature_mean}"
        + (f", {st.n_error} ERROR" if st.n_error else "") for arm, st in res.arms.items())
    return res


def run_ef_family(client, store, run_id: str, model: str, family: str, n_trials: int = 20, arms=None,
                  cost_cap: float = 1.0, parallelism: int = 4, protocol: str = "native",
                  resample_gate: bool = False, resample_n: int = 3, seed_base: int = 0, prefix: str = "",
                  on_task=None, evaluator=None, **evaluator_kwargs) -> FamilyResult:
    """Run `n_trials` paired seeds through every requested arm of `family`
    (default: all its non-N/A arms), then summarize from the store."""
    fam = FAMILIES[family]
    arms = tuple(a for a in (arms or ARM_ORDER) if a in fam.arms and fam.arms[a] is not None)
    ev = evaluator or make_evaluator(family, client, model, _StampingStore(store, prefix), run_id, cost_cap,
                                     protocol, _chained_hook(family, on_task), **evaluator_kwargs)
    jobs = [(arm, seed_base + i) for i in range(n_trials) for arm in arms]

    def config(arm):
        return {**fam.base, **fam.arms[arm]}

    def one(job):
        arm, ts = job
        ev(config(arm), ts, run_suffix=f"{prefix}/{arm}")

    with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
        list(pool.map(one, jobs))
    gated = None
    if resample_gate:
        fails = []
        for arm in arms:
            for r in store.by_run(f"{run_id}{prefix}/{arm}"):
                if r.verdict == Verdict.FAIL:
                    fails.append((arm, r.trial_seed))
        reruns = [(arm, ts) for arm, ts in dict.fromkeys(fails) for _ in range(resample_n)]

        def rerun(job):
            arm, ts = job
            ev(config(arm), ts, run_suffix=f"{prefix}/{arm}/verify")

        with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
            list(pool.map(rerun, reruns))
        gated = {(arm, ts): _gate_outcome(store, run_id, prefix, arm, ts) for arm, ts in dict.fromkeys(fails)}
    res = summarize_family(store, run_id, family, prefix, gated, arms)
    res.spent_usd = round(getattr(ev, "spent_usd", 0.0), 6)
    if gated is not None:
        res.gate = {"fails": len(gated), "noise": sum(1 for v in gated.values() if v == Verdict.PASS),
                    "stable": sum(1 for v in gated.values() if v == Verdict.FAIL),
                    "unresolved": sum(1 for v in gated.values() if v is None)}
    return res


# -- pre-registered hypotheses over several families (§5) ------------------------------------------------

def _contrast(res: FamilyResult, arm: str) -> Contrast | None:
    return next((c for c in res.contrasts if c.arm == arm), None)


def _rescues(c: Contrast | None, alpha: float = 0.05) -> bool:
    """A significant improvement over B: pass up (Holm) or signature down (Holm)."""
    if c is None or not c.pairs:
        return False
    better_pass = c.c > c.b and c.holm_p_pass < alpha
    better_sig = c.signature_diff_mean is not None and c.signature_diff_mean < 0 and c.holm_p_signature < alpha
    return better_pass or better_sig


def hypothesis_summary(results: dict) -> dict:
    """§5 verdicts at the sample in hand (families absent from `results` are 'not run')."""
    out: dict = {}
    drops = {}
    for k, r in results.items():
        a, b = r.arms.get("A"), r.arms.get("B")
        if a and b and a.pass_rate is not None and b.pass_rate is not None:
            cA = _contrast(r, "A")
            drops[k] = {"carriage": r.carriage, "A_minus_B": round(a.pass_rate - b.pass_rate, 4),
                        "significant": bool(cA and cA.b < cA.c and cA.holm_p_pass < 0.05)}
    carried = [d["A_minus_B"] for d in drops.values() if d["carriage"] == "carried"]
    in_view = [d["A_minus_B"] for d in drops.values() if d["carriage"] == "in_view"]
    out["H-EF1"] = {"per_family": drops,
                    "carried_mean_drop": round(sum(carried) / len(carried), 4) if carried else None,
                    "in_view_mean_drop": round(sum(in_view) / len(in_view), 4) if in_view else None,
                    "consistent": (bool(carried) and bool(in_view)
                                   and all(d["significant"] for d in drops.values() if d["carriage"] == "carried")
                                   and not any(d["significant"] for d in drops.values() if d["carriage"] == "in_view"))}
    out["H-EF2"] = {k: _rescues(_contrast(results[k], "C")) for k in ("tickets", "belief", "comments")
                    if k in results}
    out["H-EF3"] = {k: {"D_rescues": _rescues(_contrast(r, "D")), "C_rescues": _rescues(_contrast(r, "C"))}
                    for k, r in results.items()}
    if "tickets" in results and "repair" in results:
        t, rp = results["tickets"], results["repair"]
        t_info, t_causal = _rescues(_contrast(t, "C")), _rescues(_contrast(t, "X"))
        r_causal, r_info = _rescues(_contrast(rp, "C")), _rescues(_contrast(rp, "X"))
        verdict = ("two families" if (t_info and not t_causal and r_causal and not r_info) else
                   "one mechanism" if (t_info and t_causal and r_causal and r_info) else "unresolved")
        out["H-EF4"] = {"tickets_info_C": t_info, "tickets_causal_X": t_causal, "repair_causal_C": r_causal,
                        "repair_info_X": r_info, "verdict": verdict}
    out["control"] = {k: r.control_ok for k, r in results.items()}
    return out


# -- registry runners ----------------------------------------------------------------------------------

def _runner(family: str):
    def run(client, store, run_id: str, model: str, n_trials: int = 20, cost_cap: float = 1.0,
            parallelism: int = 4, resample_gate: bool = False, resample_n: int = 3, protocol: str = "native",
            context_window: int | None = None, max_tokens: int | None = None) -> FamilyResult:
        kw: dict = {}
        if max_tokens:
            kw["max_tokens"] = max_tokens
        if context_window and family == "repair":
            kw["context_window"] = context_window
        return run_ef_family(client, store, run_id, model, family, n_trials=n_trials, cost_cap=cost_cap,
                             parallelism=parallelism, protocol=protocol, resample_gate=resample_gate,
                             resample_n=resample_n, **kw)
    run.__name__ = f"run_ef_{family}"
    run.__doc__ = f"Evidence Fidelity Campaign, {family} family: arms {', '.join(a for a, v in FAMILIES[family].arms.items() if v is not None)} on paired seeds."
    return run


run_ef_belief = _runner("belief")
run_ef_tools = _runner("tools")
run_ef_tickets = _runner("tickets")
run_ef_comments = _runner("comments")
run_ef_repair = _runner("repair")


@dataclass
class TransferResult:
    tickets: FamilyResult
    repair: FamilyResult
    h_ef4: dict

    def to_json_dict(self) -> dict:
        return {"tickets": self.tickets.to_json_dict(), "repair": self.repair.to_json_dict(), "H-EF4": self.h_ef4,
                "note": f"H-EF4 {self.h_ef4.get('verdict')}"}


def run_ef_transfer(client, store, run_id: str, model: str, n_trials: int = 20, cost_cap: float = 2.0,
                    parallelism: int = 4, resample_gate: bool = False, resample_n: int = 3,
                    protocol: str = "native", context_window: int | None = None) -> TransferResult:
    """H-EF4: each family's own-kind C and the other family's kind (X), vs B, on paired seeds."""
    t = run_ef_family(client, store, run_id, model, "tickets", n_trials, ("B", "C", "X"), cost_cap / 2, parallelism,
                      protocol, resample_gate, resample_n, prefix="/tickets")
    kw = {"context_window": context_window} if context_window else {}
    r = run_ef_family(client, store, run_id, model, "repair", n_trials, ("B", "C", "X"), cost_cap / 2, parallelism,
                      protocol, resample_gate, resample_n, prefix="/repair", **kw)
    return TransferResult(t, r, hypothesis_summary({"tickets": t, "repair": r}).get("H-EF4", {}))


__all__ = ["FAMILIES", "ARM_ORDER", "Family", "FamilyResult", "run_ef_family", "summarize_family",
           "hypothesis_summary", "holm", "wilcoxon_two_sided", "run_ef_belief", "run_ef_tools", "run_ef_tickets",
           "run_ef_comments", "run_ef_repair", "run_ef_transfer", "make_evaluator"]
