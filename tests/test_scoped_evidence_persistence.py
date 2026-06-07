from __future__ import annotations

import asyncio

from src.models.audit import AuditResult, DimensionFinding
from src.models.context import EvidenceCards
from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    RequirementItem,
    ScopedEvidence,
    StructuralCard,
    SymptomCard,
)
from src.models.verdict import ClosureVerdict
from src.orchestrator.engine import _derive_rework_specs
from src.tools.ingestion_tools import (
    init_working_memory,
    reset_requirement_for_rework,
    reset_submitted_evidence,
    update_localization,
)


def _cards(*requirements: RequirementItem) -> EvidenceCards:
    return EvidenceCards(
        symptom=SymptomCard(),
        constraint=ConstraintCard(),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=list(requirements),
    )


def _active_req(req_id: str = "req-001") -> RequirementItem:
    return RequirementItem(
        id=req_id,
        text="Some behavior under investigation.",
        origin="requirements",
        verdict="AS_IS_VIOLATED",
        evidence_locations=["src/app.py:10-20"],
        findings="Read src/app.py:10-20; the guard is missing.",
    )


# ── 1. scoped_evidence persistence: the core bug this change fixes ──────────

def test_update_localization_writes_scoped_evidence_and_aggregate():
    """update_localization must populate the requirement's own scoped_evidence
    slice AND have it reflected in the aggregate top-level cards."""
    reset_submitted_evidence()
    memory = init_working_memory("issue", _cards(_active_req("req-001")))

    asyncio.run(update_localization.handler({
        "scope_requirement_id": "req-001",
        "exact_code_regions": ["src/app.py:10-20"],
        "must_co_edit_relations": ["src/app.py -> src/caller.py must be updated"],
        "behavioral_constraints": ["input must be non-null"],
    }))

    req = memory.evidence_cards.requirements[0]
    # Per-requirement source slice (with attribution).
    assert req.scoped_evidence.localization.exact_code_regions == ["src/app.py:10-20"]
    assert req.scoped_evidence.structural.must_co_edit_relations == [
        "src/app.py -> src/caller.py must be updated"
    ]
    assert req.scoped_evidence.constraint.behavioral_constraints == ["input must be non-null"]
    # Derived aggregate view.
    assert memory.evidence_cards.localization.exact_code_regions == ["src/app.py:10-20"]
    assert memory.evidence_cards.structural.must_co_edit_relations == [
        "src/app.py -> src/caller.py must be updated"
    ]
    assert memory.evidence_cards.constraint.behavioral_constraints == ["input must be non-null"]


def test_scoped_evidence_survives_json_roundtrip():
    """The scoped slice must be PERSISTED (present after a model_dump_json ->
    model_validate_json round trip), not merely live in memory."""
    reset_submitted_evidence()
    memory = init_working_memory("issue", _cards(_active_req("req-001")))
    asyncio.run(update_localization.handler({
        "scope_requirement_id": "req-001",
        "exact_code_regions": ["src/app.py:10-20"],
    }))

    dumped = memory.evidence_cards.model_dump_json()
    restored = EvidenceCards.model_validate_json(dumped)

    assert restored.requirements[0].scoped_evidence.localization.exact_code_regions == [
        "src/app.py:10-20"
    ]


def test_update_localization_unknown_scope_is_rejected():
    reset_submitted_evidence()
    init_working_memory("issue", _cards(_active_req("req-001")))
    result = asyncio.run(update_localization.handler({
        "scope_requirement_id": "req-999",
        "exact_code_regions": ["src/app.py:1"],
    }))
    assert "ERROR" in result["content"][0]["text"]


# ── 2. cross-requirement aggregation + dedup ────────────────────────────────

def test_aggregate_dedups_across_requirements_preserving_order():
    reset_submitted_evidence()
    memory = init_working_memory(
        "issue", _cards(_active_req("req-001"), _active_req("req-002"))
    )
    asyncio.run(update_localization.handler({
        "scope_requirement_id": "req-001",
        "suspect_entities": ["shared.py:foo", "only_in_001.py:bar"],
    }))
    asyncio.run(update_localization.handler({
        "scope_requirement_id": "req-002",
        "suspect_entities": ["shared.py:foo", "only_in_002.py:baz"],
    }))

    agg = memory.evidence_cards.localization.suspect_entities
    # "shared.py:foo" appears once; req-001 entries precede req-002's.
    assert agg == ["shared.py:foo", "only_in_001.py:bar", "only_in_002.py:baz"]


# ── 3. field-level reset: findings-only keeps locations + scoped slice ──────

