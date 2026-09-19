# Design notes: viewer visual system (2026-09-19, pass 3)

This replaces the prior version of this file (research-principles list
citing Nielsen/Shneiderman/Linear/Vercel/Grafana). That pass shipped and
the user's verbatim reaction was: "It looks like AI Slop and the UX
sucks... search online for some of the best UI skills." They then pointed
at a specific design methodology (github.com/Dammyjay93/interface-design)
whose own thesis explains the failure: generic research into "what good
dashboards look like" produces the median output any AI would produce
given the same prompt — competent, restrained, and still slop, because it
was imported from somewhere else instead of derived from this product.

This document is the domain exploration that pass skipped, done before any
CSS was touched, followed by what changed in kind (not just polish) as a
result. It is written as this product's actual design brief, not a
reusable heuristics list — if a future pass needs generic UI principles,
it can look them up; this file's job is to keep pass 4 from re-inventing
what's below out of habit.

---

## Intent

**Who is this.** Not "a researcher." A specific moment: someone has just
clicked "Start mapping" on `/launch` against `nvidia/Nemotron-3-Ultra-550b-
a55b`, chosen `full_map`, and switched to `/live`. They watch the stage
tracker move state → order → belief. A FAIL appears in the belief stage.
Their immediate question is not "does this look nice" — it's "is this
real, or is this the Ultra MoE-routing noise that burned an entire day of
this project once already" (the retracted "Priya" claim, `docs/lab-
log.md`, 2026-09-17: a single-trial FAIL reported as a finding, then
4-of-5 resamples came back correct). They click into the record wanting
the exact prompt, whether it survived a resample gate, and if it matches
the project's one dominant named mechanism (`final_true_state` — belief-
to-reality collapse) or something new. Later, reading `/findings`, the
same person needs to trust that a "confirmed" card is safe to cite in a
writeup and a "needs-replication" card is not, at a glance, without
reading the paragraph.

**What they must accomplish, in order:** launch a campaign without
reading source → watch it run and know if it's alive or stuck (the
literal bug fixed earlier today) → read a verdict and believe it or
distrust it correctly → drill into one trial's exact evidence when they
don't believe it → skim `/findings` and instantly separate confirmed
claims from unreplicated ones.

**What this should feel like.** A falsifying instrument, not a product.
Closer to a lab notebook or an oscilloscope than a SaaS dashboard — every
number on screen is evidence for or against a specific claim about a
specific model, most of them claims this project has already caught
itself getting wrong once (the witness-rendering bug, the Priya noise
claim — both in `/findings`'s Retractions). Cold and exact, not warm;
dense with real information, not padded with reassuring whitespace; a
verdict should look like a claim that could be wrong, not a green sticker
that ends the conversation.

## Domain vocabulary (mined from source, not the prior CSS)

Pulled directly from `cartographer/world/foils.py`, `cartographer/report/
verdict.py`, `cartographer/confirm/confirmation.py`, `cartographer/
schedule/escalate.py`, `cartographer/ablate/attribute.py` — the words this
codebase actually uses to talk about itself:

- **witness / witnessed** — an entity either saw an event or didn't;
  belief is built ONLY from what was witnessed, which is the entire
  mechanism the whole project probes (`world/foils.py`'s `_last_witnessed_
  by_anyone`, the `witness_cutoff` dim).
- **belief vs. reality** — the founding tension. A query asks what an
  entity *believes*; the ground truth is what's *actually true*; the
  project's single dominant failure mode is the model collapsing the
  first into the second (`final_true_state` foil, 77.3% of reconstructable
  belief FAILs per the 2026-09-18 foil report).
- **foil / trap** — a plausible-but-wrong answer a shallow reasoner would
  give, planted in the story on purpose (`foil_answers`, `FoilProfile`).
  Not "an error" — a *deliberately laid trap* the model either notices or
  falls into.
- **salience** — how late/loud a trap is planted (`salience_weighted_
  score`, `_mention_lateness`: a foil mentioned in the closing sentence
  outweighs one in the opening sentence). Difficulty in this project is
  redefined as trap salience, not knob magnitude — the project's own
  strategic finding (trap-selection broke Ultra at chain_length=2, the
  *shortest* chain, where max-magnitude escalation at chain_length=25
  never did).
- **verdict** — PASS / FAIL / ERROR. Framed throughout the code as a
  falsifiable claim ("`RunVerdict`... headline... paragraphs"), not a
  binary status light.
- **stable vs. noise** — whether a FAIL survives resampling
  (`resample_failure`, `is_stable_failure`). This is the load-bearing
  distinction that prevents another Priya-style retraction; a FAIL that
  hasn't been checked yet is neither.
- **confirmed / needs-replication / retracted** — the three states a
  finding can be in (`findings.py::Finding.status`), each with a distinct
  trust level a reader must be able to tell apart without reading prose.
- **boundary / frontier / breaking point** — where a model stops holding
  (`schedule/escalate.py`'s `find_breaking_point`, `t_star`, the ramp→
  bisect→logistic-fit pipeline). A boundary is a located, bisected,
  confidence-intervalled thing (`t_ci`), not just "the model got worse."
  The escalation ray runs 0→1: `t=0` is the generator's easy baseline,
  `t=1` is its hard ceiling; if the model never breaks by `t=1`, that's
  reported as `found=False` — an honest negative result, not a failure of
  the instrument.
- **ablation / attribution** — a deterministic, answer-key-preserving
  prompt transform used to causally isolate WHY a failure happens
  (`ablate/attribute.py`'s `attribute_failure`; "cause not isolated by
  current ablation catalog" is a legitimate, expected outcome, exactly
  like escalation's "exceeds generator limits").
- **effect size / Wilson interval / Holm-Bonferroni** — every causal claim
  in this codebase carries a confidence interval and a multiple-
  comparison-corrected p-value (`confirm/confirmation.py`). A confirmed
  finding isn't "it happened once," it's "the effect survived correction
  across the whole family of hypotheses tested."
- **fresh seed / seed_base convention** — confirmation always re-probes on
  seeds that were never used to find the hypothesis in the first place
  (`seed_base >= 10_000` by hard convention vs. search's `< 10_000`) — the
  project's own honesty mechanism against fooling itself with the same
  data twice.
- **lineage** — the exact provenance chain from a stored record back
  through its generation (`store.lineage`, already in the UI as `record →
  record → record` links). This already exists; the redesign makes it
  load-bearing instead of decorative.

## Color world

Not "dark theme, blue-gray, one teal accent" (pass 2's system, kept almost
unchanged from pass 1 under that description). What colors actually exist
in the world this product measures?

- **Ground truth is achromatic.** Simulated fact — `simulate.answer()`,
  the one thing in this codebase that is never in question — reads as
  near-white on near-black: certain, not colored. Color is reserved for
  *claims about* that ground truth, never the ground truth's own display
  (the answer key in a record's evidence trail stays plain text, not
  tinted).
- **Belief is amber, not blue.** A belief query's answer is a claim from a
  limited, witnessed perspective — provisional, capable of being wrong in
  a specific, interesting way. Amber/phosphor (`#D9A441`-family) reads as
  "this is what was witnessed," distinct from the cool achromatic
  certainty of simulated ground truth. This single choice is why the
  verdict system below no longer needs a green/red pill: PASS is "the
  claim matched," which can render in restrained ink, not a stoplight.
