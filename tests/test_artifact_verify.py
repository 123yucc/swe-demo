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


def test_planned_test_file_is_not_required_in_diff(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests/test_a.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    diff = "diff --git a/src/a.py b/src/a.py\n"
    result = verify_patch_artifacts(
        tmp_path,
        _plan(
            _edit("src/a.py"),
            _edit("tests/test_a.py"),
        ),
        diff,
    )
    assert result.planned_required_files == ["src/a.py"]
    assert all(f.file != "tests/test_a.py" for f in result.findings)


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
    diff = (
        "diff --git a/src/app.ts b/src/app.ts\n"
        "+import { isKeyComboMatch } from './KeyBindingsManager';\n"
    )
    result = verify_patch_artifacts(tmp_path, _plan(_edit("src/app.ts")), diff)
    assert any(
        f.code == "IMPORT_TARGET_MISSING" and f.target == "./KeyBindingsManager"
        for f in result.findings
    )


def test_js_missing_import_feedback_suggests_closest_sibling_module(tmp_path: Path) -> None:
    (tmp_path / "src/utils").mkdir(parents=True)
    (tmp_path / "src/utils/index.js").write_text(
        "export * from './clientUuid';\n",
        encoding="utf-8",
    )
    (tmp_path / "src/utils/clientUniqueId.js").write_text(
        "export function getClientUniqueId() {}\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/src/utils/index.js b/src/utils/index.js\n"
        "+export * from './clientUuid';\n"
    )

    result = verify_patch_artifacts(tmp_path, _plan(_edit("src/utils/index.js")), diff)
    feedback = render_artifact_feedback(result.findings, tmp_path)

    assert any(
        f.code == "IMPORT_TARGET_MISSING" and f.target == "./clientUuid"
        for f in result.findings
    )
    assert "./clientUniqueId" in feedback


def test_js_relative_import_target_resolves_index_file(tmp_path: Path) -> None:
    (tmp_path / "src/key").mkdir(parents=True)
    (tmp_path / "src/key/index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "src/app.ts").write_text("import { x } from './key';\n", encoding="utf-8")
    diff = (
        "diff --git a/src/app.ts b/src/app.ts\n"
        "+import { x } from './key';\n"
    )
    result = verify_patch_artifacts(tmp_path, _plan(_edit("src/app.ts")), diff)
    assert result.ok is True


def test_js_named_import_missing_from_barrel_is_reported(tmp_path: Path) -> None:
    (tmp_path / "src/utils").mkdir(parents=True)
    (tmp_path / "src/utils/index.js").write_text(
        "export * from './clientUuid';\n",
        encoding="utf-8",
    )
    (tmp_path / "src/utils/clientUuid.js").write_text(
        "export const clientUuid = () => 'x';\n",
        encoding="utf-8",
    )
    (tmp_path / "src/app.js").write_text(
        "import { getClientUniqueId } from './utils';\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/src/app.js b/src/app.js\n"
        "+import { getClientUniqueId } from './utils';\n"
        "diff --git a/src/utils/index.js b/src/utils/index.js\n"
        "+export * from './clientUuid';\n"
    )

    result = verify_patch_artifacts(
        tmp_path,
        _plan(_edit("src/app.js"), _edit("src/utils/index.js")),
        diff,
    )

    assert any(
        f.code == "IMPORT_SYMBOL_MISSING"
        and f.file == "src/app.js"
        and f.target == "./utils"
        and f.symbol == "getClientUniqueId"
        for f in result.findings
    )


def test_js_named_import_from_barrel_is_allowed_when_symbol_is_reexported(tmp_path: Path) -> None:
    (tmp_path / "src/utils").mkdir(parents=True)
    (tmp_path / "src/utils/index.js").write_text(
        "export * from './clientUniqueId';\n",
        encoding="utf-8",
    )
    (tmp_path / "src/utils/clientUniqueId.js").write_text(
        "export function getClientUniqueId() { return 'x'; }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/app.js").write_text(
        "import { getClientUniqueId } from './utils';\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/src/app.js b/src/app.js\n"
        "+import { getClientUniqueId } from './utils';\n"
        "diff --git a/src/utils/index.js b/src/utils/index.js\n"
        "+export * from './clientUniqueId';\n"
    )

    result = verify_patch_artifacts(
        tmp_path,
        _plan(_edit("src/app.js"), _edit("src/utils/index.js")),
        diff,
    )

    assert not any(
        f.code == "IMPORT_SYMBOL_MISSING"
        and f.file == "src/app.js"
        and f.symbol == "getClientUniqueId"
        for f in result.findings
    )


def test_python_repo_import_target_missing_is_reported(tmp_path: Path) -> None:
    (tmp_path / "openlibrary/solr").mkdir(parents=True)
    (tmp_path / "openlibrary/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/solr/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/solr/update_work.py").write_text(
        "from openlibrary.solr.utils import solr_update\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/openlibrary/solr/update_work.py b/openlibrary/solr/update_work.py\n"
        "+from openlibrary.solr.utils import solr_update\n"
    )
    result = verify_patch_artifacts(
        tmp_path,
        _plan(_edit("openlibrary/solr/update_work.py")),
        diff,
    )
    assert any(
        f.code == "IMPORT_TARGET_MISSING"
        and f.target == "openlibrary.solr.utils"
        and f.symbol == "solr_update"
        for f in result.findings
    )


def test_python_missing_from_import_feedback_names_existing_symbol_candidates(tmp_path: Path) -> None:
    (tmp_path / "openlibrary/solr").mkdir(parents=True)
    (tmp_path / "openlibrary/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/solr/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/solr/update_work.py").write_text(
        "def build_subject_doc():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "openlibrary/solr/utils.py").write_text(
        "from openlibrary.solr.solr_builder.index_subjects import build_subject_doc as _impl\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/openlibrary/solr/utils.py b/openlibrary/solr/utils.py\n"
        "+from openlibrary.solr.solr_builder.index_subjects import build_subject_doc as _impl\n"
    )

    result = verify_patch_artifacts(
        tmp_path,
        _plan(_edit("openlibrary/solr/utils.py")),
        diff,
    )
    feedback = render_artifact_feedback(result.findings, tmp_path)

    assert any(
        f.code == "IMPORT_TARGET_MISSING"
        and f.target == "openlibrary.solr.solr_builder.index_subjects"
        and f.symbol == "build_subject_doc"
        for f in result.findings
    )
    assert "The import target module does not exist" in feedback
    assert "build_subject_doc -> openlibrary/solr/update_work.py:1" in feedback
    assert "module openlibrary.solr.update_work" in feedback


def test_python_namespace_from_import_resolves_child_module(tmp_path: Path) -> None:
    (tmp_path / "scripts/monitoring").mkdir(parents=True)
    (tmp_path / "scripts/monitoring/haproxy_monitor.py").write_text(
        "def main():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts/monitoring/monitor.py").write_text(
        "from scripts.monitoring import haproxy_monitor\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/scripts/monitoring/monitor.py b/scripts/monitoring/monitor.py\n"
        "+from scripts.monitoring import haproxy_monitor\n"
    )
    result = verify_patch_artifacts(
        tmp_path,
        _plan(_edit("scripts/monitoring/monitor.py")),
        diff,
    )
    assert result.ok is True


def test_python_preexisting_unresolved_import_in_changed_file_not_reported(tmp_path: Path) -> None:
    (tmp_path / "openlibrary/plugins/openlibrary").mkdir(parents=True)
    (tmp_path / "openlibrary/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/plugins/openlibrary/dev_instance.py").write_text(
        "from openlibrary.core.task import oltask\n\n"
        "VALUE = 2\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/openlibrary/plugins/openlibrary/dev_instance.py b/openlibrary/plugins/openlibrary/dev_instance.py\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )

    result = verify_patch_artifacts(
        tmp_path,
        _plan(_edit("openlibrary/plugins/openlibrary/dev_instance.py")),
        diff,
    )

    assert all(f.code != "IMPORT_TARGET_MISSING" for f in result.findings)


def test_python_from_import_missing_name_is_reported_for_added_import(tmp_path: Path) -> None:
    (tmp_path / "openlibrary/utils").mkdir(parents=True)
    (tmp_path / "openlibrary/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/utils/__init__.py").write_text(
        "def existing():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "openlibrary/solr").mkdir(parents=True)
    (tmp_path / "openlibrary/solr/utils.py").write_text(
        "from openlibrary.utils import RetryStrategy\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/openlibrary/solr/utils.py b/openlibrary/solr/utils.py\n"
        "+from openlibrary.utils import RetryStrategy\n"
    )

    result = verify_patch_artifacts(
        tmp_path,
        _plan(_edit("openlibrary/solr/utils.py")),
        diff,
    )

    assert any(
        f.code == "IMPORT_SYMBOL_MISSING"
        and f.target == "openlibrary.utils"
        and f.symbol == "RetryStrategy"
        for f in result.findings
    )


def test_python_from_import_missing_names_all_reported(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir(parents=True)
    (tmp_path / "pkg/__init__.py").write_text(
        "def present():\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "consumer.py").write_text(
        "from pkg import present, missing_a, missing_b\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/consumer.py b/consumer.py\n"
        "+from pkg import present, missing_a, missing_b\n"
    )

    result = verify_patch_artifacts(tmp_path, _plan(_edit("consumer.py")), diff)

    assert any(
        f.code == "IMPORT_SYMBOL_MISSING"
        and f.symbol == "missing_a, missing_b"
        for f in result.findings
    )


def test_python_from_import_existing_name_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "openlibrary/utils").mkdir(parents=True)
    (tmp_path / "openlibrary/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/utils/__init__.py").write_text(
        "class RetryStrategy:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "openlibrary/solr").mkdir(parents=True)
    (tmp_path / "openlibrary/solr/utils.py").write_text(
        "from openlibrary.utils import RetryStrategy\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/openlibrary/solr/utils.py b/openlibrary/solr/utils.py\n"
        "+from openlibrary.utils import RetryStrategy\n"
    )

    result = verify_patch_artifacts(
        tmp_path,
        _plan(_edit("openlibrary/solr/utils.py")),
        diff,
    )

    assert all(f.code != "IMPORT_TARGET_MISSING" for f in result.findings)


def test_python_from_import_reexported_name_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "openlibrary/core").mkdir(parents=True)
    (tmp_path / "openlibrary/utils").mkdir(parents=True)
    (tmp_path / "openlibrary/solr").mkdir(parents=True)
    (tmp_path / "openlibrary/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/core/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/utils/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "openlibrary/utils/retry.py").write_text(
        "class RetryStrategy:\n    pass\n\n"
        "class MaxRetriesExceeded(Exception):\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "openlibrary/core/helpers.py").write_text(
        "from openlibrary.utils.retry import RetryStrategy, MaxRetriesExceeded\n",
        encoding="utf-8",
    )
    (tmp_path / "openlibrary/solr/utils.py").write_text(
        "from openlibrary.core.helpers import RetryStrategy, MaxRetriesExceeded\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/openlibrary/solr/utils.py b/openlibrary/solr/utils.py\n"
        "+from openlibrary.core.helpers import RetryStrategy, MaxRetriesExceeded\n"
        "diff --git a/openlibrary/core/helpers.py b/openlibrary/core/helpers.py\n"
        "+from openlibrary.utils.retry import RetryStrategy, MaxRetriesExceeded\n"
    )

    result = verify_patch_artifacts(
        tmp_path,
        _plan(
            _edit("openlibrary/solr/utils.py"),
            _edit("openlibrary/core/helpers.py"),
        ),
        diff,
    )

    assert not any(
        f.code == "IMPORT_SYMBOL_MISSING"
        and f.target == "openlibrary.core.helpers"
        for f in result.findings
    )


def test_artifact_feedback_is_actionable(tmp_path: Path) -> None:
    result = verify_patch_artifacts(tmp_path, _plan(_edit("src/a.py")), "")
    feedback = render_artifact_feedback(result.findings)
    assert "Patch artifact verification failed" in feedback
    assert "PLAN_DIFF_MISMATCH" in feedback
