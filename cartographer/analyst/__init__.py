"""The Evidence Analyst -- Nemotron as a WORKER, not just a test subject.

This package turns the measured evidence store into a capability diagnosis
written by an LLM, under an absolute credibility rule: EVERY number comes
from the deterministic store (`evidence.build_evidence_pack`), the LLM only
narrates and interprets (`analyst.write_diagnosis`), and a deterministic
post-check (`analyst.audit_diagnosis`) flags any figure the narrator states
that is not actually present in the evidence pack. The Tavily layer
(`corroborate`) only CORROBORATES the written findings against public
literature -- nothing it returns ever becomes a stored measurement.

No LLM output is ever written back to the results store.
"""

from __future__ import annotations

from .analyst import (
    Diagnosis,
    UnsupportedClaim,
    audit_diagnosis,
    write_diagnosis,
)
from .corroborate import Corroboration, corroborate_findings, load_tavily_key
from .evidence import build_evidence_pack

__all__ = [
    "build_evidence_pack",
    "write_diagnosis",
    "audit_diagnosis",
    "Diagnosis",
    "UnsupportedClaim",
    "corroborate_findings",
    "load_tavily_key",
    "Corroboration",
]
