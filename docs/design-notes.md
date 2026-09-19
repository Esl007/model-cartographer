# Design notes: viewer presentation redesign (2026-09-19)

Prompted by direct feedback: the viewer read as generic dark-dashboard "AI
slop" — pages and copy had accumulated over two prior passes, but layout,
IA, and visual restraint never got a real design pass. This is that pass.
Research-first: below are the principles that drove the rewrite, each
tagged with where it shows up in `cartographer/viewer/`.

## Information architecture (Shneiderman + Nielsen)

1. **Overview first, zoom/filter, details on demand** (Shneiderman's
   visual information-seeking mantra). `/` now shows conclusions
   ("what did we find") with links into `/run/<id>` for full evidence;
   `/run/<id>` shows the verdict paragraph before the raw per-trial
   table; `/record/<id>` shows the Q/A/verdict before the raw
   prompt/reasoning blobs (now behind `<details>`).
2. **Match between system and the real world** (Nielsen #2): nav is
   grouped by the user's actual job — Results (Overview, Findings, Runs),
   Explore (Atlas, Coverage), Act (Launch, Live) — not by when the page
   was built.
3. **Recognition over recall** (Nielsen #6): breadcrumbs on every detail
   page (`Runs → run/<id> → record/<id>`) instead of relying on back-button
   or memory of how you got there.
4. **User control and freedom** (Nielsen #3): every dead-end page
   (`/record/<id>`) gets an escape hatch up (breadcrumb) and sideways
   (prev/next trial within the same run).
5. **Visibility of system status** (Nielsen #1): `/live` keeps a stable
   layout across its 5s auto-refresh (fixed-height stat grid, no reflow)
   so status is scannable at a glance, not something you have to re-find
   after every refresh.
6. **Error prevention + help users recognize/recover from errors**
   (Nielsen #5/#9): `/launch` shows cost/duration expectations before
   submit, not after; every "no data yet" state gets a specific pointer
   ("No runs yet — Launch your first campaign") instead of an empty table.
7. **Progressive disclosure**: nothing below the fold on `/` competes with
   the 3 first-time-visitor jobs (understand → see findings → run a test).
   Everything else (raw run table, per-model matrix) is one click away.
8. **Consistency and standards** (Nielsen #4): one grid unit, one card
   pattern (`.panel`), one pill component, reused verbatim across all 9
   pages instead of one-off `<div style=...>` blocks.
9. **Aesthetic and minimalist design** (Nielsen #8): cut the two-stat-box
   "Total trials / Total spend" grid that duplicated numbers already in
   the model×kind table; cut redundant meta-text sentences that repeated
   the h1.

## Visual design (Linear / Vercel / Grafana / Datadog / Observable-class restraint)

10. **One accent, used sparingly.** `--accent` (#2DD4BF, kept — it's part
    of the "precision lab equipment" brand identity, not a scientific
    conclusion) is now reserved for: links, the primary CTA, and the
    active nav item. Nothing else uses it. Previously the same teal
    leaked into progress bars, stat numbers, and borders — diluting it
    from "signal" to "decoration."
11. **Semantic color only.** Green/red/amber (`--pass`/`--fail`/`--error`)
    are reserved exclusively for verdict states. No other UI element uses
    saturated color — this is what stops a page from reading as "slop":
    slop uses color decoratively, an instrument uses color as data.
12. **Elevation via surface steps, not borders on every box.** Old CSS
    put a 1px border around every `.stat-box`, `pre.block`, table, — the
    classic "everything is a bordered card" AI-generated-dashboard tell.
    New system: 3 surface levels (`--bg` → `--surface-1` → `--surface-2`)
    differentiated by subtle background-lightness steps; borders now only
    appear where they separate genuinely different regions (header,
    table rules), not around every content block.
13. **Real type scale, max 4 sizes.** Collapsed the ad hoc h1/h2/stat-num/
    meta font-sizes into a defined scale: 28/20/15/13px (display, h2,
    body, small/mono-label), each with a defined weight and
    letter-spacing — used consistently rather than one-off inline sizes.
14. **Generous whitespace over dense boxing.** Increased vertical rhythm
    between sections (`--space-*` scale) instead of relying on bordered
    boxes to create visual separation.
15. **Uppercase-tracking labels used sparingly**, only for the smallest
    tier (table headers, stat labels, pills) — never for body text or
    headings, which was creeping in before.

## Dark-theme specifics

16. **Desaturated text hierarchy, not just one gray.** Three text tones
    (`--text` full, `--text-secondary`, `--text-muted`) replace the old
    2-tone system so hierarchy reads through opacity/weight rather than
    borders and boxes.
17. **Accessible contrast.** Verdict pills switched from solid-fill
    color-on-black chips (low contrast, "sticker" look) to a
    dot-plus-label pattern: a small solid dot for color, label text in
    the normal text color — keeps semantic color as an accent, not a
    block of saturated color competing with content, and improves
    contrast for the fail/error labels people most need to read quickly.
18. **Background never pure black.** Kept `#111418`/`#171b21` steps
    (already correct — near-black not true-black avoids halation on
    dark-adapted eyes); extended the same logic to add a 3rd step.

## Micro-typography for a numbers-heavy instrument

19. **Tabular numerals everywhere numbers appear in a column**
    (`font-variant-numeric: tabular-nums`) — pass rates, costs, token
    counts, timestamps in tables now line up vertically instead of
    jittering with proportional digit widths.
20. **Right-align numeric columns**, left-align text columns — tables
    previously left-aligned everything including cost/token/rate columns,
    which is the single most common "doesn't look considered" tell in a
    data table.
21. **Mono reserved for identifiers/code**, not for every number — mixed
    mono/sans use was inconsistent before; now mono is used specifically
    for run_ids, model names, and raw values, sans (with tabular figures)
    for aggregate stats.

## Task-first structure

22. **Three jobs above the fold on `/`**: (a) one-sentence "what this is,"
    (b) a findings-forward summary ("what we found" with a headline
    finding + link to full map), (c) one primary CTA button ("Map a
    model" → `/launch`). Model×kind matrix and quick-links grid demoted
    below, still present, not primary.
23. **`/launch` as a form that teaches**: preset radio-cards (not a bare
    `<select>`), `full_map` visually marked as the recommended default,
    remaining presets collapsed under "Advanced," and a live-updating
    cost/duration estimate line above the submit button. Submit button
    copy changed from generic "Start" to "Start mapping <model>."
24. **`/live` stage indicator** for `full_map` runs: state ✓ → order →
    belief → adversarial_belief → breaking_point, rendered as a fixed-
    width step tracker so progress is legible without reading numbers.
25. **Kept the brand**: teal accent, monospace identifiers, terse
    declarative copy ("a synthetic story world with a known ground
    truth") — this is restraint applied to an existing identity, not a
    generic Bootstrap-dashboard reskin.
