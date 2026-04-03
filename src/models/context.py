from pydantic import BaseModel, Field

from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    StructuralCard,
    SymptomCard,
)


class EvidenceCards(BaseModel):
    """Aggregates all four evidence cards for a single issue."""

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