- **A trap is not the same red as an infrastructure error.** The old
  system used one `--fail` red for both "the model fell into a
  deliberately laid trap" (a finding — informative, worth reading) and
  "the API timed out" (noise — not informative). Splitting these was the
  single highest-value color decision: `--verdict-fail` (a trap sprung,
  desaturated blood-red, `#C4544B`) is visually distinct from
  `--verdict-error` (infrastructure, flat slate, no hue at all) so a
  reader's eye can filter "real finding" from "run it again."
  `--verdict-pass` stays a restrained, desaturated green
  (`#5C9E7C`) — correct is unremarkable, so it doesn't compete for
  attention the way a trap-sprung FAIL should.
- **Salience gets a literal color ramp, not a size ramp.** Trap
  divergence score (`salience_weighted_score`) is a real measured
  quantity in this codebase; the redesign gives it one (`--salience-low`
  → `--salience-high`, a muted-amber-to-hot-amber ramp) so an adversarial
  record's trap intensity is visible as color temperature, not just a
  number in a table cell.
- **Stable vs. noise is a texture, not a color.** A stable failure
  (survived resampling) renders solid; noise (evaporated on resample)
  renders as the SAME hue at reduced opacity with a diagonal-hatch
  texture underneath — so a reader scanning a page of FAILs can tell
  "real" from "was noise, kept visible not deleted" without reading text,
  matching the project's own stated practice of keeping retractions
  visible rather than erasing them.
