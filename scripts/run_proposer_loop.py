"""Live smoke test for the adversarial proposer loop
(`cartographer.propose.loop.run_proposer_loop`): Nemotron Super proposes
generator configs it believes Nemotron Nano will fail on, the gauntlet
filters them offline, and survivors are measured against Nano for real.

Plain script (not a test target, same pattern as scripts/run_nebius_campaign
.py): loads local provider keys, builds a Nebius `Client`, opens
`results/campaign.sqlite`, runs the loop once, and prints the round-by-round
narrative plus whether the proposer beat its random control arm.

Hard-capped at cost_cap=$0.40 (see call below) -- this spend is
pre-authorized per docs/lab-log.md's territory brief. Uses Nano as the
subject model both because it's the cheapest available and because it has
a demonstrated real failure mode (see docs/lab-log.md, 2026-09-17 belief
finding), so measured hits are plausible rather than searching for a
needle in a haystack of a model that never fails.
"""

from __future__ import annotations

import os

from cartographer.exec.client import NEBIUS, Client
from cartographer.exec.keys import load_keys
from cartographer.propose.loop import run_proposer_loop
from cartographer.store.store import ResultsStore

RUN_ID = "propose-super-vs-nano-001"
PROPOSER_MODEL = "nvidia/nemotron-3-super-120b-a12b"
SUBJECT_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


def _print_result(result) -> None:
    print(f"\n=== proposer loop: {PROPOSER_MODEL} (proposer) vs {SUBJECT_MODEL} (subject) ===")
    for stats in result.round_stats:
        print(
            f"round {stats.round_index}: proposed={stats.proposed} "
            f"rejected_at_gauntlet={stats.rejected_at_gauntlet} "
            f"measured={stats.measured} hits={stats.hits} "
            f"control_measured={stats.control_measured} control_hits={stats.control_hits} "
            f"spend=${stats.spend_usd:.4f}"
        )

    print("\ntrap library:")
    if not result.trap_library:
        print("  (empty -- no proposal reached the hit threshold)")
    for entry in result.trap_library:
        ci = entry.wilson
        print(
            f"  round={entry.round_index} failure_rate={entry.failure_rate:.2f} "
            f"(CI [{ci.low:.2f}, {ci.high:.2f}], n={ci.n}) config={entry.config} "
            f"hypothesis={entry.hypothesis!r}"
        )

    print(f"\nproposer mean failure rate: {result.proposer_mean_failure_rate:.3f}")
    print(f"control (random) mean failure rate: {result.control_mean_failure_rate:.3f}")
    print(f"beat_random: {result.beat_random}")
    print(f"total spend: ${result.total_spend_usd:.4f}")
    print(f"stopped_reason: {result.stopped_reason}")


def main() -> None:
    keys = load_keys()
    api_key = keys["nebius"]
    client = Client(NEBIUS, api_key)

    os.makedirs("results", exist_ok=True)
    store = ResultsStore("results/campaign.sqlite")

    result = run_proposer_loop(
        client,
        store,
        run_id=RUN_ID,
        proposer_model=PROPOSER_MODEL,
        subject_model=SUBJECT_MODEL,
        rounds=2,
        proposals_per_round=4,
        k_trials=5,
        cost_cap=0.40,
    )
    _print_result(result)

    store.close()


if __name__ == "__main__":
    main()
