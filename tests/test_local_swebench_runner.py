import json
import subprocess
from pathlib import Path

import eval.local_swebench_runner as runner
from eval.local_swebench_runner import (
    analysis_model_infra_failure_detail,
    IssueSpec,
    ModelSpec,
    TaskSpec,
    ANALYSIS_HANDOFF_CHECKPOINT,
    ANALYSIS_HANDOFF_EVIDENCE,
    analysis_checkpoint_ready,
    archive_existing_eval_result,
    archive_stale_phase3_artifacts,
    can_eval_existing_patch,
    failed_patch_artifacts_ready_for_final_eval,
    container_name,
    generation_checkpoint_ready,
    load_json,
    patch_has_effective_diff,
    phase3_artifacts_ready,
    phase3_patch_ready,
    quarantine_unretryable_analysis_checkpoint,
    runner_exit_code,
    should_skip_task,
    stage2_artifacts_ready,
)
from eval.local_swebench_runner import load_eval_module


def test_container_name_is_isolated_by_run_id() -> None:
    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir="outputs_gpt-5.2"),
        issue=IssueSpec(
            issue_name="swe_issue_025",
            issue_dir=Path("/tmp/swe_issue_025"),
            metadata_path=Path("/tmp/swe_issue_025/artifacts/instance_metadata.json"),
        ),
    )

    first = container_name("gen", task, "run-alpha")
    second = container_name("gen", task, "run-beta")

    assert first != second
    assert first.startswith("swe_gen_outputs_gpt-5.2_swe_issue_025")
    assert second.startswith("swe_gen_outputs_gpt-5.2_swe_issue_025")


def test_load_json_accepts_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(b"\xef\xbb\xbf" + b'{"models": [], "issues": []}')
    assert load_json(path) == {"models": [], "issues": []}


def test_go_json_parser_ignores_non_object_json_lines() -> None:
    module = load_eval_module()
    parsed = module.parse_go_json_tests(
        'null\n[]\n"noise"\n{"Action":"pass","Test":"TestContextReader"}\n'
    )

    assert [row["name"] for row in parsed] == ["TestContextReader"]


def test_runner_exit_code_marks_infra_as_retryable_process_failure() -> None:
    assert runner_exit_code({"success": 2}) == 0
    assert runner_exit_code({"infra_failed": 2}) == 75
    assert runner_exit_code({"failed": 1, "infra_failed": 2}) == 1


def test_eval_rerun_archives_prior_result_without_removing_it(tmp_path: Path) -> None:
    issue_dir = tmp_path / "swe_issue_081"
    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir="outputs_gpt-5.2"),
        issue=IssueSpec(
            issue_name="swe_issue_081",
            issue_dir=issue_dir,
            metadata_path=issue_dir / "artifacts" / "instance_metadata.json",
        ),
    )
    eval_dir = task.output_dir / "eval_result"
    eval_dir.mkdir(parents=True)
    original = b'{"resolved": false}\n'
    (eval_dir / "eval_summary.json").write_bytes(original)

    archived = archive_existing_eval_result(task, "retry/1")

    assert archived is not None
    assert (archived / "eval_summary.json").read_bytes() == original
    assert (eval_dir / "eval_summary.json").read_bytes() == original


def test_cleanup_removes_only_explicitly_owned_images(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_docker(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "")

    monkeypatch.setattr(runner, "docker_cmd", fake_docker)
    runner.cleanup_docker(
        names=["owned-container"],
        images=["newly-pulled:image"],
        log_path=tmp_path / "cleanup.log",
        prune=True,
    )

    assert ["rm", "-f", "owned-container"] in calls
    assert ["rmi", "-f", "newly-pulled:image"] in calls
    assert not any(call[:2] in (["image", "prune"], ["container", "prune"]) for call in calls)


def test_pull_tracks_only_images_downloaded_by_this_run(monkeypatch, tmp_path: Path) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess([], 1, "missing"),
            subprocess.CompletedProcess([], 0, "pulled"),
        ]
    )
    monkeypatch.setattr(runner, "docker_cmd", lambda *args, **kwargs: next(responses))
    pulled: set[str] = set()

    image = runner.pull_first_image(
        ["owner/image:tag"],
        platform=None,
        log_path=tmp_path / "pull.log",
        pulled_images=pulled,
    )

    assert image == "owner/image:tag"
    assert pulled == {"owner/image:tag"}


def test_pull_retries_transient_registry_eof(monkeypatch, tmp_path: Path) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess([], 1, "missing"),
            subprocess.CompletedProcess([], 1, "failed to do request: EOF"),
            subprocess.CompletedProcess([], 1, "missing"),
            subprocess.CompletedProcess([], 0, "pulled"),
        ]
    )
    sleeps: list[int] = []
    monkeypatch.setattr(runner, "docker_cmd", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)

    image = runner.pull_first_image(
        ["owner/image:tag"],
        platform=None,
        log_path=tmp_path / "pull.log",
        pulled_images=set(),
    )

    assert image == "owner/image:tag"
    assert sleeps == [runner.DOCKER_PULL_RETRY_DELAYS_SECONDS[0]]
    assert "[pull-retry]" in (tmp_path / "pull.log").read_text(encoding="utf-8")


