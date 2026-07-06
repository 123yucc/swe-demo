from __future__ import annotations

from pathlib import Path

from src.models.patch import FileEditPlan, PatchPlan
from src.orchestrator.artifact_verify import (
    parse_diff_paths,
    render_artifact_feedback,
    verify_patch_artifacts,
)


def _plan(*edits: FileEditPlan) -> PatchPlan:
    return PatchPlan(overview="x", edits=list(edits))


def _edit(path: str, **kwargs) -> FileEditPlan:
    return FileEditPlan(filepath=path, change_rationale="r", **kwargs)


def test_parse_diff_paths_uses_b_side_paths() -> None:
    diff = (
        "diff --git a/src/old.ts b/src/new.ts\n"
        "--- a/src/old.ts\n"
        "+++ b/src/new.ts\n"
    )
    assert parse_diff_paths(diff) == ["src/new.ts"]


def test_empty_patch_is_no_effect(tmp_path: Path) -> None:
    result = verify_patch_artifacts(tmp_path, _plan(_edit("src/a.py")), "")
    codes = {finding.code for finding in result.findings}
    assert result.ok is False
    assert result.empty_patch is True
    assert "NO_EFFECT_PATCH" in codes
    assert "PLAN_DIFF_MISMATCH" in codes


def test_planned_file_missing_from_diff_is_mismatch(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("x = 1\n", encoding="utf-8")
    diff = "diff --git a/src/b.py b/src/b.py\n"
    result = verify_patch_artifacts(tmp_path, _plan(_edit("src/a.py")), diff)
    assert any(f.code == "PLAN_DIFF_MISMATCH" and f.file == "src/a.py" for f in result.findings)


def test_reference_only_edit_does_not_require_diff(tmp_path: Path) -> None:
    diff = "diff --git a/src/real.py b/src/real.py\n"
    result = verify_patch_artifacts(
        tmp_path,
        _plan(
            _edit("src/reference.py", reference_only=True),
            _edit("src/real.py"),
        ),
        diff,
    )
    assert all(f.file != "src/reference.py" for f in result.findings)


def test_expected_symbol_missing_is_reported(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/key.ts").write_text("export const x = 1;\n", encoding="utf-8")
    diff = "diff --git a/src/key.ts b/src/key.ts\n"
    result = verify_patch_artifacts(
        tmp_path,
        _plan(_edit("src/key.ts", expected_symbols=["isKeyComboMatch"])),
        diff,
    )
    assert any(f.code == "SYMBOL_TARGET_MISSING" for f in result.findings)


def test_js_relative_import_target_missing_is_reported(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.ts").write_text(
        "import { isKeyComboMatch } from './KeyBindingsManager';\n",
        encoding="utf-8",
    )
    diff = "diff --git a/src/app.ts b/src/app.ts\n"
    result = verify_patch_artifacts(tmp_path, _plan(_edit("src/app.ts")), diff)
    assert any(
        f.code == "IMPORT_TARGET_MISSING" and f.target == "./KeyBindingsManager"
        for f in result.findings
    )


def test_js_relative_import_target_resolves_index_file(tmp_path: Path) -> None:
    (tmp_path / "src/key").mkdir(parents=True)
    (tmp_path / "src/key/index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "src/app.ts").write_text("import { x } from './key';\n", encoding="utf-8")
    diff = "diff --git a/src/app.ts b/src/app.ts\n"
    result = verify_patch_artifacts(tmp_path, _plan(_edit("src/app.ts")), diff)
    assert result.ok is True


def test_python_repo_import_target_missing_is_reported(tmp_path: Path) -> None:
    (tmp_path / "openlibrary/solr").mkdir(parents=True)
    (tmp_path / "openlibrary/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/solr/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/solr/update_work.py").write_text(
        "from openlibrary.solr.utils import solr_update\n",
        encoding="utf-8",
    )
    diff = "diff --git a/openlibrary/solr/update_work.py b/openlibrary/solr/update_work.py\n"
    result = verify_patch_artifacts(
        tmp_path,
        _plan(_edit("openlibrary/solr/update_work.py")),
        diff,
    )
    assert any(f.code == "IMPORT_TARGET_MISSING" and f.target == "openlibrary.solr.utils" for f in result.findings)


def test_artifact_feedback_is_actionable(tmp_path: Path) -> None:
    result = verify_patch_artifacts(tmp_path, _plan(_edit("src/a.py")), "")
    feedback = render_artifact_feedback(result.findings)
    assert "Patch artifact verification failed" in feedback
    assert "PLAN_DIFF_MISMATCH" in feedback
