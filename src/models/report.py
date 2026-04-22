"""
DeepSearchReport: structured output model for the deep-search subagent.

Replaces markdown report + regex parsing with SDK structured output.
Eliminates format drift and all _extract_* parsing functions.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.models.evidence import RequirementVerdict


class DeepSearchReport(BaseModel):
    """Structured report returned by the deep-search subagent.

    Each field maps directly to an update_localization argument key,
    so the orchestrator can persist findings without any parsing.

    Starting in phase 16, a single deep-search invocation is scoped to a
    single RequirementItem (target_requirement_id). The requirement_*
    fields capture the verdict for that item, while the legacy
    localization/structural fields carry AS-IS code observations that
    are independent of any one requirement.
    """

    target_requirement_id: str = Field(
        default="",
        description=(
            "The RequirementItem.id this deep-search run was scoped to. "
            "Empty only when no requirement scope is active."
        ),
    )
    requirement_verdict: RequirementVerdict = Field(
        default="UNCHECKED",
        description=(
            "The verdict for target_requirement_id after on-site verification. "
            "UNCHECKED is only valid when no requirement scope was set."
        ),
    )
    requirement_findings: str = Field(
        default="",
        description=(
            "Concrete on-site verification summary for target_requirement_id. "
            "What does the current code actually do w.r.t. this requirement?"
        ),
    )
    requirement_evidence_locations: list[str] = Field(
        default_factory=list,
        description=(
            "Code locations ('file.py:LINE' or 'file.py:LINE-LINE') that "
            "substantiate the requirement_verdict. Required non-empty unless "
            "requirement_verdict == 'AS_IS_COMPLIANT'."
        ),
    )
    exact_code_regions: list[str] = Field(
        default_factory=list,
        description=(
            "Exact line numbers in 'file.py:N' or 'file.py:N-M' form. "
            "Paths must be relative to the repository root. "
            "Omitting this blocks evidence closure."
        ),
    )
    suspect_entities: list[str] = Field(
        default_factory=list,
        description=(
            "Files, classes, functions, or variables confirmed as involved "
            "in the defect."
        ),
    )
    call_chain_context: list[str] = Field(
        default_factory=list,
        description=(
            "Caller-Callee chains in 'A -> B -> C' format showing how "
            "the buggy code is reached."
        ),
    )
    dataflow_relevant_uses: list[str] = Field(
        default_factory=list,
        description=(
            "Def-Use relationships: variable definitions and their use sites."
        ),
    )
    must_co_edit_relations: list[str] = Field(
        default_factory=list,
        description=(
            "Co-edit dependencies: 'If A changes -> B must also change'."
        ),
    )
    dependency_propagation: list[str] = Field(
        default_factory=list,
        description=(
            "Cross-cutting dependency paths (interface/package/config)."
        ),
    )
    missing_elements_to_implement: list[str] = Field(
        default_factory=list,
        description=(
            "TO-BE elements confirmed absent from the codebase. Only list "
            "items whose DEFINITION is entirely absent (no matching "
            "'def method_name' or 'class ClassName' found)."
        ),
    )
    similar_implementation_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Existing similar APIs found as reference for the fix."
        ),
    )
    confirmed_defect_locations: list[str] = Field(
        default_factory=list,
        description=(
            "Confirmed defect locations in 'file.py:LINE -- explanation' format."
        ),
    )
    new_suspects: list[str] = Field(
        default_factory=list,
        description="New suspects discovered during investigation."
    )
    ruled_out_suspects: list[str] = Field(
        default_factory=list,
        description="Leads that turned out to be dead ends."
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="Remaining open questions."
    )
