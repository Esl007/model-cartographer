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

# (province, kind, what it asks) -- every query kind a campaign records,
# grouped under the province whose flame it feeds on the Map. Questions
# follow docs/campaign-catalog.md.
QUERY_KINDS: tuple[tuple[str, str, str], ...] = (
    ("Narrative", "state", "After a long chain of moves, where is each object really? Pure event-log replay, no perspective involved."),
    ("Narrative", "order", "When a story is told out of order, which of two events really happened first?"),
    ("Narrative", "belief", "Where does a person think an object is, based only on what that person saw or was told?"),
    ("Narrative", "adversarial_belief", "Does belief tracking survive stories searched so the tempting wrong answer is planted right there?"),
    ("Tool planning", "tool_plan", "Can the model write a whole plan of tool calls that reaches the goal, through precondition chains, decoys and look-alike tools -- and say so when no plan exists?"),
    ("Interactive", "interactive", "Calling tools one at a time and seeing each result, can the model carry returned values forward, avoid look-alikes and retry a failed call?"),
    ("Code tracing", "code_trace", "Given a generated program, can the model predict a variable's final value through aliasing, closures, look-alike names and declared dialects?"),
    ("Code repair", "code_repair", "Given a function with injected bugs, its wrong output and the right one, can the model fix the cause without breaking hidden inputs?"),
    ("Code repair", "code_write", "From a plain-English description and a few examples, can the model write a function that also gets unseen edge cases right? (Built, not yet run: the spec under-determines some answers.)"),
    ("Repository", "repo_fix", "In a multi-file repository, can the model follow the calls to the file that holds the cause and fix it there, using coding-harness tools?"),
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
    "Instruction-following under explicit constraints",
    "Quantitative/numeric reasoning",
    "Long-context retrieval (needle-in-haystack style)",
    "Image or other non-text inputs",
)


def _rows(pairs: tuple[tuple[str, str], ...]) -> str:
    import html

    return "".join(
        f'<tr><td class="mono">{html.escape(name)}</td><td>{html.escape(desc)}</td></tr>'
        for name, desc in pairs
    )


def coverage_html() -> str:
    from .serve import _shell

    import html as _html

    query_rows = "".join(
        f'<tr><td>{_html.escape(prov)}</td><td class="mono">{_html.escape(kind)}</td><td>{_html.escape(desc)}</td></tr>'
        for prov, kind, desc in QUERY_KINDS
    )
    n_provinces = len({prov for prov, _k, _d in QUERY_KINDS})
    dim_rows = _rows(DIFFICULTY_DIMS)
    foil_rows = _rows(FOIL_TRAPS)
    machinery_rows = _rows(ANALYSIS_MACHINERY)
    roadmap_items = "".join(f"<li>{__import__('html').escape(item)}</li>" for item in NOT_YET_COVERED)

    # Pass 6 composition: each inventory is a card whose header bar names
    # AND counts it (the counts used to be hard-coded into the h2 text --
    # "Query kinds (3)" -- and could silently drift from the tuples right
    # above). Paired two-up: query kinds beside foil traps (both describe
    # what a trial ASKS), difficulty dims beside analysis machinery (both
    # describe how a trial is VARIED and read). The roadmap is
    # deliberately a DIFFERENT kind of card (`.card-gap`, dashed edge):
    # it lists what this instrument can't do, and rendering it identical
    # to the four capability cards above would flatten an honest gap into
    # just another feature list.
    body = f"""
    <div class="page-head"><div class="page-head-main">
    <h1>Coverage</h1>
    <p class="subtitle">What this instrument tests, and what it honestly
    doesn't yet: every province and query kind behind the Map, the
    narrative dials and traps, the analysis machinery, and the gaps.</p>
    </div><div class="page-head-actions">
      <a class="cta-primary" href="/launch">Map a model &rarr;</a>
    </div></div>

    <div class="stat-grid">
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_provinces}</div></div><div class="stat-label">Provinces</div></div>
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{len(QUERY_KINDS)}</div><div class="stat-delta">{len(QUERY_KINDS) - 1} measured</div></div><div class="stat-label" title="Code writing is built but held out of live runs">Query kinds</div></div>
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{len(FOIL_TRAPS)}</div></div><div class="stat-label">Narrative foil traps</div></div>
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{len(NOT_YET_COVERED)}</div></div><div class="stat-label">Known gaps</div></div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">Provinces and query kinds</span>
        <span class="card-meta">{len(QUERY_KINDS)} kinds in {n_provinces} provinces</span>
      </div>
      <table><thead><tr><th>province</th><th>kind</th><th>what it asks</th></tr></thead>
      <tbody>{query_rows}</tbody></table>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-head">
          <span class="card-title">Narrative difficulty dials</span>
          <span class="card-meta">{len(DIFFICULTY_DIMS)}</span>
        </div>
        <table><thead><tr><th>dial</th><th>what it varies</th></tr></thead>
        <tbody>{dim_rows}</tbody></table>
      </div>
      <div class="card">
        <div class="card-head">
          <span class="card-title">Narrative foil traps</span>
          <span class="card-meta">{len(FOIL_TRAPS)}</span>
        </div>
        <table><thead><tr><th>trap</th><th>mechanism</th></tr></thead>
        <tbody>{foil_rows}</tbody></table>
        <p class="meta" style="padding-bottom:14px">The plausible-but-wrong answers a shallow reader gives on a
        belief query; adversarial seed search picks the worlds that pit a trap most sharply against the truth.
        Every other province has its own dials and traps, listed per campaign in
        <span class="mono">docs/campaign-catalog.md</span>.</p>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-head">
          <span class="card-title">Analysis machinery</span>
          <span class="card-meta">{len(ANALYSIS_MACHINERY)}</span>
        </div>
        <table><thead><tr><th>tool</th><th>what it does</th></tr></thead>
        <tbody>{machinery_rows}</tbody></table>
      </div>
      <div class="card card-gap">
        <div class="card-head">
          <span class="card-title">Not yet covered</span>
          <span class="card-meta">{len(NOT_YET_COVERED)} known gaps</span>
        </div>
        <div class="card-body">
          <p class="meta" style="margin-top:0">Capabilities this instrument does not probe today.</p>
          <ul class="prose-list">{roadmap_items}</ul>
        </div>
      </div>
    </div>
    """
    return _shell("Coverage", "coverage", body)
