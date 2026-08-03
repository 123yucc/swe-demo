from pathlib import Path

from src.models.context import EvidenceCards
from src.models.evidence import (
    ConstraintCard,
    LocalizationCard,
    RequirementItem,
    StructuralCard,
    SymptomCard,
)
from src.orchestrator.consistency_checks import check_consistency_anchors


def _write(repo: Path, rel: str, content: str) -> None:
    fp = repo / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")


def _cards(*reqs: RequirementItem, anchors: list[str], missing: list[str] | None = None) -> EvidenceCards:
    return EvidenceCards(
        symptom=SymptomCard(),
        constraint=ConstraintCard(
            missing_elements_to_implement=list(missing or []),
        ),
        localization=LocalizationCard(),
        structural=StructuralCard(consistency_anchors=anchors),
        requirements=list(reqs),
    )


def test_future_new_file_anchor_is_allowed_during_analysis(tmp_path: Path):
    _write(tmp_path, "caller.py", "def caller():\n    return True\n")
    req = RequirementItem(
        id="req-001",
        text="add load_config",
        origin="new_interfaces",
        verdict="TO_BE_MISSING",
        evidence_locations=["caller.py:1"],
        explicit_paths=["pkg/utils.py"],
        explicit_symbols=["load_config"],
        contract_kind="interface",
    )
    cards = _cards(
        req,
        anchors=["pkg/utils.py:func:load_config <-> caller.py:1"],
        missing=[
            "Type: New Public Function Name: load_config Path: pkg/utils.py Description: helper"
        ],
    )

    assert check_consistency_anchors(cards, tmp_path) == []


def test_future_new_symbol_in_existing_file_is_allowed_during_analysis(tmp_path: Path):
    _write(tmp_path, "pkg/utils.py", "def helper():\n    return 1\n")
    _write(tmp_path, "caller.py", "def caller():\n    return helper()\n")
    req = RequirementItem(
        id="req-002",
        text="add load_config",
        origin="new_interfaces",
        verdict="TO_BE_PARTIAL",
        evidence_locations=["caller.py:1"],
        explicit_paths=["pkg/utils.py"],
        explicit_symbols=["load_config"],
        contract_kind="interface",
    )
    cards = _cards(
        req,
        anchors=["pkg/utils.py:func:load_config <-> caller.py:1"],
    )

    assert check_consistency_anchors(cards, tmp_path) == []


def test_missing_nonfuture_anchor_still_fails(tmp_path: Path):
    _write(tmp_path, "caller.py", "def caller():\n    return True\n")
    req = RequirementItem(
        id="req-003",
        text="touch caller only",
        origin="requirements",
        verdict="AS_IS_VIOLATED",
        evidence_locations=["caller.py:1"],
    )
    cards = _cards(
        req,
        anchors=["ghost.py:func:load_config <-> caller.py:1"],
    )

    failures = check_consistency_anchors(cards, tmp_path)
    assert len(failures) == 1
    assert "ghost.py" in failures[0].reason
