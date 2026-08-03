"""Tests for repatch-round evidence slicing (issue 010).

When ``evidence_focus_files`` is set (a repatch round), ``format_for_prompt``
must inject only the requirements whose evidence touches those files, carrying
their per-requirement scoped_evidence, and must SKIP the redundant top-level
aggregate localization/structural/constraint cards. Empty focus = unchanged
full dump.
"""

from __future__ import annotations

from src.models.context import EvidenceCards
from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    RequirementItem,
    ScopedEvidence,
    StructuralCard,
    SymptomCard,
)
from src.models.memory import SharedWorkingMemory


def _req(req_id: str, *, region: str, suspect: str) -> RequirementItem:
    return RequirementItem(
        id=req_id,
        text=f"requirement {req_id}",
        origin="requirements",
        verdict="AS_IS_VIOLATED",
        evidence_locations=[region],
        findings=f"finding for {req_id}",
        scoped_evidence=ScopedEvidence(
            localization=LocalizationCard(
                suspect_entities=[suspect],
                exact_code_regions=[region],
            ),
        ),
    )


def _memory(*reqs: RequirementItem) -> SharedWorkingMemory:
    cards = EvidenceCards(
        symptom=SymptomCard(observable_failures=["it breaks"]),
        constraint=ConstraintCard(),
        # Aggregate cards carry a distinctive marker so we can assert they are
        # dropped on slicing. In production these duplicate the scoped slices.
        localization=LocalizationCard(suspect_entities=["AGGREGATE_MARKER"]),
        structural=StructuralCard(),
        requirements=list(reqs),
    )
    return SharedWorkingMemory(issue_context="ctx", evidence_cards=cards)


def test_full_dump_when_focus_empty():
    mem = _memory(
        _req("req-001", region="core/a.go:10", suspect="core/a.go"),
        _req("req-002", region="core/b.go:20", suspect="core/b.go"),
    )
    out = mem.format_for_prompt()
    # Full dump includes the aggregate cards and every requirement.
    assert "AGGREGATE_MARKER" in out
    assert "req-001" in out and "req-002" in out
    assert "active repair context" in out


def test_large_full_dump_omits_duplicate_scoped_evidence(monkeypatch):
    mem = _memory(
        _req("req-001", region="core/a.go:10", suspect="SCOPED_ONLY_MARKER"),
    )
    monkeypatch.setenv("EVIDENCE_PROMPT_COMPACTION_MIN_CHARS", "1")
    out = mem.format_for_prompt()
    assert "AGGREGATE_MARKER" in out
    assert "req-001" in out
    assert "SCOPED_ONLY_MARKER" not in out
    assert "Nested requirement scoped_evidence is omitted" in out


def test_full_dump_compaction_can_be_disabled(monkeypatch):
    mem = _memory(
        _req("req-001", region="core/a.go:10", suspect="SCOPED_ONLY_MARKER"),
    )
    monkeypatch.setenv("EVIDENCE_PROMPT_COMPACTION", "off")
    monkeypatch.setenv("EVIDENCE_PROMPT_COMPACTION_MIN_CHARS", "1")
    out = mem.format_for_prompt()
    assert "SCOPED_ONLY_MARKER" in out
    assert "Nested requirement scoped_evidence is omitted" not in out


def test_slice_keeps_only_touching_requirements():
    mem = _memory(
        _req("req-001", region="core/a.go:10", suspect="core/a.go"),
        _req("req-002", region="core/b.go:20", suspect="core/b.go"),
    )
    mem.evidence_focus_files = ["core/a.go"]
    out = mem.format_for_prompt()
    # Sliced view: only the requirement touching core/a.go survives.
    assert "req-001" in out
    assert "req-002" not in out
    # Aggregate cards are dropped this round.
    assert "AGGREGATE_MARKER" not in out
    # Sliced header is shown; symptom is still present.
    assert "SLICED" in out
    assert "it breaks" in out


def test_slice_normalizes_path_prefix():
    mem = _memory(_req("req-001", region="core/a.go:10", suspect="core/a.go"))
    # Focus path carries a ./ prefix and backslashes; must still match.
    mem.evidence_focus_files = ["./core/a.go"]
    out = mem.format_for_prompt()
    assert "req-001" in out
    assert "SLICED" in out


def test_slice_falls_back_to_all_when_no_match():
    # Errors only in a test file no requirement cited → keep all requirements
    # rather than emit an empty evidence section.
    mem = _memory(
        _req("req-001", region="core/a.go:10", suspect="core/a.go"),
        _req("req-002", region="core/b.go:20", suspect="core/b.go"),
    )
    mem.evidence_focus_files = ["core/a_test.go"]
    out = mem.format_for_prompt()
    assert "req-001" in out and "req-002" in out
    # Still a slice (aggregate dropped), just with all requirements kept.
    assert "AGGREGATE_MARKER" not in out


def test_slice_is_smaller_than_full_dump():
    mem = _memory(
        _req("req-001", region="core/a.go:10", suspect="core/a.go"),
        _req("req-002", region="core/b.go:20", suspect="core/b.go"),
    )
    full = mem.format_for_prompt()
    mem.evidence_focus_files = ["core/a.go"]
    sliced = mem.format_for_prompt()
    assert len(sliced) < len(full)
