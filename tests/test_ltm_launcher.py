from pathlib import Path

from src.memory import launcher


def _data_dir(tmp_path: Path) -> Path:
    (tmp_path / "experience_data.json").write_text("[]", encoding="utf-8")
    (tmp_path / "chroma_db_experience").mkdir()
    return tmp_path


def test_concurrent_worker_waits_for_shared_server(tmp_path, monkeypatch) -> None:
    data = _data_dir(tmp_path)
    (data / ".experience_server.start.lock").write_text("other", encoding="ascii")
    monkeypatch.setattr(launcher, "health_check", lambda timeout=3: None)
    monkeypatch.setattr(launcher, "wait_until_ready", lambda max_wait_sec: True)

    assert launcher.ensure_running(data, max_wait_sec=1) is True
    assert (data / ".experience_server.start.lock").exists()


def test_launch_owner_removes_lock_and_can_persist(tmp_path, monkeypatch) -> None:
    data = _data_dir(tmp_path)
    monkeypatch.setattr(launcher, "health_check", lambda timeout=3: None)
    monkeypatch.setattr(launcher, "wait_until_ready", lambda max_wait_sec: True)
    monkeypatch.setenv("MEMGOVERN_PERSIST", "1")
    captured = {}

    class Proc:
        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return Proc()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    assert launcher.ensure_running(data, max_wait_sec=1) is True
    assert captured["start_new_session"] is True
    assert not (data / ".experience_server.start.lock").exists()

