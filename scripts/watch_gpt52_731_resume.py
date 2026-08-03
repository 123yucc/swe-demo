#!/usr/bin/env python3
"""Wait for safe GPT-5.2 resume conditions, then start and verify the supervisor."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.continue_gpt52_pipeline import registry_ready, verify_supervisor_runtime
    from scripts.run_gpt52_731 import phase3_complete as phase3_artifacts_complete
except ModuleNotFoundError:
    from continue_gpt52_pipeline import registry_ready, verify_supervisor_runtime
    from run_gpt52_731 import phase3_complete as phase3_artifacts_complete

try:
    import fcntl
except ImportError:  # pragma: no cover - the watcher runs on the Linux host
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime" / "gpt52-731"
MANIFEST = ROOT / "eval" / "manifests" / "swebench-pro-081-731.gpt5.2.json"
STATE_PATH = RUNTIME / "resume-watcher.state.json"
LOCK_PATH = RUNTIME / "resume-watcher.lock"
PID_PATH = RUNTIME / "resume-watcher.pid"
EXPECTED_ENDPOINT = "https://165.154.193.90"
EXPECTED_CA = "/home/user/demo/runtime/caddy_ca.ip.pem"


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


def process_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def supervisor_alive() -> bool:
    return process_alive(load_json(RUNTIME / "supervisor.state.json").get("pid"))


def supervisor_restart_allowed() -> bool:
    state = load_json(RUNTIME / "supervisor.state.json")
    return str(state.get("status") or "") in {
        "",
        "incomplete",
        "waiting_for_model",
        "waiting_for_disk",
        "restarting_recovery_pass",
    }


def disk_free_gib() -> float:
    return shutil.disk_usage(ROOT).free / 1024**3


def completed_case_count() -> int:
    return sum(
        phase3_artifacts_complete(label, "outputs_gpt-5.2")
        for label in range(81, 732)
    )


def model_ready() -> tuple[bool, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "OPENAI_BASE_URL": EXPECTED_ENDPOINT,
            "SSL_CERT_FILE": EXPECTED_CA,
            "REQUESTS_CA_BUNDLE": EXPECTED_CA,
        }
    )
    try:
        result = subprocess.run(
            [
                "python3",
                "scripts/probe_openai_model.py",
                "--manifest",
                str(MANIFEST),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "model probe timed out"
    diagnostic = (result.stderr or result.stdout).strip()
    return result.returncode == 0, diagnostic[-2000:]


def start_supervisor() -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GPT52_RECOVERY_PASS": "3",
            "GPT52_MIN_FREE_BEFORE_GIB": "75",
            "GPT52_MIN_FREE_AFTER_GIB": "60",
        }
    )
    return subprocess.run(
        ["bash", "scripts/start_gpt52_731.sh"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def update_state(state: dict, **changes: object) -> None:
    state.update(changes)
    state["updated_at"] = utc_now()
    state["pid"] = os.getpid()
    write_json(STATE_PATH, state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=int, default=300)
    parser.add_argument("--min-free-gib", type=float, default=75.0)
    parser.add_argument("--max-starts", type=int, default=3)
    parser.add_argument("--verify-timeout-seconds", type=int, default=1800)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if fcntl is None:
        parser.error("resume watcher requires a POSIX host")
    if args.poll_seconds < 1 and not args.once:
        parser.error("--poll-seconds must be at least 1")
    if args.min_free_gib < 60:
        parser.error("--min-free-gib must be at least 60")
    if args.max_starts < 1:
        parser.error("--max-starts must be at least 1")

    RUNTIME.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("resume watcher is already running", flush=True)
        return 0

    PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    previous = load_json(STATE_PATH)
    launches = list(previous.get("launches") or [])
    state = {
        "schema_version": 1,
        "status": "starting",
        "started_at": previous.get("started_at") or utc_now(),
        "endpoint": EXPECTED_ENDPOINT,
        "ca": EXPECTED_CA,
        "min_free_gib": args.min_free_gib,
        "max_starts": args.max_starts,
        "launches": launches,
    }
    update_state(state)

    while True:
        free_gib = disk_free_gib()
        completed_cases = completed_case_count()
        if completed_cases == 651:
            update_state(
                state,
                status="complete",
                completed_cases=completed_cases,
                disk_free_gib=round(free_gib, 2),
                finished_at=utc_now(),
            )
            return 0
        if supervisor_alive():
            update_state(
                state,
                status="supervisor_running",
                completed_cases=completed_cases,
                disk_free_gib=round(free_gib, 2),
            )
        elif not supervisor_restart_allowed():
            update_state(
                state,
                status="paused_no_blind_retry",
                completed_cases=completed_cases,
                disk_free_gib=round(free_gib, 2),
                supervisor_status=load_json(
                    RUNTIME / "supervisor.state.json"
                ).get("status"),
            )
        elif len(launches) >= args.max_starts:
            update_state(
                state,
                status="max_starts_reached",
                completed_cases=completed_cases,
                disk_free_gib=round(free_gib, 2),
                finished_at=utc_now(),
            )
            return 75
        elif free_gib < args.min_free_gib:
            update_state(
                state,
                status="waiting_for_disk",
                completed_cases=completed_cases,
                disk_free_gib=round(free_gib, 2),
                registry_ready=None,
                model_ready=None,
            )
        else:
            registry_ok = registry_ready()
            if not registry_ok:
                update_state(
                    state,
                    status="waiting_for_registry_proxy",
                    completed_cases=completed_cases,
                    disk_free_gib=round(free_gib, 2),
                    registry_ready=False,
                    model_ready=None,
                )
            else:
                model_ok, model_diagnostic = model_ready()
                if not model_ok:
                    update_state(
                        state,
                        status="waiting_for_model",
                        completed_cases=completed_cases,
                        disk_free_gib=round(free_gib, 2),
                        registry_ready=True,
                        model_ready=False,
                        model_diagnostic=model_diagnostic,
                    )
                else:
                    update_state(
                        state,
                        status="starting_supervisor",
                        completed_cases=completed_cases,
                        disk_free_gib=round(free_gib, 2),
                        registry_ready=True,
                        model_ready=True,
                    )
                    started = start_supervisor()
                    launch = {
                        "attempt": len(launches) + 1,
                        "started_at": utc_now(),
                        "returncode": started.returncode,
                        "stdout": started.stdout[-4000:],
                        "stderr": started.stderr[-4000:],
                    }
                    launches.append(launch)
                    update_state(state, launches=launches)
                    if started.returncode != 0:
                        update_state(state, status="supervisor_start_failed")
                    else:
                        update_state(state, status="verifying_runtime")
                        verification = verify_supervisor_runtime(
                            state, timeout_seconds=args.verify_timeout_seconds
                        )
                        launch["runtime_verification"] = verification
                        update_state(
                            state,
                            launches=launches,
                            status=(
                                "supervisor_running"
                                if verification.get("valid")
                                else "runtime_verification_failed"
                            ),
                        )
                        if not verification.get("valid"):
                            return 1

        if args.once:
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
