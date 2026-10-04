"""Ablation catalog for interactive (Province III) copy-fidelity failures
(docs/spec-ticket-fabrication.md, Part B).

Each ablation is a config switch the interactive evaluator understands
(`InteractiveSpec`), excluded from the task seed, so the ablated episode runs
on the IDENTICAL world: same plan, same ticket checking, same answer key.
Only the surface the ablation names changes:

  * `ticket_restatement` -- every tool result restates the currently valid
    tickets (carry distance -> 0 without changing the plan). Rescue here
    means the failure is retrieval-from-history, not copying itself.
  * `copy_instruction` -- one system-prompt line: tickets are opaque, copy
    them exactly. If THIS rescues, the finding is elicitation, not
    capability (and doubles as an agent-config fix).
  * `opaque_rename` -- tool and entity ids renamed to patternless tokens,
    removing the material a pattern-shaped ticket could be built from.

`attribute_ticket_failure` resample-gates the failure first (same config,
same trial seed, n reruns); only a STABLE failure is attributed. Each
ablation is then run n times; a one-sided Fisher exact test (ablated passes
> baseline passes) gives a p-value per ablation, and Holm-Bonferroni
controls the family-wise error across the catalog. With n = 6 per arm a
full rescue (6/6 vs 0/6, p = 0.001) or 5/6 vs 0/6 (p = 0.008) clears Holm's
first threshold (0.05 / 3); n = 5 cannot carry a 4/5 rescue. No LLM anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import comb
from typing import Callable

from ..confirm.stats import holm_bonferroni

TICKET_ABLATIONS: dict[str, dict[str, float]] = {
    "ticket_restatement": {"restate_tickets": 1.0},
    "copy_instruction": {"copy_instruction": 1.0},
    "opaque_rename": {"opaque_names": 1.0},
}

Evaluator = Callable[[dict, int], bool]


def fisher_one_sided(pass_a: int, n_a: int, pass_b: int, n_b: int) -> float:
    """P(arm A shows >= pass_a passes | the margins), i.e. the one-sided Fisher
    exact p-value for 'A passes more than B'."""
    total, n = pass_a + pass_b, n_a + n_b
    if n_a == 0 or n_b == 0:
        return 1.0
    denom = comb(n, n_a)
    return sum(comb(total, x) * comb(n - total, n_a - x)
               for x in range(pass_a, min(total, n_a) + 1)) / denom


@dataclass
class AblationArm:
    name: str
    n: int
    n_pass: int
    p_value: float = 1.0
    alpha_threshold: float = 0.0  # Holm step-down threshold this arm was tested against
    rescues: bool = False


@dataclass
class TicketAttribution:
    config: dict
    trial_seed: int
    status: str  # "noise" (did not survive the gate) | "attributed"
    baseline_n: int
    baseline_pass: int
    arms: list[AblationArm] = field(default_factory=list)

    @property
    def rescued_by(self) -> list[str]:
        return [a.name for a in self.arms if a.rescues]

    def to_json_dict(self) -> dict:
        return {
            "config": self.config, "trial_seed": self.trial_seed, "status": self.status,
            "baseline": f"{self.baseline_pass}/{self.baseline_n}",
            "arms": [vars(a) for a in self.arms], "rescued_by": self.rescued_by,
        }


def attribute_ticket_failure(
    evaluator: Evaluator,
    config: dict,
    trial_seed: int,
    n: int = 6,
    alpha: float = 0.05,
    ablations: dict[str, dict[str, float]] | None = None,
    stable_threshold: float = 0.5,
) -> TicketAttribution:
    """Resample-gate one failing (config, trial_seed), then run each ablation
    `n` times on the same task and test which ones rescue it."""
    ablations = TICKET_ABLATIONS if ablations is None else ablations
    base_pass = sum(bool(evaluator(dict(config), trial_seed)) for _ in range(n))
    out = TicketAttribution(dict(config), trial_seed, "attributed", n, base_pass)
    if base_pass / n > stable_threshold:
        out.status = "noise"
        return out
    for name, switch in ablations.items():
        passes = sum(bool(evaluator({**config, **switch}, trial_seed)) for _ in range(n))
        out.arms.append(AblationArm(name, n, passes, fisher_one_sided(passes, n, base_pass, n)))
    for arm, (reject, thr) in zip(out.arms, holm_bonferroni([a.p_value for a in out.arms], alpha)):
        arm.rescues, arm.alpha_threshold = bool(reject), thr
    return out
