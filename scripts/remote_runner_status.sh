#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <run-name>" >&2
  exit 2
fi

run_name=$1
script_dir=$(cd "$(dirname "$0")" && pwd)
if [ -f "$script_dir/.env" ] && [ -d "$script_dir/eval" ]; then
  repo_root=$script_dir
else
  repo_root=$(cd "$script_dir/.." && pwd)
fi
pid_path="$repo_root/${run_name}.pid"
log_path="$repo_root/${run_name}.log"
status_path="$repo_root/${run_name}.status"
state_path="$repo_root/workdir/${run_name}.state.json"

echo "RUN_NAME=$run_name"
if [ -f "$pid_path" ]; then
  pid=$(cat "$pid_path" 2>/dev/null || true)
  echo "PID=${pid:-}"
  if [ -n "${pid:-}" ]; then
    ps -p "$pid" -o pid=,pgid=,ppid=,stat=,etime=,cmd= 2>/dev/null || true
    pstree -ap "$pid" 2>/dev/null || true
  fi
else
  echo "PID_MISSING"
fi

if [ -f "$status_path" ]; then
  echo "STATUS_FILE=$(cat "$status_path" 2>/dev/null || true)"
else
  echo "STATUS_FILE_MISSING"
fi

if [ -f "$state_path" ]; then
  echo "STATE_FILE=$state_path"
  python3 - <<PY
import json
from pathlib import Path
path = Path(r"$state_path")
raw = path.read_text(encoding="utf-8").strip()
if not raw:
    print("STATE_COUNTS={}")
    print("STATE_UPDATED_AT=")
    raise SystemExit(0)
data = json.loads(raw)
tasks = data.get("tasks", {})
counts = {}
for payload in tasks.values():
    status = str(payload.get("status", "unknown"))
    counts[status] = counts.get(status, 0) + 1
print("STATE_COUNTS=" + json.dumps(counts, ensure_ascii=False, sort_keys=True))
print("STATE_UPDATED_AT=" + str(data.get("updated_at") or data.get("started_at") or ""))
PY
else
  echo "STATE_FILE_MISSING"
fi

if [ -f "$log_path" ]; then
  echo "LOG_SIZE=$(wc -c < "$log_path" 2>/dev/null || true)"
  tail -40 "$log_path" 2>/dev/null || true
else
  echo "LOG_MISSING"
fi
