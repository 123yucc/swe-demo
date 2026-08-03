from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from eval import local_swebench_runner as runner
from eval.local_swebench_runner import IssueSpec, ModelSpec, TaskSpec, expand_manifest


def _issue(workdir: Path, number: int) -> None:
    artifacts = workdir / f"swe_issue_{number:03d}" / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "instance_metadata.json").write_text("{}", encoding="utf-8")


def test_all_issues_and_shared_defaults_expand_cartesian_product(tmp_path):
    workdir = tmp_path / "workdir"
    _issue(workdir, 1)
    _issue(workdir, 2)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "defaults": {"env": {"HARNESS_READ_MAX_LINES": "240"}},
        "models": [
            {"name": "gpt", "backend": "openai", "model": "gpt-x"},
            {"name": "claude", "backend": "anthropic", "model": "claude-x"},
        ],
        "issues": "all",
        "expected_issue_count": 2,
    }), encoding="utf-8")

    tasks, _ = expand_manifest(manifest, workdir)
    assert len(tasks) == 4
    assert {task.model.env["HARNESS_READ_MAX_LINES"] for task in tasks} == {"240"}
    assert {task.model.env["MODEL_BACKEND"] for task in tasks} == {"openai", "anthropic"}


def test_expected_issue_count_fails_closed(tmp_path):
    workdir = tmp_path / "workdir"
    _issue(workdir, 1)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "models": [{"name": "claude", "model": "claude-x"}],
        "issues": "all",
        "expected_issue_count": 731,
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="expected 731 issues but discovered 1"):
        expand_manifest(manifest, workdir)


def test_models_sharing_issue_are_serialized(monkeypatch, tmp_path):
    issue_dir = tmp_path / "swe_issue_001"
    issue_dir.mkdir()
    issue = IssueSpec("swe_issue_001", issue_dir, issue_dir / "meta.json")
    active = 0
    maximum = 0
    guard = threading.Lock()

    def fake_run(task, **kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return {"status": "success", "issue": task.issue.issue_name}

    monkeypatch.setattr(runner, "_run_task_unlocked", fake_run)
    tasks = [
        TaskSpec(ModelSpec(name, {}, f"outputs_{name}"), issue)
        for name in ("one", "two")
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda task: runner.run_task(task), tasks))
    assert len(results) == 2
    assert maximum == 1
