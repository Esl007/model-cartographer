"""/findings -- the capability map.

Hard-coded, auditable summary of confirmed findings from `docs/lab-log.md`.
Each entry carries the date(s) and run_id(s) it's sourced from so a reader
can go verify it against the log and the results store directly -- this
page makes claims, and every claim here must be traceable.

Retracted findings are kept (not deleted) in a collapsed section: the
witness-rendering bug produced a real, later-retracted "false belief
floor" result, and showing that alongside the corrected numbers is a
deliberate demonstration of the instrument's self-auditing (see
docs/lab-log.md's 2026-09-17 witness-bug entries).
"""

from __future__ import annotations

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    name: str
    mechanism: str
    evidence: str
    status: str  # "confirmed" | "needs-replication" | "retracted"
    date: str
    run_ids: tuple[str, ...]


FINDINGS: tuple[Finding, ...] = (
    Finding(
        name="Belief-to-reality collapse (witness-rendering bug, corrected)",
        mechanism=(
            "Once non-actor witnesses are actually rendered into prose, "
            "Nemotron Super shows NO belief/reality collapse: it tracks "
            "false beliefs correctly across chain_length, scramble, "
            "coref_pressure, and distractor_events."
        ),
        evidence=(
            "camp-belief-nemotron-super-002: 105 calls, baseline 3/3, no "
            "boundary found on any of the four swept dims."
        ),
        status="confirmed",
        date="2026-09-17",
        run_ids=("camp-belief-nemotron-super-002",),
    ),
    Finding(
        name="Nano continuity-of-observation over-generalization",
        mechanism=(
            "Nemotron Nano assumes an entity keeps observing an object's "
            "moves even after the story stops placing that entity at the "
            "scene -- it over-extends 'last witnessed' into 'still "
            "watching', producing a false-belief error the true reasoning "
            "chain doesn't support."
        ),
        evidence=(
            "camp-belief-nemotron-nano-001: 105 calls, 93% pass, 7 real "
            "failures verified against regenerated ground truth -- the "
            "first confirmed tier discrimination between Super and Nano."
        ),
        status="confirmed",
        date="2026-09-17",
        run_ids=("camp-belief-nemotron-nano-001",),
    ),
    Finding(
        name="witness_cutoff generalizes the Nano failure mode",
        mechanism=(
            "Sweeping witness_cutoff (how early an entity's last witnessed "
            "exposure to an object is cut off from the object's later "
            "moves) reproduces the same over-generalization systematically: "
            "at witness_cutoff=0.7, belief/truth divergence hits 94%."
        ),
        evidence="docs/lab-log.md 2026-09-17 entry, commit 313e3b2.",
        status="confirmed",
        date="2026-09-17",
        run_ids=(),
    ),
    Finding(
        name="Ultra protagonist-continuity pronoun bias",
        mechanism=(
            "Ultra misresolves pronouns toward the story's protagonist "
            "specifically -- an ablation that swaps pronoun/name surface "
            "form while holding the answer key fixed separates this "
            "cleanly from ordinary coreference noise in 6/6 ablated cases."
        ),
        evidence=(
            "5 Super belief failures verified real against regenerated "
            "ground truth: 4 were pronoun misresolution, 1 was a "
            "continuity over-generalization (first recorded Super "
            "failures) -- ablation catalog run gave 6/6 separation."
        ),
        status="confirmed",
        date="2026-09-17",
        run_ids=(),
    ),
    Finding(
        name="Ultra abstention under trap pressure",
        mechanism=(
            "Under adversarial (foil-selected) belief queries at high trap "
            "density, Ultra sometimes abstains/answers 'unknown' rather "
            "than committing to a wrong-but-plausible foil answer -- 2 "
            "stable cases so far, not yet replicated on fresh seeds."
        ),
        evidence=(
            "2 stable failures observed during adversarial_belief probing; "
            "flagged explicitly as unreplicated in docs/lab-log.md."
        ),
        status="needs-replication",
        date="2026-09-17",
        run_ids=(),
    ),
    Finding(
        name="Trap-selection breaks Ultra where magnitude escalation didn't",
        mechanism=(
            "Escalating every difficulty dim to its generator ceiling "
            "(chain_length 25, scramble 1.0, coref_pressure 8, "
            "distractor_events 40, filler_events 60, witness_cutoff 0.9 "
            "simultaneously) never broke Ultra. Adversarial seed search "
            "at a much shorter chain_length=2 -- picking the single world "
            "among many candidates where a plausible-but-wrong foil answer "
            "most directly competes with the truth -- did break it."
        ),
        evidence=(
            "run_breaking_point_campaign (escalation ray, t up to 1.0): "
            "no breaking point found. run_adversarial_belief_campaign at "
            "chain_length=2: confirmed failures. This is the strategic "
            "headline result: difficulty magnitude and trap salience are "
            "different axes, and only the second one moved Ultra."
        ),
        status="confirmed",
        date="2026-09-17",
        run_ids=(),
    ),
)