def test_pull_does_not_retry_non_transient_missing_manifest(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    responses = iter(
        [
            subprocess.CompletedProcess([], 1, "missing"),
            subprocess.CompletedProcess([], 1, "manifest unknown"),
            subprocess.CompletedProcess([], 1, "missing"),
        ]
    )

    def fake_docker(args, **kwargs):
        calls.append(args)
        return next(responses)

    monkeypatch.setattr(runner, "docker_cmd", fake_docker)
    monkeypatch.setattr(
        runner.time,
        "sleep",
        lambda delay: (_ for _ in ()).throw(AssertionError(delay)),
    )

    try:
        runner.pull_first_image(
            ["owner/image:missing"],
            platform=None,
            log_path=tmp_path / "pull.log",
        )
    except runner.DockerInfraError as exc:
        assert "manifest unknown" in str(exc)
    else:
        raise AssertionError("expected DockerInfraError")

    assert len(calls) == 3


def test_pull_records_batch_owned_image_ledger(monkeypatch, tmp_path: Path) -> None:
    responses = iter(
        [
            subprocess.CompletedProcess([], 1, "missing"),
            subprocess.CompletedProcess([], 0, "pulled"),
        ]
    )
    monkeypatch.setattr(runner, "docker_cmd", lambda *args, **kwargs: next(responses))
    ledger = tmp_path / "owned-images.json"

    runner.pull_first_image(
        ["owner/image:tag"],
        platform=None,
        log_path=tmp_path / "pull.log",
        pulled_images=set(),
        owned_images_file=ledger,
    )

    assert runner.load_owned_images(ledger) == {"owner/image:tag"}
    runner.update_owned_images(ledger, remove={"owner/image:tag"})
    assert runner.load_owned_images(ledger) == set()


def test_quarantine_unretryable_analysis_checkpoint(tmp_path: Path) -> None:
    issue_dir = tmp_path / "swe_issue_081"
    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir="outputs_gpt-5.2"),
        issue=IssueSpec(
            issue_name="swe_issue_081",
            issue_dir=issue_dir,
            metadata_path=issue_dir / "artifacts" / "instance_metadata.json",
        ),
    )
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "checkpoint.json").write_text(
        json.dumps({
            "saved_at": "2026-07-18T00:00:00+00:00",
            "pipeline_state": "ClosureForcedFail",
            "working_memory": {
                "evidence_cards": {
                    "requirements": [
                        {"id": "req-001", "verdict": "AS_IS_VIOLATED"},
                        {"id": "req-002", "verdict": "UNCHECKED"},
                    ]
                }
            },
        }),
        encoding="utf-8",
    )
    (task.output_dir / "analysis_stage.json").write_text("{}", encoding="utf-8")

    assert quarantine_unretryable_analysis_checkpoint(task) == []
    assert (task.output_dir / "checkpoint.json").exists()
    assert (task.output_dir / "analysis_stage.json").exists()
    assert not (
        task.output_dir / "logs" / "failed_analysis_checkpoints"
    ).exists()


def test_evaluator_recovers_go_verbose_results_from_empty_parser_output() -> None:
    module = load_eval_module()
    raw_sample = {
        "fail_to_pass": ["TestContextReader", "TestContextReader/simple_read"],
    }
    output = {
        "tests": [],
        "stdout": "\n".join([
            "=== RUN   TestContextReader",
            "=== RUN   TestContextReader/simple_read",
            "--- PASS: TestContextReader/simple_read (0.00s)",
            "--- PASS: TestContextReader (0.00s)",
        ]),
        "stderr": "",
    }

    verdict = module.evaluate_expected_tests(output, raw_sample)

    assert verdict["resolved"] is True
    assert verdict["expected_test_statuses"] == {
        "TestContextReader": "PASSED",
        "TestContextReader/simple_read": "PASSED",
    }


def test_evaluator_recovers_jest_failed_tests_from_suite_output() -> None:
    module = load_eval_module()
    raw_sample = {
        "fail_to_pass": [
            "test/audio/VoiceRecording-test.ts | VoiceRecording | when starting a recording | should record high-quality audio if voice processing is disabled",
            "test/audio/VoiceRecording-test.ts | VoiceRecording | when starting a recording | should record normal-quality voice if voice processing is enabled",
        ],
    }
    output = {
        "tests": [],
        "stdout": "\n".join([
            "FAIL test/audio/VoiceRecording-test.ts",
            "  VoiceRecording",
            "    when starting a recording",
            "      ✕ should record high-quality audio if voice processing is disabled (3 ms)",
            "      ✕ should record normal-quality voice if voice processing is enabled (1 ms)",
            "",
            "  ● VoiceRecording › when starting a recording › should record high-quality audio if voice processing is disabled",
            "",
            "    TypeError: Recorder is not a constructor",
        ]),
        "stderr": "",
    }

    verdict = module.evaluate_expected_tests(output, raw_sample)

    assert verdict["resolved"] is False
    assert verdict["expected_test_statuses"] == {
        "test/audio/VoiceRecording-test.ts | VoiceRecording | when starting a recording | should record high-quality audio if voice processing is disabled": "FAILED",
        "test/audio/VoiceRecording-test.ts | VoiceRecording | when starting a recording | should record normal-quality voice if voice processing is enabled": "FAILED",
    }


def test_evaluator_recovers_jest_failures_from_stderr_details() -> None:
    module = load_eval_module()
    raw_sample = {
        "fail_to_pass": [
            "hooks/assistant/assistantUpsellConfig.test.ts | getAssistantUpsellConfig should return undefined if the user is a sub user",
            "hooks/assistant/assistantUpsellConfig.test.ts | getAssistantUpsellConfig should return paid config with yearly and monthly cycles if the user is paid with monthly billing",
        ],
    }
    output = {
        "tests": [],
        "stdout": "Running selected tests: packages/components/hooks/assistant/assistantUpsellConfig.test.ts\n",
        "stderr": "\n".join([
            "  ● getAssistantUpsellConfig › should return undefined if the user is a sub user",
            "",
            "    TypeError: _core.SelectedPlan is not a constructor",
            "",
            "  ● getAssistantUpsellConfig › should return paid config with yearly and monthly cycles if the user is paid with monthly billing",
        ]),
    }

    verdict = module.evaluate_expected_tests(output, raw_sample)

    assert verdict["resolved"] is False
    assert verdict["expected_test_statuses"] == {
        "hooks/assistant/assistantUpsellConfig.test.ts | getAssistantUpsellConfig should return undefined if the user is a sub user": "FAILED",
        "hooks/assistant/assistantUpsellConfig.test.ts | getAssistantUpsellConfig should return paid config with yearly and monthly cycles if the user is paid with monthly billing": "FAILED",
    }