- **Surfaces stay achromatic steel/graphite**, never navy or teal-tinted —
  instrumentation black, not "startup dark mode." Verified concretely:
  every surface token below has zero saturation; only semantic/salience
  tokens carry hue.

## The signature: the verdict is a claim with a visible evidence trail, not a chip

The single element that could only belong to this product, chosen over
three other candidates considered and rejected below:

**What it is.** Every verdict — on `/run/<id>`'s trial rows, on `/live`'s
recent-records table, on a `/record/<id>` page — renders as a compact
**claim strip**: `[mark] verdict-word · one-line evidence · stability
tag`, e.g.

```
✓ PASS   answered "the loading dock" — matched ground truth
✕ FAIL   answered "the garage" (matched trap: final_true_state)  verified-stable
? FAIL   answered "unknown"                                       unverified
```

The mark is not a colored dot decoration — it is a glyph that itself
carries meaning (✓ = matched, ✕ = a trap was sprung and named, ? = not yet
resample-checked), so the verdict is legible even to someone colorblind to
the amber/red-vs-green split, and because a checkmark/cross pair is what
this project's own vocabulary already calls these things ("the model
correctly identified..." / "the model fell for..."). The one-line evidence
clause is populated from data this codebase ALREADY computes and discards
at display time in the old system — `classify_belief_failure`'s trap name,
`profile.true_answer` — surfaced inline instead of requiring a click
through to `/record/<id>` to learn even the headline fact of what went
wrong. The stability tag (`verified-stable` / `noise` / `unverified`)
already existed as a table column; folding it into the claim strip makes
"was this checked" part of reading the verdict, not an adjacent fact.

**Why this and not something else.** Three candidates considered and
rejected:

1. *A literal chain-of-custody diagram* (seed → world → prompt → response
   → verdict → verification, as a connected visual lineage graph on every
   page). Rejected: this genuinely is what `store.lineage()` already
   renders as a `record → record → record` link chain on `/record/<id>`,
   and duplicating it as a bigger diagram on every listing page would be
   decoration competing with the actual verdict for attention — the
   existing textual lineage chain is kept, not replaced, because it
   already does this job in the one place it matters (the single-record
   deep-read view).
2. *Salience made physically visible via a "trap density" heat strip*
   next to every adversarial row (a horizontal bar shaded by
   `salience_weighted_score`). Genuinely considered — this IS real,
   measured data unique to this project. Folded into the claim strip's
   evidence clause instead of a separate always-on visual element,
   because most runs (state, order, plain belief) have no adversarial
   salience score at all, and a UI element that's blank 80% of the time
   fails the signature test worse than a text clause that simply doesn't
   appear when there's nothing to say.
3. *Stable-vs-noise as the headline signature* (a literal oscilloscope-
   style "signal that survived vs. one that's still a blip" render).
   This is real and used — it's the hatch-texture treatment in the color
   world above — but it's a property of ONE axis (verified failures),
   while the claim strip covers every verdict on every page including
   runs with zero verification activity (state/order campaigns, which
   this project's own data shows never fail). Demoted to a texture
   modifier on the claim strip rather than promoted to the single
   signature, so it doesn't imply relevance it doesn't have on 3 of the
   project's 5 campaign types.

The claim strip wins because it's the one element every single verdict in
every context (listing row, live feed, record detail, verdict-panel
notable-failure line) already needs to render, it uses data this codebase
computes today and previously discarded, and it is unrecognizable as any
other product's pass/fail treatment — a Stripe dashboard's status pill and
a CI system's red/green build badge both look nothing like a glyph +
inline evidence clause + stability tag.

## Rejecting (named, on purpose)

1. **Generic dark SaaS dashboard, rounded cards, one teal accent.** This
   was pass 2's entire system, described in its own design-notes.md in
   almost exactly this language, and it's what got called "AI Slop."
   Rejected explicitly, not just superseded: no card in the new system
   uses `border-radius` above 3px (instrumentation panels have edges, not
   bubble corners), and there is no single "accent" color standing in for
   brand — amber (belief/claims) and steel (ground truth/structure) split
   the work an accent color used to do alone.
2. **Colored pill badges for pass/fail.** Rejected and replaced by the
   claim-strip signature above. A solid-fill chip reads as a decorative
   status light; this project's own verdict objects (`RunVerdict`,
   `ConfirmationResult`, `AttributionReport`) are literally dataclasses
   full of evidence, p-values, and effect sizes — the display should look
   like it's carrying that evidence, not hiding it behind a colored dot.
