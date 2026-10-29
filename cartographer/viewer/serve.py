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
import math
import os
import re
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..report.verdict import child_stages, run_verdict
from ..store.schema import ExperimentRecord
from ..store.store import ResultsStore
from . import launch as launch_mod
from .atlas import atlas_html, trials_html
from .coverage import coverage_html
from .findings import capability_trials_html, findings_html
from .frontier_page import capability_map_html, frontier_html
from .globe_data import display_name, payload_json, search_json

# The globe Map (`/`) is one static page that draws itself from
# `/api/map.json` (see `globe_data.py`). Read per request so an edited page
# shows on reload without restarting the viewer.
_MAP_PAGE = Path(__file__).resolve().parent / "static" / "map.html"

_DEFAULT_DB_PATH = "results/campaign.sqlite"

# Hosted (public, read-only) mode: the Vercel deployment sets this. Nothing
# that writes to the store or spends API credit is offered there; campaigns
# run from a local install and the store is redeployed.
HOSTED = os.environ.get("CARTOGRAPHER_HOSTED") == "1"

_API_CACHE = "public, max-age=0, s-maxage=86400, stale-while-revalidate=604800" if HOSTED else "no-store"

_HOSTED_LAUNCH_HTML = (
    '<div class="page-head"><div class="page-head-main"><h1>Launch a campaign</h1>'
    '<p class="subtitle">This is the public, read-only copy of Model Cartographer. Campaigns '
    "run for 30 to 90 minutes against Nebius Token Factory (Nemotron Nano, Super and Ultra) and "
    "write to the results store, so they launch from a local install.</p></div></div>"
    '<div class="card"><div class="card-head"><span class="card-title">Run it yourself</span></div>'
    '<div class="card-body"><ol class="prose-list">'
    "<li>Clone the repo and put a Nebius key in <span class=\"mono\">env/</span>.</li>"
    "<li>Start the viewer: <span class=\"mono\">python3 -m cartographer.viewer.serve results/campaign.sqlite</span></li>"
    "<li>Open Launch there, pick a provider and model, and start the <span class=\"mono\">full_map</span> preset.</li>"
    "</ol><p class=\"prose-p\">Everything you see on this site (Map, Findings, Breaking points, Atlas, Runs, "
    "every trial transcript) comes from that same store.</p></div></div>"
)
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

# Nav lives with the shell below (_NAV_MAIN / _NAV_BOTTOM).

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

/* =====================================================================
   20. INSTRUMENT + GLASS (brand v6) -- the direction settled on the brand
   canvas: a graphite survey field, glass instruments lifted above it, and
   one tag colour that means "a model broke". Appended as a layer so every
   page inherits it while earlier sections keep their structure.
   Type: Bricolage Grotesque for headlines and big figures, Geist for the
   interface, Geist Mono for every number and code.
   ===================================================================== */
