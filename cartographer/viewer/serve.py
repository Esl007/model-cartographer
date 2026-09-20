"""Local read-only HTML viewer over a `ResultsStore` sqlite db.

stdlib-only `http.server` app. Binds to 127.0.0.1 only. Every GET route
only reads from the store; the only mutating routes are `/launch` (spawns
a campaign thread) and `/stop` (sets that thread's cooperative-cancel
event -- see `cartographer.viewer.launch`), neither of which writes to
the results store directly.

Usage:
    python3 -m cartographer.viewer.serve [db_path] [port]

Defaults: db_path="results/pilot.sqlite", port=8788.
"""

from __future__ import annotations

import html
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..report.verdict import run_verdict
from ..store.schema import ExperimentRecord
from ..store.store import ResultsStore
from . import launch as launch_mod
from .atlas import atlas_html, trials_html
from .coverage import coverage_html
from .findings import capability_trials_html, findings_html

_DEFAULT_DB_PATH = "results/campaign.sqlite"
_DEFAULT_PORT = 8788
_REPO_ROOT = Path(__file__).resolve().parents[2]

# run_id prefix -> known campaign budget (total scheduler trial budget),
# used only to render a progress bar on /live. Unknown prefixes fall back
# to "?" (bar hidden, raw count still shown). Budget is a CEILING, not a
# promise -- campaigns legitimately end early when explore finds nothing,
# so bars/labels below phrase this as a bound ("n / budget max").
_KNOWN_RUN_BUDGETS = {
    "camp-state-": 160,
    "camp-belief-": 700,
}

# ---------------------------------------------------------------------------
# Shared shell: design system + nav.
# ---------------------------------------------------------------------------

_NAV_GROUPS = [
    ("Results", [
        ("overview", "/", "Overview"),
        ("findings", "/findings", "Findings"),
        ("report", "/report", "Report"),
        ("runs", "/runs", "Runs"),
    ]),
    ("Explore", [
        ("atlas", "/atlas", "Atlas"),
        ("coverage", "/coverage", "Coverage"),
    ]),
    ("Act", [
        ("launch", "/launch", "Launch"),
        ("live", "/live", "Live"),
    ]),
]
# Flat lookup kept for call sites that only need "is this key active".
_NAV_ITEMS = [item for _, items in _NAV_GROUPS for item in items]

