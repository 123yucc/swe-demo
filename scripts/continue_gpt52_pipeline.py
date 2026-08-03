#!/usr/bin/env python3
"""Finish eval-only retries, then start and verify the GPT-5.2 081-731 supervisor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "logs" / "runs"
RUNTIME = ROOT / "runtime" / "gpt52-731"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temporary, path)


def runner_alive(run_dir: Path) -> bool:
    try:
        pid = int((run_dir / "runner.pid").read_text().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        return False


def aggregate_attempts(run_dirs: list[Path], targets: set[str]) -> tuple[set[str], dict[str, int]]:
    successful: set[str] = set()
    attempt_counts = {issue: 0 for issue in targets}
    for run_dir in run_dirs:
        tasks = load_json(run_dir / "runner.state.json").get("tasks") or {}
        seen_this_run: set[str] = set()
        for payload in tasks.values():
            issue_name = str(payload.get("issue") or "")
            issue = issue_name.removeprefix("swe_issue_").zfill(3)
            status = str(payload.get("status") or "")
            if issue not in targets or status not in {"success", "failed", "infra_failed"}:
                continue
            if issue not in seen_this_run:
                attempt_counts[issue] += 1
                seen_this_run.add(issue)
            if status == "success":
                successful.add(issue)
    return successful, attempt_counts


def registry_ready() -> bool:
    result = subprocess.run(
        [
            "curl",
            "--proxy",
            "http://127.0.0.1:7897",
            "--silent",
            "--show-error",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--max-time",
            "20",
            "https://registry-1.docker.io/v2/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "401"


def build_retry_manifest(base: Path, target: Path, issues: list[str]) -> None:
    document = load_json(base)
    document["issues"] = issues
    document["expected_issue_count"] = len(issues)
    document["max_workers"] = min(2, len(issues)) or 1
    write_json(target, document)


def resolved_baseline_changes(selection: dict) -> list[str]:
    changed = []
    for row in selection.get("resolved_eval_baseline") or []:
        path = Path(row["path"])
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "<missing>"
        if digest != row.get("sha256"):
            changed.append(str(row.get("issue")))
    return changed


def verify_supervisor_runtime(state: dict, timeout_seconds: int = 1800) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        runner_commands = []
        for pid_path in RUNS.glob("gpt52-731-*/runner.pid"):
            try:
                pid = int(pid_path.read_text().strip())
                command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            except (OSError, ValueError):
                continue
            if "local_swebench_runner.py" in command:
                runner_commands.append(command)
        analysis_commands = [
            command
            for command in runner_commands
            if "--phase analysis" in command
        ]
        supervisor = load_json(RUNTIME / "supervisor.state.json")
        attempt_id = str(supervisor.get("attempt_id") or "")
        manifests = sorted((RUNTIME / "manifests" / attempt_id).glob("batch-*.json"))
        if analysis_commands and manifests:
            manifest = load_json(manifests[-1])
            model = (manifest.get("models") or [{}])[0]
            env = {
                **dict((manifest.get("defaults") or {}).get("env") or {}),
                **dict(model.get("env") or {}),
            }
            capacity = {
                **dict((manifest.get("defaults") or {}).get("capacity") or {}),
                **dict(model.get("capacity") or {}),
            }
            result = {
                "verified_at": utc_now(),
                "runtime_manifest": str(manifests[-1]),
                "analysis_workers_manifest": capacity.get("analysis_workers"),
                "endpoint": env.get("OPENAI_BASE_URL"),
                "ca": env.get("OPENAI_CA_CERT_PATH"),
                "analysis_commands": analysis_commands,
            }
            result["valid"] = (
                capacity.get("analysis_workers") == 8
                and env.get("OPENAI_BASE_URL") == "https://165.154.193.90"
                and env.get("OPENAI_CA_CERT_PATH")
                == "/home/user/demo/runtime/caddy_ca.ip.pem"
                and all("--max-workers 8" in command for command in analysis_commands)
            )
            return result
        if supervisor.get("status") in {"complete", "incomplete"}:
            break
        time.sleep(30)
    return {"verified_at": utc_now(), "valid": False, "error": "runtime verification timed out"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--run-prefix", required=True)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    selection_path = args.selection.resolve()
    selection = load_json(selection_path)
    targets = set(selection.get("unresolved_with_effective_patch") or [])
    base_manifest = Path(selection["manifest"])
    controller_dir = selection_path.parent
    state_path = controller_dir / "continuation.state.json"
    state = {
        "status": "waiting_for_eval_retries",
        "started_at": utc_now(),
        "selection": str(selection_path),
        "target_count": len(targets),
        "max_attempts_per_case": args.max_attempts,
        "launched_runs": [],
    }
    write_json(state_path, state)

    while True:
        run_dirs = sorted(
            path for path in RUNS.glob(f"{args.run_prefix}*") if path.is_dir()
        )
        active = [path for path in run_dirs if runner_alive(path)]
        successful, attempts = aggregate_attempts(run_dirs, targets)
        pending = sorted(targets - successful)
        exhausted = sorted(issue for issue in pending if attempts[issue] >= args.max_attempts)
        state.update(
            {
                "updated_at": utc_now(),
                "successful_count": len(successful),
                "pending_count": len(pending),
                "pending_issues": pending,
                "exhausted_issues": exhausted,
                "active_runs": [path.name for path in active],
                "attempt_counts": attempts,
            }
        )
        write_json(state_path, state)
        if active:
            time.sleep(args.poll_seconds)
            continue
        if not pending:
            break
        if exhausted:
            state.update({"status": "blocked_after_max_attempts", "finished_at": utc_now()})
            write_json(state_path, state)
            return 75
        if not registry_ready():
            state["status"] = "waiting_for_registry"
            write_json(state_path, state)
            time.sleep(args.poll_seconds)
            continue

        retry_issues = sorted(issue for issue in pending if attempts[issue] < args.max_attempts)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_name = f"{args.run_prefix}-auto-{stamp}"
        manifest = controller_dir / "retry-manifests" / f"{run_name}.json"
        build_retry_manifest(base_manifest, manifest, retry_issues)
        completed = subprocess.run(
            [
                "bash",
                "scripts/remote_start_runner.sh",
                str(manifest),
                run_name,
                "--phase",
                "evaluate",
                "--max-workers",
                "2",
                "--per-task-gb",
                "8",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        state["launched_runs"].append(
            {
                "run_name": run_name,
                "manifest": str(manifest),
                "issues": retry_issues,
                "launcher_rc": completed.returncode,
                "launcher_stdout": completed.stdout,
                "launcher_stderr": completed.stderr,
                "started_at": utc_now(),
            }
        )
        if completed.returncode != 0:
            state.update({"status": "retry_launcher_failed", "finished_at": utc_now()})
            write_json(state_path, state)
            return 1
        state["status"] = "waiting_for_eval_retries"
        write_json(state_path, state)
        time.sleep(args.poll_seconds)

    changed = resolved_baseline_changes(selection)
    if changed:
        state.update(
            {
                "status": "resolved_baseline_changed",
                "changed_resolved_issues": changed,
                "finished_at": utc_now(),
            }
        )
        write_json(state_path, state)
        return 1

    preflight = subprocess.run(
        ["bash", "scripts/start_gpt52_731.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    state["supervisor_start"] = {
        "returncode": preflight.returncode,
        "stdout": preflight.stdout,
        "stderr": preflight.stderr,
        "started_at": utc_now(),
    }
    if preflight.returncode != 0:
        state.update({"status": "supervisor_start_failed", "finished_at": utc_now()})
        write_json(state_path, state)
        return 1

    state["status"] = "verifying_supervisor_runtime"
    write_json(state_path, state)
    verification = verify_supervisor_runtime(state)
    state["runtime_verification"] = verification
    state.update(
        {
            "status": "supervisor_started" if verification.get("valid") else "runtime_verification_failed",
            "finished_at": utc_now(),
        }
    )
    write_json(state_path, state)
    return 0 if verification.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