:root {
  --surface-instrument-0: #07080a;
  --surface-instrument-1: #0e0f12;
  --surface-instrument-2: #17181c;
  --well: #08090b;
  --line: rgba(255,255,255,0.07);
  --line-strong: rgba(255,255,255,0.12);
  --ink: #ececee; --ink-secondary: #a1a1a8; --ink-muted: #8e8e97;
  --ground-truth: #ececee;
  --verdict-pass: #3fb950;
  --verdict-fail-trap: #f26b1d;
  --tag: #f26b1d;
  --accent: var(--ink);
  --glass: rgba(24,26,32,0.56);
  --module: rgba(8,9,12,0.55);
  --display: "Bricolage Grotesque", "Geist", -apple-system, "Segoe UI", sans-serif;
  --sans: "Geist", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  --mono: "Geist Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
  --glass-shadow: 0 24px 60px rgba(0,0,0,0.45), 0 2px 8px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.10), inset 0 -1px 0 rgba(255,255,255,0.03), inset 0 0 24px 6px rgba(255,255,255,0.022);
}
body { background: var(--surface-instrument-0); font-family: var(--sans); -webkit-font-smoothing: antialiased; }
body::before {
  content: ""; position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background:
    radial-gradient(circle 560px at 6% 4%, rgba(70,110,170,0.22), rgba(7,8,10,0) 70%),
    radial-gradient(circle 520px at 62% 58%, rgba(242,107,29,0.07), rgba(7,8,10,0) 70%),
    radial-gradient(circle 640px at 100% 100%, rgba(120,130,150,0.12), rgba(7,8,10,0) 70%),
    url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20width%3D%271600%27%20height%3D%271000%27%20viewBox%3D%270%200%201600%201000%27%3E%3Cpolygon%20points%3D%27564%2C620%20563%2C623%20562%2C626%20560%2C629%20558%2C631%20556%2C633%20554%2C636%20552%2C638%20549%2C640%20547%2C642%20545%2C644%20542%2C645%20539%2C647%20536%2C648%20533%2C649%20530%2C650%20527%2C650%20523%2C650%20520%2C651%20517%2C651%20513%2C651%20510%2C651%20506%2C651%20502%2C650%20499%2C650%20495%2C649%20492%2C647%20489%2C645%20487%2C642%20485%2C640%20484%2C637%20484%2C634%20484%2C631%20484%2C628%20484%2C625%20484%2C622%20484%2C620%20484%2C617%20483%2C615%20483%2C612%20483%2C609%20483%2C606%20484%2C604%20486%2C601%20488%2C599%20491%2C596%20493%2C595%20496%2C593%20499%2C592%20503%2C590%20506%2C589%20509%2C588%20513%2C587%20516%2C587%20520%2C587%20524%2C587%20527%2C588%20530%2C589%20533%2C590%20536%2C592%20539%2C594%20541%2C596%20543%2C598%20545%2C600%20548%2C601%20550%2C603%20553%2C605%20556%2C606%20559%2C609%20561%2C611%20563%2C614%20564%2C617%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.075%27%2F%3E%3Cpolygon%20points%3D%27606%2C620%20604%2C626%20601%2C631%20597%2C636%20593%2C641%20588%2C645%20584%2C650%20580%2C654%20576%2C658%20572%2C662%20568%2C666%20563%2C669%20558%2C673%20552%2C675%20546%2C677%20540%2C679%20533%2C680%20527%2C680%20520%2C681%20513%2C681%20507%2C681%20499%2C681%20492%2C681%20485%2C680%20478%2C679%20471%2C676%20465%2C673%20460%2C668%20456%2C663%20454%2C657%20452%2C651%20452%2C645%20452%2C640%20452%2C635%20452%2C630%20451%2C625%20450%2C620%20448%2C615%20447%2C610%20446%2C604%20446%2C598%20447%2C593%20449%2C587%20452%2C582%20457%2C578%20462%2C574%20468%2C570%20474%2C567%20480%2C565%20486%2C562%20493%2C560%20499%2C558%20506%2C557%20513%2C556%20520%2C556%20527%2C557%20534%2C558%20540%2C561%20546%2C564%20551%2C567%20556%2C571%20560%2C574%20565%2C577%20570%2C580%20576%2C583%20581%2C586%20587%2C589%20593%2C593%20598%2C597%20602%2C602%20605%2C608%20606%2C614%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%27646%2C620%20642%2C629%20637%2C636%20631%2C644%20625%2C651%20619%2C657%20613%2C663%20608%2C669%20603%2C676%20598%2C682%20592%2C688%20585%2C694%20577%2C699%20568%2C703%20559%2C706%20550%2C709%20540%2C710%20530%2C711%20520%2C712%20510%2C713%20500%2C713%20489%2C713%20478%2C712%20467%2C710%20457%2C707%20448%2C703%20439%2C697%20433%2C690%20428%2C682%20425%2C673%20423%2C665%20422%2C657%20421%2C649%20420%2C641%20419%2C634%20417%2C627%20414%2C620%20411%2C612%20408%2C604%20407%2C596%20407%2C587%20409%2C578%20412%2C570%20418%2C563%20425%2C556%20434%2C551%20442%2C546%20452%2C542%20461%2C538%20470%2C535%20480%2C532%20490%2C529%20500%2C527%20510%2C526%20520%2C526%20530%2C528%20540%2C530%20549%2C534%20557%2C538%20565%2C542%20573%2C546%20581%2C551%20588%2C555%20597%2C559%20605%2C563%20615%2C567%20624%2C572%20632%2C578%20639%2C585%20644%2C593%20647%2C602%20648%2C611%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%27683%2C620%20677%2C631%20670%2C641%20663%2C651%20655%2C659%20648%2C668%20642%2C676%20636%2C685%20630%2C694%20624%2C703%20616%2C711%20607%2C719%20597%2C726%20585%2C732%20573%2C736%20560%2C740%20547%2C742%20533%2C743%20520%2C744%20506%2C745%20492%2C745%20478%2C744%20464%2C743%20450%2C740%20437%2C735%20425%2C728%20415%2C720%20407%2C710%20401%2C700%20397%2C689%20395%2C678%20393%2C667%20391%2C658%20388%2C648%20385%2C639%20380%2C630%20375%2C620%20371%2C610%20367%2C598%20365%2C587%20366%2C575%20369%2C564%20375%2C553%20384%2C544%20394%2C536%20406%2C529%20418%2C523%20430%2C518%20443%2C513%20455%2C509%20468%2C505%20480%2C502%20493%2C499%20507%2C498%20520%2C498%20533%2C499%20546%2C502%20558%2C506%20569%2C511%20580%2C516%20591%2C522%20602%2C526%20613%2C531%20625%2C536%20637%2C541%20650%2C547%20662%2C555%20672%2C563%20680%2C573%20686%2C584%20688%2C596%20687%2C608%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%27718%2C620%20710%2C633%20701%2C646%20692%2C657%20684%2C668%20677%2C679%20671%2C690%20665%2C701%20659%2C713%20651%2C725%20642%2C736%20631%2C746%20618%2C755%20603%2C762%20587%2C768%20571%2C772%20554%2C774%20537%2C776%20520%2C777%20503%2C777%20485%2C777%20468%2C776%20451%2C773%20434%2C768%20418%2C761%20404%2C752%20393%2C741%20383%2C729%20377%2C716%20372%2C703%20368%2C690%20364%2C678%20360%2C667%20355%2C655%20348%2C644%20341%2C633%20334%2C620%20327%2C607%20323%2C592%20321%2C577%20323%2C563%20329%2C549%20339%2C536%20351%2C525%20364%2C516%20379%2C508%20395%2C501%20410%2C495%20426%2C489%20441%2C484%20456%2C479%20471%2C475%20487%2C472%20504%2C470%20520%2C470%20536%2C471%20552%2C474%20567%2C479%20582%2C484%20596%2C490%20610%2C495%20624%2C501%20639%2C506%20655%2C512%20671%2C519%20686%2C527%20700%2C537%20712%2C548%20721%2C562%20726%2C576%20726%2C591%20723%2C606%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%27749%2C620%20739%2C635%20730%2C650%20721%2C663%20714%2C676%20707%2C690%20702%2C704%20696%2C719%20689%2C734%20680%2C748%20669%2C762%20655%2C775%20639%2C785%20621%2C793%20602%2C799%20582%2C804%20561%2C806%20541%2C808%20520%2C809%20499%2C809%20479%2C808%20458%2C805%20438%2C801%20418%2C795%20400%2C786%20385%2C775%20371%2C762%20361%2C747%20353%2C732%20346%2C717%20340%2C703%20334%2C689%20327%2C676%20318%2C663%20308%2C650%20298%2C636%20289%2C620%20281%2C603%20277%2C586%20277%2C568%20281%2C551%20290%2C534%20303%2C520%20319%2C507%20337%2C497%20355%2C488%20374%2C480%20392%2C473%20409%2C466%20427%2C460%20444%2C454%20462%2C448%20481%2C444%20500%2C441%20520%2C441%20539%2C442%20559%2C445%20577%2C450%20595%2C455%20613%2C461%20630%2C467%20648%2C473%20667%2C480%20686%2C487%20705%2C496%20723%2C506%20738%2C519%20751%2C534%20759%2C550%20762%2C568%20761%2C586%20756%2C603%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.075%27%2F%3E%3Cpolygon%20points%3D%27777%2C620%20767%2C637%20758%2C654%20751%2C669%20744%2C685%20740%2C702%20735%2C719%20730%2C737%20722%2C756%20712%2C774%20698%2C790%20681%2C804%20661%2C816%20639%2C825%20616%2C831%20592%2C835%20568%2C838%20544%2C839%20520%2C839%20496%2C839%20472%2C837%20449%2C833%20425%2C828%20404%2C820%20384%2C809%20366%2C796%20351%2C781%20339%2C765%20329%2C748%20320%2C732%20311%2C717%20301%2C702%20290%2C687%20278%2C672%20265%2C656%20252%2C639%20241%2C620%20234%2C600%20231%2C579%20233%2C558%20241%2C539%20254%2C521%20271%2C505%20291%2C491%20312%2C480%20333%2C470%20354%2C461%20374%2C453%20393%2C445%20413%2C436%20433%2C428%20453%2C421%20475%2C415%20497%2C412%20520%2C410%20543%2C411%20565%2C415%20587%2C419%20609%2C425%20630%2C431%20652%2C438%20673%2C445%20696%2C452%20718%2C462%20739%2C473%20759%2C486%20775%2C502%20787%2C520%20794%2C540%20796%2C561%20793%2C582%20786%2C601%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%27804%2C620%20795%2C639%20788%2C658%20782%2C676%20778%2C695%20775%2C715%20772%2C736%20766%2C758%20758%2C780%20745%2C800%20728%2C819%20708%2C834%20684%2C847%20657%2C856%20630%2C862%20602%2C865%20575%2C867%20547%2C868%20520%2C868%20493%2C867%20466%2C864%20440%2C860%20414%2C853%20390%2C844%20367%2C831%20348%2C817%20331%2C800%20316%2C783%20304%2C765%20291%2C748%20279%2C731%20265%2C715%20250%2C698%20234%2C681%20218%2C663%20204%2C642%20192%2C620%20186%2C597%20185%2C573%20191%2C549%20203%2C528%20220%2C508%20242%2C491%20265%2C477%20289%2C465%20313%2C454%20335%2C444%20357%2C433%20378%2C423%20399%2C412%20421%2C402%20444%2C392%20468%2C385%20494%2C380%20520%2C378%20546%2C379%20572%2C382%20598%2C387%20623%2C393%20648%2C400%20673%2C407%20699%2C416%20724%2C425%20749%2C437%20772%2C451%20793%2C467%20809%2C487%20820%2C508%20826%2C531%20826%2C554%20821%2C578%20813%2C599%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%27832%2C620%20825%2C641%20820%2C662%20817%2C684%20816%2C706%20815%2C730%20812%2C755%20806%2C780%20795%2C805%20780%2C828%20759%2C848%20734%2C864%20705%2C876%20674%2C885%20643%2C890%20612%2C893%20581%2C895%20550%2C895%20520%2C895%20490%2C893%20460%2C890%20431%2C885%20403%2C878%20376%2C867%20351%2C854%20329%2C838%20310%2C820%20292%2C802%20276%2C784%20261%2C765%20244%2C747%20226%2C730%20207%2C711%20188%2C691%20170%2C669%20154%2C646%20144%2C620%20139%2C593%20142%2C567%20153%2C541%20169%2C518%20191%2C497%20216%2C480%20242%2C464%20268%2C451%20293%2C439%20317%2C426%20339%2C413%20361%2C400%20384%2C386%20408%2C373%20433%2C362%20461%2C353%20490%2C347%20520%2C344%20550%2C345%20580%2C348%20609%2C354%20638%2C361%20667%2C368%20695%2C377%20724%2C387%20752%2C399%20779%2C413%20803%2C430%20824%2C450%20840%2C472%20850%2C497%20854%2C523%20853%2C549%20847%2C574%20840%2C598%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%27861%2C620%20857%2C644%20856%2C667%20857%2C692%20858%2C718%20858%2C746%20855%2C775%20848%2C804%20834%2C831%20814%2C855%20789%2C876%20758%2C892%20725%2C904%20690%2C912%20655%2C916%20620%2C918%20586%2C919%20553%2C920%20520%2C919%20487%2C918%20455%2C915%20423%2C910%20392%2C902%20362%2C891%20335%2C877%20310%2C860%20287%2C842%20266%2C823%20247%2C804%20227%2C784%20206%2C765%20184%2C745%20162%2C724%20140%2C701%20121%2C676%20106%2C649%2097%2C620%2096%2C590%20104%2C561%20119%2C534%20140%2C509%20166%2C488%20194%2C469%20222%2C453%20249%2C438%20274%2C423%20298%2C408%20320%2C392%20343%2C375%20367%2C358%20393%2C342%20422%2C328%20453%2C318%20486%2C311%20520%2C308%20554%2C309%20588%2C314%20620%2C320%20653%2C328%20685%2C337%20717%2C348%20748%2C360%20778%2C374%20807%2C391%20832%2C411%20852%2C434%20868%2C459%20877%2C487%20880%2C515%20878%2C543%20873%2C570%20867%2C596%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%27894%2C620%20895%2C646%20897%2C673%20901%2C702%20904%2C732%20905%2C764%20901%2C796%20890%2C827%20873%2C857%20848%2C882%20817%2C903%20781%2C918%20743%2C929%20704%2C935%20665%2C939%20627%2C941%20591%2C942%20555%2C943%20520%2C943%20485%2C943%20449%2C941%20414%2C936%20380%2C928%20347%2C917%20317%2C902%20288%2C885%20262%2C866%20238%2C846%20214%2C825%20190%2C805%20165%2C784%20140%2C762%20115%2C738%2092%2C712%2073%2C683%2060%2C652%2054%2C620%2058%2C588%2070%2C557%2089%2C528%20115%2C502%20144%2C480%20173%2C460%20202%2C442%20229%2C425%20254%2C407%20277%2C388%20299%2C368%20323%2C347%20349%2C326%20378%2C308%20410%2C292%20445%2C280%20482%2C273%20520%2C271%20558%2C274%20595%2C279%20632%2C287%20667%2C297%20702%2C308%20737%2C320%20770%2C334%20802%2C351%20832%2C370%20858%2C393%20878%2C419%20893%2C448%20901%2C478%20904%2C508%20903%2C538%20900%2C566%20897%2C594%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.075%27%2F%3E%3Cpolygon%20points%3D%27932%2C620%20937%2C649%20943%2C680%20950%2C712%20954%2C746%20954%2C782%20947%2C817%20932%2C851%20909%2C881%20878%2C907%20842%2C927%20801%2C941%20758%2C950%20716%2C956%20674%2C959%20634%2C961%20596%2C963%20558%2C965%20520%2C968%20482%2C969%20443%2C968%20405%2C964%20367%2C956%20331%2C944%20297%2C929%20265%2C911%20235%2C891%20207%2C870%20179%2C849%20151%2C826%20123%2C803%2095%2C778%2069%2C751%2046%2C722%2028%2C689%2018%2C655%2016%2C620%2024%2C585%2041%2C552%2065%2C522%2093%2C496%20124%2C472%20154%2C451%20182%2C431%20207%2C410%20231%2C389%20253%2C365%20276%2C341%20300%2C316%20329%2C292%20361%2C270%20397%2C253%20437%2C242%20478%2C235%20520%2C235%20562%2C239%20602%2C246%20642%2C256%20680%2C267%20718%2C280%20755%2C294%20791%2C311%20824%2C330%20855%2C352%20881%2C378%20902%2C406%20917%2C437%20925%2C469%20929%2C501%20930%2C532%20930%2C562%20930%2C591%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%27976%2C620%20985%2C653%20994%2C687%201002%2C723%201006%2C761%201003%2C800%20992%2C838%20972%2C873%20943%2C904%20906%2C928%20863%2C947%20818%2C960%20771%2C968%20726%2C973%20682%2C977%20641%2C980%20600%2C984%20560%2C989%20520%2C993%20479%2C997%20437%2C997%20395%2C994%20353%2C986%20313%2C975%20276%2C959%20240%2C940%20207%2C919%20175%2C896%20143%2C873%20112%2C848%2081%2C823%2052%2C795%2025%2C764%202%2C731%20-13%2C695%20-19%2C658%20-16%2C620%20-4%2C583%2017%2C549%2044%2C518%2074%2C490%20104%2C465%20133%2C441%20159%2C418%20183%2C394%20204%2C367%20225%2C339%20249%2C310%20275%2C281%20307%2C254%20343%2C231%20384%2C214%20428%2C203%20474%2C198%20520%2C200%20565%2C206%20609%2C216%20651%2C227%20693%2C241%20733%2C255%20772%2C271%20809%2C290%20844%2C311%20876%2C335%20903%2C363%20925%2C393%20940%2C426%20951%2C459%20957%2C493%20961%2C526%20964%2C557%20969%2C589%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271024%2C620%201036%2C656%201048%2C694%201056%2C735%201058%2C777%201051%2C818%201034%2C858%201007%2C893%20971%2C923%20928%2C947%20881%2C964%20831%2C975%20782%2C983%20735%2C989%20690%2C994%20647%2C1000%20605%2C1007%20563%2C1015%20520%2C1022%20475%2C1028%20430%2C1030%20383%2C1028%20338%2C1020%20294%2C1007%20253%2C990%20214%2C970%20177%2C947%20142%2C923%20107%2C897%2074%2C870%2041%2C841%2010%2C810%20-16%2C776%20-36%2C739%20-48%2C700%20-51%2C660%20-44%2C620%20-28%2C582%20-4%2C546%2024%2C514%2054%2C484%2083%2C457%20109%2C430%20133%2C403%20153%2C374%20173%2C342%20194%2C309%20218%2C276%20248%2C243%20284%2C214%20325%2C191%20371%2C174%20420%2C165%20470%2C163%20520%2C168%20568%2C177%20615%2C189%20660%2C202%20703%2C217%20746%2C233%20787%2C250%20826%2C270%20863%2C293%20897%2C319%20925%2C348%20948%2C380%20966%2C414%20978%2C449%20988%2C484%20995%2C518%201003%2C552%201013%2C586%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271077%2C620%201091%2C660%201103%2C702%201109%2C746%201107%2C791%201095%2C834%201072%2C875%201038%2C910%20995%2C939%20946%2C961%20894%2C977%20842%2C988%20792%2C996%20744%2C1003%20698%2C1011%20654%2C1021%20611%2C1032%20566%2C1044%20520%2C1055%20472%2C1063%20422%2C1067%20371%2C1064%20322%2C1056%20274%2C1042%20229%2C1023%20187%2C1001%20147%2C976%20109%2C949%2072%2C920%2037%2C891%203%2C859%20-27%2C824%20-52%2C787%20-70%2C746%20-79%2C704%20-78%2C662%20-67%2C620%20-49%2C580%20-24%2C543%204%2C509%2032%2C478%2058%2C448%2081%2C417%20101%2C385%20119%2C351%20137%2C314%20159%2C276%20186%2C238%20220%2C204%20260%2C174%20307%2C151%20358%2C137%20412%2C131%20467%2C132%20520%2C140%20571%2C151%20620%2C165%20667%2C180%20713%2C196%20757%2C213%20801%2C231%20843%2C251%20882%2C274%20918%2C302%20949%2C332%20974%2C366%20994%2C401%201010%2C437%201023%2C474%201035%2C510%201048%2C546%201062%2C582%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271131%2C620%201146%2C664%201157%2C710%201159%2C757%201152%2C804%201133%2C849%201103%2C889%201062%2C924%201014%2C951%20961%2C972%20906%2C988%20852%2C999%20801%2C1009%20753%2C1020%20707%2C1032%20663%2C1046%20617%2C1062%20570%2C1078%20520%2C1092%20467%2C1102%20413%2C1107%20358%2C1104%20304%2C1094%20253%2C1078%20205%2C1056%20160%2C1031%20118%2C1003%2078%2C973%2040%2C942%204%2C909%20-30%2C874%20-60%2C836%20-84%2C796%20-99%2C753%20-105%2C708%20-102%2C664%20-89%2C620%20-69%2C579%20-45%2C540%20-19%2C504%206%2C470%2028%2C436%2047%2C401%2063%2C364%2079%2C324%2098%2C282%20122%2C240%20152%2C200%20191%2C164%20237%2C135%20290%2C115%20347%2C103%20406%2C101%20464%2C106%20520%2C116%20574%2C130%20625%2C145%20674%2C161%20722%2C177%20769%2C193%20815%2C211%20860%2C231%20903%2C255%20941%2C283%20975%2C315%201003%2C349%201026%2C386%201046%2C424%201063%2C462%201079%2C500%201096%2C539%201114%2C578%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.075%27%2F%3E%3Cpolygon%20points%3D%271359%2C200%201359%2C202%201359%2C204%201358%2C206%201357%2C208%201356%2C210%201355%2C211%201353%2C213%201352%2C215%201351%2C216%201349%2C218%201347%2C220%201346%2C222%201344%2C223%201341%2C225%201339%2C226%201336%2C226%201333%2C226%201330%2C226%201327%2C225%201325%2C224%201322%2C222%201320%2C221%201318%2C220%201317%2C219%201315%2C218%201313%2C217%201310%2C216%201308%2C215%201306%2C213%201304%2C212%201302%2C210%201301%2C208%201300%2C206%201299%2C204%201299%2C202%201299%2C200%201299%2C198%201300%2C196%201300%2C194%201301%2C192%201302%2C189%201303%2C187%201304%2C186%201306%2C184%201308%2C182%201310%2C181%201313%2C181%201316%2C180%201318%2C180%201321%2C180%201323%2C179%201325%2C179%201328%2C178%201330%2C178%201333%2C177%201335%2C177%201338%2C176%201341%2C176%201344%2C176%201347%2C177%201349%2C178%201351%2C180%201353%2C182%201354%2C184%201355%2C186%201356%2C188%201357%2C190%201357%2C192%201358%2C194%201358%2C196%201359%2C198%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.075%27%2F%3E%3Cpolygon%20points%3D%271392%2C200%201392%2C204%201392%2C209%201390%2C213%201389%2C217%201386%2C221%201384%2C225%201381%2C228%201378%2C232%201375%2C236%201372%2C240%201368%2C243%201364%2C247%201359%2C250%201354%2C253%201348%2C254%201342%2C255%201336%2C255%201330%2C253%201324%2C251%201319%2C249%201314%2C246%201310%2C244%201306%2C241%201302%2C239%201297%2C238%201292%2C236%201287%2C234%201282%2C232%201277%2C229%201273%2C226%201269%2C223%201267%2C218%201265%2C214%201264%2C209%201264%2C205%201264%2C200%201265%2C195%201266%2C191%201267%2C186%201268%2C182%201270%2C178%201273%2C174%201276%2C170%201280%2C166%201284%2C163%201289%2C161%201295%2C160%201300%2C158%201305%2C157%201310%2C157%201315%2C156%201320%2C154%201325%2C153%201330%2C151%201336%2C149%201341%2C148%201348%2C147%201354%2C148%201360%2C149%201366%2C151%201370%2C154%201374%2C158%201378%2C162%201380%2C166%201382%2C171%201384%2C175%201385%2C179%201387%2C183%201388%2C187%201390%2C192%201391%2C196%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271425%2C200%201426%2C207%201425%2C213%201423%2C220%201421%2C226%201417%2C233%201414%2C239%201409%2C244%201405%2C250%201400%2C256%201395%2C262%201389%2C268%201383%2C273%201375%2C277%201367%2C280%201358%2C282%201348%2C283%201339%2C282%201330%2C279%201322%2C276%201314%2C273%201307%2C269%201300%2C266%201293%2C263%201286%2C261%201279%2C259%201271%2C256%201263%2C254%201255%2C251%201247%2C246%201241%2C241%201235%2C235%201232%2C229%201229%2C222%201228%2C214%201228%2C207%201229%2C200%201230%2C193%201232%2C186%201234%2C179%201237%2C173%201240%2C166%201244%2C160%201249%2C155%201255%2C149%201262%2C145%201269%2C142%201277%2C139%201285%2C137%201292%2C135%201300%2C133%201307%2C131%201314%2C128%201322%2C125%201330%2C122%201339%2C120%201348%2C118%201358%2C118%201367%2C118%201376%2C121%201384%2C125%201391%2C130%201397%2C136%201401%2C143%201405%2C150%201408%2C156%201410%2C163%201413%2C169%201416%2C175%201419%2C181%201421%2C187%201424%2C193%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271460%2C200%201461%2C209%201460%2C218%201458%2C227%201454%2C236%201450%2C245%201444%2C253%201439%2C261%201433%2C269%201426%2C277%201419%2C284%201410%2C292%201401%2C298%201390%2C303%201379%2C307%201366%2C309%201354%2C308%201342%2C306%201330%2C303%201319%2C299%201309%2C295%201299%2C291%201290%2C288%201280%2C285%201270%2C283%201259%2C281%201248%2C278%201236%2C275%201225%2C270%201215%2C264%201207%2C257%201201%2C248%201196%2C239%201194%2C229%201193%2C219%201194%2C210%201196%2C200%201198%2C191%201200%2C182%201203%2C173%201206%2C164%201210%2C155%201216%2C147%201222%2C140%201230%2C133%201239%2C127%201249%2C123%201259%2C119%201269%2C115%201279%2C112%201288%2C108%201298%2C105%201308%2C100%201319%2C96%201330%2C92%201342%2C89%201355%2C87%201368%2C87%201380%2C89%201392%2C94%201402%2C100%201411%2C108%201418%2C116%201423%2C126%201428%2C135%201432%2C143%201436%2C151%201440%2C159%201444%2C167%201449%2C174%201453%2C183%201457%2C191%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271496%2C200%201497%2C212%201496%2C223%201493%2C235%201489%2C246%201483%2C257%201476%2C267%201469%2C278%201460%2C288%201452%2C297%201442%2C307%201431%2C315%201418%2C323%201405%2C328%201390%2C332%201375%2C333%201359%2C332%201344%2C330%201330%2C326%201317%2C322%201304%2C317%201292%2C314%201279%2C311%201267%2C309%201253%2C307%201238%2C305%201223%2C302%201208%2C297%201195%2C291%201182%2C283%201173%2C273%201166%2C261%201162%2C249%201160%2C236%201160%2C224%201162%2C212%201164%2C200%201167%2C189%201169%2C177%201173%2C166%201177%2C155%201182%2C145%201188%2C135%201197%2C125%201206%2C117%201217%2C110%201228%2C103%201240%2C98%201252%2C93%201264%2C88%201276%2C82%201289%2C77%201302%2C71%201315%2C65%201330%2C60%201346%2C57%201362%2C56%201378%2C57%201393%2C61%201407%2C68%201419%2C77%201429%2C87%201437%2C98%201443%2C109%201449%2C120%201454%2C130%201460%2C140%201467%2C149%201474%2C158%201481%2C168%201487%2C178%201492%2C189%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271534%2C200%201536%2C214%201534%2C229%201530%2C243%201524%2C256%201516%2C269%201507%2C282%201498%2C294%201488%2C306%201477%2C317%201464%2C328%201451%2C338%201435%2C346%201418%2C352%201401%2C355%201382%2C356%201364%2C355%201347%2C352%201330%2C348%201314%2C344%201299%2C341%201284%2C338%201268%2C336%201252%2C335%201234%2C333%201215%2C331%201197%2C327%201179%2C321%201163%2C312%201150%2C301%201139%2C288%201133%2C274%201129%2C259%201128%2C243%201129%2C228%201132%2C214%201134%2C200%201137%2C187%201140%2C173%201144%2C160%201148%2C147%201154%2C134%201161%2C122%201170%2C111%201181%2C100%201194%2C91%201207%2C83%201221%2C76%201235%2C69%201249%2C62%201264%2C54%201279%2C47%201295%2C40%201312%2C33%201330%2C28%201349%2C25%201368%2C25%201387%2C29%201405%2C35%201421%2C45%201434%2C56%201445%2C69%201454%2C82%201462%2C94%201469%2C106%201477%2C118%201486%2C128%201495%2C138%201505%2C149%201515%2C160%201523%2C173%201530%2C186%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.075%27%2F%3E%3Cpolygon%20points%3D%271574%2C200%201575%2C217%201572%2C234%201566%2C251%201558%2C266%201549%2C282%201538%2C296%201526%2C310%201514%2C323%201500%2C336%201486%2C348%201469%2C359%201451%2C367%201431%2C374%201411%2C377%201390%2C378%201369%2C377%201349%2C374%201330%2C371%201312%2C368%201294%2C366%201275%2C364%201256%2C363%201235%2C363%201213%2C361%201191%2C358%201170%2C353%201149%2C345%201132%2C333%201118%2C319%201108%2C303%201102%2C285%201099%2C267%201099%2C249%201101%2C232%201104%2C216%201106%2C200%201109%2C185%201111%2C169%201114%2C154%201119%2C138%201124%2C123%201133%2C109%201143%2C95%201155%2C83%201169%2C72%201185%2C61%201201%2C52%201217%2C43%201233%2C34%201251%2C25%201269%2C16%201288%2C8%201308%2C1%201330%2C-3%201352%2C-5%201375%2C-3%201396%2C2%201416%2C12%201433%2C23%201447%2C37%201460%2C52%201470%2C66%201480%2C80%201490%2C93%201501%2C104%201513%2C116%201526%2C127%201539%2C139%201551%2C153%201562%2C167%201570%2C183%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271615%2C200%201615%2C220%201610%2C240%201602%2C258%201592%2C276%201580%2C293%201567%2C309%201553%2C325%201538%2C340%201523%2C354%201505%2C367%201486%2C379%201466%2C388%201443%2C395%201420%2C398%201397%2C400%201374%2C399%201352%2C397%201330%2C395%201309%2C394%201287%2C393%201265%2C393%201242%2C393%201217%2C393%201192%2C391%201166%2C387%201142%2C379%201120%2C368%201101%2C353%201088%2C336%201078%2C316%201073%2C296%201072%2C275%201073%2C255%201075%2C236%201078%2C218%201079%2C200%201081%2C183%201082%2C165%201084%2C147%201088%2C129%201094%2C112%201102%2C95%201114%2C79%201128%2C64%201144%2C51%201161%2C39%201179%2C28%201198%2C17%201217%2C6%201237%2C-4%201258%2C-14%201281%2C-23%201305%2C-30%201330%2C-34%201356%2C-34%201381%2C-29%201404%2C-21%201425%2C-10%201444%2C4%201460%2C20%201474%2C36%201486%2C51%201499%2C65%201512%2C78%201527%2C90%201543%2C102%201559%2C114%201576%2C128%201591%2C144%201603%2C161%201612%2C180%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271656%2C200%201653%2C223%201646%2C245%201635%2C265%201622%2C285%201608%2C304%201593%2C322%201578%2C339%201561%2C355%201544%2C371%201524%2C385%201503%2C398%201480%2C408%201456%2C415%201430%2C420%201404%2C422%201379%2C423%201354%2C422%201330%2C422%201306%2C422%201281%2C423%201255%2C424%201228%2C425%201199%2C425%201170%2C422%201141%2C416%201115%2C405%201092%2C390%201074%2C372%201060%2C351%201052%2C328%201049%2C305%201048%2C282%201050%2C260%201051%2C239%201052%2C219%201052%2C200%201052%2C181%201051%2C161%201052%2C140%201055%2C120%201061%2C100%201070%2C80%201083%2C62%201099%2C45%201117%2C29%201136%2C15%201157%2C2%201178%2C-10%201200%2C-22%201224%2C-34%201248%2C-45%201274%2C-53%201302%2C-59%201330%2C-61%201358%2C-59%201386%2C-53%201411%2C-43%201434%2C-29%201454%2C-13%201472%2C4%201488%2C20%201503%2C35%201519%2C49%201536%2C62%201555%2C74%201576%2C87%201596%2C101%201616%2C117%201633%2C135%201646%2C155%201654%2C177%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271696%2C200%201690%2C225%201679%2C249%201666%2C272%201651%2C293%201634%2C314%201618%2C333%201601%2C352%201583%2C370%201564%2C387%201543%2C403%201520%2C417%201495%2C428%201468%2C437%201440%2C443%201412%2C446%201385%2C448%201357%2C449%201330%2C451%201302%2C453%201274%2C455%201244%2C457%201212%2C459%201180%2C457%201148%2C452%201117%2C443%201089%2C429%201066%2C411%201049%2C389%201037%2C364%201030%2C339%201027%2C313%201027%2C288%201027%2C265%201027%2C243%201026%2C221%201024%2C200%201020%2C178%201018%2C156%201017%2C133%201019%2C109%201025%2C86%201036%2C64%201050%2C43%201068%2C24%201089%2C7%201111%2C-9%201135%2C-23%201159%2C-37%201184%2C-50%201211%2C-63%201239%2C-73%201268%2C-81%201299%2C-86%201330%2C-87%201361%2C-83%201390%2C-74%201418%2C-62%201442%2C-47%201464%2C-30%201484%2C-13%201503%2C3%201521%2C17%201542%2C31%201564%2C43%201587%2C56%201612%2C70%201636%2C86%201658%2C104%201677%2C126%201689%2C149%201696%2C174%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3Cpolygon%20points%3D%271732%2C200%201722%2C227%201709%2C253%201693%2C278%201676%2C301%201658%2C322%201641%2C343%201623%2C364%201604%2C384%201584%2C403%201562%2C421%201537%2C437%201510%2C450%201481%2C460%201452%2C467%201421%2C472%201391%2C476%201361%2C479%201330%2C482%201299%2C486%201266%2C489%201232%2C492%201197%2C492%201161%2C489%201127%2C482%201095%2C469%201067%2C451%201044%2C429%201027%2C403%201017%2C375%201011%2C347%201008%2C320%201007%2C294%201005%2C270%201002%2C246%20998%2C223%20992%2C200%20985%2C176%20980%2C151%20978%2C125%20980%2C98%20987%2C72%201000%2C47%201017%2C25%201038%2C4%201061%2C-15%201086%2C-32%201113%2C-48%201140%2C-63%201169%2C-77%201198%2C-89%201230%2C-100%201262%2C-107%201296%2C-111%201330%2C-110%201363%2C-104%201395%2C-94%201424%2C-80%201450%2C-64%201474%2C-47%201497%2C-31%201519%2C-16%201542%2C-2%201567%2C10%201594%2C23%201623%2C36%201652%2C51%201679%2C70%201702%2C92%201720%2C116%201731%2C143%201735%2C172%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.075%27%2F%3E%3Cpolygon%20points%3D%271764%2C200%201751%2C229%201735%2C257%201716%2C283%201698%2C307%201680%2C331%201663%2C354%201645%2C376%201626%2C399%201606%2C420%201582%2C440%201556%2C458%201527%2C473%201496%2C485%201464%2C494%201431%2C501%201397%2C506%201364%2C511%201330%2C516%201295%2C520%201259%2C524%201221%2C526%201182%2C525%201144%2C519%201107%2C508%201075%2C492%201047%2C470%201025%2C444%201009%2C415%20999%2C385%20994%2C355%20990%2C327%20986%2C300%20982%2C275%20975%2C250%20966%2C226%20956%2C200%20946%2C173%20939%2C145%20936%2C116%20939%2C86%20948%2C57%20963%2C30%20983%2C6%201007%2C-17%201034%2C-37%201063%2C-55%201092%2C-71%201123%2C-87%201155%2C-101%201187%2C-114%201222%2C-124%201257%2C-130%201294%2C-133%201330%2C-131%201365%2C-124%201399%2C-112%201430%2C-98%201459%2C-82%201485%2C-67%201511%2C-51%201538%2C-38%201566%2C-25%201596%2C-13%201628%2C-0%201661%2C14%201693%2C32%201722%2C54%201746%2C79%201762%2C107%201770%2C138%201771%2C169%27%20fill%3D%27none%27%20stroke%3D%27white%27%20stroke-opacity%3D%270.04%27%2F%3E%3C%2Fsvg%3E") center / cover no-repeat;
}
.app-shell { position: relative; z-index: 1; }
a { color: var(--ink); }
a:hover { text-decoration: underline; text-decoration-color: rgba(255,255,255,0.35); }
.mono, code, td.mono, td .mono { font-family: var(--mono); }

