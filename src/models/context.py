from typing import Literal

from pydantic import BaseModel, Field

from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    RequirementItem,
    RequirementStatus,
    StructuralCard,
    SymptomCard,
)


SchemaVersion = Literal["v2"]


class EvidenceCards(BaseModel):
    """Aggregates all four evidence cards plus the RequirementItem task list
    for a single issue.

    schema_version == 'v2' introduced in phase 16: requirements is the primary
    task-driving structure; localization.* / structural.* / constraint.* are
    written ONLY by deep-search (no AS-IS/TO-BE prefix convention).
    """

    symptom: SymptomCard = Field(
        description="Observable failure symptoms.",
    )
    constraint: ConstraintCard = Field(
        description="Constraints the fix must satisfy.",
    )
    localization: LocalizationCard = Field(
        description="Code locations suspected to contain the defect.",
    )
    structural: StructuralCard = Field(
        description="Architectural / module-level context.",
    )
    requirements: list[RequirementItem] = Field(
        default_factory=list,
        description=(
            "Active repair queue. Parser initializes all extracted "
            "requirements here with verdict=UNCHECKED; deep-search keeps "
            "violated/missing/partial items here and moves verified "
            "AS_IS_COMPLIANT items to requirement_status."
        ),
    )
    requirement_status: list[RequirementStatus] = Field(
        default_factory=list,
        description=(
            "Lightweight coverage records for requirements verified as "
            "AS_IS_COMPLIANT. These records intentionally avoid long findings "
            "and are not default patch-planning material."
        ),
    )
    schema_version: SchemaVersion = Field(
        default="v2",
        description=(
            "Evidence-cards schema version. v2 introduced RequirementItem[] "
            "and removed the AS-IS/TO-BE prefix convention."
        ),
    )


class SessionContext(BaseModel):
    """Top-level state object passed between orchestrator steps."""

    issue_id: str = Field(
        description="Unique identifier for the issue being investigated.",
    )
    evidence: EvidenceCards = Field(
        description="Current evidence collected for this issue.",
    )
    pending_todos: list[str] = Field(
        default_factory=list,
        description="Outstanding investigation tasks dispatched by the orchestrator.",
    )
    is_closed: bool = Field(
        default=False,
        description="True when evidence closure has been confirmed.",
    )
