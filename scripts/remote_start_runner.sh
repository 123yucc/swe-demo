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

run_dir="$repo_root/logs/runs/$run_name"
mkdir -p "$run_dir"
log_path="$run_dir/runner.log"
pid_path="$run_dir/runner.pid"
status_path="$run_dir/runner.status"
state_path="$run_dir/runner.state.json"
run_id=$(date -u +%Y%m%dT%H%M%SZ)-$$

if [ -f "$pid_path" ]; then
  old_pid=$(cat "$pid_path" 2>/dev/null || true)
  if [ -n "${old_pid:-}" ] && kill -0 "$old_pid" >/dev/null 2>&1; then
    echo "ALREADY_RUNNING pid=$old_pid" >&2
    exit 1
  fi
fi

if [ -e "$log_path" ] || [ -e "$status_path" ] || [ -e "$state_path" ]; then
  echo "REFUSING_TO_OVERWRITE run=$run_name directory=$run_dir" >&2
  exit 3
fi

: > "$log_path"
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

launcher="cd $(printf '%q' "$repo_root") && set -a && . ./.env && set +a && export OPENAI_CA_CERT_PATH=/home/user/demo/runtime/caddy_ca.ip.pem HTTP_PROXY=\${HTTP_PROXY:-http://127.0.0.1:7897} HTTPS_PROXY=\${HTTPS_PROXY:-http://127.0.0.1:7897} http_proxy=\${http_proxy:-\$HTTP_PROXY} https_proxy=\${https_proxy:-\$HTTPS_PROXY} NO_PROXY=\${NO_PROXY:+\$NO_PROXY,}127.0.0.1,localhost,165.154.193.90,claude.buzz7.top no_proxy=\${no_proxy:+\$no_proxy,}127.0.0.1,localhost,165.154.193.90,claude.buzz7.top; sg docker -c $(printf '%q' "$quoted_cmd") >> $(printf '%q' "$log_path") 2>&1; rc=\$?; echo \$rc > $(printf '%q' "$status_path")"

setsid /bin/bash -lc "$launcher" >/dev/null 2>&1 < /dev/null &
runner_pid=$!
echo "$runner_pid" > "$pid_path"
echo "STARTED_PID=$runner_pid"
echo "STATE_FILE=$state_path"
echo "RUN_ID=$run_id"
