"""Phase 2: deterministic surface rendering of `World`s into prompts, and verification."""

from .naming import NameMap, build_name_map
from .templates import RenderedTask, render
from .verify import verify

__all__ = [
    "NameMap",
    "build_name_map",
    "RenderedTask",
    "render",
    "verify",
]