def test_evaluator_only_uses_unique_suffix_aliases() -> None:
    module = load_eval_module()
    raw_sample = {
        "fail_to_pass": [
            "test/MatrixClientPeg-test.ts | MatrixClientPeg | setJustRegisteredUserId",
        ],
    }
    output = {
        "tests": [
            {
                "name": "test/SlidingSyncManager-test.ts | MatrixClientPeg | setJustRegisteredUserId",
                "status": "PASSED",
            },
            {
                "name": "test/MatrixClientPeg-test.ts | MatrixClientPeg | setJustRegisteredUserId",
                "status": "FAILED",
            },
        ],
    }

    verdict = module.evaluate_expected_tests(output, raw_sample)

    assert verdict["resolved"] is False
    assert verdict["expected_test_statuses"] == {
        "test/MatrixClientPeg-test.ts | MatrixClientPeg | setJustRegisteredUserId": "FAILED",
    }


def test_eval_only_overrides_skip_and_reuses_existing_patch(tmp_path: Path) -> None:
    output_dir = tmp_path / "outputs_gpt-5.2"
    output_dir.mkdir(parents=True)
    (output_dir / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (output_dir / "runner_task.json").write_text('{"status":"success"}', encoding="utf-8")

    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir="outputs_gpt-5.2"),
        issue=IssueSpec(
            issue_name="swe_issue_025",
            issue_dir=tmp_path,
            metadata_path=tmp_path / "artifacts" / "instance_metadata.json",
        ),
    )

    skip, _ = should_skip_task(
        task,
        force_restart=False,
        redo_eval=False,
        eval_only=True,
        phase="evaluate",
    )
    assert skip is False
    assert can_eval_existing_patch(task, redo_eval=False, eval_only=True) is True


def test_empty_patch_is_not_eval_eligible(tmp_path: Path) -> None:
    output_dir = tmp_path / "outputs_gpt-5.2"
    output_dir.mkdir(parents=True)
    (output_dir / "patch.diff").write_text("", encoding="utf-8")
    (output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"NO_EFFECT_PATCH"}',
        encoding="utf-8",
    )
    (output_dir / "prediction.json").write_text("{}", encoding="utf-8")

    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir="outputs_gpt-5.2"),
        issue=IssueSpec(
            issue_name="swe_issue_032",
            issue_dir=tmp_path,
            metadata_path=tmp_path / "artifacts" / "instance_metadata.json",
        ),
    )

    assert patch_has_effective_diff(output_dir / "patch.diff") is False
    assert can_eval_existing_patch(task, redo_eval=True, eval_only=True) is False


def test_stage2_artifacts_ready_rejects_failed_build_outcomes(tmp_path: Path) -> None:
    output_dir = tmp_path / "outputs_gpt-5.2"
    output_dir.mkdir(parents=True)
    (output_dir / "patch.diff").write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    (output_dir / "compile_check.json").write_text(
        '[{"system":"go","command":"go build ./x","outcome":"FAILED_AFTER_REPAIR"}]',
        encoding="utf-8",
    )
    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir="outputs_gpt-5.2"),
        issue=IssueSpec(
            issue_name="swe_issue_076",
            issue_dir=tmp_path,
            metadata_path=tmp_path / "artifacts" / "instance_metadata.json",
        ),
    )

    (output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"BUILD_FAILED_AFTER_REPAIR"}',
        encoding="utf-8",
    )
    assert stage2_artifacts_ready(task) is False

    (output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"BUILD_UNVERIFIABLE"}',
        encoding="utf-8",
    )
    assert stage2_artifacts_ready(task) is False

    (output_dir / "compile_check.json").write_text(
        '[{"system":"go","command":"go build ./x && go test -c -o /tmp/build-verify-1.test ./x","outcome":"FAILED_AFTER_REPAIR"}]',
        encoding="utf-8",
    )
    assert stage2_artifacts_ready(task) is True

    (output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"PATCH_SUCCESS"}',
        encoding="utf-8",
    )
    assert stage2_artifacts_ready(task) is True


def test_failed_build_patch_is_only_phase3_ready_with_final_eval_opt_in(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "outputs_gpt-5.2"
    output_dir.mkdir(parents=True)
    (output_dir / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n", encoding="utf-8"
    )
    (output_dir / "compile_check.json").write_text(
        '[{"outcome":"FAILED_AFTER_REPAIR"}]', encoding="utf-8"
    )
    (output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"BUILD_FAILED_AFTER_REPAIR"}', encoding="utf-8"
    )
    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir="outputs_gpt-5.2"),
        issue=IssueSpec(
            issue_name="swe_issue_076",
            issue_dir=tmp_path,
            metadata_path=tmp_path / "artifacts" / "instance_metadata.json",
        ),
    )

    assert failed_patch_artifacts_ready_for_final_eval(task) is True
    assert phase3_patch_ready(task) is False
    assert phase3_patch_ready(task, allow_failed_patch_eval=True) is True

    (output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"MODEL_INFRA_FAILURE"}', encoding="utf-8"
    )
    assert failed_patch_artifacts_ready_for_final_eval(task) is False


def test_phase3_artifacts_ready_requires_dynamic_and_eval_outputs(tmp_path: Path) -> None:
    output_dir = tmp_path / "outputs_gpt-5.2"
    output_dir.mkdir(parents=True)
    (output_dir / "patch.diff").write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    (output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"PATCH_SUCCESS"}',
        encoding="utf-8",
    )
    (output_dir / "compile_check.json").write_text(
        '[{"outcome":"PASSED"}]',
        encoding="utf-8",
    )
    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir="outputs_gpt-5.2"),
        issue=IssueSpec(
            issue_name="swe_issue_023",
            issue_dir=tmp_path,
            metadata_path=tmp_path / "artifacts" / "instance_metadata.json",
        ),
    )

    assert phase3_artifacts_ready(task) is False
    (output_dir / "eval_result").mkdir()
    (output_dir / "eval_result" / "eval_summary.json").write_text(
        '{"resolved": false}',
        encoding="utf-8",
    )
    assert phase3_artifacts_ready(task) is False
    (output_dir / "dynamic_closure.json").write_text("{}", encoding="utf-8")
    assert phase3_artifacts_ready(task) is True


