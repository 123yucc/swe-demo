#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)
cd "$repo_root"

runtime_dir="$repo_root/runtime/gpt52-731"
mkdir -p "$runtime_dir"
pid_path="$runtime_dir/supervisor.pid"
status_path="$runtime_dir/supervisor.status"
log_path="$runtime_dir/supervisor.log"

if [ -f "$pid_path" ]; then
  old_pid=$(cat "$pid_path" 2>/dev/null || true)
  if [ -n "${old_pid:-}" ] && kill -0 "$old_pid" >/dev/null 2>&1; then
    echo "ALREADY_RUNNING pid=$old_pid"
    exit 0
  fi
fi

if [ ! -f .env ]; then
  echo "missing $repo_root/.env" >&2
  exit 2
fi

if ! python3 - <<'PY'
import socket
with socket.create_connection(("127.0.0.1", 7897), timeout=3):
    pass
PY
then
  echo "proxy preflight failed: 127.0.0.1:7897 is not reachable" >&2
  echo "start the SSH reverse tunnel before retrying" >&2
  exit 2
fi

registry_http=$(
  timeout 20 curl \
    --proxy http://127.0.0.1:7897 \
    --silent --show-error \
    --output /dev/null \
    --write-out '%{http_code}' \
    https://registry-1.docker.io/v2/ || true
)
if [ "$registry_http" != "401" ]; then
  echo "proxy preflight failed: Docker Registry returned ${registry_http:-no-response}" >&2
  exit 2
fi

if ! sg docker -c 'docker info >/dev/null 2>&1'; then
  echo "docker preflight failed for the current user" >&2
  exit 2
fi

if ! openssl x509 -in runtime/caddy_ca.ip.pem -checkend 3600 -noout; then
  echo "model API preflight failed: runtime/caddy_ca.ip.pem is expired or expires within one hour" >&2
  exit 2
fi

if [ "${SKIP_MODEL_PREFLIGHT:-0}" != "1" ] && ! (
  set -a
  . ./.env
  set +a
  export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost,165.154.193.90,claude.buzz7.top"
  export no_proxy="$NO_PROXY"
  python3 scripts/probe_openai_model.py \
    --manifest eval/manifests/swebench-pro-081-731.gpt5.2.json
); then
  echo "model API preflight failed; supervisor was not started" >&2
  exit 2
fi

if [ "${START_PREFLIGHT_ONLY:-0}" = "1" ]; then
  echo "PREFLIGHT_OK proxy=127.0.0.1:7897 registry=ready docker=ready ca=valid model=ready"
  exit 0
fi

launcher_history="$runtime_dir/history/launcher-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$launcher_history"
for prior in "$pid_path" "$status_path"; do
  if [ -f "$prior" ]; then
    cp -a "$prior" "$launcher_history/"
  fi
done
rm -f "$status_path"
launcher="cd $(printf '%q' "$repo_root") && set -a && . ./.env && set +a && export OPENAI_CA_CERT_PATH=/home/user/demo/runtime/caddy_ca.ip.pem HTTP_PROXY=\${HTTP_PROXY:-http://127.0.0.1:7897} HTTPS_PROXY=\${HTTPS_PROXY:-http://127.0.0.1:7897} http_proxy=\${http_proxy:-\$HTTP_PROXY} https_proxy=\${https_proxy:-\$HTTPS_PROXY} NO_PROXY=\${NO_PROXY:+\$NO_PROXY,}127.0.0.1,localhost,165.154.193.90,claude.buzz7.top no_proxy=\${no_proxy:+\$no_proxy,}127.0.0.1,localhost,165.154.193.90,claude.buzz7.top; python3 -u scripts/run_gpt52_731.py >> $(printf '%q' "$log_path") 2>&1; rc=\$?; echo \$rc > $(printf '%q' "$status_path")"

setsid /bin/bash -lc "$launcher" >/dev/null 2>&1 < /dev/null &
pid=$!
echo "$pid" > "$pid_path"
echo "STARTED pid=$pid"
echo "LOG=$log_path"
echo "STATUS_COMMAND=watch -n 10 python3 scripts/gpt52_731_status.py"
