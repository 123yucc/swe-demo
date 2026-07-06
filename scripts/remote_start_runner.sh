#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <manifest-path> <run-name> [extra runner args...]" >&2
  exit 2
fi

manifest_path=$1
run_name=$2
shift 2

script_dir=$(cd "$(dirname "$0")" && pwd)
if [ -f "$script_dir/.env" ] && [ -d "$script_dir/eval" ]; then
  repo_root=$script_dir
else
  repo_root=$(cd "$script_dir/.." && pwd)
fi
cd "$repo_root"

if [ ! -f .env ]; then
  echo ".env missing at $repo_root/.env" >&2
  exit 2
fi

log_path="$repo_root/${run_name}.log"
pid_path="$repo_root/${run_name}.pid"
status_path="$repo_root/${run_name}.status"
state_path="$repo_root/workdir/${run_name}.state.json"
run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$

if [ -f "$pid_path" ]; then
  old_pid=$(cat "$pid_path" 2>/dev/null || true)
  if [ -n "${old_pid:-}" ] && kill -0 "$old_pid" >/dev/null 2>&1; then
    echo "ALREADY_RUNNING pid=$old_pid" >&2
    exit 1
  fi
fi

: > "$log_path"
rm -f "$status_path"
: > "$state_path"

cmd=(
  python3
  -u
  eval/local_swebench_runner.py
  --manifest "$manifest_path"
  --state-file "$state_path"
  --run-id "$run_id"
  "$@"
)

quoted_cmd=""
for arg in "${cmd[@]}"; do
  quoted_cmd+=" $(printf '%q' "$arg")"
done
quoted_cmd=${quoted_cmd# }

launcher="cd $(printf '%q' "$repo_root") && set -a && . ./.env && set +a && export HTTP_PROXY=\${HTTP_PROXY:-http://127.0.0.1:7897} HTTPS_PROXY=\${HTTPS_PROXY:-http://127.0.0.1:7897} http_proxy=\${http_proxy:-\$HTTP_PROXY} https_proxy=\${https_proxy:-\$HTTPS_PROXY} NO_PROXY=\${NO_PROXY:-127.0.0.1,localhost} no_proxy=\${no_proxy:-127.0.0.1,localhost}; sg docker -c $(printf '%q' "$quoted_cmd") >> $(printf '%q' "$log_path") 2>&1; rc=\$?; echo \$rc > $(printf '%q' "$status_path")"

setsid /bin/bash -lc "$launcher" >/dev/null 2>&1 < /dev/null &
runner_pid=$!
echo "$runner_pid" > "$pid_path"
echo "STARTED_PID=$runner_pid"
echo "STATE_FILE=$state_path"
echo "RUN_ID=$run_id"