def test_should_not_skip_when_eval_is_stale_for_stage2_or_phase3(tmp_path: Path) -> None:
    output_dir = tmp_path / "outputs_gpt-5.2"
    (output_dir / "eval_result").mkdir(parents=True)
    (output_dir / "eval_result" / "eval_summary.json").write_text(
        '{"resolved": false}',
        encoding="utf-8",
    )
    (output_dir / "patch.diff").write_text("diff --git a/a.py b/a.py\n", encoding="utf-8")
    (output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"PATCH_INCOMPLETE"}',
        encoding="utf-8",
    )
    (output_dir / "compile_check.json").write_text(
        '[{"outcome":"FAILED"}]',
        encoding="utf-8",
    )
    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir="outputs_gpt-5.2"),
        issue=IssueSpec(
            issue_name="swe_issue_023",
            issue_dir=tmp_path,
            metadata_path=tmp_path / "artifacts" / "instance_metadata.json",
        ),
    )

    skip, reason = should_skip_task(
        task,
        force_restart=False,
        redo_eval=False,
        eval_only=False,
        phase="evaluate",
    )
    assert skip is False
    assert "stale" in reason or "unusable" in reason

    skip, reason = should_skip_task(
        task,
        force_restart=False,
        redo_eval=False,
        eval_only=False,
        phase="phase3",
    )
    assert skip is False
    assert "phase3 artifacts" in reason


def test_archive_stale_phase3_artifacts_moves_old_outputs_aside(tmp_path: Path) -> None:
    output_dir = tmp_path / "outputs_gpt-5.2"
    (output_dir / "eval_result").mkdir(parents=True)
    (output_dir / "eval_result" / "eval_summary.json").write_text(
        '{"resolved": false}',
        encoding="utf-8",
    )
    (output_dir / "dynamic_closure.json").write_text("{}", encoding="utf-8")
    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir="outputs_gpt-5.2"),
        issue=IssueSpec(
            issue_name="swe_issue_023",
            issue_dir=tmp_path,
            metadata_path=tmp_path / "artifacts" / "instance_metadata.json",
        ),
    )

    moved = archive_stale_phase3_artifacts(task, reason="pre-stage2")

    assert len(moved) == 2
    assert not (output_dir / "eval_result").exists()
    assert not (output_dir / "dynamic_closure.json").exists()
    assert len(list(output_dir.glob("eval_result.pre-stage2.*.stale"))) == 1
    assert len(list(output_dir.glob("dynamic_closure.json.pre-stage2.*.stale"))) == 1


def test_analysis_checkpoint_accepts_new_and_legacy_handoffs(tmp_path: Path) -> None:
    output_dir = tmp_path / "outputs_gpt-5.2"
    output_dir.mkdir(parents=True)
    task = TaskSpec(
        model=ModelSpec(name="gpt-5.2", env={}, output_subdir=output_dir.name),
        issue=IssueSpec(
            issue_name="swe_issue_001",
            issue_dir=tmp_path,
            metadata_path=tmp_path / "artifacts" / "instance_metadata.json",
        ),
    )
    (output_dir / "checkpoint.json").write_text(
        '{"pipeline_state":"Closed","budget_counters":{}}',
        encoding="utf-8",
    )
    (output_dir / "analysis_stage.json").write_text(
        '{"status":"analysis_complete","handoff_version":2,"handoff_ready":true}',
        encoding="utf-8",
    )

    assert analysis_checkpoint_ready(task) is True
    assert generation_checkpoint_ready(task) is True
    (output_dir / "analysis_stage.json").write_text(
        '{"status":"analysis_complete","dynamic_grounding_deferred":true}',
        encoding="utf-8",
    )
    assert analysis_checkpoint_ready(task) is True

    (output_dir / "checkpoint.json").write_text(
        '{"pipeline_state":"UnderSpecified","budget_counters":{"dynamic_grounding_done":true}}',
        encoding="utf-8",
    )
    assert generation_checkpoint_ready(task) is True


def _staged_task(tmp_path: Path) -> TaskSpec:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "instance_metadata.json").write_text(
        '{"instance_id":"instance-test","dockerhub_tag":"test-tag"}',
        encoding="utf-8",
    )
    (tmp_path / "repo").mkdir()
    return TaskSpec(
        model=ModelSpec(name="test-model", env={}, output_subdir="outputs-test"),
        issue=IssueSpec(
            issue_name="swe_issue_001",
            issue_dir=tmp_path,
            metadata_path=artifacts / "instance_metadata.json",
        ),
    )


def test_analysis_phase_never_runs_docker_cleanup(tmp_path: Path, monkeypatch) -> None:
    task = _staged_task(tmp_path)

    def fake_analysis(*args, **kwargs) -> int:
        task.output_dir.mkdir(parents=True, exist_ok=True)
        (task.output_dir / "checkpoint.json").write_text(
            '{"pipeline_state":"Closed","budget_counters":{}}',
            encoding="utf-8",
        )
        (task.output_dir / "analysis_stage.json").write_text(
            '{"status":"analysis_complete","handoff_version":2,"handoff_ready":true}',
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(runner, "run_analysis", fake_analysis)
    monkeypatch.setattr(
        runner,
        "cleanup_docker",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("Docker cleanup called")),
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=6.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "analysis.state.json",
        eval_module=None,
        run=runner.RunContext(run_id="analysis-run"),
        eval_only=False,
        phase="analysis",
    )

    assert result["status"] == "success"
    assert result["phase"] == "analysis"


