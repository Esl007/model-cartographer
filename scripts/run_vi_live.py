"""Province VI live session on the self-hosted Gemma 4 QAT endpoint (Modal),
inside a hard GPU-time budget (docs/province-vi-spec.md §10).

    python3 scripts/run_vi_live.py --hours 2.5 --log <file>          # the session
    python3 scripts/run_vi_live.py --watchdog --deadline <epoch> --pid <driver pid> --log <file>

Session: deploy scripts/modal_serve_gemma4.py, check the URL against the key
file, warm until /models answers, confirm one tiny chat, then run the stages
below with parallelism 4 and the text tool protocol. Every episode is
guarded by a per-episode wall cap and the session deadline (checked before
every turn), and the app is ALWAYS stopped in `finally`.

Watchdog: a separate process that stops the app at the hard deadline, or as
soon as the session process is gone while the app still runs.

Stages (sizes adapt to the measured episode time after the smoke):
  S0 smoke     VI.control at 1 and 4 files, 3 seeds          smoke-vi-<label>
  S1 control   1 / 4 / 8 files, n_funcs 8, cause 1 call away camp-repo-control-<label>-NNN
  S2 graph     paired graph off/on, 8 files, cause 3 away    camp-repo-graph-<label>-NNN
  S3 navigate  (cause, files) = (1,4), (4,9), (6,12)          camp-repo-navigate-<label>-NNN
No resample gate (rates and paired designs, not certified breaking points;
serving nondeterminism on this endpoint is documented, lab log 2026-10-06 E).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODAL = str(ROOT / ".venv/bin/modal")
APP = "gemma4-qat"
MODEL = "gemma-4-31b-it-qat-w4a16-ct"
LABEL = "gemma4qat"
DB = str(ROOT / "results/campaign.sqlite")
CONTEXT_WINDOW = 32768  # scripts/modal_serve_gemma4.py MAX_MODEL_LEN
PARALLELISM = 4
EPISODE_WALL_S = 25 * 60
# Per-turn output budget for tool turns (a call, or a whole small module in write_file, fits
# well inside 6k). The first smoke (16k, retry 32k) saw first turns run to the cap and the
# retry die in Modal's 303 polling after ~22 min.
TURN_TOKENS = 6000
TURN_RETRY_TOKENS = 12000
SMOKE_RUN = f"smoke-vi-{LABEL}-2"  # `smoke-vi-<label>` holds the pre-fix protocol-debug smoke
SMOKE_ONLY = False
GRAPH_LEVELS = (4, 8, 12)  # §8 H4: paired at n_modules in {4, 8, 12}


def log(fh, msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


def app_state() -> str:
    try:
        out = subprocess.run([MODAL, "app", "list"], capture_output=True, text=True, timeout=120).stdout
    except subprocess.TimeoutExpired:
        return "unknown"
    rows = [ln for ln in out.splitlines() if APP[:8] in ln]
    if any(("deployed" in ln) or ("initiali" in ln) or ("stopping" in ln) for ln in rows):
        return "deployed"  # any live app of this name counts; stopped history rows are ignored
    return "stopped" if rows else "absent"


def stop_app(fh) -> None:
    for attempt in range(3):
        r = subprocess.run([MODAL, "app", "stop", APP, "--yes"], capture_output=True, text=True, timeout=180)
        state = app_state()
        log(fh, f"app stop rc={r.returncode} state={state}")
        if state in ("stopped", "absent"):
            return
        time.sleep(20)


# -- watchdog -------------------------------------------------------------------------------------

def watchdog(deadline: float, pid: int, fh) -> None:
    log(fh, f"WATCHDOG up; hard deadline {time.strftime('%H:%M:%S', time.localtime(deadline))}, session pid {pid}")
    last_report = 0.0
    while True:
        alive = os.path.exists(f"/proc/{pid}")
        now = time.time()
        if now >= deadline:
            log(fh, "WATCHDOG hard deadline reached: stopping the app")
            stop_app(fh)
            break
        if not alive:
            state = app_state()
            log(fh, f"WATCHDOG session process gone; app {state}")
            if state == "deployed":
                stop_app(fh)
            break
        if now - last_report >= 600:
            log(fh, f"WATCHDOG app {app_state()}; {int((deadline - now) / 60)} min to hard deadline")
            last_report = now
        time.sleep(30)
    log(fh, f"WATCHDOG done; final app state {app_state()}")


# -- session --------------------------------------------------------------------------------------

def deploy_and_warm(fh, keys) -> None:
    from cartographer.exec.client import list_models, modal_provider

    r = subprocess.run([MODAL, "deploy", str(ROOT / "scripts/modal_serve_gemma4.py")], capture_output=True,
                       text=True, timeout=900, cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f"deploy failed rc={r.returncode}: {r.stderr[-400:]}")
    urls = re.findall(r"https://[\w.-]+\.modal\.run", r.stdout)
    want = urlparse(keys["modal_url"]).hostname
    if urls and urlparse(urls[-1]).hostname != want:
        raise RuntimeError(f"deployed host {urlparse(urls[-1]).hostname} != key file host {want}")
    log(fh, f"deployed; URL host matches key file: {bool(urls)}")
    prov = modal_provider(keys["modal_url"])
    t0 = time.time()
    while time.time() - t0 < 20 * 60:
        try:
            ids = list_models(prov, keys["modal"])
            log(fh, f"warm: /models answered after {int(time.time() - t0)} s: {ids}")
            return
        except RuntimeError as e:
            time.sleep(15)
    raise RuntimeError("endpoint not warm after 20 min")


def tiny_chat(fh, client) -> None:
    r = client.chat_turn(MODEL, [{"role": "user", "content": "Reply with the single word: ready"}], None,
                         max_tokens=16, temperature=0.0)
    log(fh, f"tiny chat: {r.text!r} finish={r.finish_reason} error={r.error}")
    if r.error:
        raise RuntimeError(f"tiny chat failed: {r.error}")


def run_jobs(fh, ev, jobs, name: str) -> dict:
    """jobs: [(config, trial_seed)]. Returns tallies."""
    t0 = time.time()
    done = [0]
    lock = threading.Lock()

    def one(job):
        cfg, ts = job
        ev(cfg, ts)
        key = ("", ts, tuple(sorted(cfg.items())))
        verdict, sig, m = ev.outcomes.get(key, (None, "missing", {}))
        with lock:
            done[0] += 1
            log(fh, f"{name} {done[0]}/{len(jobs)} cfg={ {k: v for k, v in cfg.items() if k != 'tool_budget'} } "
                    f"seed={ts} -> {getattr(verdict, 'name', verdict)} {sig} calls={m.get('n_calls')} "
                    f"cfcr={m.get('calls_to_first_cause_read')}")
        return verdict, sig

    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        res = list(pool.map(one, jobs))
    tally: dict = {}
    for v, sig in res:
        k = f"{getattr(v, 'name', v)}:{sig}"
        tally[k] = tally.get(k, 0) + 1
    wall = time.time() - t0
    log(fh, f"{name} done in {wall / 60:.1f} min: {tally}")
    return {"wall_s": wall, "n": len(jobs), "tally": tally,
            "errors": sum(n for k, n in tally.items() if k.startswith("ERROR"))}


def _fresh_run_id(spec, store) -> str:
    """The next `<template>-NNN` with no record under it, suffixed runs
    (`<id>/graph_on`, `<id>/verify`) included -- the registry's `next_run_id`
    matches exact ids only, and the paired graph study stores only suffixed ones."""
    import sqlite3

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        ids = {r[0] for r in con.execute("select distinct run_id from experiments")}
    finally:
        con.close()
    for n in range(1, 1000):
        rid = spec.run_id(LABEL, n)
        if not any(x == rid or x.startswith(rid + "/") for x in ids):
            return rid
    raise RuntimeError("no free run id")


def session(hours: float, fh, dry_run: bool) -> dict:
    from cartographer.campaigns.registry import REGISTRY, next_run_id
    from cartographer.exec.client import Client, modal_provider
    from cartographer.exec.keys import load_keys
    from cartographer.repo.evaluator import RepoFixEvaluator, run_paired_graph
    from cartographer.store.store import ResultsStore

    summary: dict = {"stages": {}}
    keys = load_keys()
    if "modal_url" not in keys:
        raise RuntimeError("no `modal <key> <url>` line in the key file")
    store = ResultsStore(DB)
    run_ids = {s: _fresh_run_id(REGISTRY[s], store) for s in ("VI.control", "VI.graph_value", "VI.navigate")}
    summary["run_ids"] = {**run_ids, "smoke": SMOKE_RUN}
    log(fh, f"run ids: {summary['run_ids']}")
    if dry_run:
        return summary
    t_deploy = time.time()
    hard = t_deploy + hours * 3600
    soft = hard - 15 * 60  # no new episode after this; in-flight ones stop at `hard - 8 min`
    summary["t_deploy"] = t_deploy
    deploy_and_warm(fh, keys)
    client = Client(modal_provider(keys["modal_url"]), keys["modal"])
    tiny_chat(fh, client)

    def evaluator(run_id: str) -> RepoFixEvaluator:
        return RepoFixEvaluator(client, MODEL, store, run_id, cost_cap_usd=5.0, protocol="text_fallback",
                                max_tokens=TURN_TOKENS, retry_max_tokens=TURN_RETRY_TOKENS or None,
                                context_window=CONTEXT_WINDOW, deadline=hard - 8 * 60,
                                episode_wall_s=EPISODE_WALL_S)

    def remaining() -> float:
        return soft - time.time()

    # S0 smoke
    smoke = [({"n_modules": float(k), "n_funcs": 8.0, "bug_distance": 1.0, "tool_budget": 20.0}, s)
             for k in (1, 4) for s in range(3)]
    s0 = run_jobs(fh, evaluator(summary["run_ids"]["smoke"]), smoke, "S0")
    summary["stages"]["S0"] = s0
    if SMOKE_ONLY:
        log(fh, "smoke-only session: stopping after S0")
        return summary
    if s0["errors"] >= 3:
        log(fh, "S0 smoke: too many ERRORs -- aborting before the campaign stages")
        summary["aborted"] = "smoke errors"
        return summary
    per_ep = s0["wall_s"] / len(smoke)  # wall seconds per episode at parallelism 4
    cap = int(remaining() / per_ep)
    log(fh, f"measured {per_ep / 60:.2f} wall-min per episode at parallelism {PARALLELISM}; "
            f"capacity ~{cap} episodes before the soft deadline")
    # allocate: S1 control 3 levels, S2 graph pairs (2 episodes each), S3 navigate 3 points
    want = {"S1": 8, "S2": 5, "S3": 6}  # seeds per level / pairs per graph level / seeds per point

    def cost(w):
        return 3 * w["S1"] + 2 * len(GRAPH_LEVELS) * w["S2"] + 3 * w["S3"]

    floor = {"S1": 4, "S2": 3, "S3": 3}
    while cost(want) > cap and any(want[k] > floor[k] for k in want):
        for k in ("S3", "S2", "S1"):
            if want[k] > floor[k] and cost(want) > cap:
                want[k] -= 1
    summary["plan"] = want
    log(fh, f"plan: {want} = {cost(want)} episodes")

    # S1 control: the same program over 1 / 4 / 8 files
    if remaining() > 10 * 60:
        jobs = [({"n_modules": float(k), "n_funcs": 8.0, "bug_distance": 1.0, "tool_budget": 20.0}, 100 + s)
                for s in range(want["S1"]) for k in (1, 4, 8)]
        summary["stages"]["S1"] = run_jobs(fh, evaluator(run_ids["VI.control"]), jobs, "S1")
    # S2 graph value: paired, 8 files, cause 3 calls away
    if remaining() > 10 * 60:
        t0 = time.time()
        ev = evaluator(run_ids["VI.graph_value"])
        res = run_paired_graph(ev, levels=GRAPH_LEVELS, n_seeds=want["S2"], tool_budget=20, bug_distance=3,
                               parallelism=PARALLELISM, seed_base=200)
        summary["stages"]["S2"] = {"wall_s": time.time() - t0, "note": res.note, "result": res.to_json_dict()}
        log(fh, f"S2 done in {(time.time() - t0) / 60:.1f} min: {res.note}")
    # S3 navigate grid on the navigate ray
    if remaining() > 10 * 60:
        jobs = [({"bug_distance": float(d), "n_modules": float(k), "tool_budget": 20.0}, 300 + s)
                for s in range(want["S3"]) for d, k in ((1, 4), (4, 9), (6, 12))]
        summary["stages"]["S3"] = run_jobs(fh, evaluator(run_ids["VI.navigate"]), jobs, "S3")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.5)
    ap.add_argument("--log", required=True)
    ap.add_argument("--summary", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--watchdog", action="store_true")
    ap.add_argument("--deadline", type=float)
    ap.add_argument("--pid", type=int)
    ap.add_argument("--smoke-only", action="store_true")
    ap.add_argument("--smoke-run", default=None)
    ap.add_argument("--turn-tokens", type=int, default=None)
    ap.add_argument("--retry-tokens", type=int, default=None, help="0 = no retry of a truncated turn")
    a = ap.parse_args()
    global SMOKE_ONLY, SMOKE_RUN, TURN_TOKENS, TURN_RETRY_TOKENS
    SMOKE_ONLY = a.smoke_only
    SMOKE_RUN = a.smoke_run or SMOKE_RUN
    TURN_TOKENS = a.turn_tokens or TURN_TOKENS
    TURN_RETRY_TOKENS = TURN_RETRY_TOKENS if a.retry_tokens is None else a.retry_tokens
    fh = open(a.log, "a")
    if a.watchdog:
        watchdog(a.deadline, a.pid, fh)
        return
    summary: dict = {}
    t0 = time.time()
    try:
        summary = session(a.hours, fh, a.dry_run)
    except Exception as e:  # noqa: BLE001 - the app must be stopped whatever happened
        log(fh, f"SESSION ERROR {type(e).__name__}: {e}")
        summary["error"] = f"{type(e).__name__}: {e}"
    finally:
        if not a.dry_run:
            stop_app(fh)
        summary["app_state_after"] = app_state() if not a.dry_run else "not deployed (dry run)"
        summary["session_minutes"] = round((time.time() - t0) / 60, 1)
        if a.summary:
            pathlib.Path(a.summary).write_text(json.dumps(summary, indent=1, default=str))
        log(fh, f"SESSION END ({summary['session_minutes']} min); app {summary['app_state_after']}")


if __name__ == "__main__":
    main()
