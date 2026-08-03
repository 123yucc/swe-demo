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
pid_path="$repo_root/logs/runs/$run_name/runner.pid"

if [ ! -f "$pid_path" ]; then
  echo "PID_MISSING"
  exit 0
fi

pid=$(cat "$pid_path" 2>/dev/null || true)
if [ -n "${pid:-}" ]; then
  kill -TERM -- "-$pid" >/dev/null 2>&1 || true
  kill "$pid" >/dev/null 2>&1 || true
  sleep 1
  kill -KILL -- "-$pid" >/dev/null 2>&1 || true
  kill -9 "$pid" >/dev/null 2>&1 || true
fi

rm -f "$pid_path"
echo "STOPPED"
