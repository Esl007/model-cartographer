"""Run the Phase 6 state + belief campaigns against Groq's gpt-oss-120b.

Plain script (not a test target): loads local provider keys, builds a Groq
`Client`, opens `results/campaign.sqlite`, runs the state campaign followed
by the belief campaign, and prints boundaries/hypotheses/spend for each.

Not exercised by the test suite -- no `if __name__` guard is needed there
since pytest never imports this module, but it's kept for clarity and so
the script also works when run directly.
"""

from __future__ import annotations

import os

from cartographer.exec.client import GROQ, Client
from cartographer.exec.evaluator import run_belief_campaign, run_state_campaign
from cartographer.exec.keys import load_keys
from cartographer.store.store import ResultsStore

STATE_RUN_ID = "camp-state-120b-001"
BELIEF_RUN_ID = "camp-belief-120b-001"
MODEL = "openai/gpt-oss-120b"


def _print_campaign_summary(label: str, state) -> None:
    print(f"\n=== {label} ===")
    print("boundaries:")
    for dim, (mid, ci_lo, ci_hi) in state.boundaries.items():
        print(f"  {dim}: {mid:.3f}  (CI [{ci_lo:.3f}, {ci_hi:.3f}])")
    if not state.boundaries:
        print("  (none found)")

    print("hypotheses:")
    for hyp in state.hypotheses:
        print(f"  kind={hyp.kind} factors={hyp.factors} notes={getattr(hyp, 'notes', None)}")
    if not state.hypotheses:
        print("  (none)")

    if state.notes:
        print("notes:")
        for note in state.notes:
            print(f"  - {note}")

    evaluator = getattr(state, "evaluator", None)
    if evaluator is not None:
        print(
            f"evaluator: spent=${evaluator.spent_usd:.4f} "
            f"calls={evaluator.call_count} errors={evaluator.error_count}"
        )


def main() -> None:
    keys = load_keys()
    api_key = keys["groq"]
    client = Client(GROQ, api_key)

    os.makedirs("results", exist_ok=True)
    store = ResultsStore("results/campaign.sqlite")

    state_campaign = run_state_campaign(client, store, run_id=STATE_RUN_ID, model=MODEL)
    _print_campaign_summary("state campaign", state_campaign)

    belief_campaign = run_belief_campaign(client, store, run_id=BELIEF_RUN_ID, model=MODEL)
    _print_campaign_summary("belief campaign", belief_campaign)

    total_spend = 0.0
    for state in (state_campaign, belief_campaign):
        evaluator = getattr(state, "evaluator", None)
        if evaluator is not None:
            total_spend += evaluator.spent_usd
    print(f"\ntotal spend across both campaigns: ${total_spend:.4f}")

    store.close()


if __name__ == "__main__":
    main()
