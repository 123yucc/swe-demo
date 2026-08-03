from __future__ import annotations

import json
from pathlib import Path

from scripts.prepare_harness_matrix import freeze_matrix


def test_freeze_matrix_writes_one_manifest_per_model(tmp_path):
    workdir = tmp_path / "workdir"
    for number in (1, 2):
        artifacts = workdir / f"swe_issue_{number:03d}" / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "instance_metadata.json").write_text("{}", encoding="utf-8")
    source = tmp_path / "matrix.json"
    source.write_text(json.dumps({
        "experiment": "matrix-test",
        "defaults": {"env": {"DEEP_SEARCH_BATCH_MODE": "single"}},
        "models": [
            {"name": "gpt", "backend": "openai", "model": "gpt-x"},
            {"name": "claude", "backend": "anthropic", "model": "claude-x"},
        ],
        "issues": "all",
        "expected_issue_count": 2,
    }), encoding="utf-8")

    outputs = freeze_matrix(source, workdir, tmp_path / "runtime")
    assert len(outputs) == 2
    assert all(len(json.loads(path.read_text())["models"]) == 1 for path in outputs)
    index = json.loads((outputs[0].parent.parent / "matrix.json").read_text())
    assert index["task_count"] == 4
