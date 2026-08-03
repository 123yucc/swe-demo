from __future__ import annotations

import json
import subprocess

from scripts import cleanup_owned_docker_images as cleanup_script


def test_cleanup_removes_only_images_in_ledger(monkeypatch, tmp_path):
    ledger = tmp_path / "owned.json"
    ledger.write_text(
        json.dumps({"images": ["run-owned:a", "run-owned:b"]}), encoding="utf-8"
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cleanup_script.subprocess, "run", fake_run)
    removed, failed = cleanup_script.cleanup(ledger)

    assert removed == ["run-owned:a", "run-owned:b"]
    assert failed == []
    assert calls == [
        ["docker", "rmi", "-f", "run-owned:a"],
        ["docker", "rmi", "-f", "run-owned:b"],
    ]
    assert cleanup_script.load_images(ledger) == set()
