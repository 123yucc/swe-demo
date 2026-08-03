#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <eval-retry-directory> <run-prefix>" >&2
  exit 2
fi

script_dir=$(cd "$(dirname "$0")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)
controller_dir=$(cd "$1" && pwd)
run_prefix=$2
selection="$controller_dir/selection.json"
log_path="$controller_dir/continuation.log"
pid_path="$controller_dir/continuation.pid"
status_path="$controller_dir/continuation.status"

if [ ! -f "$selection" ]; then
  echo "selection missing: $selection" >&2
  exit 2
fi
if [ -f "$pid_path" ]; then
  prior_pid=$(cat "$pid_path" 2>/dev/null || true)
  if [ -n "${prior_pid:-}" ] && kill -0 "$prior_pid" >/dev/null 2>&1; then
    echo "ALREADY_RUNNING pid=$prior_pid"
    exit 0
  fi
fi
if [ -e "$log_path" ] || [ -e "$status_path" ]; then
  echo "REFUSING_TO_OVERWRITE controller_dir=$controller_dir" >&2
  exit 3
fi

cmd=(
  python3 -u scripts/continue_gpt52_pipeline.py
  --selection "$selection"
  --run-prefix "$run_prefix"
)
quoted_cmd=""
for arg in "${cmd[@]}"; do
  quoted_cmd+=" $(printf '%q' "$arg")"
done
quoted_cmd=${quoted_cmd# }
launcher="cd $(printf '%q' "$repo_root") && $quoted_cmd >> $(printf '%q' "$log_path") 2>&1; rc=\$?; echo \$rc > $(printf '%q' "$status_path")"

setsid /bin/bash -lc "$launcher" >/dev/null 2>&1 < /dev/null &
pid=$!
echo "$pid" > "$pid_path"
echo "STARTED_PID=$pid"
echo "LOG=$log_path"
