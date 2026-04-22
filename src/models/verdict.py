"""
ClosureVerdict: structured output model for the closure-checker subagent.

Replaces free-text verdict parsing ("CLOSURE_APPROVED" in markdown)
with SDK structured output — eliminates string-matching fragility.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.models.audit import AuditResult


class ClosureVerdict(BaseModel):
    """Verdict returned by the closure-checker subagent.

    The closure-checker evaluates the AuditManifest tasks and returns
    either CLOSURE_APPROVED (all audits pass) or EVIDENCE_MISSING
    (specific gaps remain).

    Phase 18.B replaced free-text audit focus rules with a deterministic
    AuditManifest — the closure-checker must produce one AuditResult per task.
    """

    verdict: Literal["CLOSURE_APPROVED", "EVIDENCE_MISSING"] = Field(
        description=(
            "CLOSURE_APPROVED if all AuditResult checks pass, "
            "EVIDENCE_MISSING if any FAIL is present."
        ),
    )
    rationale: str = Field(
        default="",
        description=(
            "For CLOSURE_APPROVED: 1-2 sentences confirming why closure is justified. "
            "For EVIDENCE_MISSING: brief summary of the biggest gap."
        ),
    )
    audited: list[AuditResult] = Field(
        default_factory=list,
        description=(
            "One AuditResult per AuditTask in the manifest. The orchestrator "
            "validates that every task requirement_id appears here."
        ),
    )
    missing: list[str] = Field(
        default_factory=list,
        description=(
            "When verdict is EVIDENCE_MISSING: one line per failed audit, "
            "each naming the requirement id and the specific check that failed. "
            "Empty for CLOSURE_APPROVED."
        ),
    )
    suggested_tasks: list[str] = Field(
        default_factory=list,
        description=(
            "When verdict is EVIDENCE_MISSING: requirement ids that need a "
            "deep-search rework. Empty for CLOSURE_APPROVED."
        ),
    )
