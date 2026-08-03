import json
from pathlib import Path

from scripts import gpt52_731_status as status


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_phase3_ready_is_orthogonal_to_latest_infra_failure(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(status, "WORKDIR", tmp_path)
    out = status.output_dir(106)
    out.mkdir(parents=True)
    (out / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n",
        encoding="utf-8",
    )
    (out / "compile_check.json").write_text("{}", encoding="utf-8")
    write_json(out / "patch_outcome.json", {"patch_outcome": "PATCH_SUCCESS"})
    write_json(out / "runner_task.json", {"status": "infra_failed"})

    assert status.classify(106) == "infra_failed"
    assert status.stage2_ready(out)
    assert not status.phase3_complete(out)


def test_phase3_complete_requires_frozen_eval_artifacts(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(status, "WORKDIR", tmp_path)
    out = status.output_dir(211)
    (out / "eval_result").mkdir(parents=True)
    (out / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n",
        encoding="utf-8",
    )
    (out / "compile_check.json").write_text("{}", encoding="utf-8")
    write_json(out / "patch_outcome.json", {"patch_outcome": "PATCH_SUCCESS"})

    assert status.stage2_ready(out)
    assert not status.phase3_complete(out)

    (out / "dynamic_closure.json").write_text("{}", encoding="utf-8")
    (out / "eval_result" / "eval_summary.json").write_text("{}", encoding="utf-8")
    assert status.phase3_complete(out)
