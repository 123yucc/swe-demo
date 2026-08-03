import json
from pathlib import Path

from scripts.collect_metrics import (
    failure_reason_for,
    get_build_gate_info,
    is_docker_infra_failure,
    is_model_infra_failure,
    load_json,
    load_run_metrics,
)


def test_load_json_reads_existing_file(tmp_path) -> None:
    path = tmp_path / "value.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    assert load_json(path) == {"ok": True}


def test_compile_metric_keeps_only_final_outcome() -> None:
    info = get_build_gate_info([
        {"system": "go", "outcome": "FAILED"},
        {"system": "go", "outcome": "PASSED"},
    ])
    assert info == {"compile_outcome": "PASSED", "build_system": "go"}
    assert "build_rounds" not in info


def test_load_run_metrics_falls_back_to_analysis_file(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "run_metrics.analysis.json").write_text(
        json.dumps({"wall_clock_seconds": 12.5}),
        encoding="utf-8",
    )
    metrics, name, analysis, generation = load_run_metrics(outputs)
    assert metrics["wall_clock_seconds"] == 12.5
    assert name == "run_metrics.analysis.json"
    assert analysis["wall_clock_seconds"] == 12.5
    assert generation == {}


def test_analysis_complete_has_no_failure_reason(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    reason = failure_reason_for(
        outputs=outputs,
        resolved=None,
        patch_outcome="N/A",
        runner_task={"status": "failed", "error": "stale runner error"},
        analysis_stage={"status": "analysis_complete", "handoff_ready": True},
    )
    assert reason == ""


def test_model_infra_failure_reason_is_distinct(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    runner_task = {"status": "infra_failed", "failure_kind": "model_infra"}
    assert is_model_infra_failure("MODEL_INFRA_FAILURE", runner_task) is True
    reason = failure_reason_for(
        outputs=outputs,
        resolved=None,
        patch_outcome="MODEL_INFRA_FAILURE",
        runner_task=runner_task,
        analysis_stage={},
    )
    assert reason == "model_infra_failure; patch_outcome=MODEL_INFRA_FAILURE"


def test_docker_infra_failure_reason_is_distinct(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    runner_task = {"status": "infra_failed", "failure_kind": "docker_infra"}
    assert is_docker_infra_failure("DOCKER_INFRA_FAILURE", runner_task) is True
    reason = failure_reason_for(
        outputs=outputs,
        resolved=None,
        patch_outcome="DOCKER_INFRA_FAILURE",
        runner_task=runner_task,
        analysis_stage={},
    )
    assert reason == "docker_infra_failure; patch_outcome=DOCKER_INFRA_FAILURE"


def test_infra_reason_wins_over_stale_analysis_complete(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    reason = failure_reason_for(
        outputs=outputs,
        resolved=None,
        patch_outcome="DOCKER_INFRA_FAILURE",
        runner_task={"status": "infra_failed", "failure_kind": "docker_infra"},
        analysis_stage={"status": "analysis_complete"},
    )
    assert reason.startswith("docker_infra_failure")
