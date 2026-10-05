"""Failure hunt and shrink for the code-tracing province (docs/spec-code-hunt.md, WP-H).

* `rewrites` -- meaning-preserving rewrites of a `Program` (answer-preserving
  by construction and by assertion);
* `shrink` -- delta debugging over top-level statements down to a minimal
  program that still fails;
* `hunt` -- the loop: sample, rewrite the passes, resample-gate every fail,
  shrink the stable ones, confirm on fresh reruns.
"""

from .rewrites import REWRITES, RewriteUnavailable, apply_rewrite_program
from .shrink import ShrinkAborted, ShrinkResult, shrink

__all__ = ["REWRITES", "RewriteUnavailable", "ShrinkAborted", "ShrinkResult", "apply_rewrite_program", "shrink"]
