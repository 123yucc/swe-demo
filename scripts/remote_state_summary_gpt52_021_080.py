#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

TARGET_ISSUES = [
    21, 25, 26, 31, 32, 38, 39, 42, 47, 49,
    52, 54, 59, 63, 64, 67, 69, 70, 73, 77,
]


def main() -> int:
    run_state_path = Path("workdir/swebench-gpt52-missing-021-080.state.json")
    state_path = run_state_path if run_state_path.exists() else Path("workdir/local_runner_state.json")
    raw = state_path.read_text(encoding="utf-8").strip()
    if not raw:
        print(
            json.dumps(
                {
                    "running_count": 0,
                    "success_count": 0,
                    "failed_count": 0,
                    "missing_count": len(TARGET_ISSUES),
                    "running": [],
                    "success": [],
                    "failed": [],
                    "missing": [f"swe_issue_{n:03d}" for n in TARGET_ISSUES],
                },
                indent=2,
            )
        )
        return 0
    state = json.loads(raw)
    tasks = state.get("tasks", {})

    running: list[str] = []
    success: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []

    for n in TARGET_ISSUES:
        issue = f"swe_issue_{n:03d}"
        key = f"outputs_gpt-5.2:{issue}"
        task = tasks.get(key)
        if not task:
            missing.append(issue)
            continue
        status = task.get("status")
        if status == "running":
            running.append(issue)
        elif status == "success":
            success.append(issue)
        elif status == "failed":
            failed.append(issue)
        elif status == "skipped":
            skipped.append(issue)
        else:
            missing.append(issue)

    print(
        json.dumps(
            {
                "running_count": len(running),
                "success_count": len(success),
                "failed_count": len(failed),
                "skipped_count": len(skipped),
                "missing_count": len(missing),
                "running": running,
                "success": success,
                "failed": failed,
                "skipped": skipped,
                "missing": missing,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
