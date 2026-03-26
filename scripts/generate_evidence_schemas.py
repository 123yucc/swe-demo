"""Generate strict JSON Schema files from evidence card models.

Usage:
    python scripts/generate_evidence_schemas.py
"""

from __future__ import annotations

import json
from pathlib import Path

from src.evidence_cards import (
    CandidateLocation,
    CardVersion,
    CoEditGroup,
    Constraint,
    ConstraintCard,
    DependencyEdge,
    EntityReference,
    EvidenceSource,
    ExpectedBehavior,
    LocalizationCard,
    ObservedFailure,
    StructuralCard,
    SymptomCard,
)


def main() -> None:
    out_dir = Path("docs/schemas")
    out_dir.mkdir(parents=True, exist_ok=True)

    models = {
        "symptom_card.schema.json": SymptomCard,
        "localization_card.schema.json": LocalizationCard,
        "constraint_card.schema.json": ConstraintCard,
        "structural_card.schema.json": StructuralCard,
        "card_version.schema.json": CardVersion,
        "evidence_source.schema.json": EvidenceSource,
        "observed_failure.schema.json": ObservedFailure,
        "expected_behavior.schema.json": ExpectedBehavior,
        "entity_reference.schema.json": EntityReference,
        "candidate_location.schema.json": CandidateLocation,
        "constraint.schema.json": Constraint,
        "dependency_edge.schema.json": DependencyEdge,
        "co_edit_group.schema.json": CoEditGroup,
    }

    for file_name, model in models.items():
        schema = model.model_json_schema()
        (out_dir / file_name).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(f"Generated {len(models)} schema files in {out_dir}")


if __name__ == "__main__":
    main()
