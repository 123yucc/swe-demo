#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <run-name-or-prefix>" >&2
  exit 2
fi

query=$1
script_dir=$(cd "$(dirname "$0")" && pwd)
if [ -f "$script_dir/.env" ] && [ -d "$script_dir/eval" ]; then
  repo_root=$script_dir
else
  repo_root=$(cd "$script_dir/.." && pwd)
fi

python3 - "$repo_root" "$query" <<'PY'
import json
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
query = sys.argv[2]
runs_dir = repo_root / "logs" / "runs"

exact = runs_dir / query
if exact.is_dir():
    run_dirs = [exact]
else:
    run_dirs = sorted(
        p for p in runs_dir.glob(f"{query}*")
        if p.is_dir() and (p / "runner.state.json").exists()
    )

if not run_dirs:
    print(f"no runs matched: {query}")
    raise SystemExit(1)

for run_dir in run_dirs:
    state_path = run_dir / "runner.state.json"
    status_path = run_dir / "runner.status"
    pid_path = run_dir / "runner.pid"
    log_path = run_dir / "runner.log"

    counts = {}
    updated_at = ""
    total = 0
    try:
        raw = state_path.read_text(encoding="utf-8").strip()
        state = json.loads(raw) if raw else {}
        updated_at = str(state.get("updated_at") or state.get("started_at") or "")
        tasks = state.get("tasks", {})
        total = len(tasks)
        for payload in tasks.values():
            status = str(payload.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
    except Exception:
        pass

    if log_path.exists():
        try:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "[plan]" in line and "tasks=" in line:
                    for part in line.split():
                        if part.startswith("tasks="):
                            total = max(total, int(part.split("=", 1)[1]))
        except Exception:
            pass

    done = counts.get("success", 0) + counts.get("skipped", 0)
    running = counts.get("running", 0)
    failed = counts.get("failed", 0)
    queued = max(0, total - done - running - failed)

    exit_state = "running"
    if status_path.exists():
        exit_state = f"exit={status_path.read_text(encoding='utf-8').strip() or '?'}"
    elif not pid_path.exists():
        exit_state = "stopped"

    print(
        f"{run_dir.name}: total={total} done={done} running={running} "
        f"failed={failed} queued={queued} {exit_state} updated={updated_at}"
    )
PY