_CSS = """
/* ---------------------------------------------------------------------
   1. TOKENS -- see docs/design-notes.md "Color world" + "Direction" for
   the reasoning. Surfaces are achromatic (zero saturation, steel/
   graphite, never navy/teal-tinted). Hue is reserved for what it MEANS
   in this product's own vocabulary: ground truth is achromatic (never in
   question), a belief-query claim is amber/phosphor (provisional,
   witnessed, can be wrong in a specific way), a sprung trap is a
   different red from an infra error (one is a finding, the other is
   noise), salience is a real measured ramp. Every token name is read
   aloud in the design-notes "token test" -- none of them could sit
   unchanged in an unrelated project.
   ------------------------------------------------------------------- */
:root {
  --surface-instrument-0: #0c0d0e;  /* body -- near-black, not pure black */
  --surface-instrument-1: #131415;  /* panel */
  --surface-instrument-2: #1a1b1d;  /* raised: table header, active nav, hover */
  --well: #08090a;                  /* inputs -- DARKER than surroundings, a well not a chip */
  --line: rgba(255,255,255,0.08);
  --line-strong: rgba(255,255,255,0.16);

  --ink: #E7E5E2;
  --ink-secondary: #A8A6A1;
  --ink-muted: #6E6C68;

  --ground-truth: #E7E5E2;          /* simulated fact -- achromatic, never tinted */

  --claim-belief: #D9A441;          /* a belief-query claim: provisional, witnessed */
  --claim-belief-dim: #8A6A2E;

  --verdict-pass: #5C9E7C;          /* correct -- restrained, unremarkable on purpose */
  --verdict-fail-trap: #C4544B;     /* a laid trap was sprung -- a FINDING, worth reading */
  --verdict-error-infra: #86898f;   /* infra/timeout/cap -- noise, not a finding; no hue */

  --salience-low: #6B5730;
  --salience-mid: #B0812E;
  --salience-high: #F0A93A;

  --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Consolas, "Liberation Mono", Menlo, monospace;
  --sans: "Manrope", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;

  /* Spacing scale on a 4px grid. REVISED (pass 4): pass 3 chose
     "workbench-tight, not SaaS-airy" and landed at ~7px cell padding /
     13.6px card padding. Measured against the real shipped references
     this design is derived from (admin-dashboard KPI cards,
     test-factory request/response cards), that is materially tighter
     than any of them -- it read as cramped rather than precise, which
     is the difference between "instrument" and "unstyled". Still
     denser than consumer SaaS; but a card must have enough padding
     that the grouping itself is legible. */
  --sp-1: 0.25rem; --sp-2: 0.5rem; --sp-3: 0.75rem; --sp-4: 1rem;
  --sp-5: 1.25rem; --sp-6: 2rem; --sp-7: 3rem;

  /* type scale: real ~1.25 ratio, 4 sizes. */
  --fs-display: 1.75rem;
  --fs-h2: 1.125rem;
  --fs-body: 0.9rem;
  --fs-small: 0.78rem;

  /* back-compat aliases so every call site below this block (and any
     future edit that reaches for the old names out of habit) resolves to
     the new system instead of silently breaking -- see design-notes.md
     "what's the same, deliberately." */
  --bg: var(--surface-instrument-0);
  --surface-1: var(--surface-instrument-1);
  --surface-2: var(--surface-instrument-2);
  --bg-raised: var(--surface-instrument-1);
  --border: var(--line);
  --border-strong: var(--line-strong);
  --text: var(--ink);
  --text-secondary: var(--ink-secondary);
  --text-muted: var(--ink-muted);
  --accent: var(--claim-belief);
  --accent-dim: var(--claim-belief-dim);
  --pass: var(--verdict-pass);
  --fail: var(--verdict-fail-trap);
  --error: var(--verdict-error-infra);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  background: var(--surface-instrument-0);
  color: var(--ink);
  line-height: 1.5;
  font-size: var(--fs-body);
}
a { color: var(--claim-belief); text-decoration: none; }
a:hover { text-decoration: underline; }

.num, td.num, .stat-num {
  font-variant-numeric: tabular-nums;
}

/* ---------------------------------------------------------------------
   2. SHELL -- fixed LEFT SIDEBAR for global nav, not a top bar. Every
   real reference with this many nav items (7) uses a sidebar -- a
   horizontal strip was a paint job away from the right container, never
   the right container itself (see docs/design-notes.md's sidebar
   addendum). Global nav (sidebar, never changes across pages) and
   page-level context (.page-strip, page-specific) are two different
   jobs in two different regions, not stacked in one header row.
   ------------------------------------------------------------------- */
html, body { height: 100%; }
.app-shell { display: flex; min-height: 100vh; align-items: stretch; }

.sidebar {
  width: 248px; flex-shrink: 0;
  background: var(--surface-instrument-1);
  border-right: 1px solid var(--line-strong);
  display: flex; flex-direction: column;
  padding: var(--sp-5) 0;
}

.brand-block {
  display: flex; align-items: center; gap: 0.65em;
  padding: 0 var(--sp-4) var(--sp-5);
  margin-bottom: var(--sp-4);
  border-bottom: 1px solid var(--line);
  color: var(--ink);
}
.brand-block:hover { text-decoration: none; }
.brand-mark {
  font-family: var(--mono); font-weight: 700; font-size: 12px;
  color: var(--surface-instrument-0); background: var(--claim-belief);
  width: 26px; height: 26px; border-radius: 3px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.brand-text { display: flex; flex-direction: column; gap: 0.15em; min-width: 0; }
.brand-name {
  font-weight: 650; font-size: 0.85rem; color: var(--ink);
  letter-spacing: -0.005em; white-space: nowrap;
}
.brand-block .counter {
  font-family: var(--mono); font-size: 10px; color: var(--ink-muted); font-weight: 500;
}

.sidebar-nav { flex: 1; padding: 0 var(--sp-3); overflow-y: auto; }
.nav-group { margin-bottom: var(--sp-5); }
.nav-group:last-child { margin-bottom: 0; }
.nav-group-label {
  color: var(--ink-muted); font-size: 9.5px; text-transform: uppercase;
  letter-spacing: 0.08em; font-weight: 650; font-family: var(--mono);
  padding: 0 var(--sp-2); margin-bottom: var(--sp-2);
}
.nav-row {
  display: flex; align-items: center; gap: 0.65em;
  color: var(--ink-secondary); font-size: var(--fs-small);
  padding: 0.42rem var(--sp-3); border-radius: 6px; margin-bottom: 1px;
}
.nav-row:hover { color: var(--ink); text-decoration: none; background: var(--surface-instrument-2); }
/* Active item: a full INVERTED-CONTRAST pill -- light fill, dark text --
   not a subtle tint+bar. Confirmed against a real shipped dev-tool
   sidebar (interface-design skill's own test-factory.png gallery
   example): the active row is a solid light rounded-rect with dark text,
   the strongest possible "this one is selected" signal, reserved for
   exactly one row at a time so it stays meaningful. */
.nav-row.active {
  color: var(--surface-instrument-0); background: var(--ink);
  font-weight: 650;
}
.nav-icon {
  font-family: var(--mono); font-size: 12px; width: 1.1em; text-align: center;
  flex-shrink: 0; color: var(--ink-muted);
}
.nav-row.active .nav-icon, .nav-row:hover .nav-icon { color: inherit; }

main.shell-main {
  flex: 1; min-width: 0;
  padding: var(--sp-6) var(--sp-6) var(--sp-7);
  max-width: 1040px;
}

/* Page strip: breadcrumb LEFT, page-specific actions RIGHT -- a real
   shipped example (test-factory.png) puts "Test Suites / User API /
   GET /users/:id" on the left and secondary+primary buttons on the
   right of the SAME slim strip, distinct from the big page title below
   it. `.page-strip .actions` is an optional right-aligned slot callers
   populate for pages with a page-level action (currently unused by any
   page here, wired for future use so the pattern exists when needed). */
.page-strip {
  margin-bottom: var(--sp-4);
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4);
}
.page-strip .actions { display: flex; align-items: center; gap: var(--sp-2); flex-shrink: 0; }
.breadcrumb {
  color: var(--ink-muted); font-size: var(--fs-small); margin: 0;
  font-family: var(--mono);
}
.breadcrumb a { color: var(--ink-secondary); }
.breadcrumb a:hover { color: var(--claim-belief); }
.breadcrumb .sep { margin: 0 var(--sp-2); color: var(--line-strong); }

/* h1-adjacent inline status pill -- a real shipped example
   (test-factory.png) renders a trial/test's verdict as "H1 + small
   pill" beside the title, not a big separate card below it. Used by
   run_html/record_html's headline verdict; the FULL evidence (claim
   strip with trap names, stability tag) stays in the body below --
   this pill is just the at-a-glance "what happened" signal next to
   the title itself. */
.h1-row { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; margin: 0 0 var(--sp-1); }
.h1-row h1 { margin: 0; }
.title-pill {
  display: inline-flex; align-items: center; gap: 0.35em;
  font-family: var(--mono); font-size: 11px; font-weight: 700;
  letter-spacing: 0.03em; padding: 0.2em 0.55em; border-radius: 4px;
  text-transform: uppercase;
}
.title-pill.tp-pass { background: rgba(92,158,124,0.15); color: var(--verdict-pass); }
.title-pill.tp-fail { background: rgba(196,84,75,0.15); color: var(--verdict-fail-trap); }
.title-pill.tp-mixed { background: rgba(217,164,65,0.15); color: var(--claim-belief); }
.title-pill.tp-error { background: rgba(134,137,143,0.15); color: var(--verdict-error-infra); }

/* ---------------------------------------------------------------------
   3. TYPE SCALE.
   ------------------------------------------------------------------- */
h1 {
  font-size: var(--fs-display); font-weight: 650; margin: 0 0 var(--sp-1);
  letter-spacing: -0.015em; color: var(--ink);
}
.subtitle { color: var(--ink-secondary); font-size: var(--fs-body); margin: 0 0 var(--sp-5); }
h2 {
  font-size: var(--fs-h2); font-weight: 600; margin: var(--sp-6) 0 var(--sp-3);
  color: var(--ink); letter-spacing: -0.005em;
}
h3 { font-size: var(--fs-body); font-weight: 650; margin: var(--sp-4) 0 var(--sp-1); color: var(--ink); }

.meta { color: var(--ink-muted); font-size: var(--fs-small); }
.mono { font-family: var(--mono); }
.eyebrow {
  text-transform: uppercase; letter-spacing: 0.07em; font-size: 9.5px;
  font-weight: 700; color: var(--ink-muted); font-family: var(--mono);
}

/* ---------------------------------------------------------------------
   4. SURFACES -- 3 elevation steps by background lightness. Sharp
   corners (max 3px), not bubble-rounded cards (design-notes "Rejecting"
   #1) -- an instrument panel has edges. .panel is the one reusable
   surface pattern.
   ------------------------------------------------------------------- */
.panel {
  background: var(--surface-instrument-1);
  border: 1px solid var(--line);
  border-radius: 3px;
  padding: var(--sp-5);
}
.panel + .panel { margin-top: var(--sp-3); }
.panel-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: var(--sp-3);
  margin: var(--sp-3) 0 var(--sp-6);
}
.panel-grid .panel { padding: var(--sp-4); }

/* COMPOSITION (pass 5). Passes 3-4 fixed surfaces but left every page as
   one stacked column: h1 -> panel -> h2 -> table -> h2 -> list. No
   reference composes that way. admin-dashboard and Numra both open with
   a KPI row across the top, then a TWO-COLUMN row (list beside chart /
   funnel beside signals), then a full-width table card. The grid IS the
   dashboard; without it, better borders and type still read as a
   document. `.page-head` also gives every page the reference's
   title-left / primary-action-right header row (test-factory's "Run
   Test" sits top-right, not inline in the body). */
.page-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--sp-5); margin-bottom: var(--sp-5); flex-wrap: wrap;
}
.page-head .subtitle { margin-bottom: 0; max-width: 62ch; }
.page-head h1 { margin-bottom: var(--sp-2); }
.grid-2 {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: var(--sp-3); margin: var(--sp-3) 0 var(--sp-6);
}
.grid-2 > .card { margin: 0; }
/* A card's body list shouldn't carry a browser default indent on top of
   the card's own padding. */
.card-body ul { margin: 0; padding-left: 1.05rem; }
.card-body ul li + li { margin-top: var(--sp-2); }

/* COMPOSITION, pass 6 -- the same system applied to the remaining eight
   pages. Three additions the overview didn't need:
   (a) `.page-head` has to hold a title PILL beside the title (run/record
       pages) and MORE THAN ONE action on the right (a run with unverified
       failures offers both "watch live" and "verify now"), so the left
       and right halves each get their own flex wrapper;
   (b) a card body can be a preformatted transcript or a nested
       disclosure, which carry their own margins and would otherwise
       double the card padding;
   (c) /coverage's "not yet covered" roadmap is a different KIND of
       content from the four capability inventories above it -- honest
       gaps, not features -- so it gets a visibly distinct card rather
       than a fifth identical one. */
.page-head-main { min-width: 0; }
.page-head-actions {
  display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap;
  justify-content: flex-end;
}
.page-head .h1-row { margin-bottom: var(--sp-2); }
.card-body > pre.block { margin: 0; }
.card-body > details { margin: 0; }
.card-body > details > pre.block { margin-top: var(--sp-2); margin-bottom: 0; }
.card-body > .preset-cards { margin: 0; }
/* Preset radio-cards now live INSIDE a card body, so they need a raised
   surface to read as objects on it rather than dissolving into it, plus
   a hover and a checked state strong enough to look selectable at a
   glance (the hidden radio meant selection was communicated only by a
   1px border tint). */
.card-body .preset-card { background: var(--surface-instrument-2); border-color: var(--line-strong); }
.preset-card:hover { border-color: var(--claim-belief-dim); }
.preset-card:has(input:checked) { box-shadow: inset 0 0 0 1px var(--claim-belief); }
/* A data grid revealed by a disclosure INSIDE a card must not draw a
   second border on top of the card's -- the card is already the
   enclosure, so the inner grid goes flat. */
.card-body > details > table { background: none; border: none; border-radius: 0; margin: var(--sp-3) 0 0; }
/* Cards nested inside a card body (retracted findings inside the
   retractions card) sit on the raised surface and keep an even rhythm. */
.card-body .card { margin: var(--sp-3) 0 0; background: var(--surface-instrument-2); }
.card-body > .preset-cards + details { margin-top: var(--sp-3); }
/* Roadmap card: dashed edge + muted surface, reading as an outline of
   something not built rather than a panel of something that is. */
/* A card whose title is a real NAME (a finding's claim), not a section
   label. Uppercase 10px mono is right for "PROMPT" / "ALL RUNS"; it is
   unreadable for "Belief-to-reality collapse (witness-rendering bug,
   corrected)". Same header bar, sentence-case sans title. */
.card-title-name {
  font-family: var(--sans); font-size: var(--fs-body); font-weight: 650;
  text-transform: none; letter-spacing: -0.005em; color: var(--ink);
}
.card-gap { background: transparent; border-style: dashed; border-color: var(--line-strong); }
.card-gap .card-title { color: var(--ink-muted); }
.card-gap .card-body ul li { color: var(--ink-secondary); }

/* CARD -- pass 4. The structural fix behind "it still doesn't look like
   a SaaS app": content was sitting DIRECTLY on the page background. In
   every real reference (test-factory's REQUEST/RESPONSE/ASSERTIONS,
   admin-dashboard's Services/Requests/Tenants, bug-tracker's list), no
   table or data block ever floats on the canvas -- each is enclosed in
   a bordered card with its own header bar: uppercase label left,
   contextual meta right, hairline beneath, body flush below. That
   enclosure is most of what reads as "designed application" rather
   than "styled document". A table inside a card goes edge-to-edge (the
   card owns the padding), so `.card > table` resets its own margins. */
.card {
  background: var(--surface-instrument-1);
  border: 1px solid var(--line);
  border-radius: 3px;
  margin: var(--sp-3) 0 var(--sp-6);
  overflow: hidden;   /* keeps the flush table's corners inside the radius */
}
.card-head {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--line);
}
.card-title {
  font-size: 10px; font-weight: 650; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--ink-secondary);
  font-family: var(--mono);
}
.card-meta {
  font-size: var(--fs-small); color: var(--ink-muted);
  font-family: var(--mono); font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.card-body { padding: var(--sp-5); }
.card-body > :first-child { margin-top: 0; }
.card-body > :last-child { margin-bottom: 0; }
/* A table is the card's whole body: flush to the edges, no stray margin,
   and no double border where the last row meets the card edge. */
.card > table { margin: 0; }
.card > table th:first-child, .card > table td:first-child { padding-left: var(--sp-5); }
.card > table th:last-child, .card > table td:last-child { padding-right: var(--sp-5); }
.card > table tbody tr:last-child td { border-bottom: none; }

/* Hero KPI stat card -- confirmed against a real shipped admin-dashboard
   example: a big bold number (~32-36px, not 24px) + a small inline
   context/delta badge on the SAME baseline immediately after it, muted
   label below, near-invisible border, flat surface (no elevation
   shadow). Applied to Overview's trials/spend cards -- `.stat-delta` is
   optional (only rendered when a caller has a real delta to show). */
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: var(--sp-2); margin: var(--sp-3) 0 var(--sp-5); }
.stat-box { background: var(--surface-instrument-1); border: 1px solid var(--line); border-radius: 4px; padding: var(--sp-4); }
.stat-box .stat-num-row { display: flex; align-items: baseline; gap: 0.5em; }
.stat-box .stat-num { font-family: var(--mono); font-size: 2rem; font-weight: 650; color: var(--ink); line-height: 1; }
.stat-box .stat-delta { font-family: var(--mono); font-size: 11px; font-weight: 600; color: var(--ink-muted); }
.stat-box .stat-delta.up { color: var(--verdict-pass); }
.stat-box .stat-label { color: var(--ink-muted); font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em; margin-top: var(--sp-2); font-family: var(--mono); }

/* ---------------------------------------------------------------------
   12b. ASSERTION ROWS -- expected/actual mismatch, only the mismatch
   colored. Confirmed against a real shipped API-test-runner example
   (test-factory.png's "Assertions" card): a near-perfect structural fit
   for this product's own core claim -- "expected: what entity X
   BELIEVED" vs "actual: what the model ANSWERED." Reused anywhere a
   model's wrong answer needs to sit directly beside the correct one
   (claim-strip evidence, verdict-panel notable failures).
   ------------------------------------------------------------------- */
.assertion-row { font-family: var(--mono); font-size: 0.85em; }
.assertion-row .aa-label { color: var(--ink-muted); }
.assertion-row .aa-expected { color: var(--ink-secondary); }
.assertion-row .aa-actual-mismatch { color: var(--verdict-fail-trap); font-weight: 600; }
.assertion-row .aa-actual-match { color: var(--ink-secondary); }

/* ---------------------------------------------------------------------
   5. TABLES -- tight workbench density (row padding cut from pass 2's
   0.6rem to ~7px), numeric columns right-aligned + tabular, header row
   raised one surface step with a real rule beneath it.
   ------------------------------------------------------------------- */
/* Every table is self-enclosing (pass 4). Previously a bare table element
   sat directly on the page canvas with no container -- the strongest
   "unstyled document, not an application" tell, and the thing no
   reference does anywhere. Rather than hand-wrapping 11 emission sites
   across 3 modules (and churning the tests that assert on their markup),
   the table IS the card: its own surface, border, and radius. `.card >
   table` above opts out for the cases that want an explicit header bar.
   border-collapse must be `separate` for the radius to render, so the
   row rule moves to a bottom border per cell with spacing zeroed. */
table {
  border-collapse: separate; border-spacing: 0;
  width: 100%; margin: var(--sp-3) 0 var(--sp-6); font-size: var(--fs-small);
  background: var(--surface-instrument-1);
  border: 1px solid var(--line);
  border-radius: 3px;
}
.card > table { background: none; border: none; border-radius: 0; }
/* Row height, pass 4: 0.44rem vertical padding gave 34px rows -- denser
   than every reference measured (which sit ~44-52px). Scanning a column
   of numbers needs the row to be taller than the glyphs. */
th, td { padding: 0.7rem var(--sp-4); text-align: left; border-bottom: 1px solid var(--line); }
tbody tr:last-child td { border-bottom: none; }
th {
  color: var(--ink-muted); text-transform: uppercase; font-size: 10px;
  letter-spacing: 0.06em; font-weight: 650; border-bottom: 1px solid var(--line-strong);
  background: var(--surface-instrument-2); font-family: var(--mono);
  white-space: nowrap;
}
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.mono, td .mono { font-family: var(--mono); }
tbody tr:hover { background: var(--surface-instrument-1); }

/* ---------------------------------------------------------------------
   6. THE CLAIM STRIP -- the signature component (see design-notes.md
   "The signature"). A verdict renders as [mark] verdict-word · one-line
   evidence clause · stability tag, not a colored chip. The mark itself
   (not just its color) carries meaning: check = matched, cross = a trap
   was sprung and named, "?" = not yet resample-checked -- so it reads
   correctly even without color. A trap-sprung FAIL (--verdict-fail-trap,
   informative) is visually distinct from an infra ERROR
   (--verdict-error-infra, flat, no hue -- just noise). "verified-stable"
   uses ink-secondary + a small pass-green check, NOT fail-trap red --
   evidence that held up under a resample check is a neutral audited
   fact, not itself alarming (see design-notes.md's squint/token-test
   log for the earlier draft that got this wrong). "noise" renders
   struck-through and hatched: kept legible, not deleted, matching this
   project's own practice of keeping retractions visible.
   ------------------------------------------------------------------- */
.claim {
  display: flex; align-items: baseline; gap: 0.6em; flex-wrap: wrap;
  font-size: var(--fs-small); padding: 0.4rem 0;
}
.claim .mark {
  font-family: var(--mono); font-weight: 700; width: 1em; flex-shrink: 0;
  text-align: center;
}
.claim.v-pass .mark { color: var(--verdict-pass); }
.claim.v-fail-trap .mark, .claim.v-fail-unverified .mark { color: var(--verdict-fail-trap); }
.claim.v-error .mark { color: var(--verdict-error-infra); }

.claim .verdict-word {
  font-family: var(--mono); font-weight: 650; font-size: 0.85em;
  letter-spacing: 0.03em; width: 3.6em; flex-shrink: 0;
}
.claim.v-pass .verdict-word { color: var(--verdict-pass); }
.claim.v-fail-trap .verdict-word, .claim.v-fail-unverified .verdict-word { color: var(--verdict-fail-trap); }
.claim.v-error .verdict-word { color: var(--verdict-error-infra); }

.claim .evidence { color: var(--ink-secondary); flex: 1; min-width: 12em; }
.claim .evidence b { color: var(--ink); font-weight: 600; }
.claim .evidence .trap-name { color: var(--claim-belief); font-family: var(--mono); font-size: 0.95em; }
.claim .sal-swatch { display: inline-block; width: 22px; height: 5px; border-radius: 1px; vertical-align: middle; margin-right: 0.35em; }

.claim .stability {
  font-family: var(--mono); font-size: 0.82em; letter-spacing: 0.03em;
  flex-shrink: 0; white-space: nowrap; color: var(--ink-muted);
}
.claim .stability.stable { color: var(--ink-secondary); }
.claim .stability.stable::before { content: "\\2713 "; color: var(--verdict-pass); }
.claim .stability.noise {
  color: var(--ink-muted);
  background-image: repeating-linear-gradient(135deg, rgba(255,255,255,0.04) 0 3px, transparent 3px 7px);
  text-decoration: line-through; text-decoration-color: var(--ink-muted); text-decoration-thickness: 1px;
  padding: 0.05em 0.3em;
}
.claim .stability.unverified { opacity: 0.65; }
.claim .stability.unverified::before { content: "\\25CB "; }

/* Table-row variant: the claim strip collapsed into a single <td> so it
   still works inside the existing per-trial tables. */
td.claim-cell { padding-top: 0.3rem; padding-bottom: 0.3rem; }
td.claim-cell .claim { padding: 0; }

/* Stage tracker for full_map runs on /live. */
.stage-track { display: flex; align-items: center; gap: var(--sp-2); margin: var(--sp-3) 0 var(--sp-4); flex-wrap: wrap; }
.stage-step {
  display: flex; align-items: center; gap: 0.4em; font-size: var(--fs-small);
  color: var(--ink-muted); padding: 0.25rem 0.6rem; border-radius: 3px;
  background: var(--surface-instrument-1); border: 1px solid var(--line); font-family: var(--mono);
}
.stage-step .dot { width: 6px; height: 6px; border-radius: 999px; background: var(--line-strong); }
.stage-step.done { color: var(--verdict-pass); }
.stage-step.done .dot { background: var(--verdict-pass); }
.stage-step.active { color: var(--claim-belief); background: var(--surface-instrument-2); font-weight: 650; border-color: var(--claim-belief-dim); }
.stage-step.active .dot { background: var(--claim-belief); }
.stage-arrow { color: var(--line-strong); }

/* ---------------------------------------------------------------------
   7. PROGRESS -- claim-belief amber reserved for in-progress fill;
   complete state switches to verdict-pass so the amber stays meaningful
   (it means "a claim is still provisional/running").
   ------------------------------------------------------------------- */
.progress-track { background: var(--line-strong); border-radius: 2px; height: 6px; width: 100%; overflow: hidden; }
.progress-fill { background: var(--claim-belief); height: 100%; border-radius: 2px; transition: width 0.3s ease; }
.progress-fill.complete { background: var(--verdict-pass); }
.progress-row { display: flex; align-items: center; gap: var(--sp-3); margin: var(--sp-2) 0 var(--sp-1); min-height: 6px; }
.progress-row .progress-track { flex: 1; }
.progress-text { font-size: var(--fs-small); color: var(--ink-muted); white-space: nowrap; font-variant-numeric: tabular-nums; font-family: var(--mono); }

/* ---------------------------------------------------------------------
   8. CODE / DETAILS -- long mono blobs stay collapsed (progressive
   disclosure), rendered in the WELL (darker than surroundings, not a
   raised panel) since a prompt/response blob is closer to an input than
   a card.
   ------------------------------------------------------------------- */
pre.block {
  white-space: pre-wrap; word-wrap: break-word;
  background: var(--well);
  border: 1px solid var(--line);
  border-radius: 3px;
  padding: var(--sp-4);
  font-family: var(--mono);
  font-size: var(--fs-small);
  color: var(--ink-secondary);
}
details { margin: var(--sp-2) 0; }
details summary {
  cursor: pointer; font-weight: 600; margin: var(--sp-2) 0; color: var(--ink-secondary);
  font-size: var(--fs-small); list-style: none;
}
details summary::-webkit-details-marker { display: none; }
details summary::before { content: "\\25b8  "; color: var(--ink-muted); }
details[open] summary::before { content: "\\25be  "; }
details[open] summary { color: var(--ink); }

/* ---------------------------------------------------------------------
   9. FORMS -- radio-card preset pattern for /launch. Inputs render in
   the WELL token -- darker than their surrounding panel, a well the eye
   drops into, not a raised chip (design-notes "Direction": audited and
   fixed from pass 2, which used a LIGHTER surface for inputs).
   ------------------------------------------------------------------- */
form p { margin: var(--sp-3) 0; }
label { font-size: var(--fs-small); color: var(--ink-secondary); }
input, select, button, textarea {
  font-family: var(--sans);
  background: var(--well);
  color: var(--ink);
  border: 1px solid var(--line-strong);
  border-radius: 3px;
  padding: 0.4rem 0.55rem;
  font-size: var(--fs-body);
}
input:focus, select:focus, textarea:focus {
  outline: none; border-color: var(--claim-belief);
}
/* Primary action = NEUTRAL inverted-contrast (light fill, dark text),
   not accent-filled -- confirmed against a real shipped dev-tool
   (test-factory.png): "Run Test" is a solid light/white button with
   dark text and a play glyph, not a colored-accent button. Reserves
   claim-belief amber for what it actually MEANS in this product's
   vocabulary (a belief-query claim / in-progress state) instead of
   diluting it into "the color of every button," which is exactly the
   decorative-accent failure mode design-notes.md's Rejecting section
   already named once for teal. */
button {
  background: var(--ink);
  color: var(--surface-instrument-0);
  font-weight: 650;
  cursor: pointer;
  border: none;
  border-radius: 4px;
  padding: 0.55rem 1rem;
}
button:hover { opacity: 0.88; }
button:disabled { opacity: 0.4; cursor: not-allowed; }
button.btn-primary { font-size: var(--fs-body); }
button.btn-secondary {
  background: transparent; color: var(--ink-secondary);
  border: 1px solid var(--line-strong);
}
button.btn-secondary:hover { color: var(--ink); border-color: var(--ink-muted); opacity: 1; }

.preset-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: var(--sp-3); margin: var(--sp-3) 0; }
.preset-card {
  display: block; background: var(--surface-instrument-1); border: 1px solid var(--line);
  border-radius: 3px; padding: var(--sp-4); cursor: pointer;
}
.preset-card:has(input:checked) { border-color: var(--claim-belief); background: var(--surface-instrument-2); }
.preset-card .preset-title { font-weight: 650; display: flex; align-items: center; gap: var(--sp-2); }
.preset-card .preset-desc { color: var(--ink-muted); font-size: var(--fs-small); margin-top: var(--sp-1); }
.badge {
  font-size: 9px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase;
  padding: 0.1rem 0.35rem; border-radius: 2px; font-family: var(--mono);
}
.badge-recommended { background: var(--claim-belief); color: #0b0d10; }
.badge-new { background: var(--surface-instrument-2); color: var(--claim-belief); border: 1px solid var(--claim-belief-dim); }

/* ---------------------------------------------------------------------
   10. EMPTY STATES -- helpful pointer, not a bare box.
   ------------------------------------------------------------------- */
.empty-state {
  background: var(--surface-instrument-1); border: 1px solid var(--line); border-radius: 3px; padding: var(--sp-6);
  text-align: center; color: var(--ink-secondary);
}
.empty-state .meta { margin-top: var(--sp-2); }

/* ---------------------------------------------------------------------
   11. CTA -- the one primary action on /. Same neutral inverted-contrast
   treatment as button (see above) for the same reason -- a CTA link
   styled as a button should look like the same primary-action language,
   not a second, differently-colored button system.
   ------------------------------------------------------------------- */
.cta-row { margin: var(--sp-4) 0 var(--sp-6); }
.cta-primary {
  display: inline-block; background: var(--ink); color: var(--surface-instrument-0);
  font-weight: 650; font-size: var(--fs-body); padding: 0.7rem 1.3rem;
  border-radius: 4px;
}
.cta-primary:hover { text-decoration: none; opacity: 0.88; }

/* ---------------------------------------------------------------------
   12. STATUS TAGS -- confirmed / needs-replication / retracted, the
   trust-level of a FINDING (distinct concept from a trial verdict --
   see cartographer/viewer/findings.py). Text-label + weight, not a
   colored chip, since these need to be distinguishable at a glance
   while reading dense prose, not scanned as isolated table cells.
   ------------------------------------------------------------------- */
.status-tag {
  font-family: var(--mono); font-size: 0.78rem; font-weight: 650;
  letter-spacing: 0.02em; padding: 0.08em 0.4em; border-radius: 2px;
}
.status-tag.confirmed { color: var(--verdict-pass); border: 1px solid var(--verdict-pass); }
.status-tag.needs-replication { color: var(--claim-belief); border: 1px solid var(--claim-belief-dim); }
.status-tag.retracted {
  color: var(--ink-muted); border: 1px solid var(--line-strong);
  text-decoration: line-through; text-decoration-color: var(--ink-muted);
}

/* ---------------------------------------------------------------------
   13. VISUAL ENCODING -- data bars, sparklines, chips, row severity.

   The diagnosis this section answers: every surface in this app rendered
   quantities as digits in table cells. A bare "81%" makes the reader do
   the comparison arithmetic themselves, row by row; a bar of length 81%
   makes the comparison pre-attentive. The shipped references this design
   derives from (admin-dashboard's in-cell progress meters, the
   bug-tracker's left-edge severity bars) encode magnitude and criticality
   visually and reserve digits for the exact value.

   Focal-point discipline is preserved, NOT regressed into a rainbow: a
   data bar is achromatic ink-secondary by DEFAULT and only takes
   --verdict-fail-trap when the value crosses a threshold that means
   something. The row edge is likewise colorless unless the row is a
   finding. Nothing here introduces a new hue.
   ------------------------------------------------------------------- */
.databar-cell {
  display: flex; align-items: center; gap: var(--sp-2); justify-content: flex-end;
}
th.num + td .databar-cell { justify-content: flex-end; }
.databar {
  position: relative; display: block; width: 58px; height: 6px; flex: none;
  background: var(--line-strong); border-radius: 1px; overflow: hidden;
}
.databar-fill {
  display: block; height: 100%; border-radius: 1px;
  background: var(--ink-secondary);
}
.databar.below .databar-fill { background: var(--verdict-fail-trap); }
.databar.full .databar-fill { background: var(--verdict-pass); }
.databar-num {
  font-family: var(--mono); font-variant-numeric: tabular-nums;
  font-size: var(--fs-small); font-weight: 600; color: var(--ink);
  min-width: 3.1em; text-align: right;
}
.databar-num.below { color: var(--verdict-fail-trap); }
.databar-sub {
  font-family: var(--mono); font-size: 10px; color: var(--ink-muted);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}

/* Sparkline: a trend, not a chart -- no axes, no labels, terminal point
   marked so "where it ended up" reads without a legend. */
.sparkline { display: inline-block; vertical-align: middle; }
.sparkline .spark-area { fill: rgba(255,255,255,0.055); }
.sparkline .spark-line { fill: none; stroke: var(--ink-muted); stroke-width: 1.25; }
.sparkline .spark-mid { stroke: var(--line-strong); stroke-width: 1; stroke-dasharray: 2 2; }
.sparkline .spark-last { fill: var(--claim-belief); }
.spark-row { display: flex; align-items: center; gap: var(--sp-2); }
.spark-row .spark-caption {
  font-family: var(--mono); font-size: 10px; color: var(--ink-muted);
  text-transform: uppercase; letter-spacing: 0.04em;
}

/* Chips: categorical metadata (query kind, trap kinds, render mode) that
   used to be comma-joined prose. A chip is a bounded token you can scan
   down a column; a comma-joined string is something you have to read. */
.chip {
  display: inline-flex; align-items: center; gap: 0.3em;
  font-family: var(--mono); font-size: 10.5px; font-weight: 500;
  line-height: 1.6; padding: 0.1em 0.45em; border-radius: 2px;
  background: var(--surface-instrument-2); color: var(--ink-secondary);
  border: 1px solid var(--line); white-space: nowrap;
}
.chip-set { display: inline-flex; flex-wrap: wrap; gap: var(--sp-1); }
.chip.chip-trap { color: var(--claim-belief); border-color: var(--claim-belief-dim); }
.chip.chip-live { color: var(--claim-belief); border-color: var(--claim-belief-dim); }
.chip.chip-live::before { content: "\\25CF"; font-size: 8px; }
.chip.chip-done::before { content: "\\2713"; font-size: 9px; color: var(--ink-muted); }
.chip.chip-muted { color: var(--ink-muted); }
a.chip:hover { border-color: var(--claim-belief-dim); color: var(--claim-belief); text-decoration: none; }

/* Row severity: the bug-tracker reference puts a colored bar on the LEFT
   EDGE of a row so criticality reads before any text does. Applied here
   to the verdict axis, and deliberately NOT applied to PASS rows -- if
   every row has an edge, no edge means anything. */
tbody tr.row-sev td:first-child { box-shadow: inset 3px 0 0 0 transparent; }
tbody tr.row-sev-fail td:first-child { box-shadow: inset 3px 0 0 0 var(--verdict-fail-trap); }
tbody tr.row-sev-unverified td:first-child { box-shadow: inset 3px 0 0 0 var(--claim-belief-dim); }
tbody tr.row-sev-error td:first-child { box-shadow: inset 3px 0 0 0 var(--verdict-error-infra); }
tbody tr:hover { background: var(--surface-instrument-2); }
tbody tr:focus-within { background: var(--surface-instrument-2); outline: 1px solid var(--line-strong); }

/* ---------------------------------------------------------------------
   14. INSTRUMENT -- the interactive chart surface on /atlas.

   Controls are real <a> links first (every view state has a URL and
   works with JS off); the JS layer below adds hover readout and series
   toggling on top. `.js-only` affordances are hidden until the inline
   script sets `.js` on <html>, so a no-JS reader is never shown a dead
   button.
   ------------------------------------------------------------------- */
.js-only { display: none !important; }
.js .legend.js-only, .js .chip-set.js-only, .js .seg-control.js-only { display: inline-flex !important; }
.js .block-js-only { display: block !important; }

.seg-control {
  display: inline-flex; flex-wrap: wrap; gap: 2px; padding: 2px;
  background: var(--well); border: 1px solid var(--line); border-radius: 4px;
}
.seg-item {
  font-family: var(--mono); font-size: 11px; font-weight: 500;
  padding: 0.3em 0.7em; border-radius: 3px; color: var(--ink-secondary);
}
.seg-item:hover { background: var(--surface-instrument-2); color: var(--ink); text-decoration: none; }
.seg-item:focus-visible { outline: 2px solid var(--claim-belief); outline-offset: 1px; }
.seg-item.active { background: var(--surface-instrument-2); color: var(--ink); font-weight: 650; box-shadow: inset 0 0 0 1px var(--line-strong); }
.control-row {
  display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-4);
  margin: 0 0 var(--sp-4);
}
.control-row-item { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.control-label {
  font-family: var(--mono); font-size: 9.5px; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--ink-muted);
}

.chart-wrap { position: relative; overflow-x: auto; }
.chart-tip {
  position: absolute; z-index: 20; pointer-events: none; opacity: 0;
  transform: translate(-50%, -100%);
  background: var(--surface-instrument-2); border: 1px solid var(--line-strong);
  border-radius: 3px; padding: var(--sp-2) var(--sp-3);
  font-family: var(--mono); font-size: 11px; color: var(--ink);
  white-space: pre; box-shadow: 0 4px 14px rgba(0,0,0,0.45);
  transition: opacity 120ms ease;
}
@media (prefers-reduced-motion: reduce) {
  .chart-tip { transition: none; }
  .databar-fill, .heat-cell, .series-dot { transition: none !important; }
}
.chart-tip[data-open="1"] { opacity: 1; }

.legend { display: inline-flex; flex-wrap: wrap; gap: var(--sp-2); margin: var(--sp-3) 0 0; }
.legend-toggle {
  display: inline-flex; align-items: center; gap: 0.45em; cursor: pointer;
  font-family: var(--mono); font-size: 11px; color: var(--ink-secondary);
  background: var(--surface-instrument-1); border: 1px solid var(--line);
  border-radius: 3px; padding: 0.25em 0.6em;
}
.legend-toggle:hover { border-color: var(--line-strong); color: var(--ink); }
.legend-toggle:focus-visible { outline: 2px solid var(--claim-belief); outline-offset: 1px; }
.legend-toggle .swatch { width: 10px; height: 3px; border-radius: 1px; flex: none; }
.legend-toggle[aria-pressed="false"] { opacity: 0.4; text-decoration: line-through; }
.legend-toggle[aria-pressed="false"] .swatch { background: var(--ink-muted) !important; }

/* Series + heat cells: hidden via an attribute so the state is readable
   from the DOM (and from a test) rather than only from computed style. */
[data-hidden="1"] { display: none; }
.series-dot { cursor: pointer; }
.series-dot:hover, .series-dot:focus { stroke: var(--ink); stroke-width: 2; outline: none; }
.heat-cell { cursor: pointer; }
.heat-cell:hover rect.hc-bg, .heat-cell:focus rect.hc-bg { stroke: var(--ink); stroke-width: 1.5; outline: none; }
.heat-note {
  font-family: var(--mono); font-size: 10.5px; color: var(--ink-muted);
  margin: var(--sp-3) 0 0;
}

/* ---------------------------------------------------------------------
   15. THE CAPABILITY MAP -- /findings' models x capabilities grid, plus
   the trust vocabulary that hangs off each derived finding.

   The ramp is built ONLY from tokens this product already uses to mean
   something: verdict-pass green (correct), claim-belief amber (a
   provisional claim), salience amber (a measured ramp), verdict-fail-trap
   red (a trap was sprung). Read top to bottom it is monotone green ->
   amber -> red, so it is a heat scale and not a rainbow, and it
   introduces no hue the rest of the app doesn't already have. Every cell
   is an <a>: the grid's whole job is to make a weak province visible AND
   reachable in one click.
   ------------------------------------------------------------------- */
.cap-map-wrap { overflow-x: auto; }
table.cap-map { margin: 0; background: none; border: none; }
table.cap-map th.cap-group {
  text-align: center; background: var(--surface-instrument-2);
  border-bottom: 1px solid var(--line-strong);
}
table.cap-map th.cap-corner { background: var(--surface-instrument-2); }
table.cap-map th, table.cap-map td { padding: 0.3rem 0.35rem; }
table.cap-map th.cap-model {
  text-align: left; text-transform: none; font-family: var(--sans);
  font-size: var(--fs-small); font-weight: 650; color: var(--ink);
  background: none; letter-spacing: 0; white-space: nowrap;
  border-bottom: 1px solid var(--line); vertical-align: middle;
}
table.cap-map th.cap-model a { color: var(--ink); }
.cap-model-meta {
  display: block; font-family: var(--mono); font-size: 9.5px;
  color: var(--ink-muted); font-weight: 500;
}
.cap-arrow { color: var(--ink-muted); margin-left: 0.2em; }
td.cap-cell { border-bottom: 1px solid var(--line); }
.cap-block {
  display: block; border-radius: 2px; padding: 0.35rem 0.4rem;
  text-align: center; font-family: var(--mono); min-width: 58px;
  border: 1px solid transparent;
}
a.cap-block:hover { text-decoration: none; border-color: var(--ink); }
a.cap-block:focus-visible { outline: 2px solid var(--claim-belief); outline-offset: 1px; }
.cap-block .cap-rate {
  display: block; font-size: var(--fs-small); font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.cap-block .cap-n { display: block; font-size: 9px; opacity: 0.75; font-variant-numeric: tabular-nums; }
.heat-5 { background: rgba(92,158,124,0.22); color: var(--verdict-pass); }
.heat-4 { background: rgba(217,164,65,0.16); color: var(--claim-belief); }
.heat-3 { background: rgba(176,129,46,0.30); color: var(--salience-high); }
.heat-2 { background: rgba(196,84,75,0.26); color: var(--verdict-fail-trap); }
.heat-1 { background: rgba(196,84,75,0.55); color: var(--ink); }
.heat-none {
  background-image: repeating-linear-gradient(135deg, rgba(255,255,255,0.04) 0 3px, transparent 3px 7px);
  color: var(--ink-muted);
}
.cap-row-selected td.cap-cell, .cap-row-selected th.cap-model { background: var(--surface-instrument-2); }
.cap-legend { display: flex; flex-wrap: wrap; gap: var(--sp-3); margin: var(--sp-3) 0; }
.cap-legend-item {
  display: inline-flex; align-items: center; gap: 0.4em;
  font-family: var(--mono); font-size: 9.5px; color: var(--ink-muted);
}
.cap-swatch { width: 16px; height: 10px; border-radius: 2px; display: inline-block; }

/* Model switcher: the same inverted-contrast "exactly one is selected"
   language as the sidebar's active nav row, at page scope. */
.model-tabs {
  display: flex; flex-wrap: wrap; gap: var(--sp-2);
  margin: var(--sp-5) 0 var(--sp-4);
  border-bottom: 1px solid var(--line); padding-bottom: var(--sp-3);
}
.model-tab {
  font-size: var(--fs-small); font-weight: 600; color: var(--ink-secondary);
  padding: 0.35rem 0.7rem; border-radius: 6px; border: 1px solid var(--line);
  background: var(--surface-instrument-1); white-space: nowrap;
}
.model-tab:hover { text-decoration: none; color: var(--ink); border-color: var(--line-strong); }
.model-tab.active { background: var(--ink); color: var(--surface-instrument-0); border-color: var(--ink); }

/* A derived finding's left edge carries its polarity -- the same
   left-edge-severity idiom section 13 uses on table rows, so "this is a
   weak spot" reads before the sentence does. */
.finding-weakness { border-left: 2px solid var(--verdict-fail-trap); }
.finding-strength { border-left: 2px solid var(--verdict-pass); }
.card-body details.replicate { margin-top: var(--sp-3); }

/* Verification state of a whole capability cell -- the SAME three words
   the claim strip already uses for one FAIL (verified-stable / noise /
   unverified), and the same treatments: stable is a neutral audited fact
   (pass-green edge, ink-secondary text, not alarming), noise is hatched,
   unverified is a dashed outline. Replication state is a second,
   orthogonal tag with the same grammar. */
.status-tag.verif-verified-stable { color: var(--ink-secondary); border: 1px solid var(--verdict-pass); }
.status-tag.verif-contains-noise {
  color: var(--ink-muted); border: 1px solid var(--line-strong);
  background-image: repeating-linear-gradient(135deg, rgba(255,255,255,0.05) 0 3px, transparent 3px 7px);
}
.status-tag.verif-unverified { color: var(--ink-muted); border: 1px dashed var(--line-strong); }
/* Zero failures: nothing was resampled, so this deliberately does NOT get
   the pass-green edge "verified-stable" earns. A clean cell is quiet. */
.status-tag.verif-no-failures { color: var(--ink-muted); border: 1px solid var(--line); }
.status-tag.repl-replicated { color: var(--verdict-pass); border: 1px solid var(--verdict-pass); }
.status-tag.repl-replication-disagrees { color: var(--claim-belief); border: 1px solid var(--claim-belief-dim); }
.status-tag.repl-replication-incomplete { color: var(--ink-muted); border: 1px dashed var(--claim-belief-dim); }
.status-tag.repl-not-replicated { color: var(--ink-muted); border: 1px dashed var(--line-strong); }

/* A cited run_id whose records are no longer in the store. Struck, not
   hidden: the citation is still the honest provenance of the claim, it
   just can't be followed any more. */
.dangling { color: var(--ink-muted); text-decoration: line-through; text-decoration-color: var(--verdict-fail-trap); }
"""