/* glass: one recipe, used by every lifted surface */
.sidebar, .card, .panel, .console, .link-card {
  position: relative;
  background: var(--glass);
  -webkit-backdrop-filter: blur(18px) saturate(150%); backdrop-filter: blur(18px) saturate(150%);
  border: 1px solid rgba(255,255,255,0.09);
  box-shadow: var(--glass-shadow);
}
.sidebar::before, .card::before, .panel::before, .console::before, .link-card::before {
  content: ""; position: absolute; left: 8%; right: 8%; top: -1px; height: 1px; pointer-events: none;
  background: linear-gradient(90deg, rgba(255,255,255,0), rgba(255,255,255,0.38), rgba(255,255,255,0));
}

/* shell: the sidebar floats as an instrument panel */
.sidebar {
  position: sticky; top: 12px; align-self: flex-start;
  width: 232px; height: calc(100vh - 24px); margin: 12px 0 12px 12px;
  border-radius: 18px; padding: 16px 0; overflow-y: auto;
}
.brand-block { border-bottom: 1px solid var(--line); padding: 0 16px 16px; }
.brand-mark { background: none; color: var(--ink); width: 26px; height: 26px; }
.brand-mark svg { display: block; }
.brand-name { font-family: var(--display); font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
.brand-block .counter { font-family: var(--mono); font-size: 11px; color: var(--ink-muted); }
.nav-group-label { text-transform: none; letter-spacing: 0; font-family: var(--sans); font-size: 11.5px; color: var(--ink-muted); padding: 0 18px; }
.nav-row { margin: 1px 8px; padding: 7px 10px; border-radius: 8px; color: var(--ink-secondary); border: 0; font-size: 13px; }
.nav-row:hover { background: rgba(255,255,255,0.05); color: var(--ink); text-decoration: none; }
.nav-row.active { background: rgba(255,255,255,0.08); color: var(--ink); box-shadow: inset 0 1px 0 rgba(255,255,255,0.08); }
.nav-icon { width: 16px; display: inline-flex; justify-content: center; color: currentColor; }
.nav-icon svg { display: block; }
main.shell-main { flex: 1; min-width: 0; padding: 24px 32px 56px; }
.page-strip { background: transparent; border-bottom: 1px solid var(--line); }

/* type */
h1 { font-family: var(--display); font-weight: 600; letter-spacing: -0.03em; line-height: 1.1; }
h2 { font-family: var(--display); font-weight: 600; letter-spacing: -0.02em; }
h3 { font-family: var(--sans); font-weight: 600; }
.subtitle { color: var(--ink-secondary); }

/* cards, stats, tables */
.card { border-radius: 16px; overflow: hidden; }
.card-head { background: transparent; border-bottom: 1px solid var(--line); }
.card-title { font-family: var(--sans); font-weight: 600; color: var(--ink); }
.card-meta { color: var(--ink-muted); }
.panel { border-radius: 14px; }
.stat-box { background: var(--module); border: 1px solid var(--line); border-radius: 14px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.05); }
.stat-box .stat-num { font-family: var(--display); font-weight: 600; letter-spacing: -0.02em; }
th { background: transparent; color: var(--ink-muted); font-family: var(--sans); font-weight: 500; text-transform: none; letter-spacing: 0; }
th, td { border-bottom: 1px solid var(--line); }
tbody tr:hover { background: rgba(255,255,255,0.03); }
.databar-fill { background: #6b6c73; }
.databar.below .databar-fill { background: var(--tag); }

/* controls */
input, select, textarea { background: var(--well); border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; color: var(--ink); font-family: var(--sans); }
input:focus, select:focus, textarea:focus { outline: none; border-color: rgba(255,255,255,0.35); box-shadow: 0 0 0 3px rgba(255,255,255,0.06); }
button { border-radius: 8px; font-family: var(--sans); }
button.btn-primary, .cta-primary { background: var(--ink); color: #07080a; border: 0; font-weight: 600; border-radius: 8px; }
button.btn-primary:hover, .cta-primary:hover { background: #ffffff; text-decoration: none; }
button.btn-secondary { background: transparent; border: 1px solid rgba(255,255,255,0.14); color: var(--ink); }
.preset-card { background: var(--module); border: 1px solid var(--line); border-radius: 12px; }
.preset-card:has(input:checked) { border-color: rgba(255,255,255,0.45); background: rgba(255,255,255,0.05); }

/* ---- the home console ---- */
.hero { display: flex; justify-content: space-between; align-items: flex-end; gap: 32px; margin: 6px 0 24px; }
.hero h1 { font-size: 2.7rem; margin: 0; }
.hero h1 .dim { color: var(--ink-secondary); }
.hero .lede { max-width: 640px; color: var(--ink-secondary); font-size: 15px; line-height: 1.6; margin: 12px 0 0; }
.hero .rules { font-size: 11.5px; letter-spacing: 0.18em; color: var(--ink-muted); margin-top: 14px; font-weight: 500; }
.hero .cta-primary { flex: none; display: inline-flex; align-items: center; gap: 8px; height: 40px; padding: 0 16px; }
.console { border-radius: 22px; border-color: rgba(255,255,255,0.12); box-shadow: 0 0 0 1px rgba(0,0,0,0.4), 0 40px 110px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.12), inset 0 0 24px 6px rgba(255,255,255,0.022); }
.console-bar { display: flex; align-items: center; gap: 18px; height: 50px; padding: 0 18px; border-bottom: 1px solid var(--line); font-size: 12px; color: var(--ink-secondary); }
.console-bar .title { font-family: var(--display); font-size: 15px; font-weight: 600; color: var(--ink); }
.console-bar .right { margin-left: auto; display: flex; align-items: center; gap: 16px; }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--verdict-pass); display: inline-block; margin-right: 6px; }
.console-grid { display: grid; grid-template-columns: 244px minmax(0, 1fr) 296px; gap: 12px; padding: 14px; }
.console-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; padding: 0 14px 14px; }
.col { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.mod { background: var(--module); border: 1px solid var(--line); border-radius: 14px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.05); padding: 12px 14px; min-width: 0; }
.mod-head { display: flex; align-items: baseline; gap: 8px; font-size: 12.5px; font-weight: 600; color: var(--ink); margin-bottom: 8px; }
.mod-head .aside { margin-left: auto; font-weight: 500; font-size: 11.5px; color: var(--ink-muted); }
.mod-head a.aside:hover { color: var(--ink); }
.stat-mod { display: grid; grid-template-columns: minmax(0, 1fr) 92px; align-items: center; gap: 10px; }
.stat-mod .k { font-size: 11.5px; color: var(--ink-secondary); }
.stat-mod .v { font-family: var(--display); font-size: 22px; font-weight: 600; letter-spacing: -0.02em; color: var(--ink); line-height: 1.2; }
.stat-mod .s { font-family: var(--mono); font-size: 11px; color: var(--ink-muted); }
.list-row { display: flex; gap: 10px; align-items: flex-start; padding: 8px 0; border-top: 1px solid var(--line); font-size: 12.5px; line-height: 1.45; }
.list-row:first-of-type { border-top: 0; }
.list-row .when { margin-left: auto; font-family: var(--mono); font-size: 11px; color: var(--ink-muted); white-space: nowrap; }
.list-row .sub { display: block; font-family: var(--mono); font-size: 11px; color: var(--ink-muted); }
.dot { width: 6px; height: 6px; border-radius: 50%; flex: none; margin-top: 6px; background: var(--ink-muted); }
.dot.ok { background: var(--verdict-pass); } .dot.tag { background: var(--tag); } .dot.hollow { background: none; border: 1px solid var(--ink-muted); }
.profile-mod { display: flex; flex-direction: column; gap: 10px; }
.profile-mod svg { width: 100%; height: auto; }
.readouts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; border-top: 1px solid var(--line); padding-top: 10px; }
.readouts .k { font-size: 11px; color: var(--ink-muted); }
.readouts .v { font-family: var(--display); font-size: 17px; font-weight: 600; letter-spacing: -0.01em; color: var(--ink); }
.readouts .v.low { color: var(--tag); }
.readouts .s { font-size: 11px; color: var(--ink-secondary); }
.run-cell { min-width: 0; flex: 1; }
.run-id { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.clamp { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; font-family: var(--sans) !important; font-size: 11.5px !important; color: var(--ink-secondary) !important; margin-top: 2px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 11.5px; color: var(--ink-secondary); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 14px; height: 2px; display: inline-block; }
.matrix { width: 100%; border-collapse: collapse; font-size: 12px; }
.matrix th, .matrix td { padding: 6px 4px; border-bottom: 1px solid var(--line); }
.matrix td.num { font-family: var(--mono); text-align: right; }
.matrix td.low { color: var(--tag); }
.links-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-top: 18px; }
.link-card { display: flex; flex-direction: column; gap: 6px; padding: 16px 18px; border-radius: 16px; color: var(--ink); }
.link-card:hover { text-decoration: none; border-color: rgba(255,255,255,0.2); }
.link-card .t { font-family: var(--display); font-size: 15px; font-weight: 600; display: flex; justify-content: space-between; align-items: center; }
.link-card p { margin: 0; color: var(--ink-secondary); font-size: 12.5px; line-height: 1.5; }
@media (max-width: 1180px) { .console-grid { grid-template-columns: 1fr 1fr; } .console-grid > .col:nth-child(2) { grid-column: 1 / -1; order: -1; } }
@media (max-width: 760px) { .console-grid, .console-row, .links-grid { grid-template-columns: 1fr; } .hero { flex-direction: column; align-items: flex-start; } .sidebar { display: none; } }
/* one ramp everywhere: brightness for how well a model did, the tag only
   where it is failing (Findings heat map, run tables) */
