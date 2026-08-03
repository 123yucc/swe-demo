from pathlib import Path

from scripts import watch_gpt52_731_resume as watcher


def test_process_alive_rejects_invalid_values(monkeypatch) -> None:
    assert not watcher.process_alive(None)
    assert not watcher.process_alive("123")
    assert not watcher.process_alive(0)
    monkeypatch.setattr(watcher.os, "kill", lambda pid, signal: None)
    assert watcher.process_alive(123)


def test_model_ready_uses_fixed_endpoint_and_ca(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return Result()

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    monkeypatch.setattr(watcher, "ROOT", tmp_path)
    monkeypatch.setattr(watcher, "MANIFEST", tmp_path / "manifest.json")

    ready, diagnostic = watcher.model_ready()

    assert ready
    assert diagnostic == "ok"
    assert captured["env"]["OPENAI_BASE_URL"] == "https://165.154.193.90"
    assert captured["env"]["SSL_CERT_FILE"] == watcher.EXPECTED_CA
    assert "--manifest" in captured["command"]


def test_start_supervisor_keeps_recovery_and_disk_guards(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return Result()

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    monkeypatch.setattr(watcher, "ROOT", tmp_path)

    watcher.start_supervisor()

    assert captured["command"] == ["bash", "scripts/start_gpt52_731.sh"]
    assert captured["env"]["GPT52_RECOVERY_PASS"] == "3"
    assert captured["env"]["GPT52_MIN_FREE_BEFORE_GIB"] == "75"
    assert captured["env"]["GPT52_MIN_FREE_AFTER_GIB"] == "60"


def test_completed_case_count_uses_authoritative_phase3_artifacts(monkeypatch) -> None:
    completed = {81, 96, 731}
    monkeypatch.setattr(
        watcher,
        "phase3_artifacts_complete",
        lambda label, output_subdir: (
            output_subdir == "outputs_gpt-5.2" and label in completed
        ),
    )

    assert watcher.completed_case_count() == 3


def test_supervisor_restart_requires_recoverable_terminal_state(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(watcher, "RUNTIME", tmp_path)
    state_path = tmp_path / "supervisor.state.json"

    state_path.write_text('{"status":"waiting_for_model"}', encoding="utf-8")
    assert watcher.supervisor_restart_allowed()

    state_path.write_text('{"status":"needs_manual_recovery"}', encoding="utf-8")
    assert not watcher.supervisor_restart_allowed()

    state_path.write_text('{"status":"complete"}', encoding="utf-8")
    assert not watcher.supervisor_restart_allowed()