# Inline, vanilla, dependency-free. Two jobs only: a hover/focus readout
# for any `[data-tip]` inside a `.chart-wrap`, and series toggling driven
# by `[data-toggle-model]` buttons. Both are progressive enhancements over
# markup that is already complete and readable without them -- every data
# point also carries an SVG <title> (native tooltip), and every control
# that changes what is PLOTTED is a real link, not a button.
_INSTRUMENT_JS = """
(function () {
  var d = document;
  d.documentElement.className += " js";

  function tipFor(wrap) {
    var t = wrap.querySelector(".chart-tip");
    if (!t) {
      t = d.createElement("div");
      t.className = "chart-tip";
      wrap.appendChild(t);
    }
    return t;
  }

  function showTip(wrap, el) {
    var text = el.getAttribute("data-tip");
    if (!text) return;
    var tip = tipFor(wrap);
    tip.textContent = text.replace(/\\|/g, "\\n");
    var wr = wrap.getBoundingClientRect();
    var er = el.getBoundingClientRect();
    var x = er.left - wr.left + wrap.scrollLeft + er.width / 2;
    var y = er.top - wr.top + wrap.scrollTop - 6;
    tip.style.left = x + "px";
    tip.style.top = Math.max(y, 24) + "px";
    tip.setAttribute("data-open", "1");
  }

  function hideTip(wrap) {
    var tip = wrap.querySelector(".chart-tip");
    if (tip) tip.setAttribute("data-open", "0");
  }

  function bindWrap(wrap) {
    function onOver(ev) {
      var el = ev.target && ev.target.closest ? ev.target.closest("[data-tip]") : null;
      if (el && wrap.contains(el)) showTip(wrap, el); else hideTip(wrap);
    }
    wrap.addEventListener("mouseover", onOver);
    wrap.addEventListener("focusin", onOver);
    wrap.addEventListener("mouseleave", function () { hideTip(wrap); });
    wrap.addEventListener("focusout", function () { hideTip(wrap); });
  }

  var wraps = d.querySelectorAll(".chart-wrap");
  for (var i = 0; i < wraps.length; i++) bindWrap(wraps[i]);

  // Series toggles. Scope is the nearest `.instrument` ancestor so two
  // charts on one page never toggle each other.
  function setModel(scope, model, on) {
    var targets = scope.querySelectorAll('[data-series="' + (window.CSS && CSS.escape ? CSS.escape(model) : model) + '"]');
    for (var i = 0; i < targets.length; i++) {
      if (on) targets[i].removeAttribute("data-hidden");
      else targets[i].setAttribute("data-hidden", "1");
    }
  }

  d.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest("[data-toggle-model]") : null;
    if (!btn) return;
    ev.preventDefault();
    var scope = btn.closest(".instrument") || d;
    var model = btn.getAttribute("data-toggle-model");
    var on = btn.getAttribute("aria-pressed") !== "true";
    // Never let the reader blank the chart entirely: refuse the last one off.
    if (!on) {
      var pressed = scope.querySelectorAll('[data-toggle-model][aria-pressed="true"]');
      if (pressed.length <= 1) return;
    }
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    setModel(scope, model, on);
  });
})();
"""


