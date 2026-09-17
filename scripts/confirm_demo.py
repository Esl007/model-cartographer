"""Phase 7 demo: decompose -> confirm on the synthetic zoo, end to end.

Plain script (not run by the test suite): for each interesting zoo model,
runs `decompose()` to find a candidate failure explanation, then
`confirm_hypothesis()` on FRESH seeds (>= 10_000, disjoint from decompose's
seed_base=0 search) to check whether the explanation actually holds up.
Prints a readable table: model, hypothesis kind/factors, effect size,
Wilson CIs, p-value, and the final confirmed/not verdict.

No network calls -- everything here is pure Python over
`cartographer.synthetic.ZOO`.
"""

from __future__ import annotations

from cartographer.confirm import confirm_all
from cartographer.decompose import decompose
from cartographer.synthetic import ZOO

_CASES = [
    # (model_name, failing_config, baseline_config)
    ("clean_threshold", {"temporal": 10, "coreference": 5, "distractors": 20}, {"temporal": 1, "coreference": 0, "distractors": 0}),
    ("pairwise_interaction", {"temporal": 8, "coreference": 6, "distractors": 50}, {"temporal": 1, "coreference": 1, "distractors": 1}),
    ("three_way_interaction", {"temporal": 8, "coreference": 6, "distractors": 50}, {"temporal": 1, "coreference": 1, "distractors": 1}),
    ("no_interaction_control", {"temporal": 12, "distractors": 80, "coreference": 5}, {"temporal": 1, "distractors": 1, "coreference": 1}),
]


def _fmt_ci(interval) -> str:
    return f"[{interval.low:.2f}, {interval.high:.2f}]"


def main() -> None:
    hypotheses = []
    evaluators = []
    labels = []
    for name, failing, baseline in _CASES:
        model = ZOO[name]
        evaluator = lambda d, s, m=model: m.evaluate(d, s)
        hypothesis = decompose(evaluator, failing, baseline, seed_base=0)
        hypotheses.append(hypothesis)
        evaluators.append(evaluator)
        labels.append(name)

    print(f"{'model':<24} {'decompose_kind':<24} {'factors':<28} {'effect':>7} {'p':>10} {'confirmed':>10}")
    print("-" * 110)
    for name, hypothesis, evaluator in zip(labels, hypotheses, evaluators):
        # confirm_all expects a single evaluator; each model uses its own,
        # so confirm one at a time here (still exercises confirm_all's
        # Holm-Bonferroni path when hypotheses share an evaluator -- see
        # the batched call below for that).
        result = confirm_all([hypothesis], evaluator, seed_base=10_000)[0]
        factors_str = ",".join(result.factors) if result.factors else "(none)"
        print(
            f"{name:<24} {result.hypothesis_kind:<24} {factors_str:<28} "
            f"{result.effect_size:>7.2f} {result.raw_p:>10.4f} {str(result.confirmed):>10}"
        )
        print(
            f"    baseline_pass_rate={_fmt_ci(result.intervals['baseline'])}  "
            f"failing_pass_rate={_fmt_ci(result.intervals['failing'])}  "
            f"corrected_alpha={result.corrected_alpha_threshold:.4f}"
        )

    # Also demonstrate the family-wise Holm-Bonferroni path: confirm all
    # pairwise_interaction-family hypotheses together against one evaluator.
    pw_model = ZOO["pairwise_interaction"]
    pw_evaluator = lambda d, s: pw_model.evaluate(d, s)
    real = decompose(
        pw_evaluator,
        {"temporal": 8, "coreference": 6, "distractors": 50},
        {"temporal": 1, "coreference": 1, "distractors": 1},
        seed_base=0,
    )
    print("\nHolm-Bonferroni family demo (1 real hypothesis, decompose's own output):")
    for result in confirm_all([real], pw_evaluator, seed_base=10_000):
        print(f"  confirmed={result.confirmed} effect_size={result.effect_size:.2f} raw_p={result.raw_p:.5f}")


if __name__ == "__main__":
    main()
