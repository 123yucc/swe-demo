from __future__ import annotations

import asyncio

from src.models.context import EvidenceCards
from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    RequirementItem,
    StructuralCard,
    SymptomCard,
)
from src.tools.ingestion_tools import (
    init_working_memory,
    reset_submitted_evidence,
    update_requirement_verdict,
)


def _cards(*requirements: RequirementItem) -> EvidenceCards:
    return EvidenceCards(
        symptom=SymptomCard(),
        constraint=ConstraintCard(),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=list(requirements),
    )


def test_compliant_requirement_moves_to_lightweight_status():
    reset_submitted_evidence()
    memory = init_working_memory(
        "issue",
        _cards(
            RequirementItem(
                id="req-001",
                text="Existing route should redirect correctly.",
                origin="requirements",
            )
        ),
    )

    asyncio.run(update_requirement_verdict.handler({
        "requirement_id": "req-001",
        "verdict": "AS_IS_COMPLIANT",
        "evidence_locations": ["src/routes.py:10"],
        "findings": "Read src/routes.py:10 and verified the redirect is already correct. " * 20,
    }))

    assert memory.evidence_cards.requirements == []
    assert len(memory.evidence_cards.requirement_status) == 1
    status = memory.evidence_cards.requirement_status[0]
    assert status.id == "req-001"
    assert status.verdict == "AS_IS_COMPLIANT"
    assert status.evidence_locations == ["src/routes.py:10"]
    assert len(status.short_reason) <= 240
    assert "requirement_status" not in memory.format_for_prompt()


def test_unverifiable_compliant_is_downgraded_to_partial():
    reset_submitted_evidence()
    memory = init_working_memory(
        "issue",
        _cards(
            RequirementItem(
                id="req-002",
                text="Existing behavior must be preserved.",
                origin="requirements",
            )
        ),
    )

    asyncio.run(update_requirement_verdict.handler({
        "requirement_id": "req-002",
        "verdict": "AS_IS_COMPLIANT",
        "evidence_locations": [],
        "findings": "SELF-REFLECTION RESULT - TOKEN TRACEABILITY FAILURE. Must be verified before acting.",
    }))

    assert memory.evidence_cards.requirement_status == []
    assert len(memory.evidence_cards.requirements) == 1
    req = memory.evidence_cards.requirements[0]
    assert req.id == "req-002"
    assert req.verdict == "TO_BE_PARTIAL"
    assert "AS_IS_COMPLIANT rejected" in req.findings


def test_legacy_compliant_requirements_normalize_on_init():
    reset_submitted_evidence()
    memory = init_working_memory(
        "issue",
        _cards(
            RequirementItem(
                id="req-003",
                text="Already satisfied behavior.",
                origin="requirements",
                verdict="AS_IS_COMPLIANT",
                evidence_locations=["src/app.py:20"],
                findings="Read src/app.py:20 and verified the behavior.",
            )
        ),
    )

    assert memory.evidence_cards.requirements == []
    assert [item.id for item in memory.evidence_cards.requirement_status] == ["req-003"]
