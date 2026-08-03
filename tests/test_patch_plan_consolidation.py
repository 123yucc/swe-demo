from src.agents.patch_generator_agent import _sanitize_patch_plan
from src.models.context import EvidenceCards
from src.models.evidence import ConstraintCard, LocalizationCard, StructuralCard, SymptomCard
from src.models.memory import SharedWorkingMemory
from src.models.patch import FileEditPlan, PatchPlan


def test_sanitize_consolidates_only_duplicate_explicit_scopes(tmp_path):
    target = tmp_path / "pkg" / "server.go"
    target.parent.mkdir()
    target.write_text("package pkg\n", encoding="utf-8")
    memory = SharedWorkingMemory(
        issue_context="case",
        evidence_cards=EvidenceCards(
            symptom=SymptomCard(),
            constraint=ConstraintCard(),
            localization=LocalizationCard(),
            structural=StructuralCard(),
        ),
        patch_plan=PatchPlan(
            overview="repair",
            edits=[
                FileEditPlan(
                    filepath="pkg/server.go",
                    target_functions=["Serve"],
                    change_rationale="first",
                    preserved_findings=["error one"],
                ),
                FileEditPlan(
                    filepath="pkg/server.go",
                    target_functions=["Serve"],
                    change_rationale="second",
                    preserved_findings=["error two"],
                ),
                FileEditPlan(
                    filepath="pkg/server.go",
                    target_functions=[],
                    change_rationale="file-wide one",
                ),
                FileEditPlan(
                    filepath="pkg/server.go",
                    target_functions=[],
                    change_rationale="file-wide two",
                ),
            ],
        ),
    )

    sanitized = _sanitize_patch_plan(memory, tmp_path)

    assert sanitized is not None
    assert len(sanitized.edits) == 3
    explicit = sanitized.edits[0]
    assert explicit.target_functions == ["Serve"]
    assert explicit.preserved_findings == ["error one", "error two"]
    assert "first" in explicit.change_rationale
    assert "second" in explicit.change_rationale
