from __future__ import annotations

import json

from scripts import run_gpt52_731 as supervisor


def test_phase3_complete_requires_full_frozen_artifact_set(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "WORKDIR", tmp_path)
    out = tmp_path / "swe_issue_081" / "outputs_gpt-5.2"
    (out / "eval_result").mkdir(parents=True)
    (out / "patch.diff").write_text("diff --git a/a b/a\n", encoding="utf-8")
    (out / "patch_outcome.json").write_text(
        json.dumps({"patch_outcome": "PATCH_SUCCESS"}), encoding="utf-8"
    )
    (out / "compile_check.json").write_text("[]", encoding="utf-8")
    (out / "dynamic_closure.json").write_text("{}", encoding="utf-8")

    assert not supervisor.phase3_complete(81, "outputs_gpt-5.2")
    (out / "eval_result" / "eval_summary.json").write_text("{}", encoding="utf-8")
    assert supervisor.phase3_complete(81, "outputs_gpt-5.2")

    (out / "patch_outcome.json").write_text(
        json.dumps({"patch_outcome": "BUILD_FAILED_AFTER_REPAIR"}),
        encoding="utf-8",
    )
    assert supervisor.phase3_complete(81, "outputs_gpt-5.2")


def test_batch_manifest_expands_only_pending_labels(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "RUNTIME", tmp_path)
    source = {
        "models": [{"output_subdir": "outputs_gpt-5.2"}],
        "issue_range": {"start": 81, "end": 731},
        "batch_size": 40,
    }

    path = supervisor.batch_manifest(source, [82, 85], 81, 120, "attempt-1")
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["issues"] == ["082", "085"]
    assert document["expected_issue_count"] == 2
    assert "issue_range" not in document
    assert path.parent.name == "attempt-1"


def test_prepare_keeps_successful_cases_when_one_clone_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "ROOT", tmp_path)
    monkeypatch.setattr(supervisor, "WORKDIR", tmp_path / "workdir")
    monkeypatch.setattr(supervisor, "REPO_CACHE", tmp_path / "cache")
    monkeypatch.setattr(supervisor, "disk_free_gib", lambda: 100.0)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _: None)
    dataset = [{"repo": "x/y", "base_commit": "a"}] * 82

    def setup(label, instance, workdir, repo_cache):
        if label == 82:
            raise RuntimeError("network")
        (workdir / f"swe_issue_{label:03d}" / "repo" / ".git").mkdir(parents=True)

    monkeypatch.setattr(supervisor, "setup_issue", setup)
    _, prepared, failures = supervisor.prepare([81, 82], dataset)

    assert prepared == [81]
    assert 82 in failures


def test_cleanup_repositories_can_be_scoped_to_new_transient_repos(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(supervisor, "WORKDIR", tmp_path)
    transient = tmp_path / "swe_issue_081" / "repo"
    protected = tmp_path / "swe_issue_082" / "repo"
    transient.mkdir(parents=True)
    protected.mkdir(parents=True)

    supervisor.cleanup_repositories([81])

    assert not transient.exists()
    assert protected.exists()


def test_new_repository_cleanup_is_opt_in(monkeypatch):
    monkeypatch.delenv("GPT52_CLEANUP_NEW_REPOS", raising=False)
    monkeypatch.delenv("GPT52_PRESERVE_NEW_REPOS", raising=False)
    assert not supervisor.cleanup_new_repos_enabled()

    monkeypatch.setenv("GPT52_CLEANUP_NEW_REPOS", "1")
    assert supervisor.cleanup_new_repos_enabled()

    monkeypatch.setenv("GPT52_PRESERVE_NEW_REPOS", "1")
    assert not supervisor.cleanup_new_repos_enabled()


def test_supervisor_enables_failed_patch_eval_only_on_final_recovery_pass():
    assert not supervisor.allow_failed_patch_eval(1, 3)
    assert not supervisor.allow_failed_patch_eval(2, 3)
    assert supervisor.allow_failed_patch_eval(3, 3)


def test_model_recovery_scope_excludes_semantic_failures():
    state = {
        "status": "waiting_for_model",
        "remaining_issues": ["081", "082", "083"],
        "infra_remaining_issues": ["082", "083", "999", "bad"],
    }

    assert supervisor.retry_scope_from_state(state, 81, 731) == {82, 83}
    assert supervisor.retry_scope_from_state(
        {"status": "needs_manual_recovery"}, 81, 731
    ) is None


def test_disk_floor_defaults_and_explicit_override(monkeypatch):
    monkeypatch.delenv("GPT52_MIN_FREE_BEFORE_GIB", raising=False)
    assert supervisor.disk_floor_gib("GPT52_MIN_FREE_BEFORE_GIB", 80.0) == 80.0

    monkeypatch.setenv("GPT52_MIN_FREE_BEFORE_GIB", "75")
    assert supervisor.disk_floor_gib("GPT52_MIN_FREE_BEFORE_GIB", 80.0) == 75.0


def test_prepare_accepts_safe_explicit_disk_floor(monkeypatch):
    monkeypatch.setenv("GPT52_MIN_FREE_BEFORE_GIB", "75")
    monkeypatch.setenv("GPT52_MIN_FREE_AFTER_GIB", "60")
    monkeypatch.setattr(supervisor, "disk_free_gib", lambda: 78.5)

    dataset, prepared, failures = supervisor.prepare([], None)

    assert dataset is None
    assert prepared == []
    assert failures == {}