# One restrained mono glyph per nav item -- not an imported icon set (a
# generic Feather/Lucide icon row would just be a different flavor of the
# same "any AI's sidebar" problem), single characters/brackets already at
# home in an instrument's own vocabulary (readouts, brackets, marks).
_NAV_ICONS = {
    "overview": "▣",
    "findings": "✓",
    "report": "✎",
    "runs": "≡",
    "atlas": "∿",
    "coverage": "▦",
    "launch": "▶",
    "live": "●",
}


def _shell(
    title: str,
    active_nav: str,
    body: str,
    refresh: bool = False,
    breadcrumb: str | None = None,
    trial_count: int | None = None,
) -> str:
    """Render `body` inside the shared sidebar shell.

    Global navigation lives in a fixed-width LEFT SIDEBAR, not a top bar
    (docs/design-notes.md's original top-bar skeleton was itself never
    questioned across three passes -- every real SaaS/instrument reference
    with this many nav items -- Linear, Grafana, Datadog, the interface-
    design skill's own showcase examples -- uses a sidebar; a horizontal
    strip with 7 items was always going to feel cramped/generic, a paint
    job away from a sidebar rather than the right container). Global nav
    (this sidebar) and page-level context (breadcrumb/title) are two
    different jobs and now live in two different regions: the sidebar
    never changes across pages, the slim `.page-strip` inside main is
    page-specific.

    `active_nav` is one of the keys in `_NAV_ITEMS` ("runs", "live",
    "atlas", "launch") or "" for none highlighted. Nav is grouped into
    Results / Explore / Act under real vertical whitespace + uppercase
    mono group labels -- now something a sidebar can actually show AS
    groups, not just as ordering. The active item gets a filled
    background on its whole row (selected sidebar item), not a bottom-
    border underline (that reads as "browser tab," which is exactly the
    top-bar tell being removed here).
    `breadcrumb`, if given, renders in the page strip.
    `trial_count`, if given, renders a live mono counter in the sidebar's
    identity block ("this is a running instrument," visible before
    scrolling). Optional and additive: pages without a store in scope
    (coverage/findings, the 404 handler, launch error pages) simply
    render no counter.
    """
    refresh_tag = '<meta http-equiv="refresh" content="5">' if refresh else ""
    groups_html = []
    for group_label, items in _NAV_GROUPS:
        rows = "".join(
            f'<a class="nav-row{" active" if key == active_nav else ""}" href="{href}">'
            f'<span class="nav-icon">{_NAV_ICONS.get(key, "·")}</span>'
            f"<span class=\"nav-label\">{label}</span></a>"
            for key, href, label in items
        )
        groups_html.append(
            f'<div class="nav-group"><div class="nav-group-label">{group_label}</div>{rows}</div>'
        )
    nav_groups_html = "".join(groups_html)
    counter_html = (
        f'<span class="counter">{trial_count:,} trials recorded</span>'
        if trial_count is not None
        else ""
    )
    sidebar = (
        '<aside class="sidebar">'
        '<a class="brand-block" href="/">'
        '<span class="brand-mark">MC</span>'
        '<span class="brand-text"><span class="brand-name">Model Cartographer</span>'
        f"{counter_html}</span>"
        "</a>"
        f'<nav class="sidebar-nav">{nav_groups_html}</nav>'
        "</aside>"
    )
    breadcrumb_html = f'<div class="page-strip"><p class="breadcrumb">{breadcrumb}</p></div>' if breadcrumb else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"{refresh_tag}"
        # Manrope + JetBrains Mono -- the pairing the reference examples
        # this design derives from actually ship. Purely additive: the
        # --sans/--mono tokens list full system fallbacks after these, so
        # an offline or blocked-network run degrades to the previous
        # system-stack rendering rather than breaking. `display=swap`
        # keeps first paint immediate.
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        "family=Manrope:wght@400;500;600;700&"
        'family=JetBrains+Mono:wght@400;500;600&display=swap">'
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f'<body><div class="app-shell">{sidebar}'
        f'<main class="shell-main">{breadcrumb_html}{body}</main>'
        f"</div><script>{_INSTRUMENT_JS}</script></body></html>"
    )


# ---------------------------------------------------------------------------
# Visual encoding primitives -- shared by every page that shows a rate, a
# trend, or a categorical tag. See _CSS section 13 for the reasoning; the
# short version is that a bare percentage makes the reader do the
# comparison, a bar does the comparison for them.
# ---------------------------------------------------------------------------

# Below this pass rate a bar stops being neutral and takes the trap color.
# Not a style choice: a scored pass rate under ~60% is the regime where a
# campaign's own scheduler treats a cell as "broken" rather than noisy.
_RATE_WARN_THRESHOLD = 60.0
_RATE_FULL_THRESHOLD = 99.999


def _rate_bar(
    rate_pct: float,
    *,
    sub: str | None = None,
    threshold: float | None = _RATE_WARN_THRESHOLD,
    decimals: int = 0,
) -> str:
    """Inline pass-rate meter: neutral bar + exact number, semantic color
    ONLY below `threshold` (admin-dashboard reference: in-cell meters that
    are identical gray until one crosses the line that matters).

    `threshold=None` means "magnitude only, never colored" -- for bars
    measuring a quantity where high/low carries no verdict (a share of
    failures, a share of budget). Without that escape hatch every bar on
    the page ends up colored, which is exactly the decorate-everything
    failure the threshold exists to avoid.

    `sub` is optional small print after the number (e.g. "113/214").
    Defensive: clamps to 0..100 so a NaN/overshoot can never paint a bar
    wider than its track.
    """
    try:
        pct = float(rate_pct)
    except (TypeError, ValueError):
        pct = 0.0
    if pct != pct:  # NaN
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    state = ""
    if threshold is None:
        state = ""
    elif pct < threshold:
        state = " below"
    elif pct >= _RATE_FULL_THRESHOLD:
        state = " full"
    sub_html = f'<span class="databar-sub">{sub}</span>' if sub else ""
    return (
        '<span class="databar-cell">'
        f'{sub_html}'
        f'<span class="databar{state}"><span class="databar-fill" style="width:{pct:.1f}%"></span></span>'
        f'<span class="databar-num{state}">{pct:.{decimals}f}%</span>'
        "</span>"
    )