def test_findings_only_reset_preserves_locations_and_scope():
    reset_submitted_evidence()
    memory = init_working_memory("issue", _cards(_active_req("req-001")))
    asyncio.run(update_localization.handler({
        "scope_requirement_id": "req-001",
        "exact_code_regions": ["src/app.py:10-20"],
    }))

    ok = reset_requirement_for_rework(
        "req-001",
        audit_feedback="findings cited a function that does not exist",
        fields_to_reset={"findings"},
    )
    assert ok is True

    req = memory.evidence_cards.requirements[0]
    assert req.verdict == "UNCHECKED"          # always reset so picker re-dispatches
    assert req.findings == ""                  # conclusion cleared
    assert req.evidence_locations == ["src/app.py:10-20"]   # locations preserved
    assert req.scoped_evidence.localization.exact_code_regions == ["src/app.py:10-20"]
    # negative-example snapshot present, plus the audit feedback
    assert "PREVIOUS (REJECTED) STATE" in req.rework_context
    assert "AS_IS_VIOLATED" in req.rework_context
    assert "src/app.py:10-20" in req.rework_context
    assert "findings cited a function" in req.rework_context
    # aggregate still shows the preserved scoped entry
    assert memory.evidence_cards.localization.exact_code_regions == ["src/app.py:10-20"]


# ── 4. full reset: everything cleared, snapshot kept ────────────────────────

def test_full_reset_clears_everything_and_snapshots():
    reset_submitted_evidence()
    memory = init_working_memory("issue", _cards(_active_req("req-001")))
    asyncio.run(update_localization.handler({
        "scope_requirement_id": "req-001",
        "exact_code_regions": ["src/app.py:10-20"],
    }))

    ok = reset_requirement_for_rework(
        "req-001",
        audit_feedback="verdict contradicts the code",
        fields_to_reset=None,
    )
    assert ok is True

    req = memory.evidence_cards.requirements[0]
    assert req.verdict == "UNCHECKED"
    assert req.findings == ""
    assert req.evidence_locations == []
    assert req.scoped_evidence == ScopedEvidence()
    assert "PREVIOUS (REJECTED) STATE" in req.rework_context
    assert "verdict contradicts the code" in req.rework_context
    # aggregate view rebuilt to empty
    assert memory.evidence_cards.localization.exact_code_regions == []


def test_reset_missing_requirement_returns_false():
    reset_submitted_evidence()
    init_working_memory("issue", _cards(_active_req("req-001")))
    assert reset_requirement_for_rework("req-404") is False


# ── 5. _derive_rework_specs: dimension → operator/scope mapping (phase 25) ──

def test_derive_specs_sufficiency_fail_is_deepen_full_reset():
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="sufficiency",
                status="FAIL",
                requirement_ids=["req-001"],
                conflicting_field="repair_targets",
                explanation="repair_targets do not land on a concrete location.",
            )
        ],
    )
    specs = _derive_rework_specs(verdict)
    assert set(specs) == {"req-001"}
    assert specs["req-001"].operator == "deepen"
    assert specs["req-001"].fields_to_reset is None


def test_derive_specs_consistency_cross_req_is_reconcile_full_reset():
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="consistency",
                status="FAIL",
                requirement_ids=["req-002", "req-003"],
                conflicting_field="<cross-req>",
                explanation="req-002 and req-003 reach incompatible verdicts.",
            )
        ],
    )
    specs = _derive_rework_specs(verdict)
    assert set(specs) == {"req-002", "req-003"}
    for rid in ("req-002", "req-003"):
        assert specs[rid].operator == "reconcile"
        assert specs[rid].fields_to_reset is None  # cross-req → full reset


def test_derive_specs_consistency_findings_field_resets_findings_only():
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        dimension_findings=[
            DimensionFinding(
                dimension="consistency",
                status="FAIL",
                requirement_ids=["req-005"],
                conflicting_field="findings",
                explanation="findings contradict the compliant group claim.",
            )
        ],
    )
    specs = _derive_rework_specs(verdict)
    assert specs["req-005"].operator == "reconcile"
    assert specs["req-005"].fields_to_reset == {"findings"}


def test_derive_specs_prescriptive_fail_resets_findings():
    verdict = ClosureVerdict(
        verdict="EVIDENCE_MISSING",
        audited=[
            AuditResult(
                requirement_id="req-003",
                per_check={"prescriptive_boundary_self_check": "FAIL"},
            ),
        ],
    )
    specs = _derive_rework_specs(verdict)
    assert specs["req-003"].operator == "reconcile"
    assert specs["req-003"].fields_to_reset == {"findings"}


def test_derive_specs_all_pass_is_no_rework():
    verdict = ClosureVerdict(
        verdict="CLOSURE_APPROVED",
        dimension_findings=[
            DimensionFinding(dimension="sufficiency", status="PASS"),
            DimensionFinding(dimension="consistency", status="PASS"),
        ],
        audited=[
            AuditResult(
                requirement_id="req-004",
                per_check={"prescriptive_boundary_self_check": "PASS"},
            )
        ],
    )
    assert _derive_rework_specs(verdict) == {}