.heat-5 { background: rgba(255,255,255,0.11); color: var(--ink); }
.heat-4 { background: rgba(255,255,255,0.07); color: var(--ink); }
.heat-3 { background: rgba(255,255,255,0.035); color: var(--ink-secondary); }
.heat-2 { background: rgba(242,107,29,0.16); color: #ffb48a; }
.heat-1 { background: rgba(242,107,29,0.38); color: #ffffff; }
.cap-block { border-radius: 6px; }
a.cap-block:hover { border-color: rgba(255,255,255,0.45); }
a.cap-block:focus-visible { outline-color: var(--ink); }
.databar { background: rgba(255,255,255,0.07); border-radius: 2px; }
.databar.full .databar-fill { background: var(--ink-secondary); }
.databar-num { font-family: var(--mono); }

/* =====================================================================
   21. MAP SYSTEM ALIGNMENT -- the globe Map (`/`, static/map.html) sets the
   app's palette and type: cool graphite surfaces, Google Sans Flex for the
   interface, Google Sans Code for ids and numbers. These tokens bring every
   older page onto it, so moving between the Map and these pages does not
   change the look. Layouts, the heat ramp and the orange "broke" tag stay.
   ===================================================================== */
:root {
  --surface-instrument-0: #070C10;
  --surface-instrument-1: #0B1218;
  --surface-instrument-2: #0F171E;
  --well: #091017;
  --line: rgba(180,200,215,0.10);
  --line-strong: rgba(180,200,215,0.16);
  --ink: #E8EDF1; --ink-secondary: #A9B4BE; --ink-muted: #8B98A4;
  --ground-truth: #E8EDF1;
  --accent: var(--ink);
  --glass: rgba(11,18,24,0.72);
  --module: rgba(7,12,16,0.6);
  --display: "Google Sans Flex", "Google Sans", "Geist", -apple-system, "Segoe UI", sans-serif;
  --sans: "Google Sans Flex", "Google Sans", "Geist", -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  --mono: "Google Sans Code", "Geist Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
}

/* =====================================================================
   22. MAP FRAME -- every server-rendered page now sits in the globe Map's
   own frame (static/map.html): its sidebar, its command bar, its panels,
   readouts and tables. Values are copied from the Map, not approximated,
   so moving between `/` and any page here changes content, never chrome.
   ===================================================================== */
:root {
  --void-950:#070C10; --void-900:#091017;
  --surface-900:#0B1218; --surface-800:#0F171E; --surface-700:#141E26;
  --panel: rgba(11,18,24,.86);
  --line-subtle: rgba(180,200,215,.10); --line-visible: rgba(180,200,215,.16);
  --text-1:#E8EDF1; --text-2:#8B98A4; --text-3:#65727D;
  --lum:#F5B26B;
  --f-ui: 'Google Sans Flex', 'Google Sans', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
  --f-mono: 'Google Sans Code', ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  --ink-secondary: #A9B4BE; --ink-muted: #8B98A4;
}
html, body { height: auto; }
body.mc { margin: 0; background: var(--void-950); color: var(--text-1); font: 400 13px/1.5 var(--f-ui); -webkit-font-smoothing: antialiased; overflow-x: hidden; }
body.mc::before {
  content: ""; position: fixed; inset: 0; z-index: 0; pointer-events: none; background:
    radial-gradient(900px 520px at 78% -8%, rgba(127,178,229,.07), transparent 62%),
    radial-gradient(760px 520px at 8% 108%, rgba(232,163,92,.06), transparent 60%);
  -webkit-mask-image: none; mask-image: none; opacity: 1;
}
body.mc ::selection { background: rgba(245,178,107,.28); }
body.mc a { color: var(--text-1); text-decoration-color: var(--line-visible); text-underline-offset: 3px; }
body.mc a:hover { color: var(--lum); }

/* frame */
body.mc .app-shell { position: relative; z-index: 1; display: grid; grid-template-columns: 200px minmax(0,1fr); min-height: 100vh; }
body.mc .side { position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; padding: 22px 0 18px;
  background: linear-gradient(90deg, rgba(7,9,11,.94), rgba(7,9,11,.82)); border-right: 1px solid var(--line-subtle); }
body.mc .brand { display: flex; align-items: center; gap: 14px; padding: 0 20px 34px; text-decoration: none; color: var(--text-1); }
body.mc .brand svg { flex: none; }
body.mc .brand-name { font: 500 10.5px/1.75 var(--f-ui); letter-spacing: .42em; color: var(--text-1); }
body.mc .nav { display: flex; flex-direction: column; gap: 4px; }
body.mc .nav-bottom { margin-top: auto; padding-top: 24px; }
body.mc .nav-item { position: relative; display: flex; align-items: center; gap: 14px; height: 44px; padding: 0 22px; color: var(--text-2);
  font-size: 12.5px; letter-spacing: .01em; text-decoration: none; transition: color .2s, background .2s; }
body.mc .nav-item svg { width: 18px; height: 18px; flex: none; stroke: currentColor; fill: none; stroke-width: 1.25; opacity: .85; }
body.mc .nav-item:hover { color: var(--text-1); background: rgba(255,255,255,.025); }
body.mc .nav-item.on { color: var(--text-1); background: linear-gradient(90deg, rgba(245,178,107,.10), rgba(255,255,255,.02) 70%, transparent); }
body.mc .nav-item.on::before { content: ''; position: absolute; left: 0; top: 10px; bottom: 10px; width: 2px; border-radius: 2px; background: var(--lum); box-shadow: 0 0 10px 1px rgba(245,178,107,.6); }
body.mc .nav-item.on svg { color: var(--lum); opacity: 1; }

body.mc .shell-col { min-width: 0; display: flex; flex-direction: column; }
body.mc .topbar { display: flex; align-items: center; gap: 14px; height: 76px; padding: 0 18px 0 40px; }
body.mc .search { position: relative; flex: 1 1 auto; max-width: 650px; min-width: 0; }
body.mc .search-field { display: flex; align-items: center; gap: 12px; height: 40px; padding: 0 8px 0 14px; border: 1px solid var(--line-visible); border-radius: 8px; background: rgba(11,14,17,.78); }
body.mc .search-field svg { flex: none; display: block; width: 16px; height: 16px; stroke: var(--text-2); fill: none; stroke-width: 1.4; }
body.mc .search-field input { flex: 1; min-width: 0; padding: 0; height: auto; text-overflow: ellipsis; background: none; border: 0; outline: 0; box-shadow: none; color: var(--text-1); font: 400 13px var(--f-ui); }
body.mc .search-field input::placeholder { color: var(--text-3); }
body.mc .search-field:focus-within { border-color: rgba(255,255,255,.24); }
body.mc kbd { font: 500 10.5px var(--f-ui); color: var(--text-2); padding: 3px 6px; border: 1px solid var(--line-subtle); border-radius: 4px; background: var(--surface-800); white-space: nowrap; }
body.mc .suggest { position: absolute; left: 0; right: 0; top: 46px; z-index: 40; padding: 6px; border: 1px solid var(--line-visible); border-radius: 8px; background: var(--surface-900); box-shadow: 0 18px 40px rgba(0,0,0,.5); }
body.mc .suggest a { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 5px; color: var(--text-2); font-size: 12.5px; text-decoration: none; }
body.mc .suggest a:hover, body.mc .suggest a.kb { background: var(--surface-700); color: var(--text-1); }
body.mc .suggest .kind { margin-left: auto; font-size: 10px; letter-spacing: .18em; text-transform: uppercase; color: var(--text-3); }
body.mc .suggest .dot { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--c); }
body.mc .top-right { display: flex; align-items: center; gap: 10px; margin-left: auto; }
body.mc .epoch { display: flex; align-items: center; height: 38px; border: 1px solid var(--line-subtle); border-radius: 8px; background: rgba(11,14,17,.7); }
body.mc .epoch .ep { display: flex; align-items: center; gap: 8px; height: 100%; padding: 0 12px; font-size: 12px; color: var(--text-1); white-space: nowrap; text-decoration: none; }
body.mc .epoch .ep + .ep { border-left: 1px solid var(--line-subtle); }
body.mc .epoch a.ep:hover { color: var(--lum); }
body.mc .epoch svg { width: 14px; height: 14px; stroke: var(--text-2); fill: none; stroke-width: 1.4; }

body.mc main.shell-main { flex: 1; width: 100%; max-width: 1480px; margin: 0; padding: 6px 40px 64px; min-width: 0; background: none; border: 0; box-shadow: none; }
body.mc .crumb { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 10px; padding: 4px 0 6px; font-size: 12px; color: var(--text-3); }
body.mc .crumb p, body.mc .crumb .breadcrumb { margin: 0; font: inherit; color: inherit; letter-spacing: 0; text-transform: none; }
body.mc .crumb a { color: var(--text-2); text-decoration: none; }
body.mc .crumb a:hover { color: var(--text-1); }
body.mc .crumb .sep { color: var(--text-3); margin: 0 6px; }

/* page header: the Map hero's voice -- eyebrow-quiet, wide-tracked title */
body.mc .page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px 32px; flex-wrap: wrap; margin: 10px 0 26px; padding: 0; border: 0; background: none; }
body.mc .page-head-main { min-width: 0; max-width: 820px; }
body.mc h1 { margin: 0 0 10px; font: 300 clamp(22px, 2.3vw, 30px)/1.15 var(--f-ui); letter-spacing: .2em; text-transform: uppercase; color: var(--text-1); overflow-wrap: anywhere; }
body.mc h1.id, body.mc h1 .mono, body.mc h1 code { font-family: var(--f-mono); letter-spacing: -.01em; text-transform: none; }
body.mc .h1-row h1 { margin: 0; }
body.mc h2 { font: 500 15px/1.3 var(--f-ui); letter-spacing: 0; color: var(--text-1); }
body.mc h3 { font: 500 13px/1.35 var(--f-ui); color: var(--text-1); }
body.mc .subtitle, body.mc .page-head p { margin: 0; max-width: 680px; font-size: 13px; line-height: 1.6; color: var(--text-2); }
body.mc p { color: inherit; }
body.mc .eyebrow { font: 500 10px/1 var(--f-ui); letter-spacing: .24em; text-transform: uppercase; color: var(--text-2); }
body.mc .meta, body.mc .muted { color: var(--text-2); font-size: 12px; }
body.mc .mono, body.mc code, body.mc pre, body.mc kbd.mono { font-family: var(--f-mono); }
body.mc td.mono, body.mc span.mono, body.mc div.mono { font-size: 12px; }
body.mc .num { font-variant-numeric: tabular-nums; }

