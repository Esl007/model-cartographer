"""Profile one model across every province, end to end.

Each STAGE is one campaign with its own run id (named so `frontier_summary`
picks it up) and its own hard cost cap. Stages run as separate processes in
parallel; each prints one `DONE <stage> <json>` line.

  python3 scripts/run_all_provinces.py --model google/gemma-3-27b-it --label gemma --smoke
  python3 scripts/run_all_provinces.py --model google/gemma-3-27b-it --label gemma
  python3 scripts/run_all_provinces.py --model ... --label ... --stage code_traps   # one stage

`--smoke` runs ONE trial of each province's evaluator (cents) and reports
verdicts, so a format or protocol problem is caught before the full run.
The model must have an entry in `cartographer/exec/pricing.py`, or cost caps
cannot bind (unknown models are priced at $0) -- the script refuses to run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cartographer.exec.pricing import PRICES  # noqa: E402

DB = "results/campaign.sqlite"

# stage: (run-id template, cost cap $, description)
STAGES = {
    "narrative": ("camp-full_map-{label}-001", 1.00, "I: state, order, belief, adversarial belief, breaking point"),
    "tool_depth": ("camp-tool-depth-{label}-003", 0.30, "II: plan length 7->19 calls x look-alike names"),
    "tool_deep": ("camp-tool-deep-{label}-001", 0.30, "II: plan length 7->31 calls, look-alike names on"),
    "tool_traps": ("camp-tool_bp-{label}-001", 0.30, "II: structural trap ray"),
    "tool_catalog": ("camp-tool-confuse-{label}-001", 0.20, "II: catalog size 8->66 plausible tools"),
    "tool_impossible": ("camp-irrelevance-{label}-001", 0.20, "II: impossible tasks (should decline)"),
    "interactive": ("camp-interactive-{label}-001", 0.40, "III: step-by-step execution x value passing"),
    "interactive_lookalike": ("camp-interactive-lookalike-{label}-001", 0.40, "III: step-by-step, look-alike names"),
    "interactive_fault": ("camp-interactive-fault-{label}-001", 0.30, "III: transient failure mid-plan"),
    "code_lookalike": ("camp-code-lookalike-{label}-001", 0.30, "IV: program length x look-alike variable names"),
    "code_traps": ("camp-code-traps-{label}-001", 0.30, "IV: six semantic traps"),
    "code_length": ("camp-code-length-{label}-001", 0.30, "IV: control -- length and nesting only"),
}


def _client():
    from cartographer.exec.client import NEBIUS, Client
    from cartographer.exec.keys import load_keys

    return Client(NEBIUS, load_keys()["nebius"])


def _summary(obj) -> dict:
    if hasattr(obj, "to_json_dict"):
        d = obj.to_json_dict()
    elif hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict

        d = asdict(obj)
    else:
        d = {"repr": repr(obj)}
    keep = ("found", "t_star", "t_ci", "degradation_onset", "config_at_break", "config_at_degradation",
            "pass_curve", "note", "stages", "n_trials", "balanced_accuracy", "accuracy")
    return {k: d[k] for k in keep if k in d} or d


def run_stage(stage: str, model: str, label: str, protocol: str = "native") -> dict:
    from cartographer.exec import evaluator as E
    from cartographer.interactive.campaign import default_interactive_ray, run_interactive_breaking_point_campaign
    from cartographer.schedule.escalate import EscalationRay, RayDim
    from cartographer.store.store import ResultsStore

    tmpl, cap, _ = STAGES[stage]
    run_id = tmpl.format(label=label)
    client, store = _client(), ResultsStore(DB)
    try:
        if stage == "narrative":
            res = E.run_full_map_campaign(client, store, run_id, model, budget=600, cost_cap=cap, parallelism=6)
        elif stage == "tool_depth":
            ray = EscalationRay(dims=(RayDim("plan_depth", 4, 16, True), RayDim("id_collisions_on", 0, 1, True)))
            res = E.run_tool_breaking_point_campaign(client, store, run_id, model, budget=250, cost_cap=cap,
                                                     ray=ray, parallelism=6)
        elif stage == "tool_deep":
            ray = EscalationRay(dims=(RayDim("plan_depth", 4, 28, True), RayDim("id_collisions_on", 1, 1, True)))
            res = E.run_tool_breaking_point_campaign(client, store, run_id, model, budget=250, cost_cap=cap,
                                                     ray=ray, parallelism=6)
        elif stage == "tool_traps":
            res = E.run_tool_breaking_point_campaign(client, store, run_id, model, budget=250, cost_cap=cap,
                                                     parallelism=6)
        elif stage == "tool_catalog":
            res = E.run_tool_breaking_point_campaign(client, store, run_id, model, budget=120, cost_cap=cap,
                                                     ray=E.default_tool_confusability_ray(), parallelism=6)
        elif stage == "tool_impossible":
            res = E.run_tool_irrelevance_campaign(client, store, run_id, model, n_trials=40, cost_cap=cap,
                                                  parallelism=6)
        elif stage == "interactive":
            res = run_interactive_breaking_point_campaign(client, store, run_id, model, ray=default_interactive_ray(),
                                                          budget=60, cost_cap=cap, k_trials=5, parallelism=4,
                                                          protocol=protocol)
        elif stage == "interactive_lookalike":
            ray = EscalationRay(dims=(RayDim("plan_depth", 4, 28, True), RayDim("id_collisions_on", 1, 1, True)))
            res = run_interactive_breaking_point_campaign(client, store, run_id, model, ray=ray, budget=45,
                                                          cost_cap=cap, k_trials=5, parallelism=4,
                                                          protocol=protocol)
        elif stage == "interactive_fault":
            ray = EscalationRay(dims=(RayDim("plan_depth", 4, 16, True), RayDim("value_links", 0.0, 1.0, False),
                                      RayDim("fault_step", 3, 10, True)))
            res = run_interactive_breaking_point_campaign(client, store, run_id, model, ray=ray, budget=60,
                                                          cost_cap=cap, k_trials=5, parallelism=4,
                                                          protocol=protocol)
        elif stage.startswith("code_"):
            ray = {"code_lookalike": E.default_code_lookalike_ray, "code_traps": E.default_code_trap_ray,
                   "code_length": E.default_code_length_ray}[stage]()
            res = E.run_code_breaking_point_campaign(client, store, run_id, model, ray=ray, budget=200,
                                                     cost_cap=cap, k_trials=7, parallelism=6)
        else:
            raise KeyError(stage)
        return {"run_id": run_id, **_summary(res)}
    finally:
        store.close()


def smoke(model: str, label: str) -> None:
    """One trial per province evaluator, written under `smoke-<label>-*` run ids."""
    from cartographer.exec import evaluator as E
    from cartographer.interactive.campaign import InteractiveEvaluator
    from cartographer.store.store import ResultsStore

    client, store = _client(), ResultsStore(DB)
    try:
        trials = {
            "I narrative (state)": lambda: E.PipelineEvaluator(client, model, store, f"smoke-{label}-state",
                                                               "state", 0.05)({"chain_length": 4}, 1),
            "II tool planning": lambda: E.ToolBreakingPointEvaluator(client, model, store, f"smoke-{label}-tool",
                                                                     0.05)({"plan_depth": 4}, 1),
            "III interactive": lambda: InteractiveEvaluator(client, model, store, f"smoke-{label}-interactive",
                                                            0.05)({"plan_depth": 4, "value_links": 1.0}, 1),
            "IV code tracing": lambda: E.CodeTraceEvaluator(client, model, store, f"smoke-{label}-code",
                                                            0.05)({"n_statements": 10}, 1),
        }
        for name, fn in trials.items():
            t = time.time()
            try:
                ok = fn()
                print(f"SMOKE {name}: {'PASS' if ok else 'not pass'} ({time.time() - t:.1f}s)", flush=True)
            except Exception as e:  # noqa: BLE001 - report every province, don't stop at the first
                print(f"SMOKE {name}: EXCEPTION {type(e).__name__}: {e}", flush=True)
        for prefix in ("state", "tool", "interactive", "code"):
            for r in store.by_run(f"smoke-{label}-{prefix}"):
                print(f"   {prefix:12} {r.verdict.value:6} finish={r.finish_reason} out_tokens={r.tokens_out} "
                      f"${r.cost_usd:.5f} {(r.verifier_detail or '')[:110]}", flush=True)
    finally:
        store.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", required=True, help="short tag used in run ids, e.g. gemma")
    ap.add_argument("--stage", help="run one stage in this process")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--skip", default="", help="comma-separated stages to leave out")
    ap.add_argument("--only", default="", help="comma-separated stages to run (default: all)")
    ap.add_argument("--text-tools", action="store_true",
                    help="Province III: also accept tool calls written as text (models without native calling)")
    args = ap.parse_args()
    if args.model not in PRICES:
        sys.exit(f"{args.model} has no entry in cartographer/exec/pricing.py; cost caps would not bind.")
    if args.smoke:
        smoke(args.model, args.label)
        return
    if args.stage:
        t = time.time()
        out = run_stage(args.stage, args.model, args.label, "text_fallback" if args.text_tools else "native")
        out["minutes"] = round((time.time() - t) / 60, 1)
        print(f"DONE {args.stage} {json.dumps(out, default=str)}", flush=True)
        return
    skip = {x for x in args.skip.split(",") if x}
    unknown = skip - set(STAGES)
    if unknown:
        sys.exit(f"unknown stages: {sorted(unknown)}")
    only = {x for x in args.only.split(",") if x}
    stages = [st for st in STAGES if st not in skip and (not only or st in only)]
    print(f"Launching {len(stages)} stages for {args.model}; total cost ceiling "
          f"${sum(STAGES[st][1] for st in stages):.2f}", flush=True)
    os.makedirs("results/logs", exist_ok=True)
    procs = []
    for stage in stages:
        log = open(f"results/logs/{args.label}-{stage}.log", "w")
        cmd = [sys.executable, __file__, "--model", args.model, "--label", args.label, "--stage", stage]
        if args.text_tools:
            cmd.append("--text-tools")
        procs.append((stage, subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)))
    for stage, p in procs:
        p.wait()
        print(f"stage {stage} exited {p.returncode}", flush=True)


if __name__ == "__main__":
    main()
