"""Plain-English verdict panels over stored campaign records.

See `cartographer.report.verdict.run_verdict` -- the single entry point.
"""

from .verdict import NotableFailure, RunVerdict, run_verdict

__all__ = ["NotableFailure", "RunVerdict", "run_verdict"]
