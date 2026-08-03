"""Requirement-level executable closure, run after a patch is frozen.

The synthesizer sees only requirements and a clean base tree. Generated tests
are frozen before the candidate patch is applied, then run on base and patched
trees in the same case container. Results are diagnostic and never feed back
into patch generation.
"""

from __future__ import annotations

import asyncio
import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.agents._cost_tracker import get_totals, reset as reset_cost_tracker
from src.agents._structured import run_structured_query
from src.models.context import EvidenceCards


_ALLOWED_COMMANDS = {
    "go", "python", "python3", "pytest", "node", "npm", "npx", "yarn",
    "mvn", "gradle", "./gradlew",
}
_NONCOMPLIANT = {"AS_IS_VIOLATED", "TO_BE_MISSING", "TO_BE_PARTIAL"}
_DEFAULT_SYNTHESIS_TIMEOUT_SECONDS = 240
_DEFAULT_CONTRACT_BATCH_SIZE = 3


def _synthesis_timeout_seconds() -> int:
    raw = os.environ.get("DYNAMIC_CLOSURE_SYNTH_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_SYNTHESIS_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_SYNTHESIS_TIMEOUT_SECONDS
    return max(30, value)


def _contract_batch_size() -> int:
    raw = os.environ.get("DYNAMIC_CLOSURE_CONTRACT_BATCH_SIZE", "").strip()
    if not raw:
        return _DEFAULT_CONTRACT_BATCH_SIZE
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_CONTRACT_BATCH_SIZE
    return max(1, min(6, value))


class RequirementTest(BaseModel):
    requirement_id: str
    filename: str
    source: str = Field(
        description=(
            "Complete raw compilable contents of the new test file. Never a "
            "path, prose, markdown fence, unified diff, or test plan. A Go "
            "test must begin with `package ...`."
        )
    )
    command: list[str]
    oracle_quotes: list[str] = Field(default_factory=list)
    target_locations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_shape(self) -> "RequirementTest":
        """Reject wrappers/diffs early so structured output gets retried."""
        source = self.source.lstrip("\ufeff \t\r\n")
        if source.startswith(("```", "--- ", "diff --git ")):
            raise ValueError("test source must be raw source code, not markdown or a diff")
        suffix = Path(self.filename).suffix.lower()
        if suffix == ".go":
            first_code = next(
                (
                    line.strip() for line in source.splitlines()
                    if line.strip() and not line.lstrip().startswith("//")
                ),
                "",
            )
            if not first_code.startswith("package "):
                raise ValueError("Go test source must begin with a package declaration")
        elif suffix == ".py":
            try:
                ast.parse(self.source)
            except SyntaxError as exc:
                raise ValueError(f"invalid Python test source: {exc.msg}") from exc
        return self


class ContractTests(BaseModel):
    contract_id: str
    tests: list[RequirementTest] = Field(default_factory=list)
    infeasible_requirement_ids: list[str] = Field(default_factory=list)


_SYSTEM = """\
You write executable grounding tests for atomic software requirements.
You see only the clean base repository and requirement text; you never see the
candidate patch or evaluator oracle. Return one independent test descriptor per
requirement when feasible. Assertions must be justified by an exact quote from
the requirement's source text. Do not modify production files, install
dependencies, use the network, or refer to hidden/gold tests. Test filenames
must clearly be test files. Commands must be argv arrays using the repository's
existing native test framework and must execute only the generated test or its
small package/module scope. Do not return repo-wide regression commands such as
`pytest -q`, `python -m pytest -q`, `go test ./...`, or `npm test`. If a
behavior has no focused executable oracle, mark that requirement infeasible
instead of guessing.
The contract_id may identify a small batch. Treat every payload entry as an
independent requirement and account for each id exactly once in either tests
or infeasible_requirement_ids.
The `source` field MUST contain the complete raw source code that will be
written verbatim to `filename`; it is never a filename, diff, prose plan, or
list of assertions. For Go, its first code line MUST be `package <name>`.
When explicit_symbols are provided, exercise the relevant symbol directly and
include a `path:symbol` entry in target_locations. Read existing repository
tests/imports before choosing package types; do not invent an import path.
"""


def _requirement_payload(items: list[object]) -> list[dict]:
    payload: list[dict] = []
    for item in items:
        span = getattr(item, "source_span", None)
        payload.append({
            "id": item.id,
            "parent_contract_id": item.parent_contract_id,
            "verdict": item.verdict,
            "text": item.text,
            "source_text": getattr(span, "text", "") if span else item.text,
            "explicit_paths": list(getattr(item, "explicit_paths", []) or []),
            "explicit_symbols": list(getattr(item, "explicit_symbols", []) or []),
            "evidence_locations": list(item.evidence_locations),
            "findings": getattr(item, "findings", "") or getattr(item, "short_reason", ""),
        })
    return payload


def _safe_test_path(value: str) -> str | None:
    path = value.replace("\\", "/").strip().lstrip("./")
    if not path or path.startswith("/") or ".." in Path(path).parts:
        return None
    name = Path(path).name.lower()
    if not any(token in name for token in ("test", "spec")):
        return None
    return path


def _validate_test(test: RequirementTest, item: object) -> str | None:
    safe = _safe_test_path(test.filename)
    if safe is None:
        return "unsafe or non-test filename"
    if not test.command or test.command[0] not in _ALLOWED_COMMANDS:
        return "unsupported test command"
    scoped_reason = _validate_focused_command(test, safe)
    if scoped_reason:
        return scoped_reason
    source_text = (
        getattr(getattr(item, "source_span", None), "text", "") or item.text
    )
    if not test.oracle_quotes or any(q not in source_text for q in test.oracle_quotes):
        return "oracle quote is not traceable to source span"
    if not test.source.strip():
        return "empty test source"
    evidence_files = {
        str(location).replace("\\", "/").partition(":")[0]
        for location in getattr(item, "evidence_locations", [])
        if str(location).strip()
    }
    target_files = {
        str(location).replace("\\", "/").partition(":")[0]
        for location in test.target_locations
        if str(location).strip()
    }
    if not target_files:
        return "missing evidence attribution target"
    if not evidence_files or not target_files.issubset(evidence_files):
        return "attribution target is not owned by requirement evidence"
    return None


def _is_pytest_command(command: list[str]) -> bool:
    if not command:
        return False
    if command[0] == "pytest":
        return True
    return len(command) >= 3 and command[0] in {"python", "python3"} and command[1:3] == ["-m", "pytest"]


def _pytest_args(command: list[str]) -> list[str]:
    if command[0] == "pytest":
        return command[1:]
    return command[3:]


def _validate_focused_command(test: RequirementTest, safe_path: str) -> str | None:
    """Reject repo-wide commands that turn dynamic closure into full eval."""
    command = [str(part) for part in test.command]
    if _is_pytest_command(command):
        args = _pytest_args(command)
        positional = [arg for arg in args if arg and not arg.startswith("-")]
        normalized = {arg.replace("\\", "/").lstrip("./") for arg in positional}
        safe_parent = str(Path(safe_path).parent).replace("\\", "/")
        if not positional:
            return "test command must target the generated pytest file"
        if safe_path not in normalized and safe_parent not in normalized:
            return "test command must target the generated pytest file"
        return None

    if command[:2] == ["go", "test"]:
        args = command[2:]
        if "./..." in args or "..." in args:
            return "test command must not run the entire Go module"
        if "-run" not in args:
            return "go test command must include -run for the generated test"
        return None

    if command[0] in {"npm", "yarn"} and len(command) <= 2:
        return "test command must not run the entire package test suite"

    return None


def _cleanup_timed_out_command(container: str, args: list[str]) -> None:
    patterns: list[str] = []
    if _is_pytest_command(args):
        patterns.extend(["pytest", "python.*-m pytest"])
    elif args[:2] == ["go", "test"]:
        patterns.append("go test")
    elif args:
        patterns.append(r"\s+".join(re.escape(part) for part in args[:3]))
    for pattern in patterns:
        script = (
            "if command -v pkill >/dev/null 2>&1; then "
            f"pkill -TERM -f {shlex.quote(pattern)} || true; "
            "sleep 2; "
            f"pkill -KILL -f {shlex.quote(pattern)} || true; "
            "fi"
        )
        subprocess.run(
            ["docker", "exec", container, "sh", "-lc", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )


def _docker_exec(
    container: str,
    args: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 300,
) -> tuple[int, str, bool]:
    try:
        docker_args = ["docker", "exec"]
        if input_text is not None:
            docker_args.append("-i")
        docker_args.extend(["-w", "/app", container, *args])
        proc = subprocess.run(
            docker_args,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), False
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        _cleanup_timed_out_command(container, args)
        return 124, out, True


def _reset(container: str, base_commit: str) -> tuple[int, str]:
    rc, out, _ = _docker_exec(
        container,
        ["git", "reset", "--hard", base_commit],
        timeout=120,
    )
    if rc:
        return rc, out
    rc2, out2, _ = _docker_exec(container, ["git", "clean", "-fd"], timeout=120)
    return rc2, out + out2


def _write_tests(container: str, tests: list[RequirementTest]) -> tuple[int, str]:
    for test in tests:
        path = _safe_test_path(test.filename)
        if path is None:
            return 2, f"invalid test path: {test.filename}"
        parent = str(Path(path).parent).replace("\\", "/")
        if parent not in {"", "."}:
            rc, out, _ = _docker_exec(container, ["mkdir", "-p", parent])
            if rc:
                return rc, out
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=Path(path).suffix, delete=False
        ) as handle:
            handle.write(test.source)
            temp_name = handle.name
        try:
            copied = subprocess.run(
                ["docker", "cp", temp_name, f"{container}:/app/{path}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if copied.returncode:
                return copied.returncode, (copied.stdout or "") + (copied.stderr or "")
        finally:
            Path(temp_name).unlink(missing_ok=True)
    return 0, ""


def _apply_patch(container: str, patch_text: str) -> tuple[int, str]:
    rc, out, _ = _docker_exec(
        container,
        ["git", "apply", "--whitespace=nowarn", "--binary", "-"],
        input_text=patch_text,
        timeout=120,
    )
    return rc, out


def _target_signal(test: RequirementTest, output: str) -> bool:
    blob = test.source + "\n" + output
    for loc in test.target_locations:
        head, _, tail = loc.partition(":")
        if head and (head in output or Path(head).name in output):
            return True
        if tail and not tail[:1].isdigit() and re.search(rf"\b{re.escape(tail)}\b", blob):
            return True
    return False


def _run_test(container: str, test: RequirementTest) -> dict:
    rc, out, timed_out = _docker_exec(container, test.command, timeout=300)
    return {
        "returncode": rc,
        "timed_out": timed_out,
        "output_tail": out[-4000:],
        "target_signal": _target_signal(test, out),
    }


def _unexpected(result: dict, expected_pass: bool) -> bool:
    return result["timed_out"] or ((result["returncode"] == 0) != expected_pass)


def _run_with_flake_probe(
    container: str, test: RequirementTest, expected_pass: bool,
) -> tuple[dict, bool]:
    first = _run_test(container, test)
    if not _unexpected(first, expected_pass):
        return first, False
    second = _run_test(container, test)
    flaky = first["returncode"] != second["returncode"] or first["timed_out"] != second["timed_out"]
    return second, flaky


def _base_matches_expected(result: dict, expected_pass: bool) -> bool:
    return not result["timed_out"] and ((result["returncode"] == 0) == expected_pass)


def _patched_passes(result: dict) -> bool:
    return not result["timed_out"] and result["returncode"] == 0


def _classify_matrix(
    *,
    expected_base_pass: bool,
    base: dict,
    patched: dict,
) -> tuple[Literal["PASS", "FAIL", "UNVERIFIABLE"], str]:
    """Classify a frozen oracle outcome.

    A generated test can only indict the candidate patch after it first proves
    it is a valid oracle for the base tree.  If the base outcome does not match
    the requirement verdict, the test is not a reliable dynamic closure signal.
    """
    attribution_ok = bool(base.get("target_signal") or patched.get("target_signal"))
    if not attribution_ok:
        return "UNVERIFIABLE", "test did not expose a target path/symbol signal"
    if not _base_matches_expected(base, expected_base_pass):
        return "UNVERIFIABLE", "generated oracle did not reproduce expected base state"
    if not _patched_passes(patched):
        return "FAIL", "patched tree failed frozen oracle after base matched expected state"
    return "PASS", "expected base/patched matrix and target signal observed"


async def _synthesize_contract(
    contract_id: str,
    items: list[object],
    base_repo: Path,
) -> ContractTests:
    payload = _requirement_payload(items)
    prompt = (
        f"Contract id: {contract_id}\n"
        "Generate tests for these requirements:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return await run_structured_query(
        system_prompt=_SYSTEM,
        user_prompt=prompt,
        response_model=ContractTests,
        component="dynamic-closure-synth",
        allowed_tools=["Read", "Grep", "Glob"],
        max_turns=20,
        max_budget_usd=1.5,
        cwd=str(base_repo),
        max_attempts=3,
        call_reason="dynamic_closure",
    )


def _export_base_repo(repo_dir: Path, base_commit: str, destination: Path) -> None:
    archive = subprocess.Popen(
        ["git", "-C", str(repo_dir), "archive", base_commit],
        stdout=subprocess.PIPE,
    )
    extract = subprocess.run(
        ["tar", "-xf", "-", "-C", str(destination)],
        stdin=archive.stdout,
        capture_output=True,
        check=False,
    )
    if archive.stdout:
        archive.stdout.close()
    archive.wait()
    if archive.returncode or extract.returncode:
        raise RuntimeError("failed to export clean base repository")


async def run_dynamic_closure(
    *,
    evidence_path: Path,
    repo_dir: Path,
    patch_path: Path,
    base_commit: str,
    container: str,
    output_path: Path,
) -> dict:
    """Generate frozen tests and execute base/patched differential closure."""
    started = time.monotonic()
    reset_cost_tracker()
    evidence = EvidenceCards.model_validate_json(evidence_path.read_text(encoding="utf-8"))
    items = [*evidence.requirements, *evidence.requirement_status]
    by_contract: dict[str, list[object]] = defaultdict(list)
    for item in items:
        by_contract[item.parent_contract_id or f"contract-{item.id}"].append(item)

    generated: dict[str, RequirementTest] = {}
    invalid: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="dynamic-closure-base-") as tmp:
        base_repo = Path(tmp)
        _export_base_repo(repo_dir, base_commit, base_repo)
        contract_groups = list(by_contract.items())
        batch_size = _contract_batch_size()
        batches = [
            contract_groups[index:index + batch_size]
            for index in range(0, len(contract_groups), batch_size)
        ]
        total_batches = len(batches)
        synth_timeout = _synthesis_timeout_seconds()
        for index, batch in enumerate(batches, start=1):
            contract_ids = [contract_id for contract_id, _ in batch]
            contract_items = [item for _, group in batch for item in group]
            batch_id = "+".join(contract_ids)
            print(
                "[dynamic-closure] "
                f"synthesizing batch {index}/{total_batches}: {batch_id} "
                f"contracts={len(batch)} requirements={len(contract_items)} "
                f"timeout={synth_timeout}s",
                flush=True,
            )
            try:
                bundle = await asyncio.wait_for(
                    _synthesize_contract(batch_id, contract_items, base_repo),
                    timeout=synth_timeout,
                )
                print(
                    "[dynamic-closure] "
                    f"batch {index}: generated={len(bundle.tests)} "
                    f"infeasible={len(bundle.infeasible_requirement_ids)}",
                    flush=True,
                )
            except TimeoutError as exc:
                for item in contract_items:
                    invalid[item.id] = f"synthesis timed out after {synth_timeout}s"
                print(
                    "[dynamic-closure] "
                    f"batch {index}: synthesis timed out after {synth_timeout}s",
                    flush=True,
                )
                continue
            except Exception as exc:
                for item in contract_items:
                    invalid[item.id] = f"synthesis failed: {type(exc).__name__}: {exc}"
                print(
                    "[dynamic-closure] "
                    f"batch {index}: synthesis failed: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue
            item_map = {item.id: item for item in contract_items}
            for rid in bundle.infeasible_requirement_ids:
                if rid in item_map:
                    invalid[rid] = "no executable oracle"
            for test in bundle.tests:
                item = item_map.get(test.requirement_id)
                if item is None or test.requirement_id in generated:
                    continue
                reason = _validate_test(test, item)
                if reason:
                    invalid[test.requirement_id] = reason
                else:
                    generated[test.requirement_id] = test
            for item in contract_items:
                if item.id not in generated and item.id not in invalid:
                    invalid[item.id] = "missing generated test"

    # Results must be independent. A duplicate path would make one generated
    # source overwrite another, even though execution is isolated below.
    filename_owner: dict[str, str] = {}
    for rid, test in list(generated.items()):
        path = _safe_test_path(test.filename)
        assert path is not None  # validated above
        if path in filename_owner:
            invalid[rid] = f"test filename collides with {filename_owner[path]}"
            del generated[rid]
        else:
            filename_owner[path] = rid

    tests = list(generated.values())
    frozen_hash = hashlib.sha256(
        "\n".join(
            f"{t.requirement_id}\0{t.filename}\0{t.source}\0{json.dumps(t.command)}"
            for t in sorted(tests, key=lambda x: x.requirement_id)
        ).encode("utf-8")
    ).hexdigest()
    item_map = {item.id: item for item in items}
    base_results: dict[str, dict] = {}
    patched_results: dict[str, dict] = {}
    execution_errors: dict[str, str] = {}

    # Each requirement gets a clean worktree containing only its own test.
    # Native commands such as `go test ./scanner` can otherwise discover tests
    # generated for other requirements and contaminate the result matrix.
    for test in tests:
        rid = test.requirement_id
        print(
            "[dynamic-closure] "
            f"executing frozen test for {rid}: {test.filename} cmd={test.command}",
            flush=True,
        )
        rc, out = _reset(container, base_commit)
        if rc:
            execution_errors[rid] = "base reset failed: " + out[-1000:]
            continue
        rc, out = _write_tests(container, [test])
        if rc:
            execution_errors[rid] = "failed to write frozen test on base: " + out[-1000:]
            continue
        expected_base_pass = getattr(item_map[rid], "verdict", "") not in _NONCOMPLIANT
        result, flaky = _run_with_flake_probe(container, test, expected_base_pass)
        result["flaky"] = flaky
        base_results[rid] = result

        rc, out = _reset(container, base_commit)
        if rc:
            execution_errors[rid] = "patched reset failed: " + out[-1000:]
            continue
        rc, out = _apply_patch(container, patch_path.read_text(encoding="utf-8"))
        if rc:
            execution_errors[rid] = "candidate patch apply failed: " + out[-1000:]
            continue
        rc, out = _write_tests(container, [test])
        if rc:
            execution_errors[rid] = "failed to write frozen test on patched tree: " + out[-1000:]
            continue
        result, flaky = _run_with_flake_probe(container, test, True)
        result["flaky"] = flaky
        patched_results[rid] = result

    results: list[dict] = []
    counts = {"PASS": 0, "FAIL": 0, "UNVERIFIABLE": 0, "FLAKY_UNVERIFIABLE": 0}
    for item in items:
        rid = item.id
        status: Literal["PASS", "FAIL", "UNVERIFIABLE", "FLAKY_UNVERIFIABLE"]
        reason = invalid.get(rid, "")
        base = base_results.get(rid)
        patched = patched_results.get(rid)
        execution_error = execution_errors.get(rid, "")
        if execution_error or reason or base is None or patched is None:
            status = "UNVERIFIABLE"
            reason = reason or execution_error or "missing execution result"
        elif base.get("flaky") or patched.get("flaky"):
            status = "FLAKY_UNVERIFIABLE"
            reason = "inconsistent rerun outcome"
        else:
            expected_base_pass = item.verdict not in _NONCOMPLIANT
            status, reason = _classify_matrix(
                expected_base_pass=expected_base_pass,
                base=base,
                patched=patched,
            )
        counts[status] += 1
        test = generated.get(rid)
        results.append({
            "requirement_id": rid,
            "contract_id": item.parent_contract_id,
            "verdict": item.verdict,
            "status": status,
            "reason": reason,
            "test_filename": test.filename if test else None,
            "test_hash": hashlib.sha256(test.source.encode()).hexdigest() if test else None,
            "test_source": test.source if test else None,
            "test_command": test.command if test else None,
            "oracle_quotes": test.oracle_quotes if test else None,
            "target_locations": test.target_locations if test else None,
            "base": base,
            "patched": patched,
        })

    _reset(container, base_commit)
    usage = get_totals()
    payload = {
        "schema_version": 1,
        "frozen_test_bundle_hash": frozen_hash,
        "counts": counts,
        "wall_clock_seconds": round(time.monotonic() - started, 1),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "total_cost_usd": usage.get("total_cost_usd", 0.0),
        "estimated_cost_usd": usage.get("estimated_cost_usd", 0.0),
        "infrastructure_errors": execution_errors,
        "requirements": results,
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def run_dynamic_closure_sync(**kwargs) -> dict:
    return asyncio.run(run_dynamic_closure(**kwargs))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run requirement-level dynamic closure")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run_dynamic_closure_sync(
        evidence_path=args.evidence,
        repo_dir=args.repo,
        patch_path=args.patch,
        base_commit=args.base_commit,
        container=args.container,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