def test_analysis_skips_usable_generated_patch_before_closure_quarantine(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n",
        encoding="utf-8",
    )
    (task.output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"PATCH_SUCCESS"}',
        encoding="utf-8",
    )
    (task.output_dir / "compile_check.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(
        runner,
        "quarantine_unretryable_analysis_checkpoint",
        lambda task: (_ for _ in ()).throw(
            AssertionError("completed patch reached closure quarantine")
        ),
    )
    monkeypatch.setattr(
        runner,
        "run_analysis",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("completed patch reran analysis")
        ),
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=6.0,
        platform=None,
        force_restart=False,
        retry_failed_closure=True,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "analysis-skip.state.json",
        eval_module=None,
        run=runner.RunContext(run_id="analysis-skip"),
        eval_only=False,
        phase="analysis",
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "usable generated patch already exists"
    assert (task.output_dir / "patch.diff").is_file()


def test_analysis_skips_closed_saved_handoff_after_later_stage_file_was_removed(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / ANALYSIS_HANDOFF_CHECKPOINT).write_text(
        '{"pipeline_state":"Closed","budget_counters":{}}',
        encoding="utf-8",
    )
    (task.output_dir / ANALYSIS_HANDOFF_EVIDENCE).write_text(
        '{"schema_version":"v3"}',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        runner,
        "quarantine_unretryable_analysis_checkpoint",
        lambda task: (_ for _ in ()).throw(
            AssertionError("saved handoff reached closure quarantine")
        ),
    )
    monkeypatch.setattr(
        runner,
        "run_analysis",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("saved handoff reran analysis")
        ),
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=6.0,
        platform=None,
        force_restart=False,
        retry_failed_closure=True,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "analysis-saved-handoff-skip.state.json",
        eval_module=None,
        run=runner.RunContext(run_id="analysis-saved-handoff-skip"),
        eval_only=False,
        phase="analysis",
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "saved completed analysis handoff already exists"


def test_analysis_model_infra_detection_ignores_recovered_rate_limit(
    tmp_path: Path,
) -> None:
    log = tmp_path / "generate.log"
    log.write_text(
        "[harness-preflight] run_id=current-run\n"
        "Error getting response: Error code: 429 - rate_limit_error\n"
        "[deep-search] OpenAI Agents SDK rate-limited; backing off 3.0s\n"
        "[orchestrator] Parser done.\n",
        encoding="utf-8",
    )
    assert analysis_model_infra_failure_detail(
        log,
        run_id="current-run",
    ) is None

    with log.open("a", encoding="utf-8") as handle:
        handle.write(
            "RuntimeError: deep-search: Agents SDK returned no valid "
            "structured output after 6 attempt(s): Error code: 429 - "
            "rate_limit_error\n"
        )
    assert "API rate limit" in (
        analysis_model_infra_failure_detail(log, run_id="current-run") or ""
    )


def test_analysis_terminal_rate_limit_returns_retryable_infra_payload(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)

    def fake_analysis(*args, **kwargs) -> int:
        kwargs["log_path"].write_text(
            "[harness-preflight] run_id=analysis-rate-limit\n"
            "RuntimeError: deep-search: Agents SDK returned no valid "
            "structured output after 6 attempt(s): "
            "RateLimitError: concurrency limit exceeded\n",
            encoding="utf-8",
        )
        return 1

    monkeypatch.setattr(runner, "run_analysis", fake_analysis)

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=6.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "analysis-rate-limit.state.json",
        eval_module=None,
        run=runner.RunContext(run_id="analysis-rate-limit"),
        eval_only=False,
        phase="analysis",
    )

    assert result["status"] == "infra_failed"
    assert result["retryable"] is True
    assert result["failure_kind"] == "api_rate_limit"
    assert result["phase"] == "analysis"


def test_analysis_terminal_503_is_model_infra_not_patch_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)

    def fake_analysis(*args, **kwargs) -> int:
        kwargs["log_path"].write_text(
            "[harness-preflight] run_id=analysis-503\n"
            "ModelInfrastructureError: Error code: 503 - service unavailable\n",
            encoding="utf-8",
        )
        return 1

    monkeypatch.setattr(runner, "run_analysis", fake_analysis)
    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=6.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "analysis-503.state.json",
        eval_module=None,
        run=runner.RunContext(run_id="analysis-503"),
        eval_only=False,
        phase="analysis",
    )

    assert result["status"] == "infra_failed"
    assert result["failure_kind"] == "api_unavailable"
    assert result["retryable"] is True


def test_exhausted_closure_retry_preserves_checkpoint_for_targeted_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "checkpoint.json").write_text(
        json.dumps({
            "saved_at": "2026-07-18T00:00:00+00:00",
            "pipeline_state": "ClosureForcedFail",
            "working_memory": {
                "evidence_cards": {
                    "requirements": [
                        {"id": "req-001", "verdict": "AS_IS_VIOLATED"},
                    ]
                }
            },
        }),
        encoding="utf-8",
    )
    (task.output_dir / "evidence.json").write_text("{}", encoding="utf-8")
    (task.output_dir / "run_metrics.analysis.json").write_text(
        '{"retry_kind":"closure_only"}',
        encoding="utf-8",
    )
    observed: dict[str, bool] = {}

    def fake_analysis(*args, **kwargs) -> int:
        observed["retry_failed_closure"] = kwargs["retry_failed_closure"]
        (task.output_dir / "checkpoint.json").write_text(
            '{"pipeline_state":"Closed","budget_counters":{}}',
            encoding="utf-8",
        )
        (task.output_dir / "analysis_stage.json").write_text(
            '{"status":"analysis_complete","handoff_version":2,"handoff_ready":true}',
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(runner, "run_analysis", fake_analysis)

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=6.0,
        platform=None,
        force_restart=False,
        retry_failed_closure=True,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "analysis-retry.state.json",
        eval_module=None,
        run=runner.RunContext(run_id="analysis-retry-run"),
        eval_only=False,
        phase="analysis",
    )

    assert result["status"] == "success"
    assert observed["retry_failed_closure"] is True
    assert not (
        task.output_dir / "logs" / "failed_analysis_checkpoints"
    ).exists()