RETRACTED: tuple[Finding, ...] = (
    Finding(
        name="[RETRACTED] Nemotron Super has an intrinsic false-belief floor",
        mechanism=(
            "First read of camp-belief-nemotron-super-001 (baseline only "
            "3/5) looked like a real ~60%% capability ceiling on false "
            "belief tracking, independent of surface difficulty."
        ),
        evidence=(
            "Root cause was a renderer bug, not a model limitation: "
            "cartographer/world/simulate.py assigned beliefs to every "
            "witness, but cartographer/render/templates.py only ever "
            "interpolated the acting entity into prose -- non-actor "
            "witnesses were silently never mentioned in the story the "
            "model actually read. The model was correctly saying "
            "'unknown' about entities it was never told were present. "
            "Fixed in commit 5bd297f; corrected re-run "
            "(camp-belief-nemotron-super-002) reversed the result "
            "completely -- see the corrected finding above."
        ),
        status="retracted",
        date="2026-09-17",
        run_ids=("camp-belief-nemotron-super-001",),
    ),
)


_STATUS_PILL_CLASS = {
    "confirmed": "PASS",
    "needs-replication": "ERROR",
    "retracted": "FAIL",
}


def _status_pill(status: str) -> str:
    pill_cls = _STATUS_PILL_CLASS.get(status, "REJECTED_BY_FIDELITY_GATE")
    label = status.replace("-", " ")
    return f'<span class="pill pill-{pill_cls}">{html.escape(label)}</span>'


def _finding_card(f: Finding) -> str:
    run_ids_html = (
        " &middot; ".join(f'<span class="mono">{html.escape(r)}</span>' for r in f.run_ids)
        if f.run_ids
        else "-"
    )
    return f"""
    <div class="panel">
      <p style="margin:0 0 0.35rem;"><strong>{html.escape(f.name)}</strong> {_status_pill(f.status)}</p>
      <p class="meta" style="margin:0 0 0.4rem;">{html.escape(f.mechanism)}</p>
      <p class="meta" style="margin:0;">evidence: {html.escape(f.evidence)}</p>
      <p class="meta" style="margin:0.3rem 0 0;">date: {html.escape(f.date)} &middot; run_id(s): {run_ids_html}</p>
    </div>
    """


def findings_html() -> str:
    """Pure function, no store dependency -- every entry here is a
    hard-coded, dated, run_id-tagged claim from docs/lab-log.md, not a
    live query. If the store's numbers ever drift from what's written
    here, docs/lab-log.md and this module both need a manual update.
    """
    from .serve import _shell

    cards = "".join(_finding_card(f) for f in FINDINGS)
    retracted_cards = "".join(_finding_card(f) for f in RETRACTED)

    body = f"""
    <h1>Findings</h1>
    <p class="subtitle">The capability map: every confirmed (or still-open)
    result this instrument has produced so far, sourced from
    <span class="mono">docs/lab-log.md</span>. Each card names its
    run_id(s) so you can go verify it in the results store yourself.</p>
    <p class="meta">Note (2026-09-19): campaigns now auto-verify failures by
    default (resampling each FAIL 3x against the exact stored prompt before
    trusting it) -- see run detail pages for per-failure verified-stable /
    noise / unverified status. This page's curated findings above predate
    that change and are unaffected (each was already resample-confirmed by
    hand at the time, per its own evidence line).</p>

    {cards}

    <details>
      <summary>Retractions ({len(RETRACTED)})</summary>
      <p class="meta">Kept deliberately, not deleted -- self-skepticism is
      a feature of this instrument, not a blemish. A retracted finding
      means the audit trail caught its own mistake before it shipped as a
      public claim.</p>
      {retracted_cards}
    </details>
    """
    return _shell("Findings", "findings", body)
