"""The Evidence Fidelity Campaign's report view (docs/spec-evidence-fidelity-campaign.md §7):
a province x condition matrix of mechanism-signature rates and pass rates with
95% Wilson CIs, the paired contrasts against B, and the §5 hypothesis verdicts.

    from cartographer.report.evidence_matrix import matrix_markdown
    matrix_markdown({"belief": result, ...})        # FamilyResult objects
    matrix_from_store(store, {"belief": "camp-ef-belief-gemma4qat-001", ...})

Read-only on the store; no LLM.
"""

from __future__ import annotations

from ..campaigns.evidence_fidelity import ARM_MEANING, ARM_ORDER, FAMILIES, hypothesis_summary, summarize_family


def _cell(st) -> str:
    if st is None:
        return "n/a"
    if not st.n_scored:
        return f"0 scored ({st.n_error} ERROR)"
    sig = f"{st.signature_mean:.2f}"
    if st.signature_ci:
        sig += f" [{st.signature_ci[0]:.2f}–{st.signature_ci[1]:.2f}]"
    pr = f"{st.n_pass}/{st.n_scored}"
    if st.pass_ci:
        pr += f" [{st.pass_ci[0]:.2f}–{st.pass_ci[1]:.2f}]"
    extra = f"; {st.n_error} ERR" if st.n_error else ""
    extra += f"; {st.n_unresolved} unres." if st.n_unresolved else ""
    return f"sig {sig}<br>pass {pr}{extra}"


def matrix_markdown(results: dict, title: str = "Evidence Fidelity matrix") -> str:
    arms = [a for a in ARM_ORDER if any(a in r.arms for r in results.values())]
    lines = [f"# {title}", "",
             "Cells: mechanism-signature rate per scored episode (primary) and pass rate (secondary), "
             "95% Wilson CIs, paired seeds across a row.", "",
             "| family (province; signature) | " + " | ".join(f"{a}: {ARM_MEANING[a].split(' (')[0]}" for a in arms)
             + " |", "|---|" + "---|" * len(arms)]
    for key, r in results.items():
        lines.append(f"| {key} ({r.province}; `{r.signature_name}`) | "
                     + " | ".join(_cell(r.arms.get(a)) if FAMILIES[key].arms.get(a) is not None else "n/a"
                                  for a in arms) + " |")
    lines += ["", "## Paired contrasts against B (Holm within family)", "",
              "| family | arm | pairs | pass b / c | McNemar p (Holm) | signature Δ | Wilcoxon p (Holm) |",
              "|---|---|---|---|---|---|---|"]
    for key, r in results.items():
        for c in r.contrasts:
            lines.append(f"| {key} | {c.arm} | {c.pairs} | {c.b} / {c.c} | {c.mcnemar_p:.3g} ({c.holm_p_pass:.3g}) | "
                         f"{'' if c.signature_diff_mean is None else f'{c.signature_diff_mean:+.2f}'} | "
                         f"{c.wilcoxon_p:.3g} ({c.holm_p_signature:.3g}) |")
    h = hypothesis_summary(results)
    lines += ["", "## Pre-registered hypotheses (§5) at this sample", "",
              f"- **Control (A pass ≥ 0.8):** {h.get('control')}",
              f"- **H-EF1 (conflict cost tracks carriage):** {h.get('H-EF1')}",
              f"- **H-EF2 (C rescues carried, not in-view):** {h.get('H-EF2')}",
              f"- **H-EF3 (D rescues nothing, or reclassifies):** {h.get('H-EF3')}",
              f"- **H-EF4 (two families):** {h.get('H-EF4', 'not run (needs tickets and repair with X arms)')}", ""]
    return "\n".join(lines)


def matrix_from_store(store, run_ids: dict, prefixes: dict | None = None) -> str:
    results = {k: summarize_family(store, rid, k, (prefixes or {}).get(k, "")) for k, rid in run_ids.items()}
    return matrix_markdown(results)
