from pathlib import Path

from eval.local_swebench_runner import (
    IssueSpec,
    ModelSpec,
    TaskSpec,
    can_eval_existing_patch,
    container_name,
    patch_has_effective_diff,
    should_skip_task,
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

    skip, _ = should_skip_task(task, force_restart=False, redo_eval=False, eval_only=True)
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