def _rate_sequence(records: list, n_buckets: int = 24) -> list[float]:
    """Pass rate (0..1) over the trial sequence, chunked into at most
    `n_buckets` equal slices -- the input to a run's sparkline. ERRORs are
    excluded from the rate but not from ordering. Returns [] when there is
    nothing scoreable (so callers render no sparkline at all rather than a
    flat lie)."""
    scored = [r for r in records if getattr(r, "verdict", None) is not None and r.verdict.name in ("PASS", "FAIL")]
    if len(scored) < 4:
        return []
    k = max(2, min(n_buckets, len(scored) // 2))
    out: list[float] = []
    for i in range(k):
        lo = i * len(scored) // k
        hi = (i + 1) * len(scored) // k
        chunk = scored[lo:hi]
        if not chunk:
            continue
        out.append(sum(1 for r in chunk if r.verdict.name == "PASS") / len(chunk))
    return out


def _verdict_seq_rates(verdict_names: list[str], n_buckets: int = 24) -> list[float]:
    """`_rate_sequence` over a bare list of verdict NAMES (what the /runs
    aggregate query has in hand -- it never materializes records)."""
    scored = [v for v in verdict_names if v in ("PASS", "FAIL")]
    if len(scored) < 4:
        return []
    k = max(2, min(n_buckets, len(scored) // 2))
    out: list[float] = []
    for i in range(k):
        chunk = scored[i * len(scored) // k:(i + 1) * len(scored) // k]
        if chunk:
            out.append(sum(1 for v in chunk if v == "PASS") / len(chunk))
    return out


def _sparkline(values: list[float], width: int = 76, height: int = 20, label: str = "") -> str:
    """Tiny inline SVG trend over `values` (each 0..1). No library, no
    axes, no labels -- a 50% reference dash and a marked terminal point,
    which is all a sparkline is allowed to say.

    Returns "" for fewer than 2 points: a one-point "trend" is a dot
    pretending to be information.
    """
    pts = [v for v in values if isinstance(v, (int, float))]
    if len(pts) < 2:
        return ""
    pad = 2.0
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad
    step = inner_w / (len(pts) - 1)
    coords = [
        (pad + i * step, pad + (1.0 - max(0.0, min(1.0, v))) * inner_h)
        for i, v in enumerate(pts)
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"{pad:.1f},{height - pad:.1f} " + line + f" {coords[-1][0]:.1f},{height - pad:.1f}"
    mid_y = pad + 0.5 * inner_h
    lx, ly = coords[-1]
    aria = f' role="img" aria-label="{html.escape(label)}"' if label else ' aria-hidden="true"'
    return (
        f'<svg class="sparkline" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" xmlns="http://www.w3.org/2000/svg"{aria}>'
        f'<polygon class="spark-area" points="{area}"/>'
        f'<line class="spark-mid" x1="{pad}" y1="{mid_y:.1f}" x2="{width - pad}" y2="{mid_y:.1f}"/>'
        f'<polyline class="spark-line" points="{line}"/>'
        f'<circle class="spark-last" cx="{lx:.1f}" cy="{ly:.1f}" r="2"/>'
        "</svg>"
    )


def _chip(label: str, cls: str = "", href: str | None = None, title: str = "") -> str:
    """One categorical token. `label` is escaped here -- callers pass raw
    db strings."""
    if not label:
        return ""
    classes = f"chip {cls}".strip()
    title_attr = f' title="{html.escape(title)}"' if title else ""
    inner = html.escape(str(label))
    if href:
        return f'<a class="{classes}" href="{html.escape(href)}"{title_attr}>{inner}</a>'
    return f'<span class="{classes}"{title_attr}>{inner}</span>'


def _chip_set(labels, cls: str = "") -> str:
    """A comma-joined metadata string rendered as scannable chips. Accepts
    a list or a raw "a,b,c" string; empty input renders nothing."""
    if isinstance(labels, str):
        labels = [p.strip() for p in labels.split(",")]
    items = [_chip(str(x).strip(), cls) for x in (labels or []) if str(x).strip()]
    if not items:
        return ""
    return f'<span class="chip-set">{"".join(items)}</span>'


# Backwards-compatible alias used by atlas.py and launch.py call sites that
# don't need nav highlighting (kept internal to this module's API surface).
def _page(title: str, body: str, refresh: bool = False, active_nav: str = "") -> str:
    return _shell(title, active_nav, body, refresh=refresh)


# mark, css-state-class per verdict name -- the glyph itself carries
# meaning (not just its color), so the claim strip reads correctly even
# without color: check = matched, cross = a trap was sprung, "?" is
# reserved for the FAIL-but-not-yet-resample-checked case (set by callers
# that pass stability="unverified"; see _claim_strip).
_VERDICT_MARK = {"PASS": "✓", "FAIL": "✕", "ERROR": "✕"}
_VERDICT_STATE_CLASS = {"PASS": "v-pass", "FAIL": "v-fail-trap", "ERROR": "v-error"}


def _title_pill_html(records: list[ExperimentRecord]) -> str:
    """An inline pill beside an h1 summarizing a set of records' verdicts
    at a glance -- confirmed against a real shipped API-test-runner
    example (test-factory.png: "Get User by ID  [PASSED]", a small pill
    directly beside the title, not a big separate card). The FULL
    evidence still lives in the verdict panel / per-trial table below;
    this is only the "what happened" glance. Returns "" for an empty
    record set (nothing to summarize)."""
    scored = [r for r in records if r.verdict.name in ("PASS", "FAIL")]
    if not scored:
        return ""
    passn = sum(1 for r in scored if r.verdict.name == "PASS")
    total = len(scored)
    if passn == total:
        return '<span class="title-pill tp-pass">PASSED</span>'
    if passn == 0:
        return '<span class="title-pill tp-fail">FAILED</span>'
    rate = passn / total * 100.0
    return f'<span class="title-pill tp-mixed">{rate:.0f}% PASS</span>'


def _short_answer(response_text: str | None, max_chars: int = 60) -> str:
    """A claim strip's evidence clause needs a SHORT quote of what the
    model actually answered -- but some models (observed live: MiniMax-M3
    on adversarial_belief runs) write their full chain-of-thought directly
    into `response_text`, with the stated final answer at the very END,
    not the start. Naively slicing the first `max_chars` characters lands
    on a truncated reasoning fragment ("Looking at this problem, I need to
    track what Hakim has p...") instead of an answer -- misleading, not
    just ugly. Mirrors `verdict.py::_notable_failures`'s own pattern for
    extracting the QUESTION from a multi-line prompt (`.splitlines()[-1]`):
    take the LAST non-empty line, which is far more likely to be the
    stated answer for a verbose response, and is a no-op for a terse
    single-line answer (its only line IS its last line).
    """
    text = (response_text or "").strip()
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    tail = lines[-1] if lines else text
    return tail if len(tail) <= max_chars else tail[: max_chars - 3] + "..."


def _claim_strip(
    verdict_name: str,
    evidence: str | None = None,
    stability: str | None = None,
    salience: float | None = None,
) -> str:
    """The signature verdict component (see docs/design-notes.md "The
    signature"): [mark] VERDICT  evidence clause  stability tag -- not a
    colored chip. Degrades gracefully to bare mark+word when no evidence
    is available (state/order records, or a plain PASS with nothing
    notable to say), so it never renders dangling punctuation.

    `stability`, if given, is one of "stable"/"noise"/"unverified" (see
    `_VERIFY_STATUS_LABEL`) -- FAIL rows only. A FAIL that hasn't been
    resample-checked yet renders with a "?" mark instead of a cross, since
    a not-yet-checked failure is a different epistemic state from a
    resample-confirmed one, not just a visual variant.
    `salience`, if given (adversarial belief records only), renders a
    small color-ramp swatch ahead of the evidence clause.
    """
    safe_verdict = html.escape(verdict_name)
    state_cls = _VERDICT_STATE_CLASS.get(verdict_name, "v-error")
    mark = _VERDICT_MARK.get(verdict_name, "?")
    row_cls = f"claim {state_cls}"
    if verdict_name == "FAIL" and stability == "unverified":
        mark = "?"
        row_cls = "claim v-fail-unverified"

    evidence_html = f'<span class="evidence">{evidence}</span>' if evidence else ""
    if salience is not None and evidence:
        if salience >= 0.66:
            sal_color = "var(--salience-high)"
        elif salience >= 0.33:
            sal_color = "var(--salience-mid)"
        else:
            sal_color = "var(--salience-low)"
        evidence_html = (
            f'<span class="evidence"><span class="sal-swatch" '
            f'style="background:{sal_color}"></span>{evidence}</span>'
        )

    stability_html = ""
    if stability and verdict_name == "FAIL":
        label = html.escape(_VERIFY_STATUS_LABEL.get(stability, stability))
        stability_html = f'<span class="stability {html.escape(stability)}">{label}</span>'

    return (
        f'<span class="{row_cls}">'
        f'<span class="mark">{mark}</span>'
        f'<span class="verdict-word">{safe_verdict}</span>'
        f"{evidence_html}{stability_html}"
        "</span>"
    )


def _verdict_pill(verdict_name: str) -> str:
    """Bare claim strip with no evidence clause -- used at call sites that
    only have a verdict name in hand (no evidence context available).
    Prefer `_claim_strip` directly when evidence/stability data exists."""
    return _claim_strip(verdict_name)


# Kept as an alias: some call sites/tests refer to the old name.
_verdict_span = _verdict_pill


def _parse_verifier_detail(detail: str | None) -> dict[str, str]:
    """Parse a `;`-separated `key=value` string, e.g. 'kind=state;sim=near_duplicate'.
    Tolerant of missing/malformed detail -- returns an empty dict rather than
    raising, since this is display-only.
    """
    out: dict[str, str] = {}
    if not detail:
        return out
    for part in detail.split(";"):
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


def _relative_time(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    age_s = _age_seconds(ts)
    if age_s < 0:
        age_s = 0
    if age_s < 60:
        return f"{int(age_s)}s ago"
    if age_s < 3600:
        return f"{int(age_s // 60)}m ago"
    if age_s < 86400:
        return f"{int(age_s // 3600)}h ago"
    return f"{int(age_s // 86400)}d ago"


def _verdict_headline_safe(store: ResultsStore, run_id: str) -> str:
    """`run_verdict`'s headline, defensively -- never let a verdict
    computation (which regenerates worlds) break a listing page."""
    try:
        return run_verdict(store, run_id).headline
    except Exception:
        return ""


def verdict_panel_html(store: ResultsStore, run_id: str) -> str:
    """Renders the "what this run revealed" panel: headline + paragraphs +
    up to 3 notable failure examples. Meant to sit at the TOP of the run
    detail page, above the raw per-trial table."""
    try:
        verdict = run_verdict(store, run_id)
    except Exception as exc:  # never let a verdict crash the run page
        return (
            '<div class="stat-box" style="grid-column: 1 / -1;">'
            '<p class="meta">verdict unavailable for this run '
            f"({html.escape(repr(exc))}).</p></div>"
        )

    paragraphs_html = "".join(f"<p>{html.escape(p)}</p>" for p in verdict.paragraphs)

    # Assertion-row pattern (docs/design-notes.md, sidebar/showcase
    # addendum): expected/actual side by side, mismatch colored, "actual:"
    # label stays muted -- a near-exact structural fit confirmed against a
    # real shipped API-test-runner's "Assertions" card, reused here for
    # "expected: what entity X believed" vs "actual: what the model said."
    failures_html = ""
    if verdict.notable_failures:
        items = []
        for f in verdict.notable_failures:
            # `f.model_answer` is `verdict.py`'s raw response_text, full
            # length (that module is off-limits territory for this pass
            # -- cartographer/report/** -- so the short-answer fix stays a
            # display-time transform here, same _short_answer used for
            # every other response_text render in this file, rather than
            # a change to NotableFailure's own data model).
            model_answer_short = _short_answer(f.model_answer, max_chars=100)
            items.append(
                "<li style=\"margin-bottom:0.6rem;\">"
                f'<a href="/record/{html.escape(f.record_id)}">record {html.escape(f.record_id[:8])}</a> '
                f'<span class="meta">&mdash; asked "{html.escape(f.question)}"</span>'
                '<div class="assertion-row" style="margin:0.2rem 0 0.15rem;">'
                f'<span class="aa-label">expected:</span> <span class="aa-expected">{html.escape(f.correct_answer)}</span>'
                "</div>"
                '<div class="assertion-row">'
                f'<span class="aa-label">actual:</span> <span class="aa-actual-mismatch">{html.escape(model_answer_short)}</span>'
                "</div>"
                f'<p class="meta" style="margin:0.2rem 0 0;">{html.escape(f.why_wrong)}</p>'
                "</li>"
            )
        failures_html = (
            "<h3>Notable failures</h3><ul style=\"list-style:none; padding-left:0;\">"
            + "".join(items) + "</ul>"
        )

    return (
        '<div class="panel" data-testid="verdict-panel">'
        f'<h2 style="margin-top:0;">{html.escape(verdict.headline)}</h2>'
        f"{paragraphs_html}"
        f"{failures_html}"
        "</div>"
    )


def _runs_table_rows(store: ResultsStore) -> tuple[list[str], dict[str, dict]]:
    cur = store._conn.execute(
        "SELECT run_id, model_name, verdict, cost_usd, created_at FROM experiments"
    )
    by_run: dict[str, dict] = {}
    for row in cur.fetchall():
        run_id = row["run_id"]
        agg = by_run.setdefault(
            run_id,
            {"counts": {}, "model": row["model_name"], "cost": 0.0, "created_ats": [], "seq": []},
        )
        agg["counts"][row["verdict"]] = agg["counts"].get(row["verdict"], 0) + 1
        agg["cost"] += row["cost_usd"] or 0.0
        # Verdicts in insertion order -- the input to this run's sparkline.
        # Cheap: one extra string per row on a query already being read.
        agg["seq"].append(row["verdict"])
        if row["created_at"]:
            agg["created_ats"].append(row["created_at"])

    # full_map runs record no rows directly against the parent run_id --
    # every trial lives under f"{parent}/{stage}" child run_ids. Fold those
    # child rows' aggregates up into a synthetic parent entry (and drop the
    # raw child rows from the top-level table) so /runs shows one grouped
    # row per full_map launch instead of five disconnected stage rows.
    parents: dict[str, dict] = {}
    child_run_ids: set[str] = set()
    for run_id in list(by_run.keys()):
        if "/" in run_id:
            parent, stage = run_id.rsplit("/", 1)
            child_run_ids.add(run_id)
            agg = by_run[run_id]
            p = parents.setdefault(
                parent,
                {"counts": {}, "model": agg["model"], "cost": 0.0, "created_ats": [], "seq": []},
            )
            for verdict_name, n in agg["counts"].items():
                p["counts"][verdict_name] = p["counts"].get(verdict_name, 0) + n
            p["cost"] += agg["cost"]
            p["created_ats"].extend(agg["created_ats"])
            p["seq"].extend(agg["seq"])
    for parent, agg in parents.items():
        by_run[parent] = agg
    for child in child_run_ids:
        by_run.pop(child, None)

    active = launch_mod.current_run()
    active_run_id = active["run_id"] if active else None

    def sort_key(run_id: str) -> str:
        ats = by_run[run_id]["created_ats"]
        return max(ats) if ats else ""

    rows_html = []
    for run_id in sorted(by_run.keys(), key=sort_key, reverse=True):
        agg = by_run[run_id]
        counts = agg["counts"]
        total = sum(counts.values())
        passn = counts.get("PASS", 0)
        pass_rate = (passn / total * 100.0) if total else 0.0
        newest = None
        if agg["created_ats"]:
            newest = _parse_created_at(max(agg["created_ats"]))
        is_live = run_id == active_run_id or (
            newest is not None and _age_seconds(newest) <= _COMPLETE_AFTER_S
        )
        status = "live" if is_live else "complete"
        safe_run_id = html.escape(run_id)
        model_short = html.escape(_short_model(agg["model"]))
        headline = _verdict_headline_safe(store, run_id)
        headline_html = (
            f'<div class="meta" style="margin-top:0.15rem;">{html.escape(headline)}</div>'
            if headline
            else ""
        )
        # A run's pass rate over its own trial sequence: did it degrade as
        # the scheduler pushed difficulty, or was it flat? That trend is
        # the single most useful thing about a run that a total can't say.
        spark = _sparkline(
            _verdict_seq_rates(agg["seq"]),
            label=f"pass rate trend for {run_id}",
        )
        # Row edge: only a run that actually broke gets one (see _CSS 13).
        row_cls = ' class="row-sev row-sev-fail"' if total and pass_rate < _RATE_WARN_THRESHOLD else ""
        status_chip = (
            _chip("live", "chip-live") if is_live else _chip("complete", "chip-done chip-muted")
        )
        rows_html.append(
            f"<tr{row_cls}>"
            f'<td class="mono"><a href="/run/{safe_run_id}">{safe_run_id}</a>{headline_html}</td>'
            f'<td class="mono">{model_short}</td>'
            f'<td class="num">{total}</td>'
            f'<td class="num">{_rate_bar(pass_rate, sub=f"{passn}/{total}")}</td>'
            f'<td>{spark}</td>'
            f'<td class="mono num">${agg["cost"]:.4f}</td>'
            f"<td>{html.escape(_relative_time(newest))}</td>"
            f"<td>{status_chip}</td>"
            "</tr>"
        )
    return rows_html, by_run


def _trial_count(store: ResultsStore) -> int:
    """Total recorded trials -- feeds the header's live mono counter. A
    cheap COUNT(*), not a full row fetch."""
    cur = store._conn.execute("SELECT COUNT(*) AS n FROM experiments")
    row = cur.fetchone()
    return int(row["n"]) if row is not None else 0


def report_html(store: ResultsStore) -> str:
    """The /report page -- the analyst's latest capability diagnosis, its
    contamination audit, and any literature corroboration. Read-only: the
    report was generated offline by `scripts/write_report.py`; this route
    only renders the sidecar it wrote (or an empty state if none exists)."""
    from ..analyst.report_page import load_report, report_body

    body = report_body(load_report())
    return _shell(
        "Capability report", "report", body, trial_count=_trial_count(store)
    )


def runs_html(store: ResultsStore) -> str:
    """The raw per-run table (formerly the home page)."""
    rows_html, by_run = _runs_table_rows(store)
    head = (
        '<div class="page-head">'
        '<div class="page-head-main"><h1>Runs</h1>'
        '<p class="subtitle">Every campaign/pilot run recorded in this store, one row each '
        '(full_map runs are grouped under their parent run_id). Click a run_id for the full '
        "report and per-trial table.</p></div>"
        '<div class="page-head-actions">'
        '<a class="cta-primary" href="/launch">Map a model &rarr;</a>'
        "</div></div>"
    )
    if not rows_html:
        body = (
            f"{head}"
            '<div class="empty-state"><p>No runs yet.</p>'
            '<p class="meta"><a href="/launch">Launch your first campaign &rarr;</a></p></div>'
        )
        return _shell("Runs", "runs", body, trial_count=_trial_count(store))

    # KPI row, computed from the same grouped aggregate the table renders
    # so the numbers can never disagree with the rows beneath them.
    n_runs = len(by_run)
    n_trials = sum(sum(agg["counts"].values()) for agg in by_run.values())
    total_spend = sum(agg["cost"] for agg in by_run.values())
    n_models = len({agg["model"] for agg in by_run.values() if agg["model"]})
    stat_grid = (
        '<div class="stat-grid">'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_runs}</div></div>'
        '<div class="stat-label">Runs recorded</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_trials:,}</div></div>'
        '<div class="stat-label">Total trials</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">${total_spend:.4f}</div></div>'
        '<div class="stat-label">Total spend</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_models}</div></div>'
        '<div class="stat-label">Models covered</div></div>'
        "</div>"
    )
    table = (
        "<table><thead><tr><th>run_id</th><th>model</th><th class=\"num\">records</th>"
        "<th class=\"num\">pass rate</th><th>trend</th><th class=\"num\">cost</th>"
        "<th>started</th><th>status</th>"
        "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>"
    )
    body = (
        f"{head}"
        f"{stat_grid}"
        '<div class="card"><div class="card-head">'
        '<span class="card-title">All runs</span>'
        f'<span class="card-meta">{n_runs} run{"" if n_runs == 1 else "s"}</span>'
        f"</div>{table}</div>"
    )
    return _shell("Runs", "runs", body, trial_count=_trial_count(store))


_QUICK_LINKS = [
    ("/findings", "Findings", "The capability map -- confirmed failure modes, mechanisms, and evidence."),
    ("/coverage", "Coverage", "What we test (query kinds, difficulty dims, foil traps) and what we don't."),
    ("/runs", "Runs", "Raw table of every campaign/pilot run recorded in this store."),
    ("/live", "Live", "Watch an in-progress campaign's trials come in, with ETA."),
    ("/atlas", "Atlas", "Cross-model pass-rate charts per query kind and dimension."),
    ("/launch", "Launch", "Start a new campaign against a model from the browser."),
]


def index_html(store: ResultsStore) -> str:
    """Overview dashboard: what this instrument does, three summary cards
    computed live from the store, and quick links into the rest of the
    site. The raw per-run table lives at /runs.
    """
    cur = store._conn.execute("SELECT verdict, cost_usd, verifier_detail, model_name FROM experiments")
    rows = cur.fetchall()

    total_trials = len(rows)
    total_spend = sum(r["cost_usd"] or 0.0 for r in rows)

    by_model_kind: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        kind = _query_kind_from_detail(r["verifier_detail"])
        model = _short_model(r["model_name"])
        agg = by_model_kind.setdefault((model, kind), {"n": 0, "pass": 0})
        agg["n"] += 1
        if r["verdict"] == "PASS":
            agg["pass"] += 1

    model_kind_rows = "".join(
        f'<tr><td class="mono">{html.escape(model)}</td><td>{html.escape(kind)}</td>'
        f'<td>{agg["pass"]}/{agg["n"]}</td>'
        f'<td>{(agg["pass"] / agg["n"] * 100.0 if agg["n"] else 0.0):.0f}%</td></tr>'
        for (model, kind), agg in sorted(by_model_kind.items())
        if kind != "-"
    )
    model_kind_table = (
        '<table><thead><tr><th>model</th><th>query kind</th><th class="num">pass/total</th>'
        '<th class="num">rate</th></tr></thead><tbody>'
        + (model_kind_rows or '<tr><td colspan="4" class="meta">No trials recorded yet.</td></tr>')
        + "</tbody></table>"
    )
    n_models = len({model for (model, kind) in by_model_kind if kind != "-"})
    n_scored = sum(agg["n"] for (_, kind), agg in by_model_kind.items() if kind != "-")
    n_passed = sum(agg["pass"] for (_, kind), agg in by_model_kind.items() if kind != "-")
    overall_rate = (n_passed / n_scored * 100.0) if n_scored else 0.0

    findings_summary = "".join(
        f"<li>{html.escape(f.name)} <span class=\"meta\">({html.escape(f.status)})</span></li>"
        for f in _confirmed_findings_preview()
    )

    _, by_run_for_headlines = _runs_table_rows(store)

    def _headline_sort_key(rid: str) -> str:
        ats = by_run_for_headlines[rid]["created_ats"]
        return max(ats) if ats else ""

    recent_run_ids = sorted(by_run_for_headlines.keys(), key=_headline_sort_key, reverse=True)[:5]
    recent_verdicts_items = []
    for rid in recent_run_ids:
        headline = _verdict_headline_safe(store, rid)
        if not headline:
            continue
        recent_verdicts_items.append(
            f'<li><a href="/run/{html.escape(rid)}"><span class="mono">{html.escape(rid)}</span></a>'
            f" &mdash; {html.escape(headline)}</li>"
        )
    recent_verdicts_html = (
        "".join(recent_verdicts_items)
        if recent_verdicts_items
        else '<li class="meta">no runs yet</li>'
    )

    # Task-first overview (docs/design-notes.md principle 22): a first-time
    # visitor's 3 jobs, in order, above the fold -- (a) what is this,
    # (b) what did it find, (c) one primary CTA to run a test. Everything
    # else (raw stat counts, per-model matrix, quick links) is demoted
    # below as "see evidence" / "where to go" material.
    body = f"""
    <div class="page-head">
      <div>
        <h1>Model Cartographer</h1>
        <p class="subtitle">A synthetic story world with a known ground truth
        is used to probe a model's grasp of object state, belief, and event
        order -- so capability boundaries and failure modes can be mapped and
        audited, not just guessed at.</p>
      </div>
      <a class="cta-primary" href="/launch">Map a model &rarr;</a>
    </div>

    <div class="stat-grid">
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{total_trials:,}</div></div><div class="stat-label">Total trials</div></div>
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_models}</div></div><div class="stat-label">Models mapped</div></div>
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">{overall_rate:.0f}%</div><div class="stat-delta">{n_passed:,}/{n_scored:,} scored</div></div><div class="stat-label">Overall pass rate</div></div>
      <div class="stat-box"><div class="stat-num-row"><div class="stat-num">${total_spend:.4f}</div></div><div class="stat-label">Total spend</div></div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-head">
          <span class="card-title">What we've found</span>
          <a class="card-meta" href="/findings">capability map &rarr;</a>
        </div>
        <div class="card-body">
          <ul>{findings_summary or '<li class="meta">No confirmed findings yet -- launch a campaign to start mapping.</li>'}</ul>
        </div>
      </div>
      <div class="card">
        <div class="card-head">
          <span class="card-title">What recent runs revealed</span>
          <a class="card-meta" href="/runs">all runs &rarr;</a>
        </div>
        <div class="card-body">
          <ul>{recent_verdicts_html}</ul>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">Models tested &middot; pass rate by query kind</span>
        <span class="card-meta">{n_models} model{"" if n_models == 1 else "s"}</span>
      </div>
      {model_kind_table}
    </div>

    <h2>Where to go</h2>
    <div class="panel-grid">
      {"".join(
          f'<div class="panel"><p style="margin:0 0 0.3rem;"><a href="{href}"><strong>{html.escape(label)}</strong></a></p>'
          f'<p class="meta" style="margin:0;">{html.escape(desc)}</p></div>'
          for href, label, desc in _QUICK_LINKS
      )}
    </div>
    """
    return _shell("Model Cartographer", "overview", body, trial_count=_trial_count(store))


def _query_kind_from_detail(detail: str | None) -> str:
    parsed = _parse_verifier_detail(detail)
    return parsed.get("kind", "-")


def _confirmed_findings_preview():
    from .findings import FINDINGS

    return [f for f in FINDINGS if f.status == "confirmed"][:4]


def _short_model(model_name: str) -> str:
    return model_name.split("/")[-1] if model_name else "-"


def _budget_for_run(run_id: str) -> int | None:
    for prefix, budget in _KNOWN_RUN_BUDGETS.items():
        if run_id.startswith(prefix):
            return budget
    return None


def _parse_created_at(created_at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None


def _config_summary(realized_dims: dict) -> str:
    if not realized_dims:
        return "-"
    parts = [f"{k}={v}" for k, v in sorted(realized_dims.items())]
    return ", ".join(parts)


# A run whose newest record is older than this is considered finished:
# /live renders a "RUN COMPLETE" state (full green bar, no ETA) instead of
# a frozen partial progress bar with a bogus ETA.
_COMPLETE_AFTER_S = 180.0

# ETA is computed over a RECENT window only -- not the whole run's average
# rate, which stalls or fast bursts easily poison. Window = last N records
# or last M minutes of records, whichever gives the LARGER sample.
_ETA_WINDOW_RECORDS = 25
_ETA_WINDOW_MINUTES = 3.0
_ETA_MIN_SAMPLE = 10


def _now_utc() -> datetime:
    """Split out so tests can monkeypatch 'now'."""
    return datetime.now(timezone.utc)


def _age_seconds(newest: datetime) -> float:
    if newest.tzinfo is None:
        # created_at strings without an offset are treated as UTC (that's
        # how ExperimentRecord stamps them).
        newest = newest.replace(tzinfo=timezone.utc)
    return (_now_utc() - newest).total_seconds()


def _recent_rate_per_min(records: list[ExperimentRecord]) -> tuple[float | None, int]:
    """Rate (records/min) computed over a RECENT window, plus the sample
    size used. Returns (None, n) if there's no usable time span.

    Window selection: take the records from the last `_ETA_WINDOW_MINUTES`
    minutes (relative to the newest record's own timestamp, not wall
    clock, so this works fine in tests). A long stall followed by a fast
    burst should reflect the fast burst's rate, not the whole run's
    average -- so the time-based window is preferred whenever it already
    has enough samples to be meaningful. Only when that time-based window
    is too thin (a slow-but-steady run with few records per minute) do we
    widen to the last `_ETA_WINDOW_RECORDS` records instead, so the
    estimate still has a decent sample size rather than reading "estimating..."
    unnecessarily.
    """
    dated = [
        (r, t) for r, t in ((r, _parse_created_at(r.created_at)) for r in records) if t is not None
    ]
    if len(dated) < 2:
        return None, len(dated)
    dated.sort(key=lambda pair: pair[1])

    newest_t = dated[-1][1]
    cutoff = newest_t.timestamp() - _ETA_WINDOW_MINUTES * 60.0
    by_time = [pair for pair in dated if pair[1].timestamp() >= cutoff]

    if len(by_time) >= _ETA_MIN_SAMPLE:
        window = by_time
    else:
        by_count = dated[-_ETA_WINDOW_RECORDS:]
        window = by_count if len(by_count) >= len(by_time) else by_time

    if len(window) < 2:
        return None, len(window)

    span_s = (window[-1][1] - window[0][1]).total_seconds()
    if span_s <= 0:
        return None, len(window)
    rate = (len(window) - 1) / (span_s / 60.0) if span_s > 0 else None
    if rate is None or rate <= 0:
        return None, len(window)
    return rate, len(window)


def _format_eta(remaining: int, rate_per_min: float | None, sample_n: int) -> str:
    if sample_n < _ETA_MIN_SAMPLE:
        return "estimating..."
    if rate_per_min is None or rate_per_min <= 0:
        return "estimating..."
    eta_min = remaining / rate_per_min
    if eta_min > 120:
        return "at most &gt; 2h at current rate"
    return f"at most ~{eta_min:.1f} min at current rate"


def _stop_button_html(is_active: bool, stopping: bool) -> str:
    """Renders a "Stop campaign" form-POST button when a campaign is
    actively registered, or a "stopping..." notice once its cancel_event
    has been set but the thread hasn't exited yet. Renders nothing when no
    campaign is active -- there's nothing to stop.
    """
    if not is_active:
        return ""
    if stopping:
        return '<p class="meta"><strong>stopping&hellip;</strong> waiting for the current trial to finish.</p>'
    return (
        '<form method="post" action="/stop" style="display:inline">'
        '<button type="submit">Stop campaign</button></form>'
    )


def _collect_run_records(
    store: ResultsStore, run_id: str
) -> tuple[list[ExperimentRecord], bool, list[ExperimentRecord]]:
    """Aggregate every record actually associated with `run_id`.

    A plain campaign's trials live directly under `run_id`. But two cases
    write elsewhere, and any caller that only does `store.by_run(run_id)`
    (as `live_html` used to) silently sees zero records for the entire
    life of the run even though it's actively spending money:

    - A `full_map` parent run_id has NO records of its own -- every trial
      lives under `f"{run_id}/{stage}"` child run_ids (mirrors the same
      fallback `run_html` already uses, kept in sync with it here).
    - A "Verify failures now" pass against an already-recorded run writes
      its resample trials under `f"{run_id}/verify"` (or, for a full_map
      parent, `f"{run_id}/{stage}/verify"` per stage) -- never under
      `run_id` itself, so a campaign's own (already-complete, unchanging)
      records never reflect an in-flight verification pass layered on
      top of it.

    Returns (records, is_full_map_parent, verify_records).
    """
    from ..exec.evaluator import VERIFY_SUFFIX

    records = store.by_run(run_id)
    is_full_map_parent = False
    if not records:
        for stage in _FULL_MAP_RUN_STAGES:
            child = store.by_run(f"{run_id}/{stage}")
            if child:
                is_full_map_parent = True
                records.extend(child)
        records.sort(key=lambda r: (r.created_at or "", r.record_id))

    if is_full_map_parent:
        verify_records = [
            r
            for stage in _FULL_MAP_RUN_STAGES
            for r in store.by_run(f"{run_id}/{stage}{VERIFY_SUFFIX}")
        ]
    else:
        verify_records = store.by_run(f"{run_id}{VERIFY_SUFFIX}")
    return records, is_full_map_parent, verify_records


def live_html(store: ResultsStore) -> str:
    """Live monitor. Prefers the ACTIVE registered campaign run_id (from
    `launch_mod.current_run()`) even if it has zero records yet -- a
    just-launched campaign has no records for its first seconds-to-minutes
    (world gen + first API call latency), and without this the page would
    keep showing the previous run. Falls back to the run with the newest
    record only when nothing is actively registered.
    """
    active = launch_mod.current_run()
    run_id: str | None = active["run_id"] if active else None
    stopping = launch_mod.is_stopping() if active else False

    if run_id is None:
        cur = store._conn.execute(
            "SELECT run_id FROM experiments ORDER BY created_at DESC, record_id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            body = (
                "<h1>Live campaign monitor</h1>"
                '<div class="empty-state"><p>No records found.</p>'
                '<p class="meta"><a href="/launch">Launch your first campaign &rarr;</a></p></div>'
            )
            return _shell("Live campaign monitor", "live", body, refresh=True, trial_count=_trial_count(store))
        run_id = row["run_id"]

    safe_run_id = html.escape(run_id)
    records, _is_full_map_parent, verify_records = _collect_run_records(store, run_id)

    # A "Verify failures now" pass on an already-complete run is real,
    # billed activity with nothing to show in `records` (those are all
    # old and static) -- surface it explicitly so watching /live doesn't
    # look inert while it's happening.
    verifying_html = ""
    if verify_records:
        v_total = len(verify_records)
        v_pass = sum(1 for r in verify_records if r.verdict.name == "PASS")
        v_latest = max(
            (r.created_at for r in verify_records if r.created_at), default=None
        )
        verifying_html = f"""
        <div class="card">
          <div class="card-head">
            <span class="card-title">Verifying failures</span>
            <span class="card-meta">{v_total} resample{"s" if v_total != 1 else ""}</span>
          </div>
          <div class="card-body">
            <p class="meta">A verification pass is re-testing prior failures against this
            run &mdash; {v_total} resample{"s" if v_total != 1 else ""} so far
            ({v_pass} passed on retest, i.e. noise), latest at
            {html.escape(v_latest or "-")}.</p>
          </div>
        </div>
        """

    if not records:
        body = f"""
        <div class="page-head"><div class="page-head-main">
          <h1>Live: {safe_run_id}</h1>
          <p class="meta">starting&hellip; 0 records</p>
        </div><div class="page-head-actions">
          {_stop_button_html(active is not None, stopping)}
        </div></div>
        <div class="progress-track"><div class="progress-fill" style="width:0%"></div></div>
        {verifying_html}
        """
        return _shell(f"Live: {run_id}", "live", body, refresh=True, trial_count=_trial_count(store))

    total = len(records)
    passn = sum(1 for r in records if r.verdict.name == "PASS")
    failn = sum(1 for r in records if r.verdict.name == "FAIL")
    errn = sum(1 for r in records if r.verdict.name == "ERROR")
    pass_rate = (passn / total * 100.0) if total else 0.0
    total_cost = sum(r.cost_usd for r in records)

    timestamps = [t for t in (_parse_created_at(r.created_at) for r in records) if t is not None]
    if len(timestamps) >= 2:
        elapsed_s = max((max(timestamps) - min(timestamps)).total_seconds(), 0.0)
    else:
        elapsed_s = 0.0

    complete = bool(timestamps) and _age_seconds(max(timestamps)) > _COMPLETE_AFTER_S
    # A run still actively registered is never "complete" even if its
    # newest record happens to be stale (e.g. a slow phase) -- it's still
    # in-flight from the launcher's point of view.
    if active is not None and run_id == active["run_id"]:
        complete = False

    budget = _budget_for_run(run_id)
    overall_rate_per_min = (total / (elapsed_s / 60.0)) if elapsed_s > 0 else 0.0

    if complete:
        pct_str = ""
        if budget is not None and budget > 0:
            pct_str = f" ({min(total / budget * 100.0, 100.0):.1f}% of {budget} budget max)"
        bar_html = (
            '<div class="progress-track"><div class="progress-fill complete" '
            'style="width:100%"></div></div>'
            f'<p class="meta"><strong>RUN COMPLETE</strong> &mdash; finished at '
            f"{total} calls{pct_str}</p>"
        )
    elif budget is not None and budget > 0:
        pct = min(total / budget * 100.0, 100.0)
        remaining = max(budget - total, 0)
        rate_per_min, sample_n = _recent_rate_per_min(records)
        eta_str = _format_eta(remaining, rate_per_min, sample_n)
        bar_html = (
            '<div class="progress-row">'
            f'<div class="progress-track"><div class="progress-fill" style="width:{pct:.1f}%"></div></div>'
            f'<span class="progress-text">{total} / {budget} max ({pct:.1f}%)</span>'
            "</div>"
            f'<p class="meta">ETA: {eta_str}</p>'
        )
    else:
        bar_html = f'<p class="meta">{total} records (budget unknown for this run_id)</p>'

    # Hero KPI row, moved to the TOP of the page (it used to sit under an
    # "<h2>Tallies</h2>" below the progress bar). Throughput rides as the
    # cost box's inline delta rather than a seventh box, so the row stays
    # a scannable KPI strip instead of a number wall.
    stat_grid = (
        '<div class="stat-grid">'
        f'<div class="stat-box"><div class="stat-num">{passn}</div><div class="stat-label">Pass</div></div>'
        f'<div class="stat-box"><div class="stat-num">{failn}</div><div class="stat-label">Fail</div></div>'
        f'<div class="stat-box"><div class="stat-num">{errn}</div><div class="stat-label">Error</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{pass_rate:.1f}%</div>'
        f'<div class="stat-delta">{passn}/{total}</div></div><div class="stat-label">Pass rate</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">${total_cost:.4f}</div>'
        f'<div class="stat-delta">{overall_rate_per_min:.1f}/min</div></div>'
        '<div class="stat-label">Total cost</div></div>'
        "</div>"
    )

    recent = list(reversed(records))[:15]
    rows_html = []
    for r in recent:
        r_answer_short = _short_answer(r.response_text, max_chars=50)
        if r.verdict.name == "PASS":
            r_evidence = "matched ground truth" if not r_answer_short else f'answered <b>"{html.escape(r_answer_short)}"</b>'
        elif r.verdict.name == "FAIL":
            r_evidence = f'answered <b>"{html.escape(r_answer_short)}"</b>' if r_answer_short else None
        else:
            r_evidence = None
        rows_html.append(
            "<tr>"
            f'<td class="mono">{html.escape(r.created_at or "-")}</td>'
            f'<td class="claim-cell">{_claim_strip(r.verdict.name, r_evidence)}</td>'
            f'<td class="mono">{html.escape(_config_summary(r.realized_dims))}</td>'
            "</tr>"
        )
    recent_table = (
        "<table><thead><tr><th>time</th><th>verdict</th><th>config</th></tr></thead>"
        "<tbody>" + "".join(rows_html) + "</tbody></table>"
    )

    is_active_run = active is not None and run_id == active["run_id"]
    stage_html = _stage_track_html(run_id, records)
    # The progress block keeps a fixed slot regardless of which branch
    # rendered it (bar vs. "RUN COMPLETE" vs. budget-unknown line), so the
    # 5s meta-refresh can't shift everything below it.
    body = f"""
    <div class="page-head"><div class="page-head-main">
      <h1>Live: {safe_run_id}</h1>
      <p class="subtitle">Auto-refreshing monitor for an in-progress (or just-finished) campaign run.</p>
    </div><div class="page-head-actions">
      <a href="/run/{safe_run_id}">full run detail &rarr;</a>
      {_stop_button_html(is_active_run, stopping)}
    </div></div>
    {stat_grid}
    <div class="card">
      <div class="card-head">
        <span class="card-title">Progress</span>
        <span class="card-meta">{total:,} records</span>
      </div>
      <div class="card-body">
        {stage_html}
        {bar_html}
      </div>
    </div>
    {verifying_html}
    <div class="card">
      <div class="card-head">
        <span class="card-title">Recent records</span>
        <span class="card-meta">last {len(recent)}, newest first</span>
      </div>
      {recent_table}
    </div>
    """
    return _shell(f"Live: {run_id}", "live", body, refresh=not complete, trial_count=_trial_count(store))


def _stage_track_html(run_id: str, records: list[ExperimentRecord]) -> str:
    """For a full_map campaign (run_id or its records' run_id carries a
    "/<stage>" suffix), render a fixed step tracker: state (done) ->
    order -> belief -> ... so progress reads at a glance without parsing
    numbers (design-notes principle 24). Returns "" for non-full_map runs
    so it never appears for a plain state/belief/order campaign.
    """
    seen_stages = {
        _stage_from_run_id_local(r.run_id) for r in records if "/" in r.run_id
    }
    seen_stages.discard(None)
    if not seen_stages:
        return ""
    steps = []
    # the CURRENT stage is the last one with any records; everything
    # before it is "done", everything after is upcoming.
    reached = [s for s in _FULL_MAP_RUN_STAGES if s in seen_stages]
    current = reached[-1] if reached else None
    for i, stage in enumerate(_FULL_MAP_RUN_STAGES):
        cls = "done" if stage in seen_stages and stage != current else ""
        cls = "active" if stage == current else cls
        steps.append(f'<span class="stage-step {cls}"><span class="dot"></span>{html.escape(stage)}</span>')
        if i < len(_FULL_MAP_RUN_STAGES) - 1:
            steps.append('<span class="stage-arrow">&rarr;</span>')
    return f'<div class="stage-track">{"".join(steps)}</div>'


_FULL_MAP_RUN_STAGES = ["state", "order", "belief", "adversarial_belief", "breaking_point"]


_VERIFY_STATUS_LABEL = {
    "stable": "verified-stable",
    "noise": "noise",
    "unverified": "unverified",
}


def run_html(store: ResultsStore, run_id: str) -> str:
    records = store.by_run(run_id)
    safe_run_id = html.escape(run_id)

    # A full_map PARENT run_id has no records of its own -- every trial
    # lives under f"{run_id}/{stage}" child run_ids. Pull those in (labeled
    # by stage) so the table still shows every trial for a full_map launch.
    is_full_map_parent = False
    if not records:
        for stage in _FULL_MAP_RUN_STAGES:
            child = store.by_run(f"{run_id}/{stage}")
            if child:
                is_full_map_parent = True
                records.extend(child)

    breadcrumb = f'<a href="/runs">Runs</a><span class="sep">/</span>{safe_run_id}'

    if not records:
        body = (
            '<div class="page-head"><div class="page-head-main">'
            f"<h1>Run {safe_run_id}</h1></div>"
            '<div class="page-head-actions"><a href="/runs">all runs &rarr;</a></div></div>'
            f"{verdict_panel_html(store, run_id)}"
            '<div class="empty-state"><p>No records found.</p></div>'
        )
        return _shell(f"Run {run_id}", "runs", body, breadcrumb=breadcrumb, trial_count=_trial_count(store))

    # Verification status per FAIL record_id, keyed by the run_id its
    # verify trials actually live under (f"{run_id}/verify" for a plain
    # run, or f"{run_id}/{stage}/verify" per stage for a full_map parent --
    # breaking_point is never auto-verified, see run_full_map_campaign).
    from ..exec.evaluator import classify_all_verified_failures

    verify_status_by_record: dict[str, str] = {}
    verify_source_run_ids = (
        [f"{run_id}/{s}" for s in _FULL_MAP_RUN_STAGES] if is_full_map_parent else [run_id]
    )
    for src in verify_source_run_ids:
        for record_id, fv in classify_all_verified_failures(store, src).items():
            verify_status_by_record[record_id] = fv.status

    rows_html = []
    for r in records:
        detail = _parse_verifier_detail(r.verifier_detail)
        kind = detail.get("kind", "-")
        # difficulty is not always encoded in verifier_detail; fall back to "-".
        difficulty = detail.get("difficulty", "-")
        traps = detail.get("traps", "")
        foil_score_raw = detail.get("foil_score", "")
        stage_html = ""
        if is_full_map_parent:
            stage = _stage_from_run_id_local(r.run_id)
            if stage:
                stage_html = f'<td class="mono">{html.escape(stage)}</td>'

        # Evidence clause for the claim strip: what was answered, and (for
        # an adversarial record) which traps this world was SEEDED for --
        # `traps` is `FoilProfile.trap_kinds`, a world-level property
        # (which plausible-but-wrong answers this story plants) computed
        # before the model ever answers, not "which trap the model's
        # answer happened to match" -- the data this table used to leave
        # in a separate raw "traps=..." span, now folded into the verdict
        # itself since that's the whole point of the claim strip
        # (design-notes.md "The signature"). Every planted trap is shown,
        # not just one, since a world can plant several at once and which
        # one(s) the model actually fell for is exactly the open question
        # a reader clicking into the full transcript wants to answer.
        answer_short = _short_answer(r.response_text, max_chars=60)
        stability = None
        evidence = None
        if r.verdict.name == "PASS":
            evidence = "matched ground truth" if not answer_short else f'answered <b>"{html.escape(answer_short)}"</b>'
        elif r.verdict.name == "FAIL":
            stability = verify_status_by_record.get(r.record_id, "unverified")
            if traps:
                trap_names_html = ", ".join(
                    f'<span class="trap-name">{html.escape(t.strip())}</span>' for t in traps.split(",") if t.strip()
                )
                evidence = (
                    (f'answered <b>"{html.escape(answer_short)}"</b> ' if answer_short else "")
                    + f"(world seeded for: {trap_names_html})"
                )
            elif answer_short:
                evidence = f'answered <b>"{html.escape(answer_short)}"</b>'
        else:  # ERROR
            evidence = html.escape(detail.get("error", "") or r.finish_reason or "infra failure")

        salience = None
        if foil_score_raw:
            try:
                salience = float(foil_score_raw)
            except ValueError:
                salience = None

        # Left-edge severity bar (bug-tracker reference): criticality
        # readable before any text is. PASS rows get no edge on purpose --
        # an edge on every row is an edge that means nothing. A FAIL that
        # has not been resample-checked is its own state, amber not red,
        # matching the "?" mark the claim strip already gives it.
        if r.verdict.name == "FAIL":
            row_cls = (
                ' class="row-sev row-sev-unverified"'
                if stability == "unverified"
                else ' class="row-sev row-sev-fail"'
            )
        elif r.verdict.name == "ERROR":
            row_cls = ' class="row-sev row-sev-error"'
        else:
            row_cls = ""

        rows_html.append(
            f"<tr{row_cls}>"
            f'<td class="mono">{html.escape(r.model_name)}</td>'
            f"{stage_html}"
            f'<td class="claim-cell">{_claim_strip(r.verdict.name, evidence, stability, salience)}</td>'
            f"<td>{_chip(kind, 'chip-muted') if kind and kind != '-' else '-'}</td>"
            f"<td>{_chip(difficulty, 'chip-muted') if difficulty and difficulty != '-' else '-'}</td>"
            f'<td class="mono">{r.cost_usd:.6f}</td>'
            f'<td class="mono">{r.tokens_in}/{r.tokens_out}</td>'
            f'<td><a href="/record/{html.escape(r.record_id)}">full transcript &rarr;</a></td>'
            "</tr>"
        )

    stage_header = "<th>stage</th>" if is_full_map_parent else ""
    table = (
        "<table><thead><tr><th>model</th>"
        f"{stage_header}"
        "<th>verdict</th><th>kind</th>"
        "<th>difficulty</th><th class=\"num\">cost ($)</th><th class=\"num\">tokens in/out</th><th></th>"
        "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>"
    )

    n_unverified_fails = sum(
        1
        for r in records
        if r.verdict.name == "FAIL"
        and verify_status_by_record.get(r.record_id, "unverified") == "unverified"
        and (not is_full_map_parent or _stage_from_run_id_local(r.run_id) != "breaking_point")
    )
    verify_now_html = ""
    verify_action_html = ""
    if n_unverified_fails:
        verify_action_html = (
            f'<a class="cta-primary" href="#verify-failures">'
            f'Verify {n_unverified_fails} failure(s) &rarr;</a>'
        )
        verify_now_html = f"""
        <div class="card" id="verify-failures">
          <div class="card-head">
            <span class="card-title">Verify failures now</span>
            <span class="card-meta">{n_unverified_fails} unverified</span>
          </div>
          <div class="card-body">
          <details>
          <summary>Verify {n_unverified_fails} failure(s) now
            <span class="meta">(re-tests each ~3x against the model; est. a few percent of the original run's cost)</span>
          </summary>
          <form method="post" action="/verify">
            <input type="hidden" name="run_id" value="{safe_run_id}">
            <p><label>Provider:
              <select name="provider">{launch_mod._provider_options()}</select></label></p>
            <p><label>API key:
              <input type="password" name="api_key" value="" autocomplete="off"></label>
              <span class="meta">leave empty to use env/ key file</span></p>
            <p><label>Model:
              <input name="model" value="{html.escape(records[0].model_name)}"></label></p>
            <p><button type="submit">Verify failures now</button></p>
          </form>
          </details>
          </div>
        </div>
        """

    # KPI row: the four numbers a reader wants before reading anything.
    # The failure box carries the verified/noise split as its inline delta
    # -- a raw FAIL count without it overstates the finding, since an
    # unreproducible failure is noise, not evidence (see commit 271703e).
    n_trials = len(records)
    n_pass = sum(1 for r in records if r.verdict.name == "PASS")
    n_fail = sum(1 for r in records if r.verdict.name == "FAIL")
    n_error = n_trials - n_pass - n_fail
    n_stable = sum(
        1 for r in records
        if r.verdict.name == "FAIL" and verify_status_by_record.get(r.record_id) == "stable"
    )
    n_noise = sum(
        1 for r in records
        if r.verdict.name == "FAIL" and verify_status_by_record.get(r.record_id) == "noise"
    )
    # Pass rate must be computed the SAME way the verdict panel directly
    # below it computes its headline -- over SCORED trials only (PASS+FAIL,
    # excluding infra ERRORs and excluding failures already classified as
    # noise; see report/verdict.py's `_verified_only` + `_pass_rate`).
    # Dividing by total trials instead puts "16%" in the KPI row directly
    # above a panel saying "92% pass rate" on any error-heavy run (e.g.
    # camp-adversarial_belief-minimax-m3-001: 115 of 140 trials ERRORed).
    n_scored = n_pass + (n_fail - n_noise)
    run_pass_rate = (n_pass / n_scored * 100.0) if n_scored else 0.0
    run_cost = sum(r.cost_usd for r in records)
    trials_delta = (
        f'<div class="stat-delta">{n_error:,} error{"" if n_error == 1 else "s"}</div>'
        if n_error
        else ""
    )
    fail_delta = (
        f'<div class="stat-delta">{n_stable} stable / {n_noise} noise</div>'
        if (n_stable or n_noise)
        else ""
    )
    # The pass-rate KPI is one number; the sparkline beside it says whether
    # the run got there smoothly or fell off a cliff partway through. For
    # an adaptive campaign (difficulty climbs along the sequence) that
    # shape IS the finding, not decoration.
    run_spark = _sparkline(
        _rate_sequence(records), width=96, height=22, label="pass rate over the trial sequence"
    )
    spark_html = (
        f'<div class="spark-row" style="margin-top:var(--sp-2);">{run_spark}'
        '<span class="spark-caption">over trials</span></div>'
        if run_spark
        else ""
    )
    fail_share = (n_fail / n_trials * 100.0) if n_trials else 0.0
    stat_grid = (
        '<div class="stat-grid">'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_trials:,}</div>{trials_delta}</div>'
        '<div class="stat-label">Trials</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{run_pass_rate:.0f}%</div>'
        f'<div class="stat-delta">{n_pass:,}/{n_scored:,} scored</div></div>'
        f"{spark_html}"
        '<div class="stat-label">Pass rate</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_fail:,}</div>{fail_delta}</div>'
        # Magnitude only: this bar measures a SHARE of failures, where
        # neither direction is a verdict the bar itself should render.
        # The row edges and claim strips below already carry that color.
        f'<div style="margin-top:var(--sp-2);">{_rate_bar(fail_share, threshold=None)}</div>'
        '<div class="stat-label">Failures</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">${run_cost:.4f}</div></div>'
        '<div class="stat-label">Run cost</div></div>'
        "</div>"
    )

    body = (
        '<div class="page-head"><div class="page-head-main">'
        f'<div class="h1-row"><h1>Run {safe_run_id}</h1>{_title_pill_html(records)}</div>'
        '<p class="subtitle">What this run revealed, then every individual trial as evidence.</p>'
        '</div><div class="page-head-actions">'
        f'<a href="/live?run={safe_run_id}">watch live &rarr;</a>'
        f'<a href="/atlas">compare across models &rarr;</a>'
        f"{verify_action_html}"
        "</div></div>"
        f"{stat_grid}"
        f"{verdict_panel_html(store, run_id)}"
        f"{verify_now_html}"
        '<div class="card"><div class="card-head">'
        '<span class="card-title">Trials</span>'
        f'<span class="card-meta">{len(records)} trial{"" if len(records) == 1 else "s"}</span>'
        '</div><div class="card-body">'
        "<details><summary>Per-trial table "
        f'<span class="meta">({len(records)} trials -- prompt, reasoning, verdict, answer key)</span></summary>'
        f"{table}</details>"
        "</div></div>"
    )
    return _shell(f"Run {run_id}", "runs", body, breadcrumb=breadcrumb, trial_count=_trial_count(store))


def _stage_from_run_id_local(run_id: str) -> str | None:
    if "/" in run_id:
        return run_id.rsplit("/", 1)[-1]
    return None


def record_html(store: ResultsStore, record_id: str) -> str:
    cur = store._conn.execute("SELECT * FROM experiments WHERE record_id = ?", (record_id,))
    row = cur.fetchone()
    safe_record_id = html.escape(record_id)

    if row is None:
        body = f'<h1>Record {safe_record_id}</h1><div class="empty-state"><p>Not found.</p></div>'
        return _shell(f"Record {record_id}", "runs", body, trial_count=_trial_count(store))

    from ..store.store import _row_to_record

    record: ExperimentRecord = _row_to_record(row)

    prompt_block = html.escape(record.prompt_text or "")
    response_block = html.escape(record.response_text or "")
    reasoning_block = html.escape(record.reasoning_text or "")

    reasoning_html = (
        "<details><summary>Model reasoning</summary>"
        f'<pre class="block mono">{reasoning_block}</pre></details>'
        if record.reasoning_text
        else "<p class=\"meta\">(no reasoning captured)</p>"
    )

    realized_rows = "".join(
        f'<tr><td class="mono">{html.escape(str(k))}</td><td class="mono">{html.escape(str(v))}</td></tr>'
        for k, v in sorted(record.realized_dims.items())
    )
    latent_rows = "".join(
        f'<tr><td class="mono">{html.escape(str(k))}</td><td class="mono">{html.escape(str(v))}</td></tr>'
        for k, v in sorted(record.latent_params.items())
    )

    lineage_links = ""
    try:
        chain = store.lineage(record.record_id)
        lineage_links = " &rarr; ".join(
            f'<a href="/record/{html.escape(rec.record_id)}">{html.escape(rec.record_id[:8])}</a>'
            for rec in chain
        )
    except KeyError:
        lineage_links = "(lineage unavailable)"

    detail = _parse_verifier_detail(record.verifier_detail)
    traps = detail.get("traps", "")
    trap_line = (
        f'<p class="meta">trap kind(s) this world was seeded for: {_chip_set(traps, "chip-trap")}</p>'
        if traps
        else ""
    )
    # Categorical provenance as scannable chips rather than a
    # semicolon-joined verifier_detail string the reader has to parse.
    # The raw string is kept below it -- chips summarize, they don't hide.
    meta_chips = "".join(
        _chip(text, cls)
        for text, cls in (
            (detail.get("kind", ""), "chip-muted"),
            (record.render_mode.name.lower(), "chip-muted"),
            (detail.get("sim", ""), "chip-muted"),
            (record.finish_reason or "", "chip-muted"),
        )
        if text
    )
    meta_chip_row = f'<p class="meta"><span class="chip-set">{meta_chips}</span></p>' if meta_chips else ""

    # prev/next trial within the same run -- lateral navigation so a
    # dead-end detail page never strands the reader (design-notes principle 4).
    siblings = store.by_run(record.run_id)
    sib_ids = [s.record_id for s in siblings]
    prev_next_html = ""
    if record.record_id in sib_ids and len(sib_ids) > 1:
        idx = sib_ids.index(record.record_id)
        prev_id = sib_ids[idx - 1] if idx > 0 else None
        next_id = sib_ids[idx + 1] if idx < len(sib_ids) - 1 else None
        parts = []
        if prev_id:
            parts.append(f'<a href="/record/{html.escape(prev_id)}">&larr; prev trial</a>')
        parts.append(f'<span class="meta">trial {idx + 1} of {len(sib_ids)}</span>')
        if next_id:
            parts.append(f'<a href="/record/{html.escape(next_id)}">next trial &rarr;</a>')
        prev_next_html = f'<p class="meta" style="display:flex; gap:1rem; align-items:center;">{" &middot; ".join(parts)}</p>'

    breadcrumb = (
        f'<a href="/runs">Runs</a><span class="sep">/</span>'
        f'<a href="/run/{html.escape(record.run_id)}">{html.escape(record.run_id)}</a>'
        f'<span class="sep">/</span>{safe_record_id[:8]}'
    )

    _r_answer_short = _short_answer(record.response_text, max_chars=80)
    if record.verdict.name == "PASS":
        _r_evidence = "matched ground truth" if not _r_answer_short else f'answered <b>"{html.escape(_r_answer_short)}"</b>'
    elif record.verdict.name == "FAIL":
        _r_evidence = f'answered <b>"{html.escape(_r_answer_short)}"</b>' if _r_answer_short else None
    else:
        _r_evidence = html.escape(detail.get("error", "") or record.finish_reason or "infra failure")

    _r_pill_cls = {"PASS": "tp-pass", "FAIL": "tp-fail", "ERROR": "tp-error"}.get(record.verdict.name, "tp-error")
    # Card-head metas mirror test-factory's REQUEST/RESPONSE cards, which
    # carry size/status in the header bar rather than as body prose: the
    # header answers "how big / how did it end" before you read a line.
    _prompt_meta = f"{len(record.prompt_text or ''):,} chars &middot; {record.tokens_in:,} tokens in"
    _response_meta = (
        f"{record.tokens_out:,} tokens out &middot; finish: "
        f"{html.escape(record.finish_reason or '-')}"
    )
    _reasoning_meta = (
        f"{len(record.reasoning_text):,} chars" if record.reasoning_text else "not captured"
    )

    body = f"""
    <div class="page-head"><div class="page-head-main">
    <div class="h1-row"><h1>Record {safe_record_id}</h1><span class="title-pill {_r_pill_cls}">{html.escape(record.verdict.name)}</span></div>
    <p class="subtitle">Verdict and provenance first; raw prompt/response/reasoning below, collapsed.</p>
    </div><div class="page-head-actions">
      <a href="/run/{html.escape(record.run_id)}">back to run &rarr;</a>
    </div></div>

    <div class="panel">
      <p style="margin:0 0 0.5rem;">
        {_claim_strip(record.verdict.name, _r_evidence)}
        <span class="meta">&middot; model <span class="mono">{html.escape(record.model_name)}</span></span>
      </p>
      <p class="meta" style="margin:0;">
        finish_reason: <span class="mono">{html.escape(record.finish_reason or "-")}</span> &middot;
        tokens in/out: <span class="mono num">{record.tokens_in}/{record.tokens_out}</span> &middot;
        cost: <span class="mono num">${record.cost_usd:.6f}</span> &middot;
        latency: <span class="mono num">{record.latency_ms:.1f} ms</span>
      </p>
      {meta_chip_row}
      <p class="meta">verifier_detail: <span class="mono">{html.escape(record.verifier_detail or "-")}</span></p>
      {trap_line}
      <p class="meta">lineage: {lineage_links}</p>
    </div>

    {prev_next_html}

    <div class="card">
      <div class="card-head">
        <span class="card-title">Prompt</span>
        <span class="card-meta">{_prompt_meta}</span>
      </div>
      <div class="card-body"><pre class="block">{prompt_block}</pre></div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">Response</span>
        <span class="card-meta">{_response_meta}</span>
      </div>
      <div class="card-body"><pre class="block">{response_block}</pre></div>
    </div>

    <div class="card">
      <div class="card-head">
        <span class="card-title">Reasoning</span>
        <span class="card-meta">{_reasoning_meta}</span>
      </div>
      <div class="card-body">{reasoning_html}</div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-head">
          <span class="card-title">Realized dims</span>
          <span class="card-meta">{len(record.realized_dims)}</span>
        </div>
        <table><thead><tr><th>dim</th><th>value</th></tr></thead><tbody>{realized_rows}</tbody></table>
      </div>
      <div class="card">
        <div class="card-head">
          <span class="card-title">Latent params</span>
          <span class="card-meta">{len(record.latent_params)}</span>
        </div>
        <table><thead><tr><th>param</th><th>value</th></tr></thead><tbody>{latent_rows}</tbody></table>
      </div>
    </div>
    """
    return _shell(f"Record {record_id}", "runs", body, breadcrumb=breadcrumb, trial_count=_trial_count(store))


class _ViewerHandler(BaseHTTPRequestHandler):
    store: ResultsStore  # set on the class before serving
    launch_deps: launch_mod.LaunchDeps  # set on the class before serving

    def _send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 -- required BaseHTTPRequestHandler name
        path = urlsplit(self.path).path

        if path == "/":
            self._send_html(index_html(self.store))
            return

        if path == "/runs":
            self._send_html(runs_html(self.store))
            return

        if path == "/findings":
            # /findings?model=<name> -- every model's findings are rendered
            # into the page and the switcher toggles them client-side, but
            # the URL is real so a particular model's view is shareable and
            # works with JS off (see findings._switcher_html).
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            self._send_html(findings_html(self.store, model=q.get("model")))
            return

        if path == "/report":
            self._send_html(report_html(self.store))
            return

        if path == "/coverage":
            self._send_html(coverage_html())
            return

        if path == "/live":
            self._send_html(live_html(self.store))
            return

        if path == "/launch":
            body = launch_mod.handle_launch_get(self.launch_deps)
            self._send_html(_shell("Launch a campaign", "launch", body, trial_count=_trial_count(self.store)))
            return

        if path == "/atlas":
            # /atlas?kind=belief&dim=context_length -- every atlas view
            # state is a real URL, so the selectors work with JS off and
            # a particular map is shareable/bookmarkable.
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            self._send_html(atlas_html(self.store, kind=q.get("kind"), dim=q.get("dim")))
            return

        if path == "/trials":
            # Two callers share this route with different vocabularies: the
            # atlas heatmap (kind+dim+bucket index) and the /findings
            # capability map (model+axis+key). `axis` is the discriminator;
            # each renderer owns the bucketing its own surface painted, so
            # neither cell can disagree with the list it opens.
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            if q.get("axis"):
                self._send_html(capability_trials_html(self.store, q))
            else:
                self._send_html(trials_html(self.store, q))
            return

        if path.startswith("/run/"):
            run_id = path[len("/run/"):]
            if run_id:
                self._send_html(run_html(self.store, run_id))
                return

        if path.startswith("/record/"):
            record_id = path[len("/record/"):]
            if record_id:
                self._send_html(record_html(self.store, record_id))
                return

        self._send_html(_shell("Not found", "", "<h1>404</h1><p>Not found.</p>"), status=404)

    def do_POST(self) -> None:  # noqa: N802 -- required BaseHTTPRequestHandler name
        path = urlsplit(self.path).path

        if path == "/stop":
            launch_mod.request_stop()
            self.send_response(303)
            self.send_header("Location", "/live")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/verify":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            form = {
                k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()
            }
            run_id = form.get("run_id", "")
            started, payload = launch_mod.handle_verify_now(
                run_id,
                form.get("provider", ""),
                form.get("api_key", ""),
                form.get("model", ""),
                self.launch_deps,
            )
            if started:
                self.send_response(303)
                self.send_header("Location", f"/run/{run_id}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_html(_shell("Verify failures", "runs", payload))
            return

        if path == "/replicate":
            # "Replicate this finding" on /findings. Same contract as
            # /verify above: (started, run_id_or_html), redirect on success.
            # On success we land on /live rather than the new run's page --
            # a replication takes minutes and the live monitor is the page
            # that can actually show it progressing (and stop it).
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            form = {
                k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()
            }
            started, payload = launch_mod.handle_replicate_now(form, self.launch_deps)
            if started:
                self.send_response(303)
                self.send_header("Location", "/live")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_html(_shell("Replicate finding", "findings", payload))
            return

        if path != "/launch":
            self._send_html(_shell("Not found", "", "<h1>404</h1><p>Not found.</p>"), status=404)
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        form = {
            k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()
        }

        step = form.get("step", "")
        if step == "models":
            body = launch_mod.handle_models_step(form, self.launch_deps)
            self._send_html(_shell("Launch a campaign", "launch", body))
            return
        if step == "start":
            started, payload = launch_mod.handle_start(form, self.launch_deps)
            if started:
                self.send_response(303)
                self.send_header("Location", "/live")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send_html(_shell("Launch a campaign", "launch", payload))
            return

        self._send_html(
            _shell("Launch a campaign", "launch", launch_mod.error_html("unknown form step")),
            status=400,
        )

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def serve(db_path: str = _DEFAULT_DB_PATH, port: int = _DEFAULT_PORT) -> None:
    store = ResultsStore(db_path)
    handler_cls = type(
        "BoundViewerHandler",
        (_ViewerHandler,),
        {"store": store, "launch_deps": launch_mod.LaunchDeps(db_path=str(db_path))},
    )
    httpd = HTTPServer(("127.0.0.1", port), handler_cls)
    print(f"Serving results viewer for {db_path!r} at http://127.0.0.1:{port}/ (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_DB_PATH
    port = int(sys.argv[2]) if len(sys.argv) > 2 else _DEFAULT_PORT

    # Resolve relative paths against the repo root, not the caller's cwd,
    # so `modelcart` works the same from anywhere on disk.
    resolved = Path(db_path)
    if not resolved.is_absolute() and not resolved.exists():
        candidate = _REPO_ROOT / db_path
        if candidate.exists():
            resolved = candidate
    db_path = str(resolved)

    url = f"http://127.0.0.1:{port}/"
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    serve(db_path, port)


if __name__ == "__main__":
    main()