3. **Generic top-nav dashboard layout indistinguishable from a billing
   admin panel.** The grouped Results/Explore/Act nav from pass 2 is
   functionally fine (it survives — see "What's the same" below) but its
   visual treatment (soft uppercase-tracked group labels floating in a
   flex row) is indistinguishable from any B2B SaaS top-nav. Kept the
   grouping, changed the rendering to a denser, ruled tab-strip with
   harder edges and a mono run-counter next to the brand mark (see
   Signature Test below) so the chrome itself reads as instrument, not
   storefront.
4. **A second-order slop trap this pass specifically watched for:**
   swapping teal for amber and calling it a day. Amber alone is just a
   recolor — what makes this a different system in kind is (a) splitting
   trap-red from error-red, which changes what a reader can DO with the
   color (filter signal from noise) not just how it looks, (b) the claim
   strip replacing the pill as the atomic verdict unit everywhere, and
   (c) the stable/noise texture, none of which exist in any prior pass
   regardless of palette.

## Direction: what actually changes in kind

- **Token system rebuilt from scratch**, not recolored. Old tokens:
  `--accent`, `--pass`, `--fail`, `--error`, `--bg`, `--surface-1/2`. New
  tokens are named for what they measure, read them aloud —
  `--verdict-pass`, `--verdict-fail-trap`, `--verdict-error-infra`,
  `--salience-low/mid/high`, `--claim-belief`, `--ground-truth`,
  `--surface-instrument-0/1/2`, `--stability-noise-hatch`. A name like
  `--verdict-fail-trap` could not be dropped into an unrelated project and
  still make sense; `--accent`/`--gray-700` could sit in any codebase
  unchanged — this is the token test from the interface-design skill,
  applied literally.
- **The verdict/pass-fail treatment changes in kind, not just recolors**,
  per the signature section above: chip → claim strip with inline
  evidence + stability tag. This touches every page that renders a
  verdict (`/run/<id>`, `/live`, `/record/<id>`, verdict panel's notable
  failures).
- **Surface system changes structurally, not just in hex value.** Pass 2
  had 3 elevation steps differentiated by background lightness alone.
  This pass keeps 3 steps but adds a genuine structural rule from the
  craft checklist that pass 2 stated as a principle but didn't fully
  apply: **inputs render DARKER than their surrounding surface**, not
  lighter (an input is a well the eye drops into, not a raised chip) —
  audited and fixed on every form field on `/launch`.
