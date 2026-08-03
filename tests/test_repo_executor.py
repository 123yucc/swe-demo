from __future__ import annotations

import subprocess
from pathlib import Path

from src.orchestrator import repo_executor


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, check=True, capture_output=True)


def test_run_repo_command_local(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("REPO_EXECUTOR_DOCKER_CONTAINER", raising=False)
    rc, out, timed_out = repo_executor.run_repo_command(
        ["python", "-c", "print('ok')"],
        repo_dir=tmp_path,
        timeout=30,
    )
    assert rc == 0
    assert timed_out is False
    assert out.strip() == "ok"


def test_run_repo_command_docker_wraps_with_sync(tmp_path: Path, monkeypatch) -> None:
    init_repo(tmp_path)
    (tmp_path / "file.txt").write_text("changed\n", encoding="utf-8")

    calls: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["docker", "exec", "-w"]:
            return subprocess.CompletedProcess(cmd, 0, "wrapped\n", "")
        if cmd[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kwargs)

    monkeypatch.setenv("REPO_EXECUTOR_DOCKER_CONTAINER", "sandbox")
    monkeypatch.setenv("REPO_EXECUTOR_CONTAINER_WORKDIR", "/app")
    monkeypatch.setattr(repo_executor.subprocess, "run", fake_run)

    rc, out, timed_out = repo_executor.run_repo_command(
        ["go", "version"],
        repo_dir=tmp_path,
        timeout=30,
    )

    assert rc == 0
    assert timed_out is False
    assert out == "wrapped\n"
    assert any(
        call[:5] == ["docker", "exec", "-i", "-w", "/app"]
        and call[-5:] == ["git", "apply", "--whitespace=nowarn", "--binary", "-"]
        for call in calls
    )
    assert calls[-1] == ["docker", "exec", "-w", "/app", "sandbox", "go", "version"]