/* buttons: the Map's .btn, primary carries the lum edge */
body.mc .cta-primary, body.mc .btn, body.mc button, body.mc input[type=submit] {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px; height: 36px; padding: 0 14px;
  border: 1px solid var(--line-visible); border-radius: 6px; background: var(--surface-900); color: var(--text-1);
  font: 500 12.5px var(--f-ui); letter-spacing: .01em; text-transform: none; white-space: nowrap; text-decoration: none; box-shadow: none; cursor: pointer;
  transition: border-color .12s, background .12s; }
body.mc .cta-primary, body.mc button[type=submit], body.mc input[type=submit] { border-color: rgba(245,178,107,.5); background: rgba(245,178,107,.08); }
body.mc .cta-primary:hover, body.mc button[type=submit]:hover, body.mc input[type=submit]:hover { background: rgba(245,178,107,.16); color: var(--text-1); }
body.mc .btn:hover, body.mc button:hover { border-color: rgba(180,200,215,.28); background: var(--surface-800); }

/* panels: the Map's .card / .panel */
body.mc .card { position: relative; margin: 0 0 16px; padding: 0; border: 1px solid var(--line-subtle); border-radius: 8px; background: var(--surface-900);
  box-shadow: none; backdrop-filter: none; -webkit-backdrop-filter: none; overflow: hidden; }
body.mc .card::before, body.mc .card::after { display: none; }
body.mc .card-head { display: flex; align-items: center; gap: 12px; margin: 0; padding: 14px 16px; border-bottom: 1px solid var(--line-subtle); background: none; }
body.mc .card-title { font: 500 9.5px/1.4 var(--f-ui); letter-spacing: .24em; text-transform: uppercase; color: var(--text-2); }
body.mc .card-meta { margin-left: auto; font: 400 11.5px var(--f-ui); letter-spacing: 0; text-transform: none; color: var(--text-2); }
body.mc .card-body { padding: 16px; font-size: 13px; line-height: 1.6; color: var(--ink-secondary); }
body.mc .card > p, body.mc .card > .meta, body.mc .card > ul, body.mc .card > form, body.mc .card > div:not(.card-head):not(.card-body):not(.tbl-scroll) { padding-left: 16px; padding-right: 16px; }
body.mc .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr)); gap: 16px; align-items: start; }
body.mc .grid-2 > .card { margin: 0; }

/* readouts: the Map's Model-page strip */
body.mc .stat-grid { display: flex; flex-wrap: wrap; gap: 0; margin: 0 0 16px; padding: 18px 20px; border: 1px solid var(--line-subtle); border-radius: 8px; background: var(--surface-900); }
body.mc .stat-box { flex: 1 1 140px; min-width: 0; margin: 0; padding: 0 22px; border: 0; border-left: 1px solid var(--line-subtle); border-radius: 0; background: none; box-shadow: none; }
body.mc .stat-box:first-child { padding-left: 0; border-left: 0; }
body.mc .stat-box::before, body.mc .stat-box::after { display: none; }
body.mc .stat-num-row { display: flex; align-items: baseline; gap: 8px; }
body.mc .stat-num { font: 300 26px/1 var(--f-ui); letter-spacing: 0; font-variant-numeric: tabular-nums; color: var(--text-1); }
body.mc .stat-label { margin-top: 10px; font: 500 9px/1.3 var(--f-ui); letter-spacing: .24em; text-transform: uppercase; color: var(--text-3); }
body.mc .stat-delta { font-size: 11.5px; color: var(--text-2); }

/* tables: the Map's .dt */
body.mc table { width: 100%; border-collapse: collapse; font: 400 12.5px/1.45 var(--f-ui); background: none; }
body.mc th { padding: 12px 12px 10px; text-align: left; font: 500 9px/1.3 var(--f-ui); letter-spacing: .22em; text-transform: uppercase; color: var(--text-3);
  background: none; border: 0; border-bottom: 1px solid var(--line-subtle); white-space: nowrap; position: static; }
body.mc td { padding: 11px 12px; border: 0; border-bottom: 1px solid var(--line-subtle); vertical-align: middle; color: var(--ink-secondary); background: none; }
body.mc tbody tr { transition: background .12s; }
body.mc tbody tr:hover { background: rgba(180,200,215,.03); }
body.mc tbody tr:last-child td { border-bottom: 0; }
body.mc th.num, body.mc td.num { text-align: right; }
body.mc td a { color: var(--text-1); text-decoration: none; }
body.mc td a:hover { color: var(--lum); }
body.mc td .meta { font-size: 11.5px; color: var(--text-3); }
body.mc .card > table, body.mc .tbl-scroll { display: block; overflow-x: auto; }
body.mc .card > table { display: table; }

/* forms */
body.mc input[type=text], body.mc input[type=number], body.mc input:not([type]), body.mc select, body.mc textarea {
  height: 36px; padding: 0 10px; border: 1px solid var(--line-visible); border-radius: 6px; background: var(--void-900); color: var(--text-1); font: 400 13px var(--f-ui); }
body.mc textarea { height: auto; padding: 8px 10px; }
body.mc .search-field input { height: auto; padding: 0; border: 0; background: none; }
body.mc label { color: var(--text-2); }
body.mc pre, body.mc .code { padding: 12px 14px; border: 1px solid var(--line-subtle); border-radius: 6px; background: var(--void-900); color: var(--ink-secondary); font-size: 12px; }
body.mc details > summary { cursor: pointer; color: var(--text-2); }
body.mc details > summary:hover { color: var(--text-1); }

/* chips and tags keep their meaning, take the Map's shape */
body.mc .chip, body.mc .status-tag, body.mc .badge { border-radius: 999px; font: 500 10.5px/1 var(--f-ui); letter-spacing: .04em; }

/* an id in a title stays an id: code face, its own case */
body.mc .h1-id { display: block; margin-top: 8px; font: 400 clamp(17px, 1.7vw, 22px)/1.25 var(--f-mono); letter-spacing: -.01em; text-transform: none; color: var(--text-1); overflow-wrap: anywhere; }
body.mc .h1-row { align-items: flex-end; }
/* ids lead a cell in code face; the sentence under them is prose */
body.mc td.mono .meta, body.mc td .meta { font-family: var(--f-ui); font-size: 11.5px; line-height: 1.45; color: var(--text-3); }
/* analyst prose (report) */
body.mc .prose-h { margin: 18px 0 8px; font: 500 14px/1.35 var(--f-ui); color: var(--text-1); }
body.mc .prose-h:first-child { margin-top: 0; }
body.mc .prose-sub { margin: 12px 0 4px; font: 500 9.5px/1.4 var(--f-ui); letter-spacing: .22em; text-transform: uppercase; color: var(--text-3); }
body.mc .prose-sub strong { font-weight: 500; }
body.mc .prose-p { margin: 0 0 10px; color: var(--ink-secondary); line-height: 1.65; }
body.mc .prose-list { margin: 0 0 10px; padding-left: 18px; color: var(--ink-secondary); line-height: 1.6; }
body.mc .prose-list li { margin: 3px 0; }
body.mc .callout { margin: 0 0 18px; padding: 12px 16px; border: 1px solid var(--line-visible); border-radius: 10px; background: var(--surface-800); color: var(--text-2); font-size: 13px; line-height: 1.6; }
body.mc .callout b { color: var(--text-1); font-weight: 500; }
body.mc .callout-warn { border-color: rgba(211,169,87,.45); background: rgba(211,169,87,.07); }
body.mc .prose-list li::marker { color: var(--text-3); }
body.mc strong { color: var(--text-1); font-weight: 500; }
/* segmented pickers (atlas query kind / dimension, capability-map models) take the Map's chips */
body.mc .seg-item, body.mc .fm-tab, body.mc .legend-toggle { font-family: var(--f-ui); }
/* preset cards on /launch */
body.mc .preset-card { border: 1px solid var(--line-subtle); border-radius: 8px; background: var(--void-900); }
body.mc .preset-title { font-family: var(--f-mono); }

@media (max-width: 860px) {
  body.mc .app-shell { grid-template-columns: minmax(0,1fr); }
  body.mc .side { position: static; height: auto; flex-direction: row; align-items: center; flex-wrap: wrap; gap: 8px; padding: 12px 0 0; border-right: 0; border-bottom: 1px solid var(--line-subtle); background: rgba(7,9,11,.86); }
  body.mc .brand { padding: 0 16px 10px; width: 100%; }
  body.mc .nav { flex-direction: row; overflow-x: auto; scrollbar-width: none; padding: 0 8px; width: 100%; }
  body.mc .nav::-webkit-scrollbar { display: none; }
  body.mc .nav-bottom { display: none; }
  body.mc .nav-item { height: 40px; padding: 0 12px; gap: 8px; white-space: nowrap; }
  body.mc .nav-item.on::before { left: 12px; right: 12px; top: auto; bottom: 0; width: auto; height: 2px; }
  body.mc .topbar { flex-wrap: wrap; height: auto; gap: 10px; padding: 14px 16px; }
  body.mc .search { flex-basis: 100%; max-width: none; }
  body.mc .top-right { margin-left: 0; }
  body.mc main.shell-main { padding: 4px 16px 48px; }
  body.mc .stat-box { flex: 1 1 40%; padding: 8px 12px 8px 0; border-left: 0; }
  body.mc .stat-num { font-size: 21px; }
  body.mc .card { overflow-x: auto; }
  body.mc .card > table, body.mc main table.matrix { display: block; overflow-x: auto; }
  body.mc span.mono, body.mc code { overflow-wrap: anywhere; }
}
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
def _nav_svg(path: str) -> str:
    return (
        '<svg width="18" height="18" viewBox="0 0 20 20" aria-hidden="true">'
        f"{path}</svg>"
    )