def test_generate_phase_writes_patch_without_local_eval(tmp_path: Path, monkeypatch) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "checkpoint.json").write_text(
        '{"pipeline_state":"Closed","budget_counters":{}}',
        encoding="utf-8",
    )
    (task.output_dir / "analysis_stage.json").write_text(
        '{"status":"analysis_complete","handoff_version":2,"handoff_ready":true}',
        encoding="utf-8",
    )

    def fake_generation(*args, **kwargs):
        (task.output_dir / "patch.diff").write_text(
            "diff --git a/a.py b/a.py\n", encoding="utf-8"
        )
        return "jefzda/sweap-images:test-tag", 0

    monkeypatch.setattr(runner, "run_generation", fake_generation)
    monkeypatch.setattr(runner, "cleanup_docker", lambda **kwargs: None)
    monkeypatch.setattr(
        runner,
        "run_eval",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("local eval called")),
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=6.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "generate.state.json",
        eval_module=None,
        run=runner.RunContext(run_id="generate-run"),
        eval_only=False,
        phase="generate",
    )

    assert result["status"] == "success"
    assert result["phase"] == "generate"
    assert "resolved" not in result


def test_evaluate_phase_reuses_patch_without_generation(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n", encoding="utf-8"
    )
    (task.output_dir / "patch_outcome.json").write_text("{}", encoding="utf-8")
    (task.output_dir / "prediction.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(runner, "pull_first_image", lambda *args, **kwargs: "image:test")
    monkeypatch.setattr(runner, "cleanup_docker", lambda **kwargs: None)
    monkeypatch.setattr(
        runner,
        "run_generation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("generation called")
        ),
    )
    monkeypatch.setattr(
        runner,
        "run_eval",
        lambda *args, **kwargs: {"resolved": False},
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=4.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "evaluate.state.json",
        eval_module=object(),
        run=runner.RunContext(run_id="evaluate-run"),
        eval_only=True,
        phase="evaluate",
    )

    assert result["status"] == "success"
    assert result["resolved"] is False


def test_stage2_generates_without_evaluation(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "checkpoint.json").write_text(
        '{"pipeline_state":"Closed","budget_counters":{}}',
        encoding="utf-8",
    )
    (task.output_dir / "analysis_stage.json").write_text(
        '{"status":"analysis_complete","handoff_version":2,"handoff_ready":true}',
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_generation(*args, **kwargs):
        calls.append("generate")
        (task.output_dir / "patch.diff").write_text(
            "diff --git a/a.py b/a.py\n", encoding="utf-8"
        )
        (task.output_dir / "patch_outcome.json").write_text(
            '{"patch_outcome":"PATCH_SUCCESS"}', encoding="utf-8"
        )
        (task.output_dir / "compile_check.json").write_text(
            '[{"outcome":"PASSED"}]', encoding="utf-8"
        )
        return "image:test", 0

    monkeypatch.setattr(runner, "run_generation", fake_generation)
    monkeypatch.setattr(
        runner, "run_eval",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("eval called")),
    )
    monkeypatch.setattr(
        runner, "cleanup_docker", lambda **kwargs: calls.append("cleanup")
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=4.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "stage2.state.json",
        eval_module=object(),
        run=runner.RunContext(run_id="stage2-run"),
        eval_only=False,
        phase="stage2",
    )

    assert result["status"] == "success"
    assert "resolved" not in result
    assert calls == ["generate", "cleanup"]
    assert (task.output_dir / ANALYSIS_HANDOFF_CHECKPOINT).exists()


def test_stage2_model_infra_failure_returns_structured_payload(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "checkpoint.json").write_text(
        '{"pipeline_state":"Closed","budget_counters":{}}',
        encoding="utf-8",
    )
    (task.output_dir / "analysis_stage.json").write_text(
        '{"status":"analysis_complete","handoff_version":2,"handoff_ready":true}',
        encoding="utf-8",
    )

    def fake_generation(*args, **kwargs):
        (task.output_dir / "patch_outcome.json").write_text(
            '{"patch_outcome":"MODEL_INFRA_FAILURE"}', encoding="utf-8"
        )
        return "image:test", 0

    monkeypatch.setattr(runner, "run_generation", fake_generation)
    monkeypatch.setattr(
        runner, "cleanup_docker", lambda **kwargs: None
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=4.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "stage2-infra.state.json",
        eval_module=object(),
        run=runner.RunContext(run_id="stage2-infra"),
        eval_only=False,
        phase="stage2",
    )

    assert result["status"] == "infra_failed"
    assert result["failure_kind"] == "model_infra"
    assert result["patch_outcome"] == "MODEL_INFRA_FAILURE"
    assert result["phase"] == "stage2"


def test_stage2_docker_pull_failure_returns_structured_payload(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "checkpoint.json").write_text(
        '{"pipeline_state":"Closed","budget_counters":{}}',
        encoding="utf-8",
    )
    (task.output_dir / "analysis_stage.json").write_text(
        '{"status":"analysis_complete","handoff_version":2,"handoff_ready":true}',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        runner,
        "pull_first_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            runner.DockerInfraError("docker pull timed out")
        ),
    )
    monkeypatch.setattr(
        runner, "cleanup_docker", lambda **kwargs: None
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=4.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "stage2-docker-infra.state.json",
        eval_module=object(),
        run=runner.RunContext(run_id="stage2-docker-infra"),
        eval_only=False,
        phase="stage2",
    )

    assert result["status"] == "infra_failed"
    assert result["failure_kind"] == "docker_infra"
    assert result["patch_outcome"] == "DOCKER_INFRA_FAILURE"
    assert result["phase"] == "stage2"


