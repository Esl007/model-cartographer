"""The campaign registry: every campaign the cartographer can run, one name each.

Before this, campaign names mixed three different things -- the province
(`tool_*`, `code_*`), the query kind (`state`, `belief`) and the method
(`breaking_point`, `full_map`) -- and half the campaigns existed only as
stages inside scripts. Here every campaign is `<province>.<stressor>`:
which part of the map it explores, and what it turns up until the model
breaks. Every older name (/launch preset, `run_all_provinces.py` stage,
ticket ray) is kept as a legacy alias that resolves to exactly one id.

Run-id templates are the contract with `cartographer/report/frontier.py`:
`frontier.SPECS` finds evidence by regex over run ids, so each template here
must keep matching the rows it declares in `frontier_rows`. Some of those
regexes pin a run number (`camp-tool-depth-<label>-003`) or whitelist model
labels (`camp-interactive-(nano|super|ultra|gemma)-NNN`); the pinned ones are
templates without `{n}`, and the label whitelists are recorded in
`frontier_labels` so a launcher can warn before a run lands off the page.

    python3 -m cartographer.campaigns.registry --write-catalog   # docs/campaign-catalog.md
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Callable

from ..exec import evaluator as E
from ..interactive import campaign as IC
from ..repo import evaluator as R
from . import evidence_fidelity as EFC
from ..schedule.escalate import EscalationRay, RayDim
from .consistency import run_consistency_campaign

PROVINCES: dict[str, str] = {
    "I": "Narrative (state, order, belief)",
    "II": "Tool planning (one-shot JSON plans)",
    "III": "Interactive execution (multi-turn tool calls)",
    "IV": "Code tracing",
    "V": "Code repair and writing",
    "VI": "Repository navigation and repair",
    "EF": "Evidence fidelity (paired condition grid over the provinces)",
    "X": "Cross-cutting",
}

EVIDENCE_KINDS = ("axis", "rate", "battery")


@dataclass(frozen=True)
class CampaignSpec:
    id: str  # "<province>.<stressor>"
    province: str  # "I" | "II" | "III" | "IV" | "X"
    title: str
    question: str  # one plain-English sentence a non-expert understands
    stressor: str  # what is turned up
    evidence_kind: str  # "axis" (escalation ray) | "rate" (pass rate) | "battery" (several campaigns)
    runner: Callable
    run_id_template: str  # str.format(label=..., n=...); no `{n}` = pinned run id
    ray_factory: Callable | None = None
    frontier_rows: tuple[str, ...] = ()  # keys in frontier.SPECS this feeds
    protocol_note: str = ""
    legacy_aliases: tuple[str, ...] = ()
    default_kwargs: dict = field(default_factory=dict, hash=False, compare=False)
    # Labels frontier.SPECS accepts for this campaign's rows when its regex
    # whitelists model labels; empty = any `\w+` label.
    frontier_labels: tuple[str, ...] = ()

    @property
    def pinned(self) -> bool:
        """True when the run id carries a fixed number frontier.SPECS expects."""
        return "{n" not in self.run_id_template

    def run_id(self, label: str, n: int = 1) -> str:
        return self.run_id_template.format(label=label, n=n)


# --- rays that today exist only inline in scripts/run_all_provinces.py ----


def tool_depth_ray() -> EscalationRay:
    """Plan length 7 -> 19 calls; look-alike tool names switch on at t >= 0.5."""
    return EscalationRay(dims=(RayDim("plan_depth", 4, 16, True), RayDim("id_collisions_on", 0, 1, True)))


def tool_deep_ray() -> EscalationRay:
    """Plan length 7 -> 31 calls with look-alike tool names on throughout."""
    return EscalationRay(dims=(RayDim("plan_depth", 4, 28, True), RayDim("id_collisions_on", 1, 1, True)))


def interactive_lookalike_ray() -> EscalationRay:
    return EscalationRay(dims=(RayDim("plan_depth", 4, 28, True), RayDim("id_collisions_on", 1, 1, True)))


def interactive_fault_ray() -> EscalationRay:
    return EscalationRay(dims=(RayDim("plan_depth", 4, 16, True), RayDim("value_links", 0.0, 1.0, False),
                               RayDim("fault_step", 3, 10, True)))


_TEXT_TOOLS = ("Native tool calling by default; `--text-tools` switches to protocol=text_fallback "
               "for models without native calling.")
_ONE_SHOT = "One-shot prompt, JSON plan out; the verifier simulates the plan step by step."
_NARRATIVE = "One-shot story + question; answers checked against the generator's ground truth."
_CODE = "One-shot program + question; ground truth from the project's own interpreter."
_CODEFIX = ("One-shot repair / write; the answer is parsed into the project's own AST (never run as Python) and "
            "checked by its own interpreter on hidden inputs.")
_REPO = ("Interactive repository episodes (native tool calling; `--text-tools` switches to text_fallback). The "
         "files are parsed into the project's own AST and run by its interpreter on hidden inputs -- nothing the "
         "model writes is executed as Python. Records under <run_id>; resample reruns under <run_id>/verify.")
_EF = ("Evidence Fidelity Campaign (docs/spec-evidence-fidelity-campaign.md): every trial seed runs through the "
       "family's arms -- A agree, B conflict, C salience, D presentation (+ X transfer / E exploratory) -- on the "
       "SAME task, under <run_id>/<arm>; paired McNemar (pass) and Wilcoxon (mechanism signature), Holm within "
       "the family. `resample_gate` reruns every FAIL under <run_id>/<arm>/verify.")
_TICKET = ("Interactive, text_fallback protocol by default; resample-gated (failures re-run before "
           "they count). Attribution ablations: scripts/run_ticket_fidelity.py --attribute.")

_SPECS: tuple[CampaignSpec, ...] = (
    # ---- Province I: narrative ------------------------------------------
    CampaignSpec(
        "I.state", "I", "Object-state tracking",
        "After a long chain of moves, can the model say where each object really is?",
        "chain length (2 -> 30 moves)", "rate", E.run_state_campaign, "camp-state-{label}-{n:03d}",
        frontier_rows=("state",), protocol_note=_NARRATIVE, legacy_aliases=("state",),
        default_kwargs={"budget": 160, "cost_cap": 0.5}),
    CampaignSpec(
        "I.order", "I", "Event order",
        "When a story is told out of order, can the model recover what really happened first?",
        "chain length, scrambling, distractor and filler events", "rate", E.run_order_campaign,
        "camp-order-{label}-{n:03d}", frontier_rows=("order",), protocol_note=_NARRATIVE,
        legacy_aliases=("order",), default_kwargs={"budget": 400, "cost_cap": 0.5}),
    CampaignSpec(
        "I.belief", "I", "Belief tracking (theory of mind)",
        "Can the model tell where a person thinks an object is, based only on what that person saw?",
        "chain length, scrambling, coreference pressure, distractors, witness cutoff", "rate",
        E.run_belief_campaign, "camp-belief-{label}-{n:03d}", frontier_rows=("belief",),
        protocol_note=_NARRATIVE, legacy_aliases=("belief",), default_kwargs={"budget": 700, "cost_cap": 1.0}),
    CampaignSpec(
        "I.adversarial_belief", "I", "Belief tracking in misleading stories",
        "Does belief tracking survive stories built so the tempting wrong answer is right there?",
        "adversarially searched trap worlds (foil answers planted)", "rate",
        E.run_adversarial_belief_campaign, "camp-adversarial_belief-{label}-{n:03d}",
        frontier_rows=("adversarial_belief",), protocol_note=_NARRATIVE,
        legacy_aliases=("adversarial_belief",), default_kwargs={"budget": 300, "cost_cap": 1.0}),
    CampaignSpec(
        "I.breaking_point", "I", "Narrative breaking point",
        "If every kind of story difficulty is turned up at once, at what point does the model break?",
        "all six narrative difficulty dims jointly, base -> generator ceiling", "axis",
        E.run_breaking_point_campaign, "camp-breaking_point-{label}-{n:03d}",
        frontier_rows=("belief",), protocol_note=_NARRATIVE + " Bisection, then decompose + confirm.",
        legacy_aliases=("breaking_point",), default_kwargs={"budget": 350, "cost_cap": 0.75}),
    # ---- Province II: tool planning -------------------------------------
    CampaignSpec(
        "II.sweep", "II", "Tool-planning difficulty sweep",
        "Across many mixes of plan length, decoy tools and prerequisites, how often is the plan right?",
        "plan depth, decoys, precondition chains, state obscurity, parallel goals (scheduler sweep)", "rate",
        E.run_tool_planning_campaign, "camp-tool_planning-{label}-{n:03d}", protocol_note=_ONE_SHOT,
        legacy_aliases=("tool_planning",), default_kwargs={"budget": 400, "cost_cap": 0.75}),
    CampaignSpec(
        "II.plan_length", "II", "Plan length (clear, then look-alike names)",
        "How long a sequence of tool calls can the model plan before it gets one wrong?",
        "plan length 7 -> 19 calls; look-alike tool names switch on halfway", "axis",
        E.run_tool_breaking_point_campaign, "camp-tool-depth-{label}-003", ray_factory=tool_depth_ray,
        frontier_rows=("plan_length_clear", "plan_length_lookalike"), protocol_note=_ONE_SHOT,
        legacy_aliases=("tool_depth",), default_kwargs={"budget": 250, "cost_cap": 0.30, "parallelism": 6}),
    CampaignSpec(
        "II.plan_length_lookalike", "II", "Long plans with look-alike tool names",
        "With tool names that look alike, how long a plan can the model write before it calls the wrong one?",
        "plan length 7 -> 31 calls, look-alike names on throughout", "axis",
        E.run_tool_breaking_point_campaign, "camp-tool-deep-{label}-001", ray_factory=tool_deep_ray,
        frontier_rows=("plan_length_lookalike",), protocol_note=_ONE_SHOT, legacy_aliases=("tool_deep",),
        default_kwargs={"budget": 250, "cost_cap": 0.30, "parallelism": 6}),
    CampaignSpec(
        "II.traps", "II", "Structural traps",
        "Can the model plan around poisoned tools, misleading descriptions and a zero-slack call budget?",
        "trap mechanisms 0 -> 5 (poison, dishonest decoys, implicit preconditions, zero slack, id collisions)",
        "axis", E.run_tool_breaking_point_campaign, "camp-tool_bp-{label}-001", ray_factory=E.default_tool_ray,
        frontier_rows=("traps",), protocol_note=_ONE_SHOT, legacy_aliases=("tool_traps", "tool_breaking_point"),
        default_kwargs={"budget": 250, "cost_cap": 0.30, "parallelism": 6}),
    CampaignSpec(
        "II.catalog_size", "II", "Catalog size",
        "Can the model still pick the right tool when the catalog grows to dozens of plausible ones?",
        "catalog size 8 -> 66 plausible tools", "axis", E.run_tool_breaking_point_campaign,
        "camp-tool-confuse-{label}-001", ray_factory=E.default_tool_confusability_ray,
        frontier_rows=("catalog_size",), protocol_note=_ONE_SHOT, legacy_aliases=("tool_catalog",),
        default_kwargs={"budget": 120, "cost_cap": 0.20, "parallelism": 6}),
    CampaignSpec(
        "II.impossible", "II", "Impossible tasks",
        "When a goal cannot be reached with the tools given, does the model say so instead of inventing a plan?",
        "share of unsolvable goals (40%)", "rate", E.run_tool_irrelevance_campaign,
        "camp-irrelevance-{label}-{n:03d}", frontier_rows=("impossible",), protocol_note=_ONE_SHOT,
        legacy_aliases=("tool_impossible",), default_kwargs={"n_trials": 40, "cost_cap": 0.20, "parallelism": 6}),
    # ---- Province III: interactive execution ------------------------------
    CampaignSpec(
        "III.values", "III", "Step-by-step execution with values to carry",
        "Calling tools one at a time, can the model carry each returned value into the next call?",
        "plan depth 4 -> 16 together with value passing 0 -> 100% of links", "axis",
        IC.run_interactive_breaking_point_campaign, "camp-interactive-{label}-{n:03d}",
        ray_factory=IC.default_interactive_ray, frontier_rows=("interactive",), protocol_note=_TEXT_TOOLS,
        legacy_aliases=("interactive",),
        default_kwargs={"budget": 60, "cost_cap": 0.40, "k_trials": 5, "parallelism": 4}),
    CampaignSpec(
        "III.lookalike", "III", "Step-by-step execution with look-alike tool names",
        "Seeing each error as it happens, can the model avoid calling a look-alike tool by mistake?",
        "plan depth 4 -> 28, look-alike tool names on", "axis",
        IC.run_interactive_breaking_point_campaign, "camp-interactive-lookalike-{label}-{n:03d}",
        ray_factory=interactive_lookalike_ray, frontier_rows=("interactive_lookalike",),
        protocol_note=_TEXT_TOOLS, legacy_aliases=("interactive_lookalike",),
        default_kwargs={"budget": 45, "cost_cap": 0.40, "k_trials": 5, "parallelism": 4}),
    CampaignSpec(
        "III.fault", "III", "Recovery from a failed call",
        "When a tool call fails once mid-plan and a retry would work, does the model retry and finish?",
        "plan depth, value passing, and where the transient failure hits", "axis",
        IC.run_interactive_breaking_point_campaign, "camp-interactive-fault-{label}-{n:03d}",
        ray_factory=interactive_fault_ray, frontier_rows=("interactive_fault",), protocol_note=_TEXT_TOOLS,
        legacy_aliases=("interactive_fault",),
        default_kwargs={"budget": 60, "cost_cap": 0.30, "k_trials": 5, "parallelism": 4}),
    CampaignSpec(
        "III.ticket_mechanism", "III", "Ticket fidelity: form x carry distance",
        "Does the model copy an opaque ticket exactly, or 'complete' it when it looks like a pattern?",
        "ticket form opaque -> pattern-suggestive, carry distance 1 -> 10 steps", "axis",
        IC.run_ticket_fidelity_campaign, "camp-ticket-mechanism-{label}-{n:03d}",
        ray_factory=IC.TICKET_RAYS["mechanism"], protocol_note=_TICKET, legacy_aliases=("ticket_mechanism",),
        default_kwargs={"budget": 60, "cost_cap": 0.75, "k_trials": 5, "parallelism": 4,
                        "protocol": "text_fallback"}),
    CampaignSpec(
        "III.ticket_trap", "III", "Ticket fidelity: decoys x live tickets",
        "With several real tickets and some decoys in play, does the model hand back the right one?",
        "decoy tickets 0 -> 3 with live tickets 1 -> 6", "axis",
        IC.run_ticket_fidelity_campaign, "camp-ticket-trap-{label}-{n:03d}",
        ray_factory=IC.TICKET_RAYS["trap"], protocol_note=_TICKET, legacy_aliases=("ticket_trap",),
        default_kwargs={"budget": 60, "cost_cap": 0.75, "k_trials": 5, "parallelism": 4,
                        "protocol": "text_fallback"}),
    CampaignSpec(
        "III.ticket_length", "III", "Ticket fidelity: length control",
        "Before blaming anything subtler, does the model garble a ticket simply because it is long?",
        "opaque ticket length 8 -> 64 characters", "axis",
        IC.run_ticket_fidelity_campaign, "camp-ticket-length-{label}-{n:03d}",
        ray_factory=IC.TICKET_RAYS["length"], protocol_note=_TICKET, legacy_aliases=("ticket_length",),
        default_kwargs={"budget": 60, "cost_cap": 0.75, "k_trials": 5, "parallelism": 4,
                        "protocol": "text_fallback"}),
    # ---- Province IV: code tracing -----------------------------------------
    CampaignSpec(
        "IV.lookalike", "IV", "Look-alike variable names",
        "As programs get longer, does the model confuse variables with look-alike names (total / totl)?",
        "program length 6 -> 40 statements x look-alike variable names", "axis",
        E.run_code_breaking_point_campaign, "camp-code-lookalike-{label}-{n:03d}",
        ray_factory=E.default_code_lookalike_ray, frontier_rows=("code_lookalike",), protocol_note=_CODE,
        legacy_aliases=("code_lookalike", "code_tracing"),
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "IV.traps", "IV", "Semantic traps",
        "Does the model trace code correctly through aliasing, closures, off-by-one ranges and modulo?",
        "program length + six semantic traps switching on halfway", "axis",
        E.run_code_breaking_point_campaign, "camp-code-traps-{label}-{n:03d}",
        ray_factory=E.default_code_trap_ray, frontier_rows=("code_traps",), protocol_note=_CODE,
        legacy_aliases=("code_traps",),
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "IV.length", "IV", "Length and nesting (control)",
        "With no traps and clear names, how long and deeply nested a program can the model trace?",
        "program length 6 -> 60 statements, nesting 0 -> 3", "axis",
        E.run_code_breaking_point_campaign, "camp-code-length-{label}-{n:03d}",
        ray_factory=E.default_code_length_ray, protocol_note=_CODE, legacy_aliases=("code_length",),
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "IV.copy", "IV", "Copying literals in code",
        "Does the model copy an odd string literal exactly, or 'fix' it to match the naming pattern around it?",
        "program length + an opaque literal that breaks the naming scheme", "axis",
        E.run_code_breaking_point_campaign, "camp-code-copy-{label}-{n:03d}",
        ray_factory=E.default_code_copy_ray, protocol_note=_CODE,
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "I.noop", "I", "Moves that never happened",
        "Does it keep track of where things are when the story mentions moves that were only considered, planned or refused?",
        "no-op events about the queried object 0 -> 8, chain length pinned at 8", "axis",
        E.run_breaking_point_campaign, "camp-noop-{label}-{n:03d}", ray_factory=E.default_noop_ray,
        protocol_note="plain prompt, one story", legacy_aliases=(),
        default_kwargs={"query_kind": "state", "budget": 120, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "I.premise_order", "I", "Order from relative cues (control)",
        "Can it recover the order of events when the story gives 'before/after' cues instead of clock times, told out of order?",
        "relational cues 0 -> 100% together with scrambled narration 0 -> 100%, chain length pinned at 8", "axis",
        E.run_breaking_point_campaign, "camp-premise-order-{label}-{n:03d}", ray_factory=E.default_premise_order_ray,
        protocol_note="plain prompt, one story; expected small effect (control)", legacy_aliases=(),
        default_kwargs={"query_kind": "order", "budget": 120, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "I.trivial_belief", "I", "The false-belief story with the trap removed",
        "When the person actually saw the last move, does the model answer the true place, or the classic false-belief trap?",
        "full witness on; chain length 2 -> 10 with distractors 0 -> 12", "axis",
        E.run_breaking_point_campaign, "camp-trivial-belief-{label}-{n:03d}", ray_factory=E.default_trivial_belief_ray,
        protocol_note="plain prompt, one story", legacy_aliases=(),
        default_kwargs={"query_kind": "belief", "budget": 120, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "IV.dialect", "IV", "Declared code dialect (counterfactual conventions)",
        "When the program declares rules that differ from standard Python, does the model follow them, or its Python habit?",
        "program length + 0 -> 4 declared deviations from Python (inclusive range, truncating %, copy on assign, fresh defaults)",
        "axis", E.run_code_breaking_point_campaign, "camp-code-dialect-{label}-{n:03d}",
        ray_factory=E.default_code_dialect_ray, protocol_note=_CODE,
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "V.repair", "V", "Code repair (one bug, shown example)",
        "Given a short function with one injected bug, the wrong output it gives and the right one, can the model "
        "fix it -- including when the fix is also checked on inputs it was not shown and the bug sits further from "
        "the returned value?",
        "0 -> 8 hidden checks x bug 1 -> 8 statements away from the return, size pinned at 18",
        "axis", E.run_code_repair_campaign, "camp-code-repair-{label}-{n:03d}",
        ray_factory=E.default_repair_ray, protocol_note=_CODEFIX,
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "V.repair_regressions", "V", "Code repair without regressions (stacked bugs)",
        "When a function has one to three bugs, shifting from simple slips (wrong operator, off by one) to "
        "behaviour bugs (shared lists, dropped updates), does the model fix the causes without breaking anything?",
        "1 -> 3 bugs, syntactic -> semantic mix, 4 hidden checks, size pinned at 18",
        "axis", E.run_code_repair_campaign, "camp-code-repair-kinds-{label}-{n:03d}",
        ray_factory=E.default_repair_kind_ray, protocol_note=_CODEFIX,
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "V.write", "V", "Write a function from a description and examples",
        "Given a plain-English description and a few examples, can the model write a function that also gets "
        "unseen inputs and edge cases (empty list, zero, negative) right?",
        "0 -> 3 edge cases in the hidden checks while the examples shrink 3 -> 1",
        "axis", E.run_code_write_campaign, "camp-code-write-{label}-{n:03d}",
        ray_factory=E.default_write_ray, protocol_note=_CODEFIX,
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "IV.chain_depth", "IV", "Dependency depth in code (same size)",
        "When the answer depends on a long chain of updates, each one feeding the next (with the program "
        "kept the same size), does the model keep every step, or lose one along the way?",
        "5 -> 20 dependent updates on the path to the answer, program size pinned at ~22 statements",
        "axis", E.run_code_breaking_point_campaign, "camp-code-depth-{label}-{n:03d}",
        ray_factory=E.default_code_depth_ray, protocol_note=_CODE,
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "IV.stack", "IV", "Stacked list behaviour in code (same size)",
        "When one list is shared under two names, extended, copied and passed to a function before the "
        "answer reads it (plus a branch inside a loop and a while loop), does the model track what really "
        "happens to it?",
        "0 -> 4 list behaviours stacked on one list (shared name + `+=`, `x = x + [..]`, slice copy, "
        "a function that changes its argument) + a branch in a loop + a while loop; size pinned at ~22",
        "axis", E.run_code_breaking_point_campaign, "camp-code-stack-{label}-{n:03d}",
        ray_factory=E.default_code_stack_ray, protocol_note=_CODE,
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    CampaignSpec(
        "IV.gotcha", "IV", "Arithmetic and indexing gotchas in code (same size)",
        "When the program divides negative numbers and slices lists with computed end points, does the "
        "model follow Python's actual rules for rounding and for where a slice stops?",
        "0 -> 3 chained `//` `%` `*` on negative numbers + 0 -> 3 slices with computed or negative ends; "
        "size pinned at ~22 statements",
        "axis", E.run_code_breaking_point_campaign, "camp-code-gotcha-{label}-{n:03d}",
        ray_factory=E.default_code_gotcha_ray, protocol_note=_CODE,
        default_kwargs={"budget": 200, "cost_cap": 0.30, "k_trials": 7, "parallelism": 6}),
    # ---- Province VI: repositories ------------------------------------------
    CampaignSpec(
        "VI.navigate", "VI", "Find a bug's cause across files",
        "When a failing test's cause sits one to six function calls away in another file, does the model follow "
        "the calls to the right file and fix the cause there, not the symptom?",
        "cause 1 -> 6 calls from the tested function x 4 -> 12 files, 20 tool calls",
        "axis", R.run_repo_breaking_point_campaign, "camp-repo-navigate-{label}-{n:03d}",
        ray_factory=R.default_repo_navigate_ray, frontier_rows=("repo_navigate",), protocol_note=_REPO,
        default_kwargs={"budget": 60, "cost_cap": 2.0, "k_trials": 5, "parallelism": 4}),
    CampaignSpec(
        "VI.lookalike", "VI", "Look-alike function names across files",
        "When other files define functions whose names differ by a letter or two from the real ones, does the "
        "model call and import the functions that exist -- or ones that only look right?",
        "look-alike twins on x 4 -> 12 files, cause 1 -> 3 calls away",
        "axis", R.run_repo_breaking_point_campaign, "camp-repo-lookalike-{label}-{n:03d}",
        ray_factory=R.default_repo_lookalike_ray, frontier_rows=("repo_lookalike",), protocol_note=_REPO,
        default_kwargs={"budget": 60, "cost_cap": 2.0, "k_trials": 5, "parallelism": 4}),
    CampaignSpec(
        "VI.clone", "VI", "A near-copy of the buggy function elsewhere",
        "When another file holds a near-copy of the buggy function that nothing calls, does the model fix the "
        "function that actually runs?",
        "near-miss clone on x 4 -> 12 files, cause 1 -> 3 calls away",
        "axis", R.run_repo_breaking_point_campaign, "camp-repo-clone-{label}-{n:03d}",
        ray_factory=R.default_repo_clone_ray, frontier_rows=("repo_clone",), protocol_note=_REPO,
        default_kwargs={"budget": 60, "cost_cap": 2.0, "k_trials": 5, "parallelism": 4}),
    CampaignSpec(
        "VI.budget", "VI", "Tool-call budget for a cross-file fix",
        "How few tool calls does the model need to find and fix a cause three calls away in an eight-file "
        "repository?",
        "tool budget 30 -> 6 calls, cause 3 calls away, 8 files",
        "axis", R.run_repo_breaking_point_campaign, "camp-repo-budget-{label}-{n:03d}",
        ray_factory=R.default_repo_budget_ray, frontier_rows=("repo_budget",), protocol_note=_REPO,
        default_kwargs={"budget": 60, "cost_cap": 2.0, "k_trials": 5, "parallelism": 4}),
    CampaignSpec(
        "VI.control", "VI", "The same program over more files (navigation control)",
        "Does the model fix the same bug in the same program less often when that program is split over more "
        "files?",
        "1 -> 8 files, program pinned at 8 functions, cause 1 call away",
        "axis", R.run_repo_breaking_point_campaign, "camp-repo-control-{label}-{n:03d}",
        ray_factory=R.default_repo_control_ray, frontier_rows=("repo_control",),
        protocol_note=_REPO + " At one file this is Province V's task, so the drop along the ray is the cost "
                              "of navigation itself.",
        default_kwargs={"budget": 60, "cost_cap": 2.0, "k_trials": 5, "parallelism": 4}),
    CampaignSpec(
        "VI.graph_value", "VI", "Value of a code-graph tool (paired)",
        "On identical repositories, does a tool that lists a function's callers and callees shorten the model's "
        "search for the cause -- and does it change whether the bug gets fixed?",
        "get_code_neighbors off vs on, same repositories, 4 and 8 files, cause 3 calls away",
        "rate", R.run_graph_value_campaign, "camp-repo-graph-{label}-{n:03d}",
        frontier_rows=("repo_graph_value",),
        protocol_note=_REPO + " Paired: <run_id>/graph_off and <run_id>/graph_on hold the two arms; the H4 "
                              "statistics (Wilcoxon on calls before the cause's file is read, McNemar on "
                              "verdicts) are in the result note.",
        default_kwargs={"levels": (4, 8), "n_seeds": 10, "cost_cap": 3.0, "parallelism": 4}),
    # ---- Evidence fidelity (cross-cutting condition grid) --------------------
    CampaignSpec(
        "EF.belief", "EF", "Evidence vs prior: other people's beliefs",
        "When a story makes clear where a person last saw something, does the model answer from what that "
        "person saw, or from where the object really is -- and does restating or flagging what they saw help?",
        "A the person saw every move / B they missed the last ones / C what they saw restated before the "
        "question / D the same sentences flagged in place", "battery", EFC.run_ef_belief,
        "camp-ef-belief-{label}-{n:03d}", frontier_rows=("ef_belief",), protocol_note=_EF,
        default_kwargs={"n_trials": 20, "cost_cap": 0.5, "parallelism": 6}),
    CampaignSpec(
        "EF.tools", "EF", "Evidence vs prior: exact tool ids",
        "When tool ids are near-misspellings of what they 'should' be called, does the model copy the exact "
        "ids -- and does restating or flagging the ids help?",
        "A natural ids / B look-alike ids / C every id restated at the end / D ids flagged in the catalog",
        "battery", EFC.run_ef_tools, "camp-ef-tools-{label}-{n:03d}", frontier_rows=("ef_tools",),
        protocol_note=_EF, default_kwargs={"n_trials": 20, "cost_cap": 0.5, "parallelism": 6}),
    CampaignSpec(
        "EF.tickets", "EF", "Evidence vs prior: carried values (tickets)",
        "When a tool returns an opaque ticket a later call needs, does the model copy it or invent a "
        "plausible one -- and does restating or flagging the ticket help?",
        "A the ticket is guessable / B opaque, 3 calls later / C restated in every result / D flagged when "
        "issued / X the dependency chain restated (no values)", "battery", EFC.run_ef_tickets,
        "camp-ef-tickets-{label}-{n:03d}", frontier_rows=("ef_tickets",), protocol_note=_EF + " " + _TEXT_TOOLS,
        default_kwargs={"n_trials": 20, "cost_cap": 1.0, "parallelism": 4}),
    CampaignSpec(
        "EF.comments", "EF", "Evidence vs prior: misleading code comments",
        "When a comment asserts a wrong value for a line of code, does the model trace the code or trust the "
        "comment -- and does flagging the line help?",
        "A neutral comments / B misleading comments / C not applicable (it would leak the answer) / D the line "
        "flagged / E the comment itself shouted (exploratory)", "battery", EFC.run_ef_comments,
        "camp-ef-comments-{label}-{n:03d}", frontier_rows=("ef_comments",), protocol_note=_EF,
        default_kwargs={"n_trials": 20, "cost_cap": 0.5, "parallelism": 6}),
    CampaignSpec(
        "EF.repair", "EF", "Evidence vs prior: where a bug's cause is",
        "When the failing test names one file and the cause sits in another, does the model fix the cause -- "
        "and does showing the data flow, or flagging expected vs actual, help?",
        "A the bug is in the test's own file / B two calls away / C the data flow shown / D expected vs actual "
        "aligned / X the symbol table restated", "battery", EFC.run_ef_repair,
        "camp-ef-repair-{label}-{n:03d}", frontier_rows=("ef_repair",), protocol_note=_EF + " " + _REPO,
        default_kwargs={"n_trials": 20, "cost_cap": 2.0, "parallelism": 4}),
    CampaignSpec(
        "EF.transfer", "EF", "Evidence vs prior: which help fixes which failure (H-EF4)",
        "Does the help that fixes carried values (restating them) also fix bug-finding, and does the help that "
        "fixes bug-finding (the data flow) also fix carried values?",
        "tickets B / C restated values / X restated dependency chain; repair B / C data flow / X restated "
        "symbols", "battery", EFC.run_ef_transfer, "camp-ef-transfer-{label}-{n:03d}", protocol_note=_EF,
        default_kwargs={"n_trials": 20, "cost_cap": 2.0, "parallelism": 4}),
    # ---- Cross-cutting -----------------------------------------------------
    CampaignSpec(
        "X.full_map", "X", "Full narrative battery",
        "What can this model do with stories, in one launch: state, order, belief, traps and breaking point?",
        "everything in Province I, one budget", "battery", E.run_full_map_campaign,
        "camp-full_map-{label}-{n:03d}", frontier_rows=("state", "order", "belief", "adversarial_belief"),
        protocol_note=_NARRATIVE + " Child runs per stage under <run_id>/<stage>.",
        legacy_aliases=("full_map", "narrative"), default_kwargs={"budget": 600, "cost_cap": 1.00,
                                                                 "parallelism": 6}),
    CampaignSpec(
        "X.consistency", "X", "Consistency (pass^k)",
        "If you give the model the exact same task five times, does it get it right all five times?",
        "repeats of one fixed mid-difficulty task (k = 5 per task)", "rate", run_consistency_campaign,
        "camp-consistency-{label}-{n:03d}", frontier_rows=("consistency",),
        protocol_note=("Default: Province II tool planning at plan_depth 11 with look-alike names; "
                       "evaluator_kind='interactive'|'code' switches province. Temperature 0, so repeats "
                       "measure serving non-determinism. pass^k is in the result + "
                       "results/consistency/<run_id>.json; the frontier row shows pass^1."),
        default_kwargs={"m_tasks": 12, "k": 5, "cost_cap": 0.5, "parallelism": 4}),
)

REGISTRY: dict[str, CampaignSpec] = {s.id: s for s in _SPECS}
assert len(REGISTRY) == len(_SPECS), "duplicate campaign id"

_ALIASES: dict[str, str] = {}
for _s in _SPECS:
    for _a in _s.legacy_aliases:
        assert _a not in _ALIASES and _a not in REGISTRY, f"alias {_a!r} is ambiguous"
        _ALIASES[_a] = _s.id


def resolve(name: str) -> CampaignSpec:
    """A registry id (`II.traps`) or any legacy alias (`tool_traps`,
    `tool_breaking_point`) -> its `CampaignSpec`. Case-insensitive on aliases."""
    if name in REGISTRY:
        return REGISTRY[name]
    for key in (name, name.lower(), name.replace("-", "_").lower()):
        if key in _ALIASES:
            return REGISTRY[_ALIASES[key]]
    lowered = {k.lower(): k for k in REGISTRY}
    if name.lower() in lowered:
        return REGISTRY[lowered[name.lower()]]
    raise KeyError(f"unknown campaign {name!r}; see `python3 scripts/run_campaign.py --list`")


def by_province() -> dict[str, list[CampaignSpec]]:
    out: dict[str, list[CampaignSpec]] = {p: [] for p in PROVINCES}
    for s in _SPECS:
        out[s.province].append(s)
    return out


def _existing_run_ids(store_path: str) -> set[str]:
    if not store_path or not os.path.exists(store_path):
        return set()
    con = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        return {rid for (rid,) in con.execute("SELECT DISTINCT run_id FROM experiments")}
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


def next_run_id(spec: CampaignSpec, label: str, store_path: str) -> str:
    """The run id for a new run of `spec` under `label`.

    Templates with `{n}` get the next free number after the highest one in
    the store (the `-NNN` convention the interactive and ticket scripts use
    today: gaps are never refilled). Pinned templates return their fixed id
    -- frontier.SPECS matches that exact number -- so a re-run appends to the
    existing run, exactly as `scripts/run_all_provinces.py` does."""
    if spec.pinned:
        return spec.run_id(label)
    head, _, tail = spec.run_id_template.partition("{n")
    tail = tail.split("}", 1)[1]
    rx = re.compile("^" + re.escape(head.format(label=label)) + r"(\d+)" + re.escape(tail.format(label=label)) + "$")
    nums = [int(m.group(1)) for rid in _existing_run_ids(store_path) if (m := rx.match(rid))]
    return spec.run_id(label, max(nums, default=0) + 1)


# ---------------------------------------------------------------------------
# Catalog (docs/campaign-catalog.md)
# ---------------------------------------------------------------------------


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def catalog_markdown() -> str:
    from .tiers import TIERS

    lines = [
        "# Campaign catalog",
        "",
        "Generated by `python3 -m cartographer.campaigns.registry --write-catalog` from",
        "`cartographer/campaigns/registry.py` -- edit the registry, not this file.",
        "",
        "Every campaign is named `<province>.<stressor>`: which part of the map it explores and what it",
        "turns up until the model breaks. Older names (/launch presets, `run_all_provinces.py` stages,",
        "ticket rays) still work as aliases. Run one with",
        "`python3 scripts/run_campaign.py --campaign II.traps --tier standard --model ... --label ...`.",
        "",
        "Evidence kinds: **axis** = an escalation ray, bisected to the level where the pass rate collapses;",
        "**rate** = a pass rate at fixed difficulty (or over a sweep); **battery** = several campaigns in one",
        "launch.",
        "",
    ]
    for prov, specs in by_province().items():
        if not specs:
            continue
        lines += [f"## Province {prov} -- {PROVINCES[prov]}", "",
                  "| id | question | what is turned up | evidence | legacy alias | frontier rows fed |",
                  "|---|---|---|---|---|---|"]
        for s in specs:
            alias = ", ".join(f"`{a}`" for a in s.legacy_aliases) or "--"
            rows = ", ".join(f"`{r}`" for r in s.frontier_rows) or "--"
            if s.frontier_labels:
                rows += f" (labels: {', '.join(s.frontier_labels)} only)"
            lines.append(f"| `{s.id}` | {_cell(s.question)} | {_cell(s.stressor)} | {s.evidence_kind} | "
                         f"{alias} | {rows} |")
        lines.append("")
    lines += [
        "## Evidence tiers",
        "",
        "One dial for how much evidence to buy -- not how hard the tasks are (escalation finds that).",
        "",
        "| tier | trials per level | budget | cost cap | early stop | resample gate | confirm | decompose "
        "| reportable |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    yn = {True: "yes", False: "no"}
    for name, t in TIERS.items():
        rep = "yes" if t.reportable else f"no (grade {t.grade_ceiling} ceiling)"
        lines.append(f"| `{name}` | {t.k_trials} | x{t.budget_scale:g} | x{t.cost_scale:g} | {yn[t.early_stop]} | "
                     f"{yn[t.resample_gate]} | {yn[t.confirm]} | {yn[t.decompose]} | {rep} |")
    lines += [
        "",
        "## Run-id notes",
        "",
        "- Pinned run ids (no `NNN` increment) -- `frontier.SPECS` matches the exact number, so a re-run",
        "  appends to the same run: " + ", ".join(f"`{s.run_id_template}`" for s in _SPECS if s.pinned) + ".",
        *(["- Label whitelists in `frontier.SPECS` -- runs under other labels are stored but do not show on the",
           "  frontier page: " + "; ".join(f"`{s.id}` ({', '.join(s.frontier_labels)})"
                                           for s in _SPECS if s.frontier_labels) + "."]
          if any(s.frontier_labels for s in _SPECS) else []),
        "- /launch builds its own run ids (`camp-<preset>-<model-short>-NNN`); for presets whose frontier",
        "  rows filter by run id (`tool_breaking_point`, `code_tracing`) those runs do not reach the frontier.",
        "",
    ]
    return "\n".join(lines)


def write_catalog(path: str = "docs/campaign-catalog.md") -> str:
    text = catalog_markdown()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


if __name__ == "__main__":
    if "--write-catalog" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--write-catalog"]
        print(f"wrote {write_catalog(rest[0] if rest else 'docs/campaign-catalog.md')}")
    else:
        print(catalog_markdown())