# The Map's own sidebar (static/map.html): same items, order, icons and
# labels, so every page is one app. Launch and Live sit at the bottom.
_NAV_MAIN = [
    ("map", "/", "Map"),
    ("ask", "/#ask", "Ask"),
    ("findings", "/#findings", "Findings"),
    ("models", "/#models", "Models"),
    ("capmap", "/capability-map", "Breaking points"),
    ("atlas", "/atlas", "Atlas"),
    ("runs", "/runs", "Runs"),
    ("report", "/report", "Report"),
    ("coverage", "/coverage", "Coverage"),
]
_NAV_BOTTOM = [
    ("launch", "/launch", "Launch"),
    ("live", "/live", "Live"),
]
_NAV_ICONS = {
    "ask": _nav_svg('<path d="M3.5 4.5h13v8.5h-7l-4 3.2V13h-2z"/><path d="M7 8.2h6M7 10.6h4"/>'),
    "map": _nav_svg('<circle cx="10" cy="10" r="6.5"/><ellipse cx="10" cy="10" rx="9" ry="3" transform="rotate(-20 10 10)"/>'),
    "findings": _nav_svg('<circle cx="10" cy="10" r="7.5"/><circle cx="10" cy="10" r="3.2"/><path d="M10 2.5v2.3M10 15.2v2.3M2.5 10h2.3M15.2 10h2.3"/>'),
    "models": _nav_svg('<circle cx="10" cy="10" r="7.5"/><path d="M6 12.5c1-3 3-5 7-6M7.5 7.2l1.2 1.2M12.2 11.6l1 1"/>'),
    "capmap": _nav_svg('<path d="M3 15l4-5 3 3 3-6 4 8"/><path d="M13 7l1.6-2.6M14.6 4.4l1.4 1"/>'),
    "atlas": _nav_svg('<path d="M3 7h13M13 4l3 3-3 3M17 13H4M7 10l-3 3 3 3"/>'),
    "runs": _nav_svg('<path d="M3 5h4M11 5h6M3 10h9M16 10h1M3 15h2M9 15h8"/><circle cx="9" cy="5" r="1.6"/><circle cx="14" cy="10" r="1.6"/><circle cx="7" cy="15" r="1.6"/>'),
    "report": _nav_svg('<circle cx="10" cy="10" r="7.5"/><path d="M7 12.5V9.5M10 12.5V7M13 12.5v-2"/>'),
    "coverage": _nav_svg('<ellipse cx="10" cy="5" rx="6.5" ry="2.5"/><path d="M3.5 5v10c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5V5M3.5 10c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5"/>'),
    "launch": _nav_svg('<path d="M10 2.5l3 6.5h-6z"/><path d="M7.5 9v5.5h5V9M10 14.5v3"/>'),
    "live": _nav_svg('<circle cx="10" cy="10" r="2.2"/><path d="M5.5 5.5a6.4 6.4 0 0 0 0 9M14.5 5.5a6.4 6.4 0 0 1 0 9M3.2 3.2a9.6 9.6 0 0 0 0 13.6M16.8 3.2a9.6 9.6 0 0 1 0 13.6"/>'),
}
# Pages that belong under a nav item without being it: the console and a
# run's records sit under Runs; a capability detail under Breaking points.
_NAV_ALIAS = {"overview": "runs"}
_NAV_GROUPS = [("", _NAV_MAIN + _NAV_BOTTOM)]  # flat view kept for tests and lookups
_NAV_ITEMS = _NAV_MAIN + _NAV_BOTTOM

_BRAND_MARK = (
    '<svg width="34" height="34" viewBox="0 0 34 34" aria-hidden="true">'
    '<defs><linearGradient id="mcLg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#F5B26B"/>'
    '<stop offset="1" stop-color="#8a5a2c"/></linearGradient></defs>'
    '<circle cx="17" cy="17" r="10.5" fill="none" stroke="url(#mcLg)" stroke-width="1.3"/>'
    '<ellipse cx="17" cy="17" rx="15.5" ry="5" fill="none" stroke="rgba(241,242,243,.55)" stroke-width=".9" transform="rotate(-18 17 17)"/>'
    '<circle cx="29.2" cy="13.1" r="1.4" fill="#F5B26B"/></svg>'
)


def _shell(
    title: str,
    active_nav: str,
    body: str,
    refresh: bool = False,
    breadcrumb: str | None = None,
    trial_count: int | None = None,
) -> str:
    """Render `body` inside the app frame the globe Map uses.

    Same sidebar (items, icons, order, Launch/Live at the bottom) and the
    same command bar (search across models, provinces and trial ids; the
    "Data to" and trial-count readouts) as `static/map.html`, so moving
    between the Map and any server-rendered page never changes the frame.
    The readouts and search fill from `/api/search.json`, the same primary-
    trial population the Map counts. `trial_count` is accepted for call-site
    compatibility; the readout uses the Map's count so the two never differ.
    `breadcrumb`, if given, renders above the page body.
    """
    active = _NAV_ALIAS.get(active_nav, active_nav)
    refresh_tag = '<meta http-equiv="refresh" content="5">' if refresh else ""

    def _items(items):
        on, off = ' class="nav-item on" aria-current="page"', ' class="nav-item"'
        return "".join(
            f'<a{on if key == active else off} href="{href}">{_NAV_ICONS[key]}{label}</a>'
            for key, href, label in items
        )

    sidebar = (
        '<aside class="side">'
        f'<a class="brand" href="/">{_BRAND_MARK}<span class="brand-name">MODEL<br>CARTOGRAPHER</span></a>'
        f'<nav class="nav" aria-label="Primary">{_items(_NAV_MAIN)}</nav>'
        f'<div class="nav nav-bottom">{_items(_NAV_BOTTOM)}</div>'
        "</aside>"
    )
    topbar = (
        '<header class="topbar">'
        '<div class="search" id="mcSearch">'
        '<label class="search-field">'
        '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="5"/><path d="M11 11l3.5 3.5"/></svg>'
        '<input id="mcQ" type="text" autocomplete="off" placeholder="Search models, provinces, or a trial id…" '
        'aria-label="Search models, provinces, or a trial id"><kbd>Ctrl K</kbd></label>'
        '<div class="suggest" id="mcSuggest" hidden></div></div>'
        '<div class="top-right"><div class="epoch">'
        '<span class="ep"><svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><circle cx="7" cy="7" r="5.5"/>'
        '<path d="M7 4v3l2 1.4"/></svg><span id="mcEpoch">Data to …</span></span>'
        '<a class="ep" href="/runs"><span id="mcTrials">… trials</span></a>'
        "</div></div></header>"
    )
    crumb_html = f'<nav class="crumb" aria-label="Breadcrumb">{breadcrumb}</nav>' if breadcrumb else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{refresh_tag}"
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        "family=Google+Sans+Flex:wght@300..700&"
        'family=Google+Sans+Code:wght@400;500&display=swap">'
        f"<title>{html.escape(title)} · Model Cartographer</title><style>{_CSS}</style></head>"
        f'<body class="mc"><div class="app-shell">{sidebar}'
        f'<div class="shell-col">{topbar}<main class="shell-main">{crumb_html}{body}</main></div>'
        f"</div><script>{_INSTRUMENT_JS}</script><script>{_SHELL_JS}</script></body></html>"
    )


# The command bar's behaviour: the same search as the Map's (models,
# provinces, a trial-id prefix) and its two readouts.
_SHELL_JS = r"""
(function(){
  var q=document.getElementById('mcQ'), box=document.getElementById('mcSuggest'), idx=null, hits=[], kb=-1, seq=0;
  var MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function load(){ if(idx) return Promise.resolve(idx);
    return fetch('/api/search.json',{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){idx=d; return d;}); }
  load().then(function(d){
    if(d.newest){var p=d.newest.slice(0,10).split('-'); document.getElementById('mcEpoch').textContent='Data to '+MON[+p[1]-1]+' '+(+p[2]);}
    else document.getElementById('mcEpoch').textContent='No data yet';
    document.getElementById('mcTrials').textContent=d.trials.toLocaleString('en-US')+' trials';
  }).catch(function(){ document.getElementById('mcEpoch').textContent='Data unavailable'; });
  function draw(){
    if(!hits.length){ box.hidden=true; box.innerHTML=''; return; }
    box.innerHTML=hits.map(function(h,i){ return '<a href="'+h.href+'"'+(i===kb?' class="kb"':'')+'><i class="dot" style="--c:'+h.c+'"></i>'+esc(h.t)+'<span class="kind">'+h.k+'</span></a>'; }).join('');
    box.hidden=false;
  }
  function run(){
    var s=q.value.trim().toLowerCase(), my=++seq; kb=-1;
    if(!s){ hits=[]; draw(); return; }
    load().then(function(d){
      if(my!==seq) return;
      hits=d.models.filter(function(m){return (m.name+' '+m.id).toLowerCase().indexOf(s)>=0;}).slice(0,6)
        .map(function(m){return {t:m.name, k:'model', c:'#E8EDF1', href:'/#model.'+m.slug+'.overview'};})
        .concat(d.provinces.filter(function(p){return p.name.toLowerCase().indexOf(s)>=0;})
        .map(function(p){return {t:p.name, k:'province', c:p.color, href:'/#findings'};}));
      draw();
      if(/^[0-9a-f]{4,32}$/.test(s)) fetch('/api/search.json?q='+encodeURIComponent(s),{cache:'no-store'})
        .then(function(r){return r.json();}).then(function(t){ if(my!==seq) return;
          hits=hits.concat(t.trials.map(function(x){return {t:x.id.slice(0,12)+' · '+x.model, k:'trial', c:'#8B98A4', href:'/record/'+x.id};})); draw(); });
    });
  }
  q.addEventListener('input', run);
  q.addEventListener('focus', function(){ load(); if(q.value) run(); });
  q.addEventListener('keydown', function(e){
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){ if(!hits.length) return; e.preventDefault();
      kb=(kb+(e.key==='ArrowDown'?1:-1)+hits.length)%hits.length; draw(); }
    else if(e.key==='Enter'){ var h=hits[kb<0?0:kb]; if(h) location.href=h.href; }
    else if(e.key==='Escape'){ hits=[]; draw(); q.blur(); }
  });
  document.addEventListener('click', function(e){ if(!document.getElementById('mcSearch').contains(e.target)){ hits=[]; draw(); } });
  document.addEventListener('keydown', function(e){ if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){ e.preventDefault(); q.focus(); } });
})();
"""


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
        scored = passn + counts.get("FAIL", 0)
        errn = counts.get("ERROR", 0)
        pass_rate = (passn / scored * 100.0) if scored else 0.0
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
        row_cls = ' class="row-sev row-sev-fail"' if scored and pass_rate < _RATE_WARN_THRESHOLD else ""
        status_chip = (
            _chip("live", "chip-live") if is_live else _chip("complete", "chip-done chip-muted")
        )
        rows_html.append(
            f"<tr{row_cls}>"
            f'<td class="mono"><a href="/run/{safe_run_id}">{safe_run_id}</a>{headline_html}</td>'
            f'<td title="{html.escape(agg["model"])}">{model_short}</td>'
            f'<td class="num">{total}</td>'
            f'<td class="num" title="passed / scored trials; errors are not scored">'
            f'{_rate_bar(pass_rate, sub=f"{passn}/{scored}" + (f" · {errn} err" if errn else "")) if scored else "–"}</td>'
            f'<td>{spark}</td>'
            f'<td class="mono num">${agg["cost"]:.4f}</td>'
            f"<td>{html.escape(_relative_time(newest))}</td>"
            f"<td>{status_chip}</td>"
            "</tr>"
        )
    return rows_html, by_run


def _primary_trial_count(store: ResultsStore) -> int:
    """Trials that count as evidence (findings._primary_records): the number
    the top bar, the Map and Findings all show."""
    import json as _json

    return int(_json.loads(search_json(store))["trials"])


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

    report = load_report()
    body = report_body(report)
    # The report is a snapshot the analyst wrote offline; say so when the
    # store has moved on, or its rankings read as contradicting the Map.
    snap = (report or {}).get("store_totals") or {}
    now_trials = _primary_trial_count(store)
    if report is not None and snap.get("n_trials") and snap["n_trials"] != now_trials:
        import json as _json

        now_models = len(_json.loads(search_json(store))["models"])
        when = str((report.get("meta") or {}).get("generated_at", ""))[:10]
        banner = (
            '<div class="callout callout-warn" role="note"><b>Snapshot, not live.</b> '
            f'The analyst wrote this on {html.escape(when)} from {snap["n_trials"]:,} trials across '
            f'{snap.get("n_models", "?")} models. The store now holds {now_trials:,} trials across '
            f'{now_models} models, so its rankings can differ from the Map, which is always current. '
            'Regenerate with <span class="mono">scripts/write_report.py</span>.</div>'
        )
        body = body.replace("</div></div>", "</div></div>" + banner, 1)
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
    n_primary = _primary_trial_count(store)
    total_spend = sum(agg["cost"] for agg in by_run.values())
    n_models = len({agg["model"] for agg in by_run.values() if agg["model"]})
    stat_grid = (
        '<div class="stat-grid">'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_runs}</div></div>'
        '<div class="stat-label">Runs recorded</div></div>'
        f'<div class="stat-box"><div class="stat-num-row"><div class="stat-num">{n_primary:,}</div>'
        f'<div class="stat-delta">+{max(n_trials - n_primary, 0):,} checks</div></div>'
        '<div class="stat-label" title="Trials that count as evidence, the same number as the top bar. The extra records are resample checks and replications, listed under their runs below.">Trials</div></div>'
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


# ---------------------------------------------------------------------------
# Home console chart primitives. Deliberately plain: hairline axes, straight
# segments between measured points, hollow markers on real data, flat bars,
# no glow, no gradients. Hue appears only as the tag -- a rate below the
# broken-regime threshold (_RATE_WARN_THRESHOLD).
# ---------------------------------------------------------------------------
_CV_INK = ("#ececee", "#a1a1a8", "#8e8e97")
_CV_TAG = "#f26b1d"
_CV_HAIR = "rgba(255,255,255,0.06)"
_CV_BASE = "rgba(255,255,255,0.16)"
_CV_BG = "#0d0e11"
_KIND_ORDER = ["state", "order", "belief", "tool_plan"]
_KIND_LABEL = {"state": "State", "order": "Order", "belief": "Belief", "tool_plan": "Tool planning"}
_KIND_SHORT = {"tool_plan": "Tool plan"}
_KIND_ORDER.append("code_trace")
_KIND_LABEL["code_trace"] = "Code tracing"
_KIND_SHORT["code_trace"] = "Code trace"


def _cv_low(rate: float) -> bool:
    return rate * 100.0 < _RATE_WARN_THRESHOLD


def _cv_spark(vals: list[float], w: int = 92, h: int = 36) -> str:
    if len(vals) < 2:
        return f'<svg width="{w}" height="{h}" aria-hidden="true"></svg>'
    lo, hi = min(vals), max(vals)
    pts = [(3 + i * (w - 8) / (len(vals) - 1), h - 5 - (v - lo) / ((hi - lo) or 1) * (h - 12)) for i, v in enumerate(vals)]
    pl = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    ex, ey = pts[-1]
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true">'
            f'<line x1="0" y1="{h-1}" x2="{w}" y2="{h-1}" stroke="{_CV_HAIR}"/>'
            f'<polyline points="{pl}" fill="none" stroke="{_CV_INK[1]}" stroke-width="1.25" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="2.2" fill="{_CV_INK[0]}"/></svg>')