def test_stage2_reruns_when_patch_exists_but_artifacts_are_not_usable(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n", encoding="utf-8"
    )
    (task.output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"BUILD_FAILED_AFTER_REPAIR"}', encoding="utf-8"
    )
    (task.output_dir / "compile_check.json").write_text(
        '[{"outcome":"FAILED_AFTER_REPAIR"}]', encoding="utf-8"
    )
    (task.output_dir / "checkpoint.json").write_text(
        '{"pipeline_state":"Closed","budget_counters":{}}',
        encoding="utf-8",
    )
    (task.output_dir / "analysis_stage.json").write_text(
        '{"status":"analysis_complete","handoff_version":2,"handoff_ready":true}',
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_generation(*args, **kwargs):
        calls.append("generate")
        (task.output_dir / "patch_outcome.json").write_text(
            '{"patch_outcome":"PATCH_SUCCESS"}', encoding="utf-8"
        )
        (task.output_dir / "compile_check.json").write_text(
            '[{"outcome":"PASSED"}]', encoding="utf-8"
        )
        return "image:test", 0

    monkeypatch.setattr(runner, "run_generation", fake_generation)
    monkeypatch.setattr(
        runner, "run_eval",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("eval called")),
    )
    monkeypatch.setattr(
        runner, "cleanup_docker", lambda **kwargs: calls.append("cleanup")
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=4.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "stage2-rerun.state.json",
        eval_module=object(),
        run=runner.RunContext(run_id="stage2-rerun"),
        eval_only=False,
        phase="stage2",
    )

    assert result["status"] == "success"
    assert calls == ["generate", "cleanup"]


def test_stage2_infra_failure_with_patch_diff_still_returns_infra_payload(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n", encoding="utf-8"
    )
    (task.output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"BUILD_FAILED_AFTER_REPAIR"}', encoding="utf-8"
    )
    (task.output_dir / "compile_check.json").write_text(
        '[{"outcome":"FAILED_AFTER_REPAIR"}]', encoding="utf-8"
    )
    (task.output_dir / ANALYSIS_HANDOFF_CHECKPOINT).write_text(
        '{"pipeline_state":"Closed","budget_counters":{"saved":1}}',
        encoding="utf-8",
    )
    (task.output_dir / ANALYSIS_HANDOFF_EVIDENCE).write_text(
        '{"version":"analysis-handoff"}', encoding="utf-8"
    )
    calls: list[str] = []

    def fake_generation(*args, **kwargs):
        calls.append("generate")
        (task.output_dir / "patch_outcome.json").write_text(
            '{"patch_outcome":"MODEL_INFRA_FAILURE"}', encoding="utf-8"
        )
        (task.output_dir / "compile_check.json").write_text("[]", encoding="utf-8")
        return "image:test", 0

    monkeypatch.setattr(runner, "run_generation", fake_generation)
    monkeypatch.setattr(
        runner, "cleanup_docker", lambda **kwargs: calls.append("cleanup")
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=4.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "stage2-infra-diff.state.json",
        eval_module=object(),
        run=runner.RunContext(run_id="stage2-infra-diff"),
        eval_only=False,
        phase="stage2",
    )

    assert result["status"] == "infra_failed"
    assert result["patch_outcome"] == "MODEL_INFRA_FAILURE"
    assert calls == ["generate", "cleanup"]


def test_stage2_rerun_restores_saved_analysis_handoff_from_terminal_checkpoint(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n", encoding="utf-8"
    )
    (task.output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"BUILD_FAILED_NO_REPAIR"}', encoding="utf-8"
    )
    (task.output_dir / "compile_check.json").write_text(
        '[{"outcome":"STATIC_GATE_FAILED"}]', encoding="utf-8"
    )
    (task.output_dir / "checkpoint.json").write_text(
        '{"pipeline_state":"PatchSuccess","budget_counters":{"seen":1}}',
        encoding="utf-8",
    )
    (task.output_dir / "analysis_stage.json").write_text(
        '{"status":"analysis_complete","handoff_version":2,"handoff_ready":true}',
        encoding="utf-8",
    )
    (task.output_dir / "evidence.json").write_text(
        '{"version":"mutated-stage2"}', encoding="utf-8"
    )
    (task.output_dir / ANALYSIS_HANDOFF_CHECKPOINT).write_text(
        '{"pipeline_state":"Closed","budget_counters":{"saved":1}}',
        encoding="utf-8",
    )
    (task.output_dir / ANALYSIS_HANDOFF_EVIDENCE).write_text(
        '{"version":"analysis-handoff"}', encoding="utf-8"
    )
    calls: list[str] = []

    def fake_generation(*args, **kwargs):
        calls.append("generate")
        checkpoint = load_json(task.output_dir / "checkpoint.json")
        evidence = load_json(task.output_dir / "evidence.json")
        assert checkpoint["pipeline_state"] == "Closed"
        assert checkpoint["budget_counters"] == {"saved": 1}
        assert evidence["version"] == "analysis-handoff"
        (task.output_dir / "patch_outcome.json").write_text(
            '{"patch_outcome":"PATCH_SUCCESS"}', encoding="utf-8"
        )
        (task.output_dir / "compile_check.json").write_text(
            '[{"outcome":"PASSED"}]', encoding="utf-8"
        )
        return "image:test", 0

    monkeypatch.setattr(runner, "run_generation", fake_generation)
    monkeypatch.setattr(
        runner, "run_eval",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("eval called")),
    )
    monkeypatch.setattr(
        runner, "cleanup_docker", lambda **kwargs: calls.append("cleanup")
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=4.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "stage2-restore.state.json",
        eval_module=object(),
        run=runner.RunContext(run_id="stage2-restore"),
        eval_only=False,
        phase="stage2",
    )

    assert result["status"] == "success"
    assert calls == ["generate", "cleanup"]


