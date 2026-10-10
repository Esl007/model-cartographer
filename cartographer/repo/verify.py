"""End-state verdict and failure signature (docs/province-vi-spec.md §6).

The end state of an episode is parsed (never run as Python), flattened and
executed by our interpreter on the visible and hidden inputs. PASS iff every
output equals the reference's. A failure gets exactly one primary signature,
first match wins:

  outside_subset:<construct>  ERROR  a file left outside the subset
  cap_hit / api_error / cost_capped   ERROR  infrastructure (episode outcome)
  parse_error / missing_entry  FAIL   a file is not Python / `solve` is gone
  fabricated_symbol:<how>      FAIL   a call or import names a symbol that does not exist
                                      (lookalike: within edit distance 2 of a real name;
                                      wrong_module: the name exists, in another module;
                                      plausible: otherwise)
  lookalike_called             FAIL   a call to a look-alike twin (it exists, it is the wrong one)
  clone_edited                 FAIL   the near-miss clone changed and the bug's function did not
  budget_exhausted             ERROR  no edit at all and the budget ran out while tool_budget < 8
  no_change                    FAIL   behaves exactly like the buggy repository on every input
  visible_fixed_hidden_broken  FAIL   the visible test passes, a hidden input is wrong
  wrong_site_file              FAIL   every edit is in a file other than the bug's
  wrong_site                   FAIL   the bug's file was edited, the bug's statement(s) were not
  regressed                    FAIL   an input the buggy repository got right is now wrong
  introduced:<foil>            FAIL   the outputs are exactly a misreading of the program
  runtime_error:<kind>         FAIL   crashes on some input
  wrong_value                  FAIL   otherwise

(Order deviates from the spec's list in two places, both so a mechanism is
never hidden by a later-computed outcome: `clone_edited` precedes `no_change`
-- editing a never-called clone changes no output -- and
`visible_fixed_hidden_broken` precedes `wrong_site_file`, so H3 sees symptom
patches in the entry file.)
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from ..codefix.verify import _shape
from ..codeworld.foils import foils_for
from ..codeworld.model import iter_stmts
from ..store.schema import Verdict
from .compose import RepoTask, repo_outputs
from .flatten import FlatRepo, flat_outputs, flatten
from .names import edit_distance

INFRA_OUTCOMES = ("api_error", "cap_hit", "cost_capped")


@dataclass(frozen=True)
class RepoVerification:
    verdict: Verdict
    signature: str
    extra: dict = field(default_factory=dict)


def _flat_defs(flat: FlatRepo) -> dict:
    return flat.qualified_defs() if flat.ok else {}


def _changed(a, b) -> bool:
    if a is None or b is None:
        return a is not b
    return _shape(tuple(iter_stmts((a,)))) != _shape(tuple(iter_stmts((b,))))


def bug_sites_touched(task: RepoTask, initial: dict, end: dict) -> bool:
    """Did the end state change any statement at a bug site of the carrier?
    The initial parse of the carrier and the latent carrier flatten to the
    same statement sequence, so diff positions map to latent sids."""
    q = task.qualified(task.carrier)
    a_def, b_def = initial.get(q), end.get(q)
    if b_def is None:
        return True  # removed / renamed / moved: rewritten
    latent = next(s for s in task.buggy.body if getattr(s, "name", None) == task.carrier)
    sids = [s.sid for s in iter_stmts(latent.body)]
    a = [_shape(s) for s in iter_stmts(a_def.body)]
    b = [_shape(s) for s in iter_stmts(b_def.body)]
    if len(a) != len(sids):  # pragma: no cover - the round trip guarantees equal length
        return True
    touched = set()
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            touched.update(sids[i1:i2])
        else:
            touched.add(sids[min(i1, len(sids) - 1)])
    return bool(touched & set(task.bug_sites))


def classify_unresolved(u, flat: FlatRepo) -> str:
    defs = [(m, d) for m, ds in flat.defs.items() for d in ds]
    if u.how == "module":
        mods = list(flat.defs)
        return "lookalike" if any(edit_distance(u.symbol, m) <= 2 for m in mods) else "plausible"
    if any(d == u.symbol for m, d in defs):
        return "wrong_module"
    if any(edit_distance(u.symbol, d) <= 2 for _m, d in defs):
        return "lookalike"
    return "plausible"


_INITIAL_CACHE: dict = {}


def _initial_flat(task: RepoTask) -> FlatRepo:
    key = (task.params, task.seed)
    hit = _INITIAL_CACHE.get(key)
    if hit is None:
        if len(_INITIAL_CACHE) > 64:
            _INITIAL_CACHE.clear()
        hit = _INITIAL_CACHE[key] = flatten(task.files, task.entry_module, task.buggy.param_kinds)
    return hit


def verify_repo(task: RepoTask, files: dict, outcome: str | None = None, n_edits: int | None = None) -> RepoVerification:
    if outcome in INFRA_OUTCOMES:
        return RepoVerification(Verdict.ERROR, outcome)
    flat = flatten(files, task.entry_module, task.buggy.param_kinds)
    edited = sorted(p for p in set(files) | set(task.files)
                    if files.get(p) != task.files.get(p) and not p.startswith("tests/"))
    extra: dict = {"edited_files": edited}
    if flat.error is not None:
        kind = flat.error[0]
        extra["error_path"] = flat.error[2]
        if kind == "outside_subset":
            return RepoVerification(Verdict.ERROR, f"outside_subset:{flat.error[1]}", extra)
        return RepoVerification(Verdict.FAIL, kind, extra)
    inputs = [task.visible_input] + list(task.hidden_inputs)
    got = flat_outputs(flat, inputs)
    ref = repo_outputs(task.reference, inputs)
    bug = repo_outputs(task.buggy, inputs)
    if got == ref:
        return RepoVerification(Verdict.PASS, "pass", extra)
    if flat.unresolved:
        first = sorted(flat.unresolved, key=lambda u: (u.module, u.lineno, u.symbol))[0]
        extra["fabricated"] = f"{first.module}:{first.source or ''}.{first.symbol}"
        return RepoVerification(Verdict.FAIL, f"fabricated_symbol:{classify_unresolved(first, flat)}", extra)
    end_defs = _flat_defs(flat)
    init_defs = _flat_defs(_initial_flat(task))
    twin_q = {task.qualified(t) for t, _ in task.twins}
    from .compose import calls_in

    if any(c in twin_q for d in end_defs.values() for c in calls_in(d.body)):
        return RepoVerification(Verdict.FAIL, "lookalike_called", extra)
    carrier_q = task.qualified(task.carrier)
    carrier_changed = _changed(init_defs.get(carrier_q), end_defs.get(carrier_q))
    extra["carrier_changed"] = int(carrier_changed)
    if task.clone is not None:
        clone_q = task.qualified(task.clone[0])
        if _changed(init_defs.get(clone_q), end_defs.get(clone_q)) and not carrier_changed:
            return RepoVerification(Verdict.FAIL, "clone_edited", extra)
    if not edited and outcome == "budget_exhausted" and task.params.tool_budget < 8:
        return RepoVerification(Verdict.ERROR, "budget_exhausted", extra)
    if got == bug:
        return RepoVerification(Verdict.FAIL, "no_change", extra)
    if got[0] == ref[0]:
        return RepoVerification(Verdict.FAIL, "visible_fixed_hidden_broken", extra)
    bug_file = f"{task.bug_module}.py"
    if edited and bug_file not in edited:
        return RepoVerification(Verdict.FAIL, "wrong_site_file", extra)
    if bug_file in edited and not bug_sites_touched(task, init_defs, end_defs):
        return RepoVerification(Verdict.FAIL, "wrong_site", extra)
    if any(g != r and b == r for g, r, b in zip(got, ref, bug)):
        return RepoVerification(Verdict.FAIL, "regressed", extra)
    for name, sem in sorted(foils_for(task.reference).items()):
        foil = repo_outputs(task.reference, inputs, sem)
        if foil != ref and foil == got:
            return RepoVerification(Verdict.FAIL, f"introduced:{name}", extra)
    err = next((g for g in got if g[0] == "error"), None)
    if err is not None:
        return RepoVerification(Verdict.FAIL, f"runtime_error:{err[1]}", extra)
    return RepoVerification(Verdict.FAIL, "wrong_value", extra)
