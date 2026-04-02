"""Phase1 artifact parser worker implementation."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from ..evidence_cards import (
    CandidateLocation,
    ConstraintCard,
    EntityReference,
    EvidenceSource,
    ExpectedBehavior,
    LocalizationCard,
    ObservedFailure,
    StructuralCard,
    SufficiencyStatus,
    SymptomCard,
)
from ._common import read_first_existing, resolve_instance_dir, save_json


def _extract_entities(problem_text: str) -> List[EntityReference]:
    entities: List[EntityReference] = []
    tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", problem_text)
    seen = set()
    for token in tokens:
        if token.lower() in {"error", "issue", "traceback", "expected", "current"}:
            continue
        if token in seen:
            continue
        seen.add(token)
        entities.append(
            EntityReference(
                name=token,
                type="symbol",
                evidence_source=[
                    EvidenceSource(
                        source_type="artifact",
                        source_path="problem_statement.md",
                        matching_detail={"token": token},
                        confidence_contribution=0.6,
                    )
                ],
                computed_confidence=0.6,
            )
        )
        if len(entities) >= 12:
            break
    return entities


async def run_phase1_parsing(workspace_dir: str, instance_id: str) -> Dict[str, Any]:
    """Generate v1 evidence cards from artifacts."""
    instance_dir = resolve_instance_dir(workspace_dir, instance_id)
    artifacts_dir = instance_dir / "artifacts"
    evidence_dir = instance_dir / "evidence"
    v1_dir = evidence_dir / "card_versions" / "v1"

    if not artifacts_dir.exists():
        raise FileNotFoundError(f"Artifacts directory not found: {artifacts_dir}")

    problem = read_first_existing(artifacts_dir, ["problem_statement.md"])
    requirements = read_first_existing(artifacts_dir, ["requirements.md"]) or ""
    interface = read_first_existing(artifacts_dir, ["interface.md", "new_interfaces.md"]) or ""
    behavior = read_first_existing(
        artifacts_dir,
        ["expected_and_current_behavior.md", "expected_and current_behavior.md"],
    ) or ""

    if not problem:
        raise FileNotFoundError("artifacts/problem_statement.md is required for phase1")

    used = ["problem_statement.md"]
    if requirements:
        used.append("requirements.md")
    if interface:
        used.append("interface.md|new_interfaces.md")
    if behavior:
        used.append("expected_and_current_behavior.md")

    missing = []
    if not requirements:
        missing.append("requirements.md")
    if not interface:
        missing.append("interface.md/new_interfaces.md")
    if not behavior:
        missing.append("expected_and_current_behavior.md")

    error_line = next((line.strip() for line in problem.splitlines() if "error" in line.lower() or "exception" in line.lower()), "")
    entities = _extract_entities(problem)

    symptom = SymptomCard(
        version=1,
        updated_by="artifact-parser",
        observed_failure=ObservedFailure(
            description=problem.strip()[:800],
            error_message=error_line or None,
            evidence_source=[
                EvidenceSource(
                    source_type="artifact",
                    source_path="problem_statement.md",
                    confidence_contribution=0.8,
                )
            ],
        ),
        expected_behavior=ExpectedBehavior(
            description=(behavior.strip()[:300] if behavior else "Fix issue without regressions"),
            grounded_in=("expected_and_current_behavior" if behavior else "problem_statement"),
        ),
        mentioned_entities=entities,
        hinted_scope="phase1_initial_scope",
        sufficiency_status=SufficiencyStatus.SUFFICIENT if len(missing) <= 1 else SufficiencyStatus.PARTIAL,
        sufficiency_notes=("phase1 parsed" if not missing else f"missing artifacts: {', '.join(missing)}"),
        evidence_sources=used,
        missing_artifacts=missing,
    )

    localization = LocalizationCard(
        version=1,
        updated_by="artifact-parser",
        candidate_locations=[
            CandidateLocation(
                file_path="repo/unknown.py",
                symbol_name=ent.name,
                symbol_type=ent.type,
                evidence_source=ent.evidence_source,
                computed_confidence=ent.computed_confidence,
            )
            for ent in entities[:5]
        ],
        sufficiency_status=SufficiencyStatus.PARTIAL,
        sufficiency_notes="phase1 anchors only",
    )

    must_do = [line.strip("- ") for line in requirements.splitlines() if line.strip().startswith("-")][:10]
    constraint = ConstraintCard(
        version=1,
        updated_by="artifact-parser",
        must_do=must_do,
        must_not_break=["Do not break existing behavior"],
        compatibility_expectations=["Backward compatibility should be preserved"],
        api_signatures={"interface_excerpt": interface.strip()[:200]} if interface else {},
        sufficiency_status=SufficiencyStatus.PARTIAL if not interface else SufficiencyStatus.SUFFICIENT,
        sufficiency_notes="phase1 constraint extraction",
    )

    structural = StructuralCard(
        version=1,
        updated_by="artifact-parser",
        sufficiency_status=SufficiencyStatus.PARTIAL,
        sufficiency_notes="phase1 structural placeholder",
    )

    cards = {
        "symptom": symptom.model_dump(),
        "localization": localization.model_dump(),
        "constraint": constraint.model_dump(),
        "structural": structural.model_dump(),
    }

    for name, payload in cards.items():
        save_json(evidence_dir / f"{name}_card.json", payload)
        save_json(v1_dir / f"{name}_card_v1.json", payload)

    summary = {
        "phase": "phase1",
        "timestamp": datetime.utcnow().isoformat(),
        "instance_id": instance_id,
        "artifacts_used": used,
        "artifacts_missing": missing,
        "next_phase": "phase2",
    }
    save_json(evidence_dir / "phase1_summary.json", summary)

    return {"cards": cards, "summary": summary}