def _cv_axes(x0: float, x1: float, ys) -> str:
    out = ""
    for p in (0.0, 0.5, 1.0):
        out += f'<line x1="{x0}" y1="{ys(p):.1f}" x2="{x1}" y2="{ys(p):.1f}" stroke="{_CV_BASE if p == 0 else _CV_HAIR}"/>'
        out += (f'<text x="{x0-6}" y="{ys(p)+3.5:.1f}" fill="#8e8e97" font-family="Geist Mono, monospace" '
                f'font-size="10" text-anchor="end">{int(p*100)}</text>')
    return out


def _cv_bars(items: list[tuple[str, float]], w: int = 380, h: int = 140) -> str:
    """One flat bar per campaign, oldest first. Tag = below the broken threshold."""
    if not items:
        return '<p class="meta">No scored campaigns yet.</p>'
    x0, x1, y0, y1 = 30, w - 4, 8, h - 20
    slot = (x1 - x0) / len(items)
    bw = max(2.5, slot * 0.5)
    ys = lambda p: y1 - p * (y1 - y0)
    out = _cv_axes(x0, x1, ys)
    for i, (label, p) in enumerate(items):
        x = x0 + i * slot + (slot - bw) / 2
        top = ys(p)
        out += (f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{y1-top:.1f}" rx="0.8" '
                f'fill="{_CV_TAG if _cv_low(p) else "#6b6c73"}"><title>{html.escape(label)} · {p*100:.0f}%</title></rect>')
    out += (f'<text x="{x0}" y="{h-4}" fill="#8e8e97" font-family="Geist Mono, monospace" font-size="10">oldest</text>'
            f'<text x="{x1}" y="{h-4}" fill="#8e8e97" font-family="Geist Mono, monospace" font-size="10" text-anchor="end">newest</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Pass rate for each campaign, oldest to newest; '
            f'orange bars are below {int(_RATE_WARN_THRESHOLD)} percent.">{out}</svg>')


def _cv_line(points: list[tuple[str, float]], w: int = 380, h: int = 140) -> str:
    """Daily pass rate: straight segments, a hollow marker on each measured day."""
    if len(points) < 2:
        return '<p class="meta">Needs at least two days of trials.</p>'
    x0, x1, y0, y1 = 30, w - 12, 8, h - 20
    xs = lambda i: x0 + i * (x1 - x0) / (len(points) - 1)
    ys = lambda p: y1 - p * (y1 - y0)
    out = _cv_axes(x0, x1, ys)
    pts = [(xs(i), ys(p)) for i, (_, p) in enumerate(points)]
    out += f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pts)}" fill="none" stroke="{_CV_INK[0]}" stroke-width="1.25" stroke-linejoin="round"/>'
    for (x, y), (label, p) in zip(pts, points):
        col = _CV_TAG if _cv_low(p) else _CV_INK[0]
        out += (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.3" fill="{_CV_BG}" stroke="{col}" stroke-width="1.25">'
                f'<title>{html.escape(label)} · {p*100:.0f}%</title></circle>')
    out += (f'<text x="{x0}" y="{h-4}" fill="#8e8e97" font-family="Geist Mono, monospace" font-size="10">{html.escape(points[0][0][5:])}</text>'
            f'<text x="{x1}" y="{h-4}" fill="#8e8e97" font-family="Geist Mono, monospace" font-size="10" text-anchor="end">{html.escape(points[-1][0][5:])}</text>')
    return f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Pass rate per day of trials.">{out}</svg>'


def _cv_radar(profile: dict, models: list[str], kinds: list[str], w: int = 440, h: int = 350) -> str:
    """Capability profile: one spoke per query kind, radius = scored pass rate.
    Models are told apart by ink level; a spoke below the broken threshold
    gets the tag diamond."""
    if not models or not kinds:
        return '<p class="meta">No scored trials yet.</p>'
    cx, cy, R = w / 2, h / 2 + 2, min(w, h) / 2 - 32
    n = len(kinds)
    ang = lambda i: math.radians(-90 + i * 360 / n)
    out = ""
    for t in (0.25, 0.5, 0.75, 1.0):
        ring = " ".join(f"{cx + R*t*math.cos(ang(i)):.1f},{cy + R*t*math.sin(ang(i)):.1f}" for i in range(n)) if n >= 3 else ""
        if n >= 3:
            out += f'<polygon points="{ring}" fill="none" stroke="{_CV_BASE if t == 1 else _CV_HAIR}"/>'
        out += (f'<text x="{cx+4}" y="{cy - R*t - 3:.1f}" fill="#8e8e97" font-family="Geist Mono, monospace" '
                f'font-size="9.5">{int(t*100)}</text>')
    for i, k in enumerate(kinds):
        ex, ey = cx + R * math.cos(ang(i)), cy + R * math.sin(ang(i))
        out += f'<line x1="{cx}" y1="{cy}" x2="{ex:.1f}" y2="{ey:.1f}" stroke="{_CV_HAIR}"/>'
        lx, ly = cx + (R + 18) * math.cos(ang(i)), cy + (R + 18) * math.sin(ang(i))
        c = math.cos(ang(i))
        anchor = "middle" if abs(c) < 0.3 else ("start" if c > 0 else "end")
        out += (f'<text x="{lx:.1f}" y="{ly+4:.1f}" fill="#a1a1a8" font-family="Geist, sans-serif" font-size="11.5" '
                f'text-anchor="{anchor}">{_KIND_SHORT.get(k, _KIND_LABEL.get(k, k))}</text>')
    for mi, m in enumerate(models):
        col = _CV_INK[mi % len(_CV_INK)]
        pts = []
        for i, k in enumerate(kinds):
            p = profile.get((m, k))
            if p is None:
                continue
            pts.append((cx + R * p * math.cos(ang(i)), cy + R * p * math.sin(ang(i)), p, k))
        if len(pts) >= 2:
            closed = pts + ([pts[0]] if len(pts) == n and n >= 3 else [])
            out += (f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in closed)}" fill="none" '
                    f'stroke="{col}" stroke-width="1.25" stroke-linejoin="round" stroke-opacity="0.9"/>')
        for x, y, p, k in pts:
            tip = f"{html.escape(m)} · {_KIND_LABEL.get(k, k)} · {p*100:.0f}%"
            if _cv_low(p):
                out += (f'<rect x="{x-3.5:.1f}" y="{y-3.5:.1f}" width="7" height="7" fill="{_CV_TAG}" '
                        f'transform="rotate(45 {x:.1f} {y:.1f})"><title>{tip}</title></rect>')
            else:
                out += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{col}" stroke="{_CV_BG}" stroke-width="1.2"><title>{tip}</title></circle>'
    out += f'<path d="M{cx-6},{cy} h12 M{cx},{cy-6} v12" stroke="#8e8e97" stroke-width="1"/>'
    return f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Capability profile: scored pass rate per query kind for each model.">{out}</svg>'


def index_html(store: ResultsStore) -> str:
    """Home console: a one-line thesis, then a glass console of live modules
    computed from the store (totals with trends, a capability profile per
    model, findings and recent runs, per-campaign and per-day pass rates, the
    model x query-kind matrix), then links into every section."""
    rows = store._conn.execute(
        "SELECT run_id, verdict, cost_usd, verifier_detail, model_name, created_at FROM experiments ORDER BY created_at"
    ).fetchall()
    total_trials = len(rows)
    total_spend = sum(r["cost_usd"] or 0.0 for r in rows)

    by_model_kind: dict[tuple[str, str], dict[str, int]] = {}
    days: dict[str, dict[str, float]] = {}
    camps: dict[str, dict] = {}
    for r in rows:
        kind = _query_kind_from_detail(r["verifier_detail"])
        model = _short_model(r["model_name"])
        v = r["verdict"]
        agg = by_model_kind.setdefault((model, kind), {"n": 0, "pass": 0, "scored": 0})
        agg["n"] += 1
        agg["pass"] += v == "PASS"
        agg["scored"] += v in ("PASS", "FAIL")
        day = (r["created_at"] or "")[:10]
        d = days.setdefault(day, {"n": 0, "cost": 0.0, "pass": 0, "scored": 0})
        d["n"] += 1
        d["cost"] += r["cost_usd"] or 0.0
        d["pass"] += v == "PASS"
        d["scored"] += v in ("PASS", "FAIL")
        top = (r["run_id"] or "").split("/")[0]
        c = camps.setdefault(top, {"first": r["created_at"] or "", "pass": 0, "scored": 0})
        c["pass"] += v == "PASS"
        c["scored"] += v in ("PASS", "FAIL")

    scored_cells = {k: a for k, a in by_model_kind.items() if k[1] != "-"}
    n_models = len({m for (m, _k) in scored_cells})
    n_scored = sum(a["scored"] for a in scored_cells.values())
    n_passed = sum(a["pass"] for a in scored_cells.values())
    overall_rate = (n_passed / n_scored * 100.0) if n_scored else 0.0

    day_keys = sorted(k for k in days if k)
    cum_trials, cum_spend, run_t, run_s = [], [], 0, 0.0
    for k in day_keys:
        run_t += days[k]["n"]
        run_s += days[k]["cost"]
        cum_trials.append(run_t)
        cum_spend.append(run_s)
    daily_rate = [(k, days[k]["pass"] / days[k]["scored"]) for k in day_keys if days[k]["scored"]]
    camp_items = [(rid, c["pass"] / c["scored"]) for rid, c in sorted(camps.items(), key=lambda kv: kv[1]["first"]) if c["scored"] >= 10]
    n_low = sum(1 for _, p in camp_items if _cv_low(p))

    model_n: dict[str, int] = {}
    for (m, _k), a in scored_cells.items():
        model_n[m] = model_n.get(m, 0) + a["scored"]
    models = [m for m, _ in sorted(model_n.items(), key=lambda kv: -kv[1])][:3]

    def pretty(m: str) -> str:
        """Display name for legends: 'NVIDIA-Nemotron-3-Nano-30B-A3B' -> 'Nano 30B'."""
        low = m.lower()
        for tier in ("nano", "super", "ultra", "lightning"):
            if tier in low:
                size = re.search(r"(\d+)b", low.split(tier, 1)[1])
                return f"{tier.capitalize()} {size.group(1)}B" if size else tier.capitalize()
        return m
    kinds = [k for k in _KIND_ORDER if any((m, k) in scored_cells and scored_cells[(m, k)]["scored"] for m in models)]
    profile = {(m, k): scored_cells[(m, k)]["pass"] / scored_cells[(m, k)]["scored"]
               for m in models for k in kinds if (m, k) in scored_cells and scored_cells[(m, k)]["scored"]}

    def stat(label: str, value: str, sub: str, series: list[float]) -> str:
        return (f'<div class="mod stat-mod"><div><div class="k">{label}</div><div class="v">{value}</div>'
                f'<div class="s">{sub}</div></div>{_cv_spark(series)}</div>')

    left = (stat("Total trials", f"{total_trials:,}", f"{len(camps)} campaigns", [float(x) for x in cum_trials])
            + stat("Overall pass rate", f"{overall_rate:.0f}%", f"{n_passed:,}/{n_scored:,} scored", [p for _, p in daily_rate])
            + stat("Total spend", f"${total_spend:.2f}", "tracked, all runs", cum_spend)
            + stat("Models mapped", f"{n_models}", f"{len(kinds)} query kinds", [float(x) for x in cum_trials]))

    if profile:
        (wm, wk), wp = min(profile.items(), key=lambda kv: kv[1])
        best = max(models, key=lambda m: sum(profile.get((m, k), 0) for k in kinds) / max(1, sum((m, k) in profile for k in kinds)))
        best_avg = sum(profile.get((best, k), 0) for k in kinds) / max(1, sum((best, k) in profile for k in kinds))
        n_cells_low = sum(1 for p in profile.values() if _cv_low(p))
        readouts = (f'<div class="readouts">'
                    f'<div><div class="k">Weakest cell</div><div class="v{" low" if _cv_low(wp) else ""}">{wp*100:.0f}%</div><div class="s">{html.escape(pretty(wm))} · {_KIND_LABEL.get(wk, wk)}</div></div>'
                    f'<div><div class="k">Strongest model</div><div class="v">{best_avg*100:.0f}%</div><div class="s">{html.escape(pretty(best))}, mean of kinds</div></div>'
                    f'<div><div class="k">Cells below {int(_RATE_WARN_THRESHOLD)}%</div><div class="v{" low" if n_cells_low else ""}">{n_cells_low}</div><div class="s">of {len(profile)} model × kind</div></div></div>')
    else:
        readouts = ""
    legend = "".join(f'<span><i style="background:{_CV_INK[i % 3]}"></i>{html.escape(pretty(m))}</span>' for i, m in enumerate(models))
    legend += f'<span><i style="background:{_CV_TAG}; width: 7px; height: 7px; transform: rotate(45deg)"></i>below {int(_RATE_WARN_THRESHOLD)}%</span>'
    center = (f'<div class="mod profile-mod">'
              f'<div class="mod-head">Capability profile<span class="aside">scored pass rate by query kind</span></div>'
              f'{_cv_radar(profile, models, kinds)}<div class="legend">{legend}</div>{readouts}</div>')

    finding_rows = "".join(
        f'<div class="list-row"><span class="dot {"ok" if "confirm" in f.status.lower() else "hollow"}"></span>'
        f'<span>{html.escape(f.name)}<span class="sub">{html.escape(f.status)}</span></span></div>'
        for f in _confirmed_findings_preview()
    ) or '<p class="meta">No confirmed findings yet.</p>'
    _, by_run = _runs_table_rows(store)
    recent = sorted(by_run.keys(), key=lambda rid: max(by_run[rid]["created_ats"]) if by_run[rid]["created_ats"] else "", reverse=True)[:4]
    run_rows = ""
    for rid in recent:
        headline = _verdict_headline_safe(store, rid) or ""
        # The verdict sentence opens by restating the run id; the row already shows it.
        headline = re.sub(r"^Run\s+" + re.escape(rid) + r":\s*", "", headline)
        headline = headline[:1].upper() + headline[1:]
        ats = by_run[rid]["created_ats"]
        when = (max(ats) if ats else "")[5:16].replace("T", " ")
        run_rows += (f'<div class="list-row"><span class="dot"></span><span class="run-cell"><a href="/run/{html.escape(rid)}" class="mono run-id" title="{html.escape(rid)}">{html.escape(rid)}</a>'
                     f'<span class="sub clamp">{html.escape(headline or "No verdict yet")}</span></span><span class="when">{html.escape(when)}</span></div>')
    right = (f'<div class="mod"><div class="mod-head">What we\'ve found<a class="aside" href="/findings">Findings &rarr;</a></div>{finding_rows}</div>'
             f'<div class="mod"><div class="mod-head">Recent runs<a class="aside" href="/runs">All runs &rarr;</a></div>'
             f'{run_rows or "<p class=meta>No runs yet.</p>"}</div>')

    header = "".join(f"<th class=\"num\">{_KIND_LABEL.get(k, k)}</th>" for k in kinds)
    body_rows = ""
    for m in models:
        cells = ""
        for k in kinds:
            a = scored_cells.get((m, k))
            if not a or not a["scored"]:
                cells += '<td class="num">&mdash;</td>'
                continue
            p = a["pass"] / a["scored"]
            cells += f'<td class="num{" low" if _cv_low(p) else ""}" title="{a["pass"]}/{a["scored"]}">{p*100:.0f}%</td>'
        body_rows += f"<tr><td>{html.escape(pretty(m))}</td>{cells}</tr>"
    matrix = (f'<table class="matrix"><thead><tr><th>Model</th>{header}</tr></thead><tbody>{body_rows}</tbody></table>'
              if models else '<p class="meta">No trials recorded yet.</p>')

    bottom = (f'<div class="mod"><div class="mod-head">Pass rate by campaign<span class="aside">{len(camp_items)} runs · {n_low} below {int(_RATE_WARN_THRESHOLD)}%</span></div>{_cv_bars(camp_items)}</div>'
              f'<div class="mod"><div class="mod-head">Pass rate by day<span class="aside">{len(daily_rate)} days</span></div>{_cv_line(daily_rate)}</div>'
              f'<div class="mod"><div class="mod-head">Pass rate by query kind<span class="aside">top {len(models)} models</span></div>{matrix}</div>')

    links = "".join(
        f'<a class="link-card" href="{href}"><span class="t">{html.escape(label)}<span aria-hidden="true">&rarr;</span></span>'
        f"<p>{html.escape(desc)}</p></a>"
        for href, label, desc in _QUICK_LINKS
    )
    now = _now_utc().strftime("%Y-%m-%d %H:%M")
    body = f"""
    <div class="hero">
      <div>
        <h1>Capability frontiers,<br><span class="dim">mapped in simulation.</span></h1>
        <p class="lede">Model Cartographer escalates tasks until a model breaks, then shows why. Ground truth comes
        from running the world, never from another model's opinion.</p>
        <div class="rules">DETERMINISTIC &nbsp;/&nbsp; REPLICATED &nbsp;/&nbsp; AUDITED</div>
      </div>
      <a class="cta-primary" href="/launch">Map a model &rarr;</a>
    </div>
    <section class="console" aria-label="Overview console">
      <div class="console-bar"><span class="title">Overview</span>
        <span><span class="live-dot"></span>Store live</span>
        <span class="right"><span class="mono">UTC {now}</span><a href="/live">Live run &rarr;</a></span></div>
      <div class="console-grid"><div class="col">{left}</div>{center}<div class="col">{right}</div></div>
      <div class="console-row">{bottom}</div>
    </section>
    <div class="links-grid">{links}</div>
    """
    return _shell("Console", "overview", body, trial_count=_trial_count(store))