def test_stage2_empty_patch_can_resume_from_saved_analysis_handoff(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "patch.diff").write_text("", encoding="utf-8")
    (task.output_dir / "analysis_stage.json").write_text(
        '{"status":"analysis_complete","handoff_version":2,"handoff_ready":true}',
        encoding="utf-8",
    )
    (task.output_dir / ANALYSIS_HANDOFF_CHECKPOINT).write_text(
        '{"pipeline_state":"Closed","budget_counters":{"saved":1}}',
        encoding="utf-8",
    )
    (task.output_dir / ANALYSIS_HANDOFF_EVIDENCE).write_text(
        '{"version":"analysis-handoff"}', encoding="utf-8"
    )
    calls: list[str] = []

    def fake_generation(*args, **kwargs):
        calls.append("generate")
        checkpoint = load_json(task.output_dir / "checkpoint.json")
        assert checkpoint["pipeline_state"] == "Closed"
        (task.output_dir / "patch.diff").write_text(
            "diff --git a/a.py b/a.py\n", encoding="utf-8"
        )
        (task.output_dir / "patch_outcome.json").write_text(
            '{"patch_outcome":"PATCH_SUCCESS"}', encoding="utf-8"
        )
        (task.output_dir / "compile_check.json").write_text(
            '[{"outcome":"PASSED"}]', encoding="utf-8"
        )
        return "image:test", 0

    monkeypatch.setattr(runner, "run_generation", fake_generation)
    monkeypatch.setattr(
        runner, "cleanup_docker", lambda **kwargs: calls.append("cleanup")
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=4.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "stage2-empty-patch.state.json",
        eval_module=object(),
        run=runner.RunContext(run_id="stage2-empty-patch"),
        eval_only=False,
        phase="stage2",
    )

    assert result["status"] == "success"
    assert calls == ["generate", "cleanup"]


def test_phase3_runs_dynamic_closure_and_eval_in_one_container(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n", encoding="utf-8"
    )
    (task.output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"PATCH_SUCCESS"}', encoding="utf-8"
    )
    (task.output_dir / "compile_check.json").write_text(
        '[{"outcome":"PASSED"}]', encoding="utf-8"
    )
    (task.output_dir / "evidence.json").write_text("{}", encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(runner, "pull_first_image", lambda *a, **k: "image:test")
    monkeypatch.setattr(
        runner, "create_container", lambda **kwargs: calls.append("create")
    )
    monkeypatch.setattr(
        runner, "start_container_detached", lambda *a, **k: calls.append("start")
    )
    monkeypatch.setattr(
        runner,
        "run_dynamic_closure_stage",
        lambda *a, **k: calls.append("dynamic") or {"counts": {"PASS": 1}},
    )

    def fake_eval(*args, **kwargs):
        assert kwargs["existing_container"] is not None
        calls.append("eval")
        return {"resolved": True}

    monkeypatch.setattr(runner, "run_eval", fake_eval)
    monkeypatch.setattr(
        runner, "cleanup_docker", lambda **kwargs: calls.append("cleanup")
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=4.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "phase3.state.json",
        eval_module=object(),
        run=runner.RunContext(run_id="phase3-run"),
        eval_only=True,
        phase="phase3",
    )

    assert result["resolved"] is True
    assert calls == ["create", "start", "dynamic", "eval", "cleanup"]


def test_final_pass_phase3_evaluates_effective_failed_build_patch(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    (task.output_dir / "patch.diff").write_text(
        "diff --git a/a.py b/a.py\n", encoding="utf-8"
    )
    (task.output_dir / "patch_outcome.json").write_text(
        '{"patch_outcome":"BUILD_FAILED_AFTER_REPAIR"}', encoding="utf-8"
    )
    (task.output_dir / "compile_check.json").write_text(
        '[{"outcome":"FAILED_AFTER_REPAIR"}]', encoding="utf-8"
    )
    calls: list[str] = []

    monkeypatch.setattr(runner, "pull_first_image", lambda *a, **k: "image:test")
    monkeypatch.setattr(
        runner, "create_container", lambda **kwargs: calls.append("create")
    )
    monkeypatch.setattr(
        runner, "start_container_detached", lambda *a, **k: calls.append("start")
    )
    monkeypatch.setattr(
        runner,
        "run_dynamic_closure_stage",
        lambda *a, **k: calls.append("dynamic") or {"counts": {"FAIL": 1}},
    )
    monkeypatch.setattr(
        runner,
        "run_eval",
        lambda *a, **k: calls.append("eval") or {"resolved": False},
    )
    monkeypatch.setattr(
        runner, "cleanup_docker", lambda **kwargs: calls.append("cleanup")
    )

    result = runner.run_task(
        task,
        base_env={},
        dockerhub_users=["jefzda"],
        memory_gb=4.0,
        platform=None,
        force_restart=False,
        redo_eval=False,
        prune=True,
        state_file=tmp_path / "phase3-final-failed.state.json",
        eval_module=object(),
        run=runner.RunContext(run_id="phase3-final-failed"),
        eval_only=True,
        phase="phase3",
        allow_failed_patch_eval=True,
    )

    assert result["status"] == "success"
    assert result["resolved"] is False
    assert calls == ["create", "start", "dynamic", "eval", "cleanup"]


def test_attempt_history_cannot_regress_canonical_completed_result(
    tmp_path: Path, monkeypatch,
) -> None:
    task = _staged_task(tmp_path)
    task.output_dir.mkdir(parents=True)
    canonical = {
        "status": "success",
        "phase": "phase3",
        "run_id": "completed-attempt",
        "resolved": False,
    }
    (task.output_dir / "runner_task.json").write_text(
        json.dumps(canonical), encoding="utf-8"
    )
    monkeypatch.setattr(runner, "_artifact_progress_rank", lambda _task: 3)

    runner.persist_task_result(
        task,
        tmp_path / "runner.state.json",
        {
            "status": "infra_failed",
            "phase": "analysis",
            "run_id": "later-503",
            "failure_kind": "api_unavailable",
        },
    )

    assert load_json(task.output_dir / "runner_task.json") == canonical
    latest = load_json(task.output_dir / "runner_attempt.latest.json")
    assert latest["status"] == "infra_failed"
    assert latest["parent_attempt_id"] is None
    assert len(list((task.output_dir / "history" / "runner_attempts").glob("*.json"))) == 1
