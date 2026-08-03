from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "remote_run_staged_batch.sh"
)


def test_runner_failure_is_captured_without_triggering_errexit() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'if wait_for_runner "$run_name"; then' in text
    assert 'wait_for_runner "$run_name"\n    rc=$?' not in text


def test_registry_probe_retries_before_opening_infra_circuit() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "STAGED_REGISTRY_PROBE_MAX_ATTEMPTS:-4" in text
    assert "[infra-probe-retry]" in text
    assert "attempts=$max_attempts" in text


def test_final_pass_can_route_effective_failed_build_patches_to_phase3() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "ALLOW_FAILED_PATCH_EVAL" in text
    assignment = "allow_failed_patch_eval=${ALLOW_FAILED_PATCH_EVAL:-0}"
    guarded_use = 'if [ "${allow_failed_patch_eval:-0}" = "1" ]; then'
    assert assignment in text
    assert guarded_use in text
    assert text.index(assignment) < text.index(guarded_use)
    assert "final_failed_patch_ready" in text
    assert "--allow-failed-patch-eval" in text


def test_ready_manifest_filter_imports_os_and_accepts_saved_handoff() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "import os" in text
    assert 'saved_checkpoint = load(out / "checkpoint.analysis_handoff.json")' in text
    assert '(out / "evidence.analysis_handoff.json").is_file()' in text
    assert "os.replace(temporary, target)" in text


def test_stage_launcher_refuses_missing_or_unreadable_manifest() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'if [ ! -s "$manifest" ]; then' in text
    assert "manifest missing or empty" in text
    assert "manifest unreadable" in text
    assert '[[ "$task_count" =~ ^[0-9]+$ ]]' in text
