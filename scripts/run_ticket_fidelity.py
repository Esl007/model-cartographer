"""Copy-fidelity breaking point (docs/spec-ticket-fabrication.md): run the
ticket rays, resample-gated, then attribute confirmed failures with the
Part B ablations under Holm-Bonferroni.

  python3 scripts/run_ticket_fidelity.py --provider modal --model gemma-4-31b-it-qat-w4a16-ct \\
      --label gemma4qat --ray all --warm --stop-modal-app gemma4-qat
  python3 scripts/run_ticket_fidelity.py --provider openrouter --model google/gemma-4-31b-it:free \\
      --label gemma4bf16 --ray mechanism              # the bf16 contrast arm (slow: ~20 req/min)
  python3 scripts/run_ticket_fidelity.py ... --smoke  # one episode at each end of each ray, no gate

Rays, in the spec's order: `length` (rival-explanation control) is listed
first because the spec says to rule out "it's just long" before believing a
salience story; `mechanism` (ticket form x carry distance); `trap` (decoys
x live tickets). Each ray gets its own run id `camp-ticket-<ray>-<label>-NNN`
(next free NNN), its own cost cap, and gate reruns under `<run_id>/verify`;
ablation reruns go under `<run_id>/ablate`.

Guardrails: refuses a model with no pricing entry (cost caps would not
bind); with `--warm`, polls the endpoint's /models until HTTP 200 before any
episode (a cold scale-to-zero endpoint once scored startup timeouts as
failures); with `--stop-modal-app NAME`, runs `modal app stop NAME --yes` at
the end, even on error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cartographer.exec.pricing import PRICES  # noqa: E402

DB = "results/campaign.sqlite"
RAY_ORDER = ("length", "mechanism", "trap")


def make_client(provider_name: str):
    from cartographer.exec import client as C
    from cartographer.exec.keys import load_keys

    keys = load_keys()
    if provider_name == "modal":
        if "modal_url" not in keys:
            sys.exit("no `modal <key> <url>` line in the env/ key file (see scripts/modal_serve_gemma4.py)")
        return C.Client(C.modal_provider(keys["modal_url"]), keys["modal"]), keys["modal_url"], keys["modal"]
    provider = {"nebius": C.NEBIUS, "groq": C.GROQ, "openrouter": getattr(C, "OPENROUTER", None)}[provider_name]
    if provider is None or provider_name not in keys:
        sys.exit(f"provider {provider_name!r} is not configured")
    return C.Client(provider, keys[provider_name]), provider.base_url, keys[provider_name]


def warm(base_url: str, key: str, timeout_s: int = 900) -> None:
    url = base_url.rstrip("/") + "/models"
    t0 = time.time()
    while True:
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}",
                                                       "User-Agent": "model-cartographer/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status == 200:
                    print(f"endpoint warm after {time.time() - t0:.0f}s", flush=True)
                    return
        except Exception as e:  # noqa: BLE001 - cold start: keep polling
            last = e
        if time.time() - t0 > timeout_s:
            sys.exit(f"endpoint not warm after {timeout_s}s: {last}")
        time.sleep(10)


def next_run_id(ray: str, label: str, db: str = DB) -> str:
    import sqlite3

    rx = re.compile(rf"^camp-ticket-{ray}-{re.escape(label)}-(\d+)$")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        nums = [int(m.group(1)) for (rid,) in con.execute("select distinct run_id from experiments")
                if (m := rx.match(rid))]
    finally:
        con.close()
    return f"camp-ticket-{ray}-{label}-{max(nums, default=0) + 1:03d}"


def attribute_run(client, store, model, run_id, ray, protocol, n_cases, cap) -> list[dict]:
    """Attribute up to `n_cases` failing tasks (highest-difficulty first) with
    the Part B ablations. Configs are rebuilt from the records' latent params
    and checked against the stored task seed, so the reruns see the same task."""
    from cartographer.ablate.interactive import attribute_ticket_failure
    from cartographer.exec.evaluator import _task_seed
    from cartographer.interactive.campaign import InteractiveEvaluator
    from cartographer.store.schema import Verdict

    ev = InteractiveEvaluator(client, model, store, run_id + "/ablate", cap, protocol=protocol)
    names = [d.name for d in ray.dims]
    fails = [r for r in store.by_run(run_id) if r.verdict == Verdict.FAIL]
    fails.sort(key=lambda r: tuple(-r.latent_params.get(n, 0.0) for n in names))
    out, seen = [], set()
    for r in fails:
        if len(out) >= n_cases or ev.capped:
            break
        config = {n: r.latent_params[n] for n in names if n in r.latent_params}
        key = (tuple(sorted(config.items())), r.trial_seed)
        if key in seen or _task_seed(config, r.trial_seed) != r.task_seed:
            continue
        seen.add(key)
        out.append(attribute_ticket_failure(ev, config, r.trial_seed).to_json_dict())
    return out


def main() -> None:
    from cartographer.interactive.campaign import TICKET_RAYS, InteractiveEvaluator, run_ticket_fidelity_campaign
    from cartographer.store.store import ResultsStore

    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="modal", choices=["modal", "openrouter", "nebius", "groq"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--ray", default="all", choices=["all", *RAY_ORDER])
    ap.add_argument("--budget", type=int, default=60)
    ap.add_argument("--cap", type=float, default=0.75, help="cost cap per ray (tracked $)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--parallelism", type=int, default=4)
    ap.add_argument("--protocol", default="text_fallback", choices=["text_fallback", "native"])
    ap.add_argument("--no-gate", action="store_true", help="skip the resample gate (not for reported runs)")
    ap.add_argument("--attribute", type=int, default=3, help="failing tasks to attribute per ray (0 = none)")
    ap.add_argument("--attribute-cap", type=float, default=0.25)
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--warm", action="store_true", help="poll /models until 200 before starting")
    ap.add_argument("--stop-modal-app", default="", help="`modal app stop NAME --yes` when done")
    args = ap.parse_args()
    if args.model not in PRICES:
        sys.exit(f"{args.model} has no entry in cartographer/exec/pricing.py; cost caps would not bind.")

    client, base_url, key = make_client(args.provider)
    rays = RAY_ORDER if args.ray == "all" else (args.ray,)
    store = ResultsStore(DB)
    results: dict = {}
    try:
        if args.warm:
            warm(base_url, key)
        for name in rays:
            ray = TICKET_RAYS[name]()
            if args.smoke:
                ev = InteractiveEvaluator(client, args.model, store, f"smoke-ticket-{name}-{args.label}", 0.05,
                                          protocol=args.protocol)
                for t in (0.0, 1.0):
                    ok = ev(ray.config_at(t), 1)
                    rec = store.by_run(f"smoke-ticket-{name}-{args.label}")[-1]
                    print(f"SMOKE {name} t={t}: {'PASS' if ok else rec.verdict.name} {rec.verifier_detail[:180]}",
                          flush=True)
                continue
            run_id = next_run_id(name, args.label)
            t0 = time.time()
            print(f"== {name}: {run_id}", flush=True)
            bp = run_ticket_fidelity_campaign(
                client, store, run_id, args.model, ray=ray, budget=args.budget, cost_cap=args.cap,
                k_trials=args.k, parallelism=args.parallelism, protocol=args.protocol,
                resample_gate=not args.no_gate)
            res = {k: v for k, v in bp.to_json_dict().items()
                   if k in ("found", "t_star", "t_ci", "degradation_onset", "config_at_break",
                            "config_at_degradation", "pass_curve", "hypothesis", "confirmation", "note")}
            res["minutes"] = round((time.time() - t0) / 60, 1)
            if args.attribute:
                res["attribution"] = attribute_run(client, store, args.model, run_id, ray, args.protocol,
                                                   args.attribute, args.attribute_cap)
            results[run_id] = res
            print(f"DONE {name} {json.dumps(res, default=str)}", flush=True)
    finally:
        store.close()
        if args.stop_modal_app:
            subprocess.run(["modal", "app", "stop", args.stop_modal_app, "--yes"], check=False)
        if results:
            os.makedirs("results/logs", exist_ok=True)
            path = f"results/logs/ticket-{args.label}-{int(time.time())}.json"
            with open(path, "w") as f:
                json.dump(results, f, indent=1, default=str)
            print(f"results written to {path}", flush=True)


if __name__ == "__main__":
    main()