def _query_kind_from_detail(detail: str | None) -> str:
    parsed = _parse_verifier_detail(detail)
    return parsed.get("kind", "-")


def _confirmed_findings_preview():
    from .findings import FINDINGS

    return [f for f in FINDINGS if f.status == "confirmed"][:4]


def _short_model(model_name: str) -> str:
    """The Map's display name ("Gemma 4 31B QAT"), so a model reads the same on every page."""
    return display_name(model_name) if model_name else "-"


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


def _config_short(realized_dims: dict, keep: int = 4) -> str:
    """The first few dims of a record's config, the rest counted (full list on hover)."""
    if not realized_dims:
        return "-"
    parts = [f"{k}={v:g}" if isinstance(v, float) else f"{k}={v}" for k, v in sorted(realized_dims.items())]
    more = len(parts) - keep
    return ", ".join(parts[:keep]) + (f" +{more} more" if more > 0 else "")


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
    if HOSTED:
        return ""
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
    stages = child_stages(store, run_id) if not records else []
    if not records:
        for stage in stages:
            child = store.by_run(f"{run_id}/{stage}")
            if child:
                is_full_map_parent = True
                records.extend(child)
        records.sort(key=lambda r: (r.created_at or "", r.record_id))

    if is_full_map_parent:
        verify_records = [
            r
            for stage in stages
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
          <h1>Live <span class="h1-id">{safe_run_id}</span></h1>
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
    pass_rate = (passn / (passn + failn) * 100.0) if (passn + failn) else 0.0
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
        f'<div class="stat-delta">{passn}/{passn + failn}</div></div><div class="stat-label" title="passed / scored trials; errors are not scored">Pass rate</div></div>'
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
            f'<td class="mono" title="{html.escape(_config_summary(r.realized_dims))}">{html.escape(_config_short(r.realized_dims))}</td>'
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
    if complete:
        ended = _relative_time(max(timestamps)) if timestamps else "earlier"
        live_head = (f'<h1>Last run <span class="h1-id">{safe_run_id}</span></h1>'
                     f'<p class="subtitle">No campaign is running right now. This is the most recent run; '
                     f'it finished {html.escape(ended)}. Start one from Launch and this page follows it live.</p>')
    else:
        live_head = (f'<h1>Live <span class="h1-id">{safe_run_id}</span></h1>'
                     '<p class="subtitle">Following the running campaign; this page refreshes every few seconds.</p>')
    body = f"""
    <div class="page-head"><div class="page-head-main">
      {live_head}
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
    return _shell(f"{'Last run' if complete else 'Live'}: {run_id}", "live", body, refresh=not complete, trial_count=_trial_count(store))


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
    stages = child_stages(store, run_id) if not records else []
    if not records:
        for stage in stages:
            child = store.by_run(f"{run_id}/{stage}")
            if child:
                is_full_map_parent = True
                records.extend(child)

    breadcrumb = f'<a href="/runs">Runs</a><span class="sep">/</span>{safe_run_id}'

    if not records:
        body = (
            '<div class="page-head"><div class="page-head-main">'
            f'<h1>Run <span class="h1-id">{safe_run_id}</span></h1></div>'
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
        [f"{run_id}/{s}" for s in stages] if is_full_map_parent else [run_id]
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
    if n_unverified_fails and not HOSTED:
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
        f'<div class="h1-row"><h1>Run <span class="h1-id">{safe_run_id}</span></h1>{_title_pill_html(records)}</div>'
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


def _explain_card(record: ExperimentRecord) -> str:
    """On a failed trial: ask Nemotron 3 Ultra, live on Token Factory, why it failed."""
    if record.verdict.name != "FAIL":
        return ""
    rid = html.escape(record.record_id)
    return f"""
    <div class="card" id="explain">
      <div class="card-head"><span class="card-title">Why did it fail?</span>
        <span class="card-meta">Nemotron 3 Ultra &middot; live on Nebius Token Factory</span></div>
      <div class="card-body">
        <p class="meta" style="margin:0 0 10px">Ultra reads the prompt, this answer, the verified correct answer and the difficulty settings, then explains the mistake. The verifier's own classification is shown next to it for comparison.</p>
        <button class="cta-primary" id="explainGo" type="button">Explain with Nemotron 3 Ultra</button>
        <div id="explainOut" style="margin-top:12px"></div>
      </div>
    </div>
    <script>
    (function(){{
      var b = document.getElementById('explainGo'), out = document.getElementById('explainOut');
      function esc(s){{ return String(s).replace(/[&<>"]/g, function(c){{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
      b.addEventListener('click', function(){{
        b.disabled = true; b.textContent = 'Nemotron 3 Ultra is reasoning…'; out.innerHTML = '';
        var t0 = performance.now();
        fetch('/api/explain?record={rid}').then(function(r){{ return r.json().then(function(d){{ return [r.ok, d]; }}); }}).then(function(x){{
          var ok = x[0], d = x[1];
          if (!ok || d.error) throw new Error(d.error || 'Request failed');
          var c = d.call;
          out.innerHTML = '<p class="prose-p" style="color:var(--text-1);font-size:14px;white-space:pre-line">' + esc(d.explanation) + '</p>'
            + (d.truth ? '<p class="meta">verified correct answer: <b>' + esc(d.truth) + '</b> &middot; model answered: <b>' + esc(d.answer) + '</b>' + (d.trap ? ' &middot; verifier classification: <span class="mono">' + esc(d.trap) + '</span>' : '') + '</p>' : '')
            + '<p class="meta mono">' + esc(c.tier) + ' &middot; ' + c.latency_s.toFixed(1) + ' s &middot; ' + c.tokens_in + ' → ' + c.tokens_out + ' tokens &middot; reasoning ' + c.reasoning_chars + ' chars &middot; $' + c.cost_usd.toFixed(5) + ' &middot; ' + ((performance.now() - t0)/1000).toFixed(1) + ' s end to end</p>';
          b.textContent = 'Explained by Nemotron 3 Ultra';
        }}).catch(function(e){{ out.innerHTML = '<p class="meta" style="color:#F2A0A8">' + esc(e.message) + '</p>'; b.disabled = false; b.textContent = 'Explain with Nemotron 3 Ultra'; }});
      }});
    }})();
    </script>"""


def record_html(store: ResultsStore, record_id: str) -> str:
    cur = store._conn.execute("SELECT * FROM experiments WHERE record_id = ?", (record_id,))
    row = cur.fetchone()
    safe_record_id = html.escape(record_id)

    if row is None:
        body = f'<h1>Record <span class="h1-id">{safe_record_id}</span></h1><div class="empty-state"><p>Not found.</p></div>'
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
    <div class="h1-row"><h1>Record <span class="h1-id">{safe_record_id}</span></h1><span class="title-pill {_r_pill_cls}">{html.escape(record.verdict.name)}</span></div>
    <p class="subtitle">Verdict and provenance first; raw prompt/response/reasoning below, collapsed.</p>
    </div><div class="page-head-actions">
      <a href="/run/{html.escape(record.run_id)}">back to run &rarr;</a>
    </div></div>

    <div class="panel">
      <p style="margin:0 0 0.5rem;">
        {_claim_strip(record.verdict.name, _r_evidence)}
        <span class="meta">&middot; model <b title="{html.escape(record.model_name)}">{html.escape(display_name(record.model_name))}</b></span>
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

    {_explain_card(record)}

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

    def _send_json(self, obj: dict, status: int = 200, cache: bool = False) -> None:
        import json as _json

        payload = _json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", _API_CACHE if cache else "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_ask(self, path: str) -> None:
        """Live Nemotron calls on Token Factory (see viewer/ask.py)."""
        from . import ask

        q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        ip = (self.headers.get("x-forwarded-for") or self.headers.get("x-real-ip") or self.client_address[0]).split(",")[0].strip()
        try:
            ask.check_limits(ip)
            if path == "/api/explain":
                out = ask.explain(self.store, q.get("record", ""))
            elif q.get("step") == "plan":
                out = ask.plan(self.store, q.get("q", ""))
                out["labels"] = ask.facts(self.store)[2]
            else:
                split = lambda v: [x for x in (v or "").split("|") if x]  # noqa: E731
                out = ask.answer(self.store, q.get("q", ""), split(q.get("models")), split(q.get("topics")))
        except ask.AskUnavailable as e:
            self._send_json({"error": str(e)}, status=503)
            return
        self._send_json(out, cache=True)

    def _redirect(self, location: str, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if HOSTED and status == 200:
            # The store is frozen per deploy; let the CDN serve repeat views.
            self.send_header("Cache-Control", "public, max-age=0, s-maxage=86400, stale-while-revalidate=604800")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 -- required BaseHTTPRequestHandler name
        path = urlsplit(self.path).path

        if path == "/":
            # The globe Map; it fetches /api/map.json and draws client-side.
            self._send_html(_MAP_PAGE.read_text(encoding="utf-8"))
            return

        if path == "/api/map.json":
            payload = payload_json(self.store)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", _API_CACHE)
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/api/search.json":
            payload = search_json(self.store, (parse_qs(urlsplit(self.path).query).get("q") or [""])[0])
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", _API_CACHE)
            self.end_headers()
            self.wfile.write(payload)
            return

        if path in ("/api/ask", "/api/explain"):
            self._send_ask(path)
            return

        if path == "/capability-map":
            # Text verdicts per model ("where does it break"): ?model=<id or short name>, ?compare=1.
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            self._send_html(capability_map_html(self.store, model=q.get("model"), compare=q.get("compare") == "1"))
            return

        if path == "/overview":
            # The old Console duplicated the Map with different counts; the Map is home.
            self._redirect("/")
            return

        if path == "/frontier":
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            if not q.get("model"):
                self._redirect("/capability-map")
                return
            self._send_html(frontier_html(self.store, model=q.get("model"), cap=q.get("cap")))
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
            body = _HOSTED_LAUNCH_HTML if HOSTED else launch_mod.handle_launch_get(self.launch_deps)
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

        if HOSTED:
            self._send_html(_shell("Read-only", "", _HOSTED_LAUNCH_HTML, trial_count=0), status=403)
            return

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
    # `--no-browser` skips the auto-open (for embedded previews and CI);
    # the `modelcart` launcher omits it and keeps opening a tab.
    open_browser = "--no-browser" not in sys.argv
    args = [a for a in sys.argv[1:] if a != "--no-browser"]
    db_path = args[0] if len(args) > 0 else _DEFAULT_DB_PATH
    port = int(args[1]) if len(args) > 1 else _DEFAULT_PORT

    # Resolve relative paths against the repo root, not the caller's cwd,
    # so `modelcart` works the same from anywhere on disk.
    resolved = Path(db_path)
    if not resolved.is_absolute() and not resolved.exists():
        candidate = _REPO_ROOT / db_path
        if candidate.exists():
            resolved = candidate
    db_path = str(resolved)

    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    serve(db_path, port)


if __name__ == "__main__":
    main()
