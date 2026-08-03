import json
from pathlib import Path

import eval.make_eval_inputs as make_eval_inputs


def test_build_inputs_writes_generated_patch_to_requested_directory(
    tmp_path: Path, monkeypatch,
) -> None:
    workdir = tmp_path / "workdir"
    issue_dir = workdir / "swe_issue_001"
    artifacts = issue_dir / "artifacts"
    outputs = issue_dir / "outputs-test"
    artifacts.mkdir(parents=True)
    outputs.mkdir()
    (artifacts / "instance_metadata.json").write_text(
        json.dumps(
            {
                "instance_id": "instance_test",
                "base_commit": "abc123",
                "dockerhub_tag": "test-tag",
            }
        ),
        encoding="utf-8",
    )
    (outputs / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n", encoding="utf-8"
    )
    monkeypatch.setattr(make_eval_inputs, "WORKDIR", workdir)

    patches_path, samples_path = make_eval_inputs.build_inputs(
        ["swe_issue_001"], "outputs-test", tmp_path / "modal-inputs"
    )

    patches = json.loads(patches_path.read_text(encoding="utf-8"))
    sample = json.loads(samples_path.read_text(encoding="utf-8").strip())
    assert patches == [
        {"instance_id": "instance_test", "patch": "diff --git a/a.py b/a.py\n"}
    ]
    assert sample["instance_id"] == "instance_test"
    assert sample["dockerhub_tag"] == "test-tag"


def test_find_all_issues_never_falls_back_to_gold_patch(
    tmp_path: Path, monkeypatch,
) -> None:
    workdir = tmp_path / "workdir"
    issue_dir = workdir / "swe_issue_001"
    artifacts = issue_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "instance_metadata.json").write_text(
        '{"instance_id":"instance-test","patch":"gold patch must not run"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(make_eval_inputs, "WORKDIR", workdir)

    assert make_eval_inputs.find_all_issues("outputs-test") == []
