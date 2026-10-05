"""Province IV failure hunt (docs/spec-code-hunt.md, WP-H): sample programs from a
config pool, rewrite the passes, resample-gate every fail, shrink the stable ones
and confirm them on fresh reruns.

  python3 scripts/run_hunt.py --provider modal --model gemma-4-31b-it-qat-w4a16-ct \\
      --label gemma4qat --budget 300 --cap 1.0 --warm --stop-modal-app gemma4-qat

Run id `camp-code-hunt-<label>-NNN` (next free NNN); gate reruns under
`<run_id>/verify`, shrink checks and confirmations under `<run_id>/shrink`;
minimal programs in `results/hunt/<run_id>.json`.

Guardrails (as scripts/run_ticket_fidelity.py): refuses a model with no
pricing entry (cost caps would not bind); `--warm` polls /models until HTTP 200
before the first trial; `--stop-modal-app NAME` runs `modal app stop NAME --yes`
at the end, even on error.
"""

from __future__ import annotations

import argparse
import dataclasses
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

# What exists in CodeDimParams today (all six traps, dialects, look-alike names,
# opaque literals, plain length/nesting, distractors).
TODAY_CONFIGS = [
    {"n_statements": 18, "aliasing_on": 1, "late_binding_on": 1, "shadowing_on": 1,
     "inclusive_range_trap_on": 1, "negative_modulo_on": 1, "mutable_default_on": 1},
    {"n_statements": 18, "dialect_rules": 2},
    {"n_statements": 18, "identifier_confusability": 1.0},
    {"n_statements": 18, "opaque_literal_on": 1},
    {"n_statements": 30, "max_nesting": 3},
    {"n_statements": 14, "aliasing_on": 1, "late_binding_on": 1, "identifier_confusability": 0.5},
    {"n_statements": 16, "max_nesting": 2, "inclusive_range_trap_on": 1, "negative_modulo_on": 1},
    {"n_statements": 20, "distractor_fraction": 0.4, "shadowing_on": 1, "mutable_default_on": 1},
]
# WP-G dials (docs/spec-code-hunt.md WP-G), used only if this checkout has them.
WPG_CONFIGS = [
    {"n_statements": 18, "stack_depth": 3, "while_on": 1},
    {"n_statements": 25, "chain_depth": 15},
    {"n_statements": 18, "sign_chain": 2, "index_math": 2},
]


def available_wpg_configs() -> list[dict]:
    """The WP-G configs whose every dial exists both as a CodeDimParams field
    and as an evaluator config key (otherwise `build_code_task` would silently
    drop it and the config would not be what it says)."""
    from cartographer.codeworld.model import CodeDimParams
    from cartographer.exec import evaluator as ev

    fields = {f.name for f in dataclasses.fields(CodeDimParams)}
    known = set(getattr(ev, "_CODE_KNOWN_KEYS", ()))
    out = []
    for cfg in WPG_CONFIGS:
        dials = [k for k in cfg if k != "n_statements"]
        if all(k in fields and k in known for k in dials):
            out.append(cfg)
    return out


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


def next_run_id(label: str, db: str = DB) -> str:
    import sqlite3

    rx = re.compile(rf"^camp-code-hunt-{re.escape(label)}-(\d+)(/.*)?$")
    nums: list[int] = []
    if os.path.exists(db):
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            nums = [int(m.group(1)) for (rid,) in con.execute("select distinct run_id from experiments")
                    if (m := rx.match(rid))]
        finally:
            con.close()
    return f"camp-code-hunt-{label}-{max(nums, default=0) + 1:03d}"


def load_configs(spec: str) -> list[dict]:
    if spec == "default":
        return TODAY_CONFIGS + available_wpg_configs()
    with open(spec) as f:
        configs = json.load(f)
    if not isinstance(configs, list) or not configs or not all(isinstance(c, dict) for c in configs):
        sys.exit(f"{spec}: expected a non-empty JSON list of config objects")
    return configs


def main() -> None:
    from cartographer.hunt.hunt import run_hunt
    from cartographer.store.store import ResultsStore

    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="modal", choices=["modal", "nebius", "openrouter", "groq"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--label", required=True, help="letters and digits only (the frontier row's run pattern)")
    ap.add_argument("--configs", default="default", help="`default` or a JSON file with a list of configs")
    ap.add_argument("--n-programs", type=int, default=40)
    ap.add_argument("--budget", type=int, default=300, help="hard cap on model trials")
    ap.add_argument("--cap", type=float, default=1.0, help="cost cap (tracked $)")
    ap.add_argument("--parallelism", type=int, default=4)
    ap.add_argument("--warm", action="store_true", help="poll /models until 200 before starting")
    ap.add_argument("--stop-modal-app", default="", help="`modal app stop NAME --yes` when done")
    args = ap.parse_args()
    if args.model not in PRICES:
        sys.exit(f"{args.model} has no entry in cartographer/exec/pricing.py; cost caps would not bind.")
    if not re.fullmatch(r"[A-Za-z0-9]+", args.label):
        sys.exit("--label must be letters and digits only")
    configs = load_configs(args.configs)
    print(f"config pool ({len(configs)}): {json.dumps(configs)}", flush=True)

    client, base_url, key = make_client(args.provider)
    store = ResultsStore(DB)
    try:
        if args.warm:
            warm(base_url, key)
        run_id = next_run_id(args.label)
        print(f"== hunt: {run_id}", flush=True)
        t0 = time.time()
        res = run_hunt(client, store, run_id, args.model, configs=configs, n_programs=args.n_programs,
                       budget_trials=args.budget, cost_cap=args.cap, parallelism=args.parallelism)
        summary = {k: v for k, v in res.to_json_dict().items() if k != "findings"}
        summary["n_findings"] = len(res.findings)
        summary["minutes"] = round((time.time() - t0) / 60, 1)
        print(f"DONE hunt {json.dumps(summary, default=str)}", flush=True)
        for i, f in enumerate(res.findings, 1):
            print(f"\n-- finding {i}: {f.kind} detail={f.detail} truth={f.truth!r} model={f.model_answer!r} "
                  f"stmts {f.n_statements_before}->{f.n_statements_after} confirm_pass_rate={f.confirm_pass_rate}",
                  flush=True)
            print(f.shrunk_source, flush=True)
    finally:
        store.close()
        if args.stop_modal_app:
            subprocess.run(["modal", "app", "stop", args.stop_modal_app, "--yes"], check=False)


if __name__ == "__main__":
    main()
