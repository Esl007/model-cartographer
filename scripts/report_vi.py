"""Province VI live-session report, from the store alone (read-only, no LLM).

    python3 scripts/report_vi.py --summary <session summary.json> --log <session log> --out <report.md>
        [--db results/campaign.sqlite]

Reads the run ids from the session summary written by scripts/run_vi_live.py
and writes a markdown report: per-stage rates with Wilson CIs, the navigation
control (same program over 1/4/8 files, paired by seed), the paired graph-tool
study (§8 H4), the cause-distance grid with H2's pre-registered Fisher test,
H3's symptom-patch share, failure signatures, process measures, and protocol
artefacts.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cartographer.confirm.stats import wilson_interval  # noqa: E402
from cartographer.report.frontier import _fisher_one_sided  # noqa: E402
from cartographer.repo.evaluator import mcnemar_exact  # noqa: E402
from cartographer.store.schema import Verdict  # noqa: E402
from cartographer.store.store import ResultsStore  # noqa: E402


def detail(r) -> dict:
    out = {}
    for part in (r.verifier_detail or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def rate(recs) -> str:
    k = sum(r.verdict == Verdict.PASS for r in recs)
    n = sum(r.verdict in (Verdict.PASS, Verdict.FAIL) for r in recs)
    e = sum(r.verdict == Verdict.ERROR for r in recs)
    if not n:
        return f"0/0 scored ({e} ERROR)"
    w = wilson_interval(k, n)
    return f"{k}/{n} = {k / n:.0%} (95% CI {w.low:.0%}-{w.high:.0%}){f', {e} ERROR' if e else ''}"


def med(xs):
    xs = [x for x in xs if x is not None]
    return f"{statistics.median(xs):g}" if xs else "-"


def measures_table(recs) -> list[str]:
    scored = [r for r in recs if r.verdict != Verdict.ERROR]
    if not scored:
        return ["(no scored episodes)"]

    def m(key):
        return [r.realized_dims.get(key) for r in scored]

    nav = [r.realized_dims["files_read_on_path"] / r.realized_dims["files_read"]
           for r in scored if r.realized_dims.get("files_read")]
    cf = [x for x in m("calls_to_first_cause_read") if x is not None and x >= 0]
    rows = [
        ("tool calls per episode (median)", med(m("n_calls"))),
        ("file reads / searches / edits (median)", f"{med(m('n_reads'))} / {med(m('n_searches'))} / {med(m('n_edits'))}"),
        ("episodes that ever read the bug's file", f"{len(cf)}/{len(scored)}"),
        ("calls before first reading the bug's file (median, when read)", med(cf)),
        ("navigation precision: files read on the call path / files read (median)", med(nav)),
        ("edit_file `old` not found (episodes with >= 1)", f"{sum(1 for x in m('edit_nomatch') if x)}/{len(scored)}"),
        ("whole-file rewrites (episodes with >= 1)", f"{sum(1 for x in m('whole_file_rewrite') if x)}/{len(scored)}"),
        ("ran the tests after the last edit", f"{sum(1 for x in m('ran_tests_after_last_edit') if x)}/{sum(1 for x in m('n_edits') if x)} of episodes that edited"),
        ("turns that ran to the 6k cap after a complete call (episodes with >= 1)",
         f"{sum(1 for x in m('truncated_turns') if x)}/{len(scored)}"),
        ("extra calls in one message, not executed (episodes with >= 1)",
         f"{sum(1 for r in scored if detail(r).get('skipped_calls', '0') not in ('0', ''))}/{len(scored)}"),
        ("wall seconds per episode (median)", med([float(detail(r).get('wall_s', 'nan')) for r in scored if 'wall_s' in detail(r)])),
        ("prompt tokens sent per episode (median)", med(m("tokens_in_total"))),
    ]
    return ["| measure | value |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows]


def signatures(recs) -> list[str]:
    c = Counter(detail(r).get("detail", "?") for r in recs if r.verdict == Verdict.FAIL)
    e = Counter(detail(r).get("detail", "?") for r in recs if r.verdict == Verdict.ERROR)
    out = ["| failure signature | FAILs |", "|---|---|"] + [f"| `{k}` | {v} |" for k, v in c.most_common()]
    if e:
        out += ["", "| ERROR reason | count |", "|---|---|"] + [f"| `{k}` | {v} |" for k, v in e.most_common()]
    return out


def by(recs, key):
    g = defaultdict(list)
    for r in recs:
        g[r.latent_params.get(key)].append(r)
    return dict(sorted(g.items(), key=lambda kv: (kv[0] is None, kv[0])))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--db", default=str(ROOT / "results/campaign.sqlite"))
    a = ap.parse_args()
    summ = json.loads(pathlib.Path(a.summary).read_text())
    store = ResultsStore.open_read_only(a.db)
    rid = summ.get("run_ids", {})
    smoke = store.by_run(rid.get("smoke", "")) if rid else []
    control = store.by_run(rid.get("VI.control", "")) if rid else []
    g_off = store.by_run(rid.get("VI.graph_value", "") + "/graph_off") if rid else []
    g_on = store.by_run(rid.get("VI.graph_value", "") + "/graph_on") if rid else []
    nav = store.by_run(rid.get("VI.navigate", "")) if rid else []
    everything = smoke + control + g_off + g_on + nav
    model = everything[0].model_name if everything else "?"
    L = [f"# Province VI live session: {model} on Modal", "",
         "Generated by `scripts/report_vi.py` from `results/campaign.sqlite` (read-only) and the session log. "
         "Ground truth is the project's own interpreter on hidden inputs; no LLM judged anything.", ""]
    L += ["## Session", "", "| item | value |", "|---|---|"]
    L += [f"| run ids | {', '.join(f'`{v}`' for v in rid.values())} |",
          f"| session minutes (deploy to app stop) | {summ.get('session_minutes')} |",
          f"| app state after | {summ.get('app_state_after')} |",
          f"| plan after smoke (seeds per level / pairs) | {summ.get('plan')} |",
          f"| aborted / error | {summ.get('aborted') or summ.get('error') or 'no'} |",
          f"| episodes recorded | {len(everything)} |",
          f"| tracked token cost (proxy prices, not the GPU bill) | ${sum(r.cost_usd for r in everything):.4f} |"]
    for k, st in summ.get("stages", {}).items():
        L.append(f"| stage {k} wall | {st.get('wall_s', 0) / 60:.1f} min |")
    L += ["", "Protocol: text tool calls (`text_fallback`), episode protocol v2, results rendered as text, "
          "context window 32,768 (turns clamped), 20 tool calls per episode, parallelism 4, temperature 0, "
          "no resample gate (rates and paired designs; serving nondeterminism on this endpoint is documented "
          "in the lab log, 2026-10-06 E).", ""]

    L += ["## Headline rates", "", "| stage | condition | pass rate |", "|---|---|---|"]
    for name, recs in (("smoke", smoke), ("S1 control", control), ("S2 graph off", g_off), ("S2 graph on", g_on),
                       ("S3 navigate", nav)):
        if recs:
            L.append(f"| {name} | all | {rate(recs)} |")
    L.append("")

    if control:
        L += ["## S1 — navigation cost: the same program over 1 / 4 / 8 files", "",
              "Program pinned at 8 functions, the cause one call from `solve`; only the file split changes "
              "(identical programs per seed, tested offline). At 1 file this is Province V's task in function "
              "form.", "", "| files | pass rate |", "|---|---|"]
        lv = by(control, "n_modules")
        for k, recs in lv.items():
            L.append(f"| {int(k)} | {rate(recs)} |")
        one = {r.trial_seed: r for r in lv.get(1.0, []) if r.verdict != Verdict.ERROR}
        eight = {r.trial_seed: r for r in lv.get(8.0, []) if r.verdict != Verdict.ERROR}
        both = sorted(set(one) & set(eight))
        if both:
            b = sum(1 for s in both if one[s].verdict == Verdict.PASS and eight[s].verdict != Verdict.PASS)
            c = sum(1 for s in both if one[s].verdict != Verdict.PASS and eight[s].verdict == Verdict.PASS)
            L += ["", f"Paired by seed, 1 file vs 8 files: {len(both)} pairs; solved only at 1 file: {b}, only at 8 "
                      f"files: {c}; exact McNemar p = {mcnemar_exact(b, c):.3g}."]
        L += ["", "Failure signatures:", ""] + signatures(control) + ["", "Process:", ""] + measures_table(control) + [""]

    if g_off or g_on:
        res = summ.get("stages", {}).get("S2", {}).get("result", {})
        L += ["## S2 — paired graph-tool study (pre-registered H4)", "",
              "Each seed is run twice on the byte-identical repository: without and with `get_code_neighbors`. "
              "H4 (locked): median calls before the bug's file is first read, with the tool <= 0.7 x without, AND "
              "one-sided Wilcoxon p < 0.05. Never reading the bug's file is censored at budget + 1.", "",
              "| arm | pass rate |", "|---|---|", f"| graph off | {rate(g_off)} |", f"| graph on | {rate(g_on)} |", "",
              f"Paired result (complete pairs only): `{summ.get('stages', {}).get('S2', {}).get('note', '-')}`", "",
              f"**H4 {'SUPPORTED' if res.get('h4_supported') else 'NOT SUPPORTED'}** at this sample "
              f"(ratio {res.get('ratio')}, Wilcoxon p {res.get('wilcoxon_p')}; verdict McNemar p "
              f"{res.get('mcnemar_p')}, reported, not pre-registered).", "",
              "Graph-on episodes that called the tool at all: "
              f"{sum(1 for r in g_on if (r.realized_dims or {}).get('n_neighbors'))}/{len(g_on)}.", "",
              "Failure signatures (both arms):", ""] + signatures(g_off + g_on) + [""]
        L += ["Process, graph off:", ""] + measures_table(g_off) + ["", "Process, graph on:", ""] + measures_table(g_on) + [""]

    if nav:
        L += ["## S3 — cause distance (points on the VI.navigate ray)", "", "| cause calls away | files | pass rate |",
              "|---|---|---|"]
        for d, recs in by(nav, "bug_distance").items():
            ks = sorted({int(r.latent_params.get('n_modules', 0)) for r in recs})
            L.append(f"| {int(d)} | {', '.join(map(str, ks))} | {rate(recs)} |")
        lv = by(nav, "bug_distance")

        def wsf(recs):
            fails = [r for r in recs if r.verdict == Verdict.FAIL and detail(r).get("edited", "-") != "-"]
            return sum(1 for r in fails if detail(r).get("detail") == "wrong_site_file"), len(fails)

        k1, n1 = wsf(lv.get(1.0, []))
        k4, n4 = wsf(lv.get(4.0, []))
        p = _fisher_one_sided(k4, n4, k1, n1) if n1 and n4 else None
        L += ["", f"H2 (locked: Fisher exact one-sided, `wrong_site_file` share among FAILs with an edit, cause 4 "
                  f"vs 1 call away): {k4}/{n4} at 4 vs {k1}/{n1} at 1; p = "
                  f"{'n/a' if p is None else f'{p:.3g}'} (the H2 family has one test here, so Holm is the identity).",
              "", "Failure signatures:", ""] + signatures(nav) + ["", "Process:", ""] + measures_table(nav) + [""]

    multi = [r for r in everything if (r.latent_params or {}).get("n_modules", 0) >= 4]
    fails = [r for r in multi if r.verdict == Verdict.FAIL]
    vf = sum(1 for r in fails if detail(r).get("detail") == "visible_fixed_hidden_broken")
    L += ["## Pre-registered hypotheses at this sample", "",
          f"- **H1 (fabrication)**: not run tonight (needs the look-alike stage with paired `restate_symbols`). "
          f"Fabricated symbols seen anyway: {sum(1 for r in everything if detail(r).get('detail', '').startswith('fabricated_symbol'))} "
          f"episodes; look-alike calls: {sum(1 for r in everything if detail(r).get('detail') == 'lookalike_called')}.",
          f"- **H2 (symptom file)**: see S3.",
          f"- **H3 (visible-test overfitting)**: `visible_fixed_hidden_broken` is {vf}/{len(fails)} of FAILs at >= 4 "
          f"files (H3 needs >= 10%).",
          f"- **H4 (graph tools compress exploration)**: see S2.",
          f"- **Control**: 1-file rate vs Province V's single-function repair rate on this model is in S1 (Province V "
          f"fresh-seed confirm: 7/20 after the 2026-10-06 resolutions; different task family -- one function, "
          f"one shot -- so only indicative).", ""]

    L += ["## All episodes: failure signatures", ""] + signatures(everything) + [""]
    proto = Counter(detail(r).get("protocol", "?") for r in everything if r.verdict != Verdict.ERROR or True)
    outc = Counter(detail(r).get("outcome", "?") for r in everything)
    L += ["## Protocol artefacts", "", f"- protocol used: {dict(proto)}", f"- episode outcomes: {dict(outc)}",
          f"- deadline-stopped episodes: {sum(1 for r in everything if detail(r).get('deadline_hit') == '1')}",
          f"- outside-subset end states: {sum(1 for r in everything if detail(r).get('detail', '').startswith('outside_subset'))}",
          ""]
    log = pathlib.Path(a.log).read_text().splitlines() if pathlib.Path(a.log).exists() else []
    L += ["## Session log (key lines)", "", "```"] + [ln for ln in log if any(
        t in ln for t in ("deploy", "warm", "tiny chat", "measured", "plan:", "done in", "app stop", "SESSION",
                          "WATCHDOG", "ERROR"))][:80] + ["```", ""]
    pathlib.Path(a.out).write_text("\n".join(L))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
