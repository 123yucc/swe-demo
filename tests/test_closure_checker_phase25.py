"""Phase 25 closure-checker + audit re-scope tests.

Covers the schema-breakage fixes:
  - build_audit_manifest now only emits prescriptive tasks (no verdict_vs_code /
    overlap-group), and compliant items (which moved to requirement_status)
    don't appear in requirements anymore.
  - the closure-checker input surfaces the compliant group for consistency.
"""

from __future__ import annotations

from src.agents.closure_checker_agent import _format_compliant_group
from src.models.context import EvidenceCards
from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    RequirementItem,
    RequirementStatus,
    StructuralCard,
    SymptomCard,
)
from src.orchestrator.audit import build_audit_manifest


def _cards(*reqs: RequirementItem, status=None) -> EvidenceCards:
    return EvidenceCards(
        symptom=SymptomCard(),
        constraint=ConstraintCard(),
        localization=LocalizationCard(),
        structural=StructuralCard(),
        requirements=list(reqs),
        requirement_status=list(status or []),
    )


def test_manifest_emits_only_prescriptive_tasks():
    prescriptive = RequirementItem(
        id="req-001",
        text="status code behavior",
        origin="requirements",
        verdict="AS_IS_VIOLATED",
        evidence_locations=["app.py:10"],
        findings="The correct status code must be 400 instead of 404.",
    )
    plain = RequirementItem(
        id="req-002",
        text="other behavior",
        origin="requirements",
        verdict="AS_IS_VIOLATED",
        evidence_locations=["app.py:20"],
        findings="The guard is simply missing here.",
    )
    manifest = build_audit_manifest(_cards(prescriptive, plain))
    task_ids = {t.requirement_id for t in manifest.tasks}
    assert task_ids == {"req-001"}
    assert manifest.tasks[0].checks_required == ["prescriptive_boundary_self_check"]


def test_manifest_skips_unchecked():
    req = RequirementItem(
        id="req-003", text="x", origin="requirements", verdict="UNCHECKED",
        findings="correct is must be changed to X",
    )
    manifest = build_audit_manifest(_cards(req))
    assert manifest.tasks == []


def test_compliant_group_rendered_for_consistency():
    ev = _cards(
        RequirementItem(
            id="req-001", text="active one", origin="requirements",
            verdict="AS_IS_VIOLATED", evidence_locations=["a.py:1"],
        ),
        status=[
            RequirementStatus(
                id="req-009",
                text="login redirect already works",
                origin="requirements",
                short_reason="verified at routes.py:10",
                evidence_locations=["routes.py:10"],
            )
        ],
    )
    block = _format_compliant_group(ev)
    assert "req-009" in block
    assert "AS_IS_COMPLIANT" in block
    assert "routes.py:10" in block


def test_compliant_group_empty_message():
    ev = _cards(
        RequirementItem(
            id="req-001", text="x", origin="requirements",
            verdict="AS_IS_VIOLATED", evidence_locations=["a.py:1"],
        )
    )
    assert "no compliant" in _format_compliant_group(ev).lower()
