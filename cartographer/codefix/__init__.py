"""Province V: code repair and writing. See docs/province-v-spec.md.

Model code is parsed (never executed as Python) into our `Program` AST and run
by our bounded interpreter on hidden inputs.
"""

from .harness import DEFAULT_ENTRY, HarnessResult, call_entry, equal_outputs, error_kind, outputs_on, values_equal
from .parse import CONSTRUCTS, CodeParseError, SubsetViolation, parse_program, render_source

__all__ = [
    "CONSTRUCTS", "DEFAULT_ENTRY", "CodeParseError", "HarnessResult", "SubsetViolation", "call_entry",
    "equal_outputs", "error_kind", "outputs_on", "parse_program", "render_source", "values_equal",
]

# Province V tasks / bugs / verifier (build B), appended
from .bugs import BUG_KINDS, BugUnavailable, inject  # noqa: E402
from .tasks import CodeFixParams, RepairTask, WriteTask, make_repair_task, make_write_task  # noqa: E402
from .verify import FixVerification, render_repair_prompt, render_write_prompt, verify_submission  # noqa: E402

__all__ += ["BUG_KINDS", "BugUnavailable", "CodeFixParams", "FixVerification", "RepairTask", "WriteTask", "inject",
            "make_repair_task", "make_write_task", "render_repair_prompt", "render_write_prompt", "verify_submission"]
