"""Run the Phase 6 state + belief campaigns against Nebius Token Factory's
Nemotron 3 Super — the actual submission-relevant model, replacing the
Groq stand-in dress rehearsal (see scripts/run_groq_campaign.py).

Plain script (not a test target): loads local provider keys, builds a
Nebius `Client`, opens `results/campaign.sqlite` (same store, new run_ids
so both providers' data coexist), runs the state campaign followed by the
belief campaign, and prints boundaries/hypotheses/spend for each.
"""

from __future__ import annotations

import os

from cartographer.exec.client import NEBIUS, Client
from cartographer.exec.evaluator import run_belief_campaign, run_state_campaign
from cartographer.exec.keys import load_keys
from cartographer.store.store import ResultsStore

STATE_RUN_ID = "camp-state-nemotron-super-001"
BELIEF_RUN_ID = "camp-belief-nemotron-super-001"
MODEL = "nvidia/nemotron-3-super-120b-a12b"


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
    api_key = keys["nebius"]
    client = Client(NEBIUS, api_key)

    os.makedirs("results", exist_ok=True)
    store = ResultsStore("results/campaign.sqlite")

    state_campaign = run_state_campaign(client, store, run_id=STATE_RUN_ID, model=MODEL)
    _print_campaign_summary("state campaign (Nemotron Super)", state_campaign)

    belief_campaign = run_belief_campaign(client, store, run_id=BELIEF_RUN_ID, model=MODEL)
    _print_campaign_summary("belief campaign (Nemotron Super)", belief_campaign)

    total_spend = 0.0
    for state in (state_campaign, belief_campaign):
        evaluator = getattr(state, "evaluator", None)
        if evaluator is not None:
            total_spend += evaluator.spent_usd
    print(f"\ntotal spend across both campaigns: ${total_spend:.4f}")

    store.close()


if __name__ == "__main__":
    main()
