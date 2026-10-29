"""Vercel entry point: the same stdlib viewer, read-only, over a snapshot of
the results store bundled with the deployment.

Every route is rewritten here (see vercel.json). The store is opened once per
function instance with sqlite's immutable flag: the deployed file never
changes, and the function filesystem is read-only anyway. Nothing that writes
to the store or spends API credit is offered (CARTOGRAPHER_HOSTED=1).
"""
import os
import sys
from pathlib import Path

os.environ["CARTOGRAPHER_HOSTED"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cartographer.store.store import ResultsStore  # noqa: E402
from cartographer.viewer import launch as launch_mod  # noqa: E402
from cartographer.viewer.serve import _ViewerHandler  # noqa: E402

_DB = ROOT / "results" / "campaign.sqlite"
_STORE = ResultsStore(_DB, read_only=True, immutable=True)


class handler(_ViewerHandler):  # noqa: N801 -- the name Vercel's Python runtime looks for
    store = _STORE
    launch_deps = launch_mod.LaunchDeps(db_path=str(_DB))
