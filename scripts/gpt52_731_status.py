"""Print aggregate progress and host/container health for the GPT-5.2 run."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "workdir"
RUNTIME = ROOT / "runtime" / "gpt52-731"
OUTPUT_SUBDIR = "outputs_gpt-5.2"
MANIFEST = ROOT / "eval" / "manifests" / "swebench-pro-081-731.gpt5.2.json"


def effective_patch(path: Path) -> bool:
    try:
        return "diff --git " in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def output_dir(label: int) -> Path:
    return WORKDIR / f"swe_issue_{label:03d}" / OUTPUT_SUBDIR


def phase3_complete(out: Path) -> bool:
    return (
        effective_patch(out / "patch.diff")
        and (out / "compile_check.json").is_file()
        and (out / "dynamic_closure.json").is_file()
        and (out / "eval_result" / "eval_summary.json").is_file()
    )


def stage2_ready(out: Path) -> bool:
    return (
        effective_patch(out / "patch.diff")
        and (out / "compile_check.json").is_file()
        and load_json(out / "patch_outcome.json").get("patch_outcome")
        in {"PATCH_SUCCESS", "BUILD_UNVERIFIABLE"}
    )


def classify(label: int) -> str:
    out = output_dir(label)
    if phase3_complete(out):
        return "phase3_complete"
    task_path = out / "runner_task.json"
    analysis_path = out / "analysis_stage.json"
    task_status = str(load_json(task_path).get("status") or "")
    analysis_complete = load_json(analysis_path).get("status") == "analysis_complete"
    task_is_latest = (
        task_path.is_file()
        and (
            not analysis_path.is_file()
            or task_path.stat().st_mtime >= analysis_path.stat().st_mtime
        )
    )
    if task_status == "infra_failed" and task_is_latest:
        return "infra_failed"
    if stage2_ready(out):
        return "stage2_complete"
    if task_status == "failed" and task_is_latest:
        return "failed"
    if analysis_complete:
        return "analysis_complete"
    return "not_started_or_running"


def command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=5
        )
    except subprocess.TimeoutExpired:
        return "<timed out>"
    return result.stdout.strip()


def pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def main() -> None:
    print(f"GPT-5.2 SWE-bench Pro 081-731 | {datetime.now().isoformat(timespec='seconds')}")
    if MANIFEST.is_file():
        try:
            model_env = json.loads(MANIFEST.read_text(encoding="utf-8"))["models"][0]["env"]
            print(
                f"api_base_url={model_env.get('OPENAI_BASE_URL', '<inherited>')} "
                f"ca_cert={model_env.get('OPENAI_CA_CERT_PATH', '<system>')}"
            )
        except (KeyError, OSError, json.JSONDecodeError):
            print("api_base_url=<manifest-unreadable>")
    runtime_manifests = sorted(
        (RUNTIME / "manifests").glob("*/batch-*.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if runtime_manifests:
        runtime_manifest = runtime_manifests[-1]
        runtime_document = load_json(runtime_manifest)
        runtime_model = (runtime_document.get("models") or [{}])[0]
        runtime_env = {
            **dict((runtime_document.get("defaults") or {}).get("env") or {}),
            **dict(runtime_model.get("env") or {}),
        }
        runtime_capacity = {
            **dict((runtime_document.get("defaults") or {}).get("capacity") or {}),
            **dict(runtime_model.get("capacity") or {}),
        }
        print(
            f"runtime_manifest={runtime_manifest} "
            f"analysis_workers={runtime_capacity.get('analysis_workers')} "
            f"endpoint={runtime_env.get('OPENAI_BASE_URL')} "
            f"ca={runtime_env.get('OPENAI_CA_CERT_PATH')}"
        )
    counts = Counter(classify(label) for label in range(81, 732))
    aggregate_remaining = 651 - counts["phase3_complete"]
    phase3_ready = sum(
        stage2_ready(output_dir(label)) and not phase3_complete(output_dir(label))
        for label in range(81, 732)
    )
    state_path = RUNTIME / "supervisor.state.json"
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        pid = state.get("pid")
        alive = pid_alive(pid)
        effective_status = state.get("status") if alive else f"stopped/{state.get('status')}"
        print(
            f"supervisor={effective_status} alive={str(alive).lower()} pid={pid} "
            f"current_batch={state.get('current_batch')} "
            f"remaining={aggregate_remaining} "
            f"state_remaining={state.get('remaining_count', 'pending')}"
        )
        status_path = RUNTIME / "supervisor.status"
        if status_path.is_file():
            print(f"supervisor_exit_code={status_path.read_text().strip()}")
        batches = state.get("batches") or []
        if batches:
            current = batches[-1]
            print(
                f"batch={current.get('range')} status={current.get('status')} "
                f"pending_at_start={current.get('pending_at_start')} remaining={current.get('remaining', 'pending')}"
            )
    else:
        print("supervisor=not_started")

    watcher_state = load_json(RUNTIME / "resume-watcher.state.json")
    if watcher_state:
        watcher_pid = watcher_state.get("pid")
        print(
            f"resume_watcher={watcher_state.get('status')} "
            f"alive={str(pid_alive(watcher_pid)).lower()} pid={watcher_pid} "
            f"completed_cases={watcher_state.get('completed_cases', 'pending')} "
            f"disk_gate={watcher_state.get('min_free_gib', 'pending')}GiB "
            f"starts={len(watcher_state.get('launches') or [])}/"
            f"{watcher_state.get('max_starts', 'pending')}"
        )

    circuit = load_json(RUNTIME / "model-circuit.state.json")
    if circuit:
        print(
            f"model_circuit={circuit.get('status')} "
            f"failure_kind={circuit.get('failure_kind', 'none')} "
            f"failures={circuit.get('failure_count', 0)}/"
            f"{circuit.get('threshold', 3)} "
            f"open_remaining_seconds={circuit.get('open_remaining_seconds', 0)}"
        )

    print("progress=" + " ".join(f"{key}:{counts[key]}" for key in (
        "phase3_complete", "stage2_complete", "analysis_complete",
        "failed", "infra_failed", "not_started_or_running",
    )) + f" phase3_ready:{phase3_ready}")

    latest_attempts = Counter()
    for label in range(81, 732):
        attempt = load_json(output_dir(label) / "runner_attempt.latest.json")
        status = str(attempt.get("status") or "")
        if status == "infra_failed":
            latest_attempts[
                f"infra/{attempt.get('failure_kind') or 'unknown'}"
            ] += 1
        elif status == "failed":
            latest_attempts["failed"] += 1
    if latest_attempts:
        print(
            "latest_attempts="
            + " ".join(
                f"{key}:{value}"
                for key, value in sorted(latest_attempts.items())
            )
        )

    memory = command_output(["free", "-h"])
    disk = shutil.disk_usage(ROOT)
    print(f"disk_free={disk.free / 1024**3:.1f}GiB")
    if memory:
        print(memory)
    containers = command_output([
        "docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}",
    ])
    print("containers:\n" + (containers or "  none"))
    stats = command_output([
        "docker", "stats", "--no-stream", "--format",
        "{{.Name}}\tmem={{.MemUsage}}\tcpu={{.CPUPerc}}",
    ])
    if stats:
        print("container_stats:\n" + stats)
    runner_processes = command_output([
        "pgrep", "-af", "eval/local_swebench_runner.py",
    ])
    if runner_processes:
        print("runner_processes:\n" + runner_processes)


if __name__ == "__main__":
    main()