- **Density tightens to workbench levels.** Pass 2 explicitly chose
  "generous whitespace over dense boxing" as a principle (its own
  principle #14). This pass reverses that call on purpose: this is a
  workbench tool where a reader scans dozens of trial rows at once, and
  generous SaaS spaciousness actively hurts scanability at that volume.
  Paddings tighten to 12-16px (from the prior 16-24px `--sp-4`/`--sp-5`
  defaults); table row height drops accordingly. This is the single
  biggest felt-density difference from pass 2 even before any color
  changes register.
- **Nav chrome hardens**: ruled tab-strip, sharp corners, a live mono
  counter (`N` total trials recorded, ticking) sitting next to the brand
  mark instead of purely decorative branding — the one place "this is a
  running instrument, not a static site" is legible before scrolling
  past the header.

## What's the same, deliberately

The nav's information architecture (Results / Explore / Act groups),
breadcrumbs, progressive disclosure via `<details>`, the overview-first →
zoom → details-on-demand page flow, and every functional element (stop
button, verify-now form, stage tracker's five named stages, prev/next
trial nav) all worked and are not "AI slop" — the user's complaint was
never about information architecture, it was about the visual system
reading as generic. IA changes in this pass are confined to what the new
signature requires (the claim strip needs slightly more horizontal room
in trial tables) — this is a visual/systemic redesign per the task scope,
not a second IA rewrite.

---

## Self-check log (run against the actual rendered output, iterated where failed)

**Squint test** (blur the page, does hierarchy still read as ONE focal
point per screen): first pass on `/run/<id>` failed — the verdict panel's
h2 headline and the per-trial table's claim strips were competing at
similar visual weight once the strips got their new evidence-clause text.
Fixed by dropping the verdict panel's body paragraphs to
`--claim-belief`-toned secondary text and keeping ONLY the headline at
full-weight display size, so the panel's one h2 remains the unambiguous
focal point with the table clearly subordinate below the fold `<details>`
boundary — unchanged from pass 2's IA call, now actually reinforced by
weight instead of just position.

**Swap test** (could this be any other AI-graded-benchmark tool's UI):
first draft of the claim strip used a generic ✓/✗ Unicode pair with plain
green/red — indistinguishable from a CI badge. Iterated to the ✓ / ✕ / ?
three-glyph set (the `?` for unverified is the tell: a CI system has no
concept of "this failure hasn't been checked for noise yet," this project
does, structurally, everywhere) plus the inline evidence clause, which no
generic pass/fail UI populates because no generic pass/fail UI HAS a foil
name or a stability status to show.

**Signature test** (name the one element only this product could have):
the claim strip, per above — passed on the first real render, not
iterated further; the harder problem was making sure it degrades
gracefully when there's no foil/stability data to show (state/order
records), verified by rendering a plain state-query PASS row and
confirming it reads as `✓ PASS  matched ground truth` with no dangling
punctuation or empty evidence clause.

**Token test** (read CSS variable names aloud): failed once during
drafting — an early version still had `--accent-2` and `--muted-2` left
over from editing the old sheet incrementally. Caught by grepping the
final stylesheet for any token name that doesn't contain a domain word
(`verdict`, `claim`, `salience`, `stability`, `ground-truth`, `belief`,
`surface-instrument`, `mono`, `sans`, `space`, `type`) — zero matches
after the fix, confirmed by the same grep re-run clean.

---

## Addendum: two mid-pass structural corrections (same session)

Everything above this line was the domain exploration and token/signature
work, done and self-checked BEFORE two pieces of concrete, evidence-backed
feedback arrived mid-pass. Both were real misses in the plan above, not
disagreements about taste — recorded here because the root cause of both
(never questioning the inherited page SKELETON, only its paint) is exactly
the kind of thing a future pass needs to know NOT to repeat.

### Correction 1: top nav bar → left sidebar

The plan above (and every prior pass today) kept a horizontal
`header.shell-header` + `nav.shell-nav` top bar and only ever reskinned
its tokens. Direct feedback, backed by the interface-design skill's own
showcase examples (Synchro, Numra Analytics, Apex Capital — all sidebar-
based): "no saas has it like that except for some decades-old government
websites." Correct and undeniable once named — with 7 nav items grouped
into 3 sections, a horizontal strip was always going to feel cramped; a
sidebar is the right CONTAINER for that shape of nav, not a paint job
away from being one. Three passes in a row inherited the skeleton from
whatever came before and never re-examined it.

**Structural change, `cartographer/viewer/serve.py::_shell`:** a fixed
248px `<aside class="sidebar">` (identity block: "MC" mark + brand name +
live trial counter, then the SAME `_NAV_GROUPS` data structure as before
— Results/Explore/Act — now rendered as vertical grouped rows instead of
a horizontal flex row) sits beside `<main class="shell-main">` inside a
new `<div class="app-shell">` flex container. Global nav (sidebar, never
changes across pages) and page-level context (`.page-strip` — breadcrumb,
later a right-aligned actions slot) are now two different regions, not
stacked in one header row. Active item: a filled-row highlight, not a
bottom-border tab underline (a bottom border reads as "browser tab,"
which is exactly the top-bar tell being removed).

### Correction 2: verify against the ACTUAL shipped showcase pixels, not a description of them

Even after the sidebar fix, further feedback: "it's not just the left
side menu bar.. IT's everything." The user pulled the full-resolution
preview PNGs from the interface-design skill's OWN gallery
(test-factory.png — an API test runner, the closest domain analog in the
gallery to this project; bug-tracker.png; admin-dashboard-after.png) and
extracted concrete, specific structural facts that this pass's own
Rejecting-section instincts had gotten wrong by defaulting to generic
choices instead of checking a real reference:

- **Primary buttons are neutral inverted-contrast** (light fill, dark
  text — "Run Test" in test-factory.png), not accent-color-filled. This
  pass had every button/CTA filled with `--claim-belief` amber, diluting
  the same "one accent, used with intention" principle this file already
  argued for elsewhere — amber-as-every-button's-fill-color is exactly
  the decorative-accent failure this document's own Rejecting section
  named once already, just recommitted with a different hex value.
  Fixed: `button`/`.cta-primary` now use `--ink`-on-`--surface-instrument-0`
  (the same inverted-contrast language as the sidebar's active-row pill),
  freeing `--claim-belief` to mean only what it's supposed to mean
  (a belief-query claim / an in-progress state / a real finding).
- **A verdict renders as a small pill directly beside the h1**
  ("Get User by ID  [PASSED]"), not folded only into a big card below.
  Added `.title-pill` + `.h1-row` to `run_html`/`record_html` — the FULL
  claim-strip evidence still lives in the body below; the pill is only
  the at-a-glance glance-and-move-on signal next to the title itself.
- **The "assertions" pattern — expected/actual side by side, only the
  mismatch colored, the word "actual:" itself staying muted** — is a
  near-exact structural fit for this project's own central claim
  ("expected: what entity X believed" vs "actual: what the model
  answered"), closer to this product's domain than anything this pass
  invented on its own. Added `.assertion-row` and rewired
  `verdict_panel_html`'s notable-failures list to use it. This also
  surfaced a real, separate bug while wiring it in: `verdict.py`'s
  `NotableFailure.model_answer` (a module outside this pass's territory,
  not edited) carries a model's FULL raw response text, and some models
  (observed live: `MiniMax-M3` on adversarial_belief records) write their
  entire chain-of-thought into that field with the stated answer only at
  the very end — so the assertion row was initially dumping a 700-
  character reasoning fragment into what's supposed to be a terse
  "actual:" value. Fixed at the display layer (`_short_answer()`, this
  pass's own territory) by taking the LAST non-empty line rather than the
  first N characters, mirroring `verdict.py::_notable_failures`'s own
  existing pattern for extracting the question from a multi-line prompt.
  Same latent bug, same fix, was also present (and fixed) in three other
  `_short_answer` call sites in the claim strip itself, caught by
  actually rendering against `results/campaign.sqlite`'s real
  adversarial_belief data rather than only synthetic test fixtures —
  this is the concrete payoff of the "render the signature component
  against real data before doing the rest of the app" process step.
- **Hero KPI stat cards**: big bold number (~32px) with a muted label
  below and near-invisible border, not a smaller number in an
  equal-weight box. Applied to Overview's trials/spend cards and (as a
  side effect of sharing `.stat-box`) `/live`'s tally grid.
- **Focal-point coloring**: color/weight goes on the ONE thing that's
  the point, everything else stays neutral — the admin-dashboard example
  applies this to a single-series bar chart (one bar colored, rest gray).
  `/atlas` is a multi-model COMPARISON chart, where collapsing every line
  to one color would destroy the chart's actual job of telling models
  apart, so the principle was adapted rather than copied verbatim: a
  model line renders in neutral `--ink-muted` by default and switches to
  `--claim-belief` amber (bolder stroke too) exactly when that
  model/dimension pair has a REAL fitted boundary — so the reader's eye
  lands on "which model actually has a discovered weakness," not on
  whichever line the palette happened to assign teal vs. purple to.
  Verified against real data post-fix: 43 neutral lines, 15 amber lines,
  and the amber ones cluster on `belief`-kind charts (where this
  project's actual findings live) while `state`-kind charts (uniformly
  clean per the lab log) render entirely neutral — the coloring tracks
  real findings, not decoration.
- **No avatar/user-profile chrome anywhere** — checked against all three
  examples' explicit absence of one; this pass's sidebar identity block
  was already instrument-identity-only (brand mark + name + trial
  counter), so this one was already compliant, not a fix.

**Reconciliation principle applied throughout this addendum:** where a
real shipped structural pattern conflicted with a generic choice this
pass had made on its own, the more specific, evidence-grounded pattern
won (buttons, verdict placement, focal-point coloring). Where a structural
pattern needed adapting to fit data these showcase examples don't know
about (the multi-model atlas comparison), the pattern's underlying
PRINCIPLE was kept and applied to the right variable for this domain,
not copied as a literal instruction that would have broken the page's
actual job. The domain-specific WORK — what the colors mean, the claim
strip's evidence clause and stability tag, the salience ramp, the
achromatic-ground-truth rule — was not touched by either correction: the
showcase examples are correctly silent on what THIS product's own
vocabulary should look like, because they've never seen it.

**Verification after both corrections:** full test suite still 320/320
(same count as before either correction — both were markup/CSS-shape
changes over content the tests check as plain substrings, not DOM
structure), all 9 routes still 200, `data-testid="verdict-panel"` and
every `_NAV_LABELS` string still present, confirmed by curling a running
instance of `results/campaign.sqlite` directly (not just re-running
pytest) after each correction landed.
