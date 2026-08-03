from types import SimpleNamespace
import subprocess
import pytest
from pydantic import ValidationError

from src.orchestrator import dynamic_closure
from src.orchestrator.dynamic_closure import (
    RequirementTest,
    _classify_matrix,
    _safe_test_path,
    _target_signal,
    _validate_test,
)


def test_contract_batch_size_is_bounded(monkeypatch) -> None:
    monkeypatch.delenv("DYNAMIC_CLOSURE_CONTRACT_BATCH_SIZE", raising=False)
    assert dynamic_closure._contract_batch_size() == 3
    monkeypatch.setenv("DYNAMIC_CLOSURE_CONTRACT_BATCH_SIZE", "20")
    assert dynamic_closure._contract_batch_size() == 6
    monkeypatch.setenv("DYNAMIC_CLOSURE_CONTRACT_BATCH_SIZE", "invalid")
    assert dynamic_closure._contract_batch_size() == 3


def test_docker_exec_keeps_stdin_open_for_patch_input(monkeypatch) -> None:
    seen: list[str] = []

    def fake_run(cmd, **kwargs):
        seen.extend(cmd)
        assert kwargs["input"] == "patch"
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(dynamic_closure.subprocess, "run", fake_run)
    rc, _, _ = dynamic_closure._docker_exec(
        "case", ["git", "apply", "-"], input_text="patch"
    )

    assert rc == 0
    assert seen[:4] == ["docker", "exec", "-i", "-w"]


def test_go_test_source_rejects_diff_for_structured_retry() -> None:
    with pytest.raises(ValidationError, match="raw source code"):
        RequirementTest(
            requirement_id="req-001",
            filename="scanner/dynamic_req_001_test.go",
            source="--- a/scanner/test.go\n+++ b/scanner/test.go\n",
            command=["go", "test", "./scanner"],
        )


def _item():
    return SimpleNamespace(
        id="req-001",
        text="parseLine must reject an invalid source package",
        evidence_locations=["scanner/parser.go:10-20"],
        source_span=SimpleNamespace(
            text="parseLine must reject an invalid source package"
        ),
    )


def test_generated_test_requires_traceable_oracle_quote() -> None:
    test = RequirementTest(
        requirement_id="req-001",
        filename="scanner/dynamic_req_001_test.go",
        source="package scanner\nfunc TestDynamicReq001(t *testing.T) { parseLine(\"bad\") }",
        command=["go", "test", "-run", "TestDynamicReq001", "./scanner"],
        oracle_quotes=["invented behavior"],
        target_locations=["scanner/parser.go:parseLine"],
    )

    assert _validate_test(test, _item()) == "oracle quote is not traceable to source span"


def test_generated_test_rejects_production_filename_and_shell() -> None:
    assert _safe_test_path("scanner/parser.go") is None
    test = RequirementTest(
        requirement_id="req-001",
        filename="scanner/dynamic_req_001_test.go",
        source="package scanner\n",
        command=["bash", "-lc", "anything"],
        oracle_quotes=["parseLine must reject an invalid source package"],
    )
    assert _validate_test(test, _item()) == "unsupported test command"


def test_target_signal_accepts_runtime_path_or_direct_symbol_reference() -> None:
    test = RequirementTest(
        requirement_id="req-001",
        filename="scanner/dynamic_req_001_test.go",
        source="package scanner\nfunc TestDynamic(t *testing.T) { parseLine(\"bad\") }",
        command=["go", "test"],
        oracle_quotes=["parseLine must reject an invalid source package"],
        target_locations=["scanner/parser.go:parseLine"],
    )

    assert _target_signal(test, "") is True
    test.source = "callThroughPublicAPI()"
    assert _target_signal(test, "panic at scanner/parser.go:42") is True


def test_generated_target_must_belong_to_requirement_evidence() -> None:
    test = RequirementTest(
        requirement_id="req-001",
        filename="scanner/dynamic_req_001_test.go",
        source="package scanner\nfunc TestDynamicReq001(t *testing.T) { parseLine(\"bad\") }",
        command=["go", "test", "-run", "TestDynamicReq001", "./scanner"],
        oracle_quotes=["parseLine must reject an invalid source package"],
        target_locations=["invented/other.go:parseLine"],
    )

    assert _validate_test(test, _item()) == (
        "attribution target is not owned by requirement evidence"
    )

    test.target_locations = ["scanner/parser.go:parseLine"]
    assert _validate_test(test, _item()) is None


def test_dynamic_closure_rejects_repo_wide_pytest_commands() -> None:
    test = RequirementTest(
        requirement_id="req-001",
        filename="scanner/test_dynamic_req_001.py",
        source="def test_dynamic_req_001():\n    assert True\n",
        command=["pytest", "-q"],
        oracle_quotes=["parseLine must reject an invalid source package"],
        target_locations=["scanner/parser.go:parseLine"],
    )

    assert _validate_test(test, _item()) == (
        "test command must target the generated pytest file"
    )

    test.command = ["python", "-m", "pytest", "-q"]
    assert _validate_test(test, _item()) == (
        "test command must target the generated pytest file"
    )

    test.command = ["python", "-m", "pytest", "-q", "scanner/test_dynamic_req_001.py"]
    assert _validate_test(test, _item()) is None


def test_dynamic_closure_rejects_repo_wide_go_test_commands() -> None:
    test = RequirementTest(
        requirement_id="req-001",
        filename="scanner/dynamic_req_001_test.go",
        source="package scanner\nfunc TestDynamicReq001(t *testing.T) { parseLine(\"bad\") }",
        command=["go", "test", "./..."],
        oracle_quotes=["parseLine must reject an invalid source package"],
        target_locations=["scanner/parser.go:parseLine"],
    )

    assert _validate_test(test, _item()) == (
        "test command must not run the entire Go module"
    )

    test.command = ["go", "test", "./scanner"]
    assert _validate_test(test, _item()) == (
        "go test command must include -run for the generated test"
    )

    test.command = ["go", "test", "-run", "TestDynamicReq001", "./scanner"]
    assert _validate_test(test, _item()) is None


def test_dynamic_closure_matrix_requires_valid_base_oracle() -> None:
    base = {"returncode": 0, "timed_out": False, "target_signal": True}
    patched = {"returncode": 1, "timed_out": False, "target_signal": True}

    status, reason = _classify_matrix(
        expected_base_pass=False,
        base=base,
        patched=patched,
    )

    assert status == "UNVERIFIABLE"
    assert reason == "generated oracle did not reproduce expected base state"


def test_dynamic_closure_matrix_fails_only_after_base_matches() -> None:
    base = {"returncode": 1, "timed_out": False, "target_signal": True}
    patched = {"returncode": 1, "timed_out": False, "target_signal": True}

    status, reason = _classify_matrix(
        expected_base_pass=False,
        base=base,
        patched=patched,
    )

    assert status == "FAIL"
    assert reason == "patched tree failed frozen oracle after base matched expected state"


def test_dynamic_closure_matrix_passes_when_base_and_patch_close() -> None:
    base = {"returncode": 1, "timed_out": False, "target_signal": True}
    patched = {"returncode": 0, "timed_out": False, "target_signal": False}

    status, reason = _classify_matrix(
        expected_base_pass=False,
        base=base,
        patched=patched,
    )

    assert status == "PASS"
    assert reason == "expected base/patched matrix and target signal observed"
