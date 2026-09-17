"""Phase 0.5: canonical experiment schema and append-only results store."""

from .adapter import InstrumentedSyntheticRunner, record_synthetic_eval
from .schema import ExperimentRecord, Mutation, RenderMode, Verdict
from .store import ResultsStore

__all__ = [
    "ExperimentRecord",
    "Mutation",
    "RenderMode",
    "Verdict",
    "ResultsStore",
    "record_synthetic_eval",
    "InstrumentedSyntheticRunner",
]
