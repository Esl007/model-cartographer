# Frontier card — hand-off to the UI thread

**Goal:** a visitor opens a model and, within five seconds, knows where it breaks and why. No runs, records or t-values on the first screen.

## Data
`docs/frontier-summary.json` is a list with one object per model. Regenerate it after any campaign:
`python3 scripts/frontier_summary.py`.

In Python, call `cartographer.report.frontier.frontier_summaries(store)`. It is deterministic and read-only, so it is safe to call per request (about 1 s on the current store).

```jsonc
{
  "model": "nvidia/nemotron-3-super-120b-a12b",
  "name": "nemotron-3-super-120b-a12b",
  "verdict": "nemotron-3-super-120b-a12b: breaks on long plans with look-alike tool names (24-call plans). Holds on 8 of 9 capabilities tested.",
  "rows": [{
    "key": "plan_length_lookalike",          // stable id
    "label": "Plans long tool sequences when tool names look alike",
    "province": "II · Tool planning",
    "status": "BREAKS",                      // BREAKS | DEGRADES | HOLDS | NOT TESTED
    "headline": "Breaks at 24-call plans: 8/28 pass (was 6/7 at 11-call plans)",
    "boundary": "24-call plans",             // null unless BREAKS/DEGRADES
    "held_through": "11-call plans",         // last healthy level, or the tested ceiling
    "n_pass": 8, "n_scored": 28, "n_error": 0,
    "ci": [0.15, 0.47],                      // Wilson 95% at the boundary
    "confidence": "B",                       // A strong · B moderate · C thin
    "p_value": 0.0001,                       // before-vs-after Fisher, when computable
    "cause": "calls a tool that does not exist -- usually a misspelled look-alike of a real tool name",
    "cause_share": 1.0,                      // share of failures with that cause
    "exemplar": {"record_id": "...", "run_id": "...", "detail": "unknown_tool:step=7:set_dep_th_flag_6", "excerpt": "..."},
    "curve": [{"value": 4, "label": "7-call plans", "n_pass": 20, "n_scored": 21, "n_error": 0}, ...],
    "note": ""
  }]
}
```

## Card layout (proposal)
1. **Verdict sentence** at the top, large. It is already written.
2. **One row per capability**, sorted BREAKS → DEGRADES → HOLDS → NOT TESTED.
   - Status chip: icon plus word, never colour alone.
   - Label and headline.
   - Confidence badge.
3. **Bar per axis row** (rows that have a `curve` with more than one level): a horizontal track of the curve levels, coloured by pass rate, with a marker at `boundary`. The labels are already in native units.
4. **Click a row** to show:
   - the `cause` sentence
   - the `exemplar`: record link, failing detail, excerpt
   - the full `curve` as a small chart
5. **Compare toggle:** stack the same row across models on a shared axis. Labels match across models because they come from the same spec.
6. **NOT TESTED** rows stay visible and greyed out. They answer "what don't we know", which is half the point.

Runs, records and t-values move to an "Evidence" drawer reached from the exemplar link.
