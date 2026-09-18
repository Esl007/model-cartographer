"""/coverage -- what we test and what we don't.

Pure static content describing the instrument's actual surface area:
query kinds, difficulty dimensions, foil traps, and analysis machinery,
sourced from module docstrings (`cartographer/world/generate.py`,
`cartographer/world/foils.py`, `cartographer/schedule/scheduler.py`,
`cartographer/schedule/escalate.py`, `cartographer/ablate/ablations.py`,
`cartographer/confirm/confirmation.py`) plus an honest "not yet covered"
roadmap list. Every claim here must stay accurate to the code -- if a dim
or foil is renamed/added, update this module alongside it.
"""

from __future__ import annotations

QUERY_KINDS: tuple[tuple[str, str], ...] = (
    ("state", "Where is the object actually located right now? Pure event-log replay, no perspective involved."),
    ("belief", "Where does entity X believe the object is? Theory-of-mind: depends on what X actually witnessed or was told, which can diverge from the true state."),
    ("order", "Which of two events happened first? Tests recovering true chronology from narration that may be presented out of order."),
)

DIFFICULTY_DIMS: tuple[tuple[str, str], ...] = (
    ("chain_length", "Number of steps in the causal event chain (object moves/transfers) the model must track."),
    ("scramble", "0.0-1.0: how much the narration order is shuffled relative to true chronology (every sentence still carries its real timestamp)."),
    ("coref_pressure", "Number of extra coreference-stressing mentions (pronouns, repeated near-identical entities) injected into the story."),
    ("distractor_events", "Number of plausible-looking but query-irrelevant events involving other entities/objects mixed into the narration."),
    ("filler_events", "Number of narratively neutral padding events that lengthen the story without touching the query's answer."),
    ("witness_cutoff", "0.0-1.0: how early an entity's last witnessed exposure to the object is cut off relative to the object's later moves -- stresses whether a model over-generalizes 'last witnessed' into 'still watching'."),
)

FOIL_TRAPS: tuple[tuple[str, str], ...] = (
    ("final_true_state", "The object's actual final location -- the most damaging trap for belief queries: what a model says if it collapses belief into reality."),
    ("last_mentioned_location", "The last location named in narration order, regardless of true chronology or who witnessed it."),
    ("last_witnessed_by_anyone", "The destination of the last move ANYONE witnessed, not necessarily the query agent."),
    ("query_agent_own_last_move", "If the query agent themselves moved the object, their own last such move's destination (only applies when the agent moved it)."),
    ("most_frequent_location", "The most-mentioned destination across the whole story, trusting mention frequency over recency or truth."),
)

ANALYSIS_MACHINERY: tuple[tuple[str, str], ...] = (
    ("escalation", "Walks a single ray through the full joint difficulty space (all dims at once, base to generator ceiling) to find where a model actually breaks -- see cartographer/schedule/escalate.py."),
    ("resample-gate", "The scheduler's explore/refine waves re-probe weak/ambiguous signals with additional resamples before committing to a boundary, instead of trusting a single trial."),
    ("ablation", "Deterministic, answer-key-preserving prompt transforms over the same latent world, used to causally isolate which surface factor a failure actually depends on -- see cartographer/ablate/ablations.py."),
    ("fresh-seed confirmation", "Re-tests a decompose hypothesis on seeds that were never used during the search that produced it (seed_base >= 10,000 by convention), reporting an effect size + p-value rather than a bare pass/fail -- see cartographer/confirm/confirmation.py."),
)

NOT_YET_COVERED: tuple[str, ...] = (
    "Tool/action planning (multi-step agentic tool use)",
    "Instruction-following under explicit constraints",
    "Code generation or code understanding",
    "Quantitative/numeric reasoning",
    "Long-context retrieval (needle-in-haystack style)",
)


def _rows(pairs: tuple[tuple[str, str], ...]) -> str:
    import html

    return "".join(
        f'<tr><td class="mono">{html.escape(name)}</td><td>{html.escape(desc)}</td></tr>'
        for name, desc in pairs
    )


def coverage_html() -> str:
    from .serve import _shell

    query_rows = _rows(QUERY_KINDS)
    dim_rows = _rows(DIFFICULTY_DIMS)
    foil_rows = _rows(FOIL_TRAPS)
    machinery_rows = _rows(ANALYSIS_MACHINERY)
    roadmap_items = "".join(f"<li>{__import__('html').escape(item)}</li>" for item in NOT_YET_COVERED)

    body = f"""
    <h1>Coverage</h1>
    <p class="meta">What this instrument tests, and what it honestly
    doesn't yet -- the full inventory of query kinds, difficulty
    dimensions, foil traps, and analysis machinery, plus a roadmap of
    gaps.</p>

    <h2>Query kinds (3)</h2>
    <table><thead><tr><th>kind</th><th>what it tests</th></tr></thead>
    <tbody>{query_rows}</tbody></table>

    <h2>Difficulty dimensions (6)</h2>
    <table><thead><tr><th>dim</th><th>what it varies</th></tr></thead>
    <tbody>{dim_rows}</tbody></table>

    <h2>Foil traps (5)</h2>
    <p class="meta">Plausible-but-wrong answers a shallow reasoner might
    give on a belief query, used by adversarial seed search to select the
    world that most sharply pits a trap against the truth.</p>
    <table><thead><tr><th>trap</th><th>mechanism</th></tr></thead>
    <tbody>{foil_rows}</tbody></table>

    <h2>Analysis machinery</h2>
    <table><thead><tr><th>tool</th><th>what it does</th></tr></thead>
    <tbody>{machinery_rows}</tbody></table>

    <h2>Not yet covered</h2>
    <p class="meta">Roadmap -- capabilities this instrument does not probe today.</p>
    <ul>{roadmap_items}</ul>
    """
    return _shell("Coverage", "coverage", body)
