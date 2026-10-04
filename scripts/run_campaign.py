"""Run any registered campaign by id (or legacy name) at an evidence tier.

  python3 scripts/run_campaign.py --list
  python3 scripts/run_campaign.py --campaign II.traps --tier standard --model google/gemma-3-27b-it --label gemma
  python3 scripts/run_campaign.py --campaign tool_traps ...          # legacy names still resolve
  python3 scripts/run_campaign.py --all --tier scout --model ... --label ...   # every axis/rate campaign

Campaign ids are `<province>.<stressor>` (see docs/campaign-catalog.md);
`--tier` is the single evidence dial (scout / standard / rigorous, see
cartographer/campaigns/tiers.py). Each run prints one `DONE <id> <json>`
line. The model must have an entry in `cartographer/exec/pricing.py`, or
cost caps cannot bind (unknown models are priced at $0) -- the script refuses
to run. `--dry-run` prints the run id and kwargs without building a client.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cartographer.campaigns.registry import REGISTRY, by_province, next_run_id, resolve  # noqa: E402
from cartographer.campaigns.tiers import TIERS, apply_tier  # noqa: E402
from cartographer.exec.pricing import PRICES  # noqa: E402

DB = "results/campaign.sqlite"


def _client(provider_name: str = "nebius"):
    # Copied from scripts/run_all_provinces.py (owned by another session; not imported).
    from cartographer.exec.client import GROQ, NEBIUS, OPENROUTER, Client, modal_provider
    from cartographer.exec.keys import load_keys

    keys = load_keys()
    if provider_name == "modal":
        # Self-hosted Gemma 4 QAT endpoint (scripts/modal_serve_gemma4.py);
        # key file line: `modal <endpoint key> <https://...modal.run/v1>`.
        if "modal_url" not in keys:
            sys.exit("no `modal <key> <url>` line in the env/ key file; deploy "
                     "scripts/modal_serve_gemma4.py first and add one.")
        return Client(modal_provider(keys["modal_url"]), keys["modal"])
    provider = {"nebius": NEBIUS, "groq": GROQ, "openrouter": OPENROUTER}[provider_name]
    return Client(provider, keys[provider_name])


def _summary(obj) -> dict:
    if hasattr(obj, "to_json_dict"):
        d = obj.to_json_dict()
    elif hasattr(obj, "__dataclass_fields__"):
        from dataclasses import asdict

        d = asdict(obj)
    else:
        d = {"repr": repr(obj)}
    keep = ("found", "t_star", "t_ci", "degradation_onset", "config_at_break", "config_at_degradation",
            "pass_curve", "note", "stages", "n_trials", "balanced_accuracy", "accuracy",
            "pass1", "pass_k", "wilson_pass1", "wilson_passk", "flaky_tasks", "side_file")
    return {k: d[k] for k in keep if k in d} or d


def list_table() -> str:
    rows = [("id", "prov", "title", "question", "legacy aliases")]
    for specs in by_province().values():
        for s in specs:
            rows.append((s.id, s.province, s.title, s.question, ", ".join(s.legacy_aliases) or "-"))
    w = [max(len(r[i]) for r in rows[:]) if i < 3 else 0 for i in range(5)]
    out = []
    for r in rows:
        out.append(f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:<{w[2]}}  [{r[4]}]\n{'':<{w[0] + w[1] + 4}}{r[3]}")
    return "\n".join(out)


def build_kwargs(spec, tier: str, text_tools: bool = False) -> dict:
    import inspect

    kwargs = apply_tier(spec, TIERS[tier], {})
    params = inspect.signature(spec.runner).parameters
    if spec.ray_factory is not None and "ray" in params:
        kwargs["ray"] = spec.ray_factory()
    if text_tools and "protocol" in params:
        kwargs["protocol"] = "text_fallback"
    return kwargs


def run_one(campaign: str, tier: str, model: str, label: str, provider: str, text_tools: bool,
            dry_run: bool = False, db: str = DB) -> dict:
    spec = resolve(campaign)
    run_id = next_run_id(spec, label, db)
    kwargs = build_kwargs(spec, tier, text_tools)
    t = TIERS[tier]
    head = {"campaign": spec.id, "run_id": run_id, "tier": tier, "reportable": t.reportable,
            "grade_ceiling": t.grade_ceiling}
    if spec.frontier_labels and label not in spec.frontier_labels:
        head["warning"] = (f"frontier.SPECS only picks up {spec.id} runs labelled "
                           f"{'/'.join(spec.frontier_labels)}; this run is stored but will not show on the "
                           "frontier page")
        print(f"WARNING {head['warning']}", file=sys.stderr, flush=True)
    if dry_run:
        return {**head, "kwargs": {k: (repr(v) if k == "ray" else v) for k, v in kwargs.items()}}
    from cartographer.store.store import ResultsStore

    client, store = _client(provider), ResultsStore(db)
    t0 = time.time()
    try:
        res = spec.runner(client, store, run_id, model, **kwargs)
    finally:
        store.close()
    return {**head, **_summary(res), "minutes": round((time.time() - t0) / 60, 1)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list every campaign id, question and legacy alias")
    ap.add_argument("--campaign", help="registry id (e.g. II.traps) or legacy alias (e.g. tool_traps)")
    ap.add_argument("--all", action="store_true", help="every axis/rate campaign, one process each")
    ap.add_argument("--tier", default="standard", choices=list(TIERS))
    ap.add_argument("--model")
    ap.add_argument("--label", help="short tag used in run ids, e.g. gemma ([A-Za-z0-9_]+)")
    ap.add_argument("--provider", default="nebius", choices=["nebius", "modal", "openrouter", "groq"])
    ap.add_argument("--text-tools", action="store_true",
                    help="Province III: also accept tool calls written as text (models without native calling)")
    ap.add_argument("--dry-run", action="store_true", help="print run id + kwargs; no client, no spend")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    if args.list:
        print(list_table())
        return
    if not (args.campaign or args.all):
        ap.error("one of --list, --campaign or --all is required")
    if not args.model or not args.label:
        ap.error("--model and --label are required")
    if args.model not in PRICES:
        sys.exit(f"{args.model} has no entry in cartographer/exec/pricing.py; cost caps would not bind.")
    if not re.fullmatch(r"\w+", args.label):
        sys.exit(f"label {args.label!r} must be [A-Za-z0-9_]+ (frontier.SPECS matches labels with \\w+)")

    if args.campaign:
        try:
            spec = resolve(args.campaign)
        except KeyError as e:
            sys.exit(str(e))
        out = run_one(spec.id, args.tier, args.model, args.label, args.provider, args.text_tools,
                      args.dry_run, args.db)
        print(f"DONE {spec.id} {json.dumps(out, default=str)}", flush=True)
        return

    specs = [s for s in REGISTRY.values() if s.evidence_kind in ("axis", "rate")]
    caps = [build_kwargs(s, args.tier).get("cost_cap", 0.0) for s in specs]
    print(f"Launching {len(specs)} campaigns at tier {args.tier} for {args.model}; total cost ceiling "
          f"${sum(caps):.2f}", flush=True)
    os.makedirs("results/logs", exist_ok=True)
    procs = []
    for spec in specs:
        log = open(f"results/logs/{args.label}-{spec.id}.log", "w")
        cmd = [sys.executable, __file__, "--campaign", spec.id, "--tier", args.tier, "--model", args.model,
               "--label", args.label, "--provider", args.provider, "--db", args.db]
        if args.text_tools:
            cmd.append("--text-tools")
        if args.dry_run:
            cmd.append("--dry-run")
        procs.append((spec.id, subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)))
    for cid, p in procs:
        p.wait()
        print(f"campaign {cid} exited {p.returncode}", flush=True)


if __name__ == "__main__":
    main()
