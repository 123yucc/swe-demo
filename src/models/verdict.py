"""
ClosureVerdict: structured output model for the closure-checker subagent.

Replaces free-text verdict parsing ("CLOSURE_APPROVED" in markdown)
with SDK structured output — eliminates string-matching fragility.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.models.audit import AuditResult, DimensionFinding


class ClosureConflict(BaseModel):
    """One mechanically verifiable edge in the closure conflict graph."""

    left_requirement_id: str
    right_requirement_id: str
    conflicting_field: str
    shared_evidence: list[str] = Field(default_factory=list)
    explanation: str
    recommended_recheck_side: Literal["left", "right", "both"]


class SharedFactGap(BaseModel):
    """A single missing fact consumed by several requirements."""

    fact: str
    requirement_ids: list[str] = Field(default_factory=list)
    suggested_anchor: str = ""


class ClosureVerdict(BaseModel):
    """Verdict returned by the closure-checker subagent.

    The closure-checker evaluates the AuditManifest tasks and returns
    either CLOSURE_APPROVED (all audits pass) or EVIDENCE_MISSING
    (specific gaps remain).

    Phase 18.B replaced free-text audit focus rules with a deterministic
    AuditManifest.

    Phase 25 re-scope: the closure-checker is now primarily an evidence-closure
    QUESTIONER. The two factual per-task checks (verdict_vs_code,
    findings_anti_hallucination) were lowered into the code grounding gate, so
    ``audited`` now carries at most the ``prescriptive_boundary_self_check``
    result. The LLM's main output is ``dimension_findings`` — its judgement on
    the sufficiency and consistency dimensions. ``verdict`` is EVIDENCE_MISSING
    if ANY dimension_finding or audited result is a FAIL.
    """

    verdict: Literal["CLOSURE_APPROVED", "EVIDENCE_MISSING"] = Field(
        description=(
            "CLOSURE_APPROVED if all dimension_findings and AuditResult checks "
            "pass, EVIDENCE_MISSING if any FAIL is present."
        ),
    )
    rationale: str = Field(
        default="",
        description=(
            "For CLOSURE_APPROVED: 1-2 sentences confirming why closure is justified. "
            "For EVIDENCE_MISSING: brief summary of the biggest gap."
        ),
    )
    dimension_findings: list[DimensionFinding] = Field(
        default_factory=list,
        description=(
            "The closure-checker's per-dimension judgements (sufficiency / "
            "consistency). Phase 25 primary output. Any FAIL forces "
            "EVIDENCE_MISSING and drives the deepen/reconcile rework loop."
        ),
    )
    audited: list[AuditResult] = Field(
        default_factory=list,
        description=(
            "One AuditResult per AuditTask in the manifest (phase 25: only the "
            "prescriptive_boundary_self_check survives at this level). The "
            "orchestrator validates that every task requirement_id appears here."
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
    conflicts: list[ClosureConflict] = Field(
        default_factory=list,
        description="Structured and evidence-backed consistency conflict edges.",
    )
    shared_fact_gaps: list[SharedFactGap] = Field(
        default_factory=list,
        description="Deduplicated missing facts shared by multiple requirements.",
    )
