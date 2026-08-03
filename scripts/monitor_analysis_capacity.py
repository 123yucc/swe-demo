"""Sample host health and runner progress during analysis capacity ramps."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.strip().split()[0]) * 1024
    return values


def _process_count(pattern: str) -> int:
    result = subprocess.run(
        ["pgrep", "-fc", pattern], capture_output=True, text=True, check=False
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _matching_processes(patterns: list[str]) -> dict[str, int]:
    """Return host RSS/count for matching commands without requiring psutil."""
    count = 0
    rss_bytes = 0
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            command = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                errors="replace"
            )
            if not any(pattern in command for pattern in patterns):
                continue
            status = (proc_dir / "status").read_text()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        count += 1
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                rss_bytes += int(line.split()[1]) * 1024
                break
    return {"count": count, "rss_bytes": rss_bytes}


def _stop_runner(pid_path: Path) -> int | None:
    try:
        pid = int(pid_path.read_text().strip())
        os.killpg(pid, signal.SIGTERM)
        return pid
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return None


def _runner_counts(paths: list[Path]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in paths:
        try:
            tasks = json.loads(path.read_text()).get("tasks", {})
        except Exception:
            continue
        for task in tasks.values():
            status = str(task.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--duration", type=float, default=900.0)
    parser.add_argument("--runner-pid-file", action="append", type=Path, default=[])
    parser.add_argument("--runner-status-file", action="append", type=Path, default=[])
    parser.add_argument("--abort-below-available-gb", type=float, default=0.0)
    parser.add_argument("--abort-swap-growth-gb", type=float, default=0.0)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration
    baseline_swap = None
    with args.output.open("a", encoding="utf-8") as handle:
        while time.monotonic() < deadline:
            mem = _meminfo()
            swap_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
            if baseline_swap is None:
                baseline_swap = swap_used
            row = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "available_bytes": mem.get("MemAvailable", 0),
                "swap_used_bytes": swap_used,
                "swap_growth_bytes": swap_used - baseline_swap,
                "load1": os.getloadavg()[0],
                "src_main_processes": _process_count("src.main"),
                "experience_server_processes": _process_count("experience_server"),
                "agent_processes": _matching_processes(
                    ["src.main", "claude", "local_swebench_runner"]
                ),
                "runner_counts": _runner_counts(args.state_file),
            }
            reasons = []
            if (
                args.abort_below_available_gb
                and row["available_bytes"] < args.abort_below_available_gb * 1024**3
            ):
                reasons.append("available_memory")
            if (
                args.abort_swap_growth_gb
                and row["swap_growth_bytes"] > args.abort_swap_growth_gb * 1024**3
            ):
                reasons.append("swap_growth")
            if reasons and args.runner_pid_file:
                row["safety_abort"] = {
                    "reasons": reasons,
                    "runner_pids": [
                        _stop_runner(pid_path) for pid_path in args.runner_pid_file
                    ],
                }
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            if reasons:
                break
            if args.runner_status_file and all(
                status_path.exists() for status_path in args.runner_status_file
            ):
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
