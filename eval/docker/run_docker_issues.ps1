#!/usr/bin/env pwsh
# Run selected issues sequentially inside their SWE-bench Pro Docker images.
# Requires: ANTHROPIC_API_KEY set, and eval\docker\setup_wheels.ps1 run first.
#
# The harness runs INSIDE the instance's docker image with --repo-dir /app, so
# the language toolchain (go, python, ...) is present and the post-patch build
# gate (src/orchestrator/build_verify.py) can actually compile. Running the
# harness on the Windows host instead leaves Go unverifiable (no `go` on PATH),
# which is exactly the BUILD_UNVERIFIABLE condition the gate now reports.
#
# The image tag is read from each case's instance_metadata.json (dockerhub_tag),
# so adding a new case only requires appending its issue number to $ISSUE_NUMS.

param(
    # Run exactly ONE issue (e.g. -Only 008). Takes precedence over -IssueNums.
    [string]$Only = "",
    # Override the default issue list (e.g. -IssueNums 008,013).
    [string[]]$IssueNums = @(),
    # Per-container memory cap passed to `docker run --memory`. Empty = no cap.
    # Default 6g: WSL2/Docker only exposes ~7.66g total here, so the cap must sit
    # BELOW that (with headroom for the daemon) or it never actually constrains.
    [string]$Memory = "6g",
    # Pass --force-restart to src.main, ignoring any existing checkpoint.
    [switch]$ForceRestart
)

$ErrorActionPreference = "Stop"

$DEMO_DIR = "D:\demo"
$DOCKERHUB_USER = "jefzda"

function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]*?)\s*=\s*(.*)\s*$') {
            $key = $matches[1].Trim()
            $val = $matches[2].Trim().Trim('"').Trim("'")
            [System.Environment]::SetEnvironmentVariable($key, $val, 'Process')
        }
    }
}

Import-DotEnv "$DEMO_DIR\.env"

function Get-ModelOutputDirName {
    $backend = $env:MODEL_BACKEND
    if (-not $backend) { $backend = $env:LLM_BACKEND }
    if (-not $backend) { $backend = "anthropic" }
    $backend = $backend.Trim().ToLowerInvariant()

    if ($backend -in @("openai", "codex", "codex-pro")) {
        $model = $env:OPENAI_MODEL
        if (-not $model) { $model = $env:CODEX_PRO_MODEL }
        if (-not $model) { $model = $env:ANTHROPIC_MODEL }
    } else {
        $model = $env:ANTHROPIC_MODEL
    }
    if (-not $model) { $model = "unknown" }

    $safe = $model.Trim().ToLowerInvariant()
    $safe = [regex]::Replace($safe, "\s+", "-")
    $safe = [regex]::Replace($safe, "[^a-z0-9._-]+", "-")
    $safe = [regex]::Replace($safe, "-{2,}", "-").Trim("._-")
    if (-not $safe) { $safe = "unknown" }
    return "outputs_$safe"
}

$OUTPUT_DIR_NAME = Get-ModelOutputDirName
$FORCE_RESTART_ARG = ""
if ($ForceRestart) { $FORCE_RESTART_ARG = " --force-restart" }

function Add-DockerEnvArg {
    param(
        [object[]]$CurrentArgs,
        [string]$Name,
        [string]$Value
    )
    if ($null -ne $Value -and $Value -ne "") {
        return $CurrentArgs + @("-e", "${Name}=${Value}")
    }
    return $CurrentArgs
}

# Default = every workdir\swe_issue_<NUM> that has an instance_metadata.json.
# Everything else (image tag, language) is derived from that metadata, so adding
# a new case is just dropping its folder in — no edit here.
function Get-AllIssueNums {
    Get-ChildItem -Path "$DEMO_DIR\workdir" -Directory -Filter "swe_issue_*" |
        Where-Object { Test-Path "$($_.FullName)\artifacts\instance_metadata.json" } |
        ForEach-Object { ($_.Name -replace '^swe_issue_', '') } |
        Sort-Object
}

if ($Only) {
    $ISSUE_NUMS = @($Only)
} elseif ($IssueNums.Count -gt 0) {
    $ISSUE_NUMS = $IssueNums
} else {
    $ISSUE_NUMS = @(Get-AllIssueNums)
}

if ($ISSUE_NUMS.Count -eq 0) {
    Write-Error "No issues to run (no swe_issue_* dir with instance_metadata.json found). Aborting."
    exit 1
}
Write-Host "[plan] running $($ISSUE_NUMS.Count) issue(s): $($ISSUE_NUMS -join ', ')"

# ── Single-instance lock ──────────────────────────────────────────────────
# Kill any previous run and clear its lock before acquiring a new one.
# This prevents OOM / exit 137 from two concurrent runs sharing /demo.
$LOCK_FILE = "$DEMO_DIR\eval\docker\.run_docker_issues.lock"
if (Test-Path $LOCK_FILE) {
    $oldPid = Get-Content $LOCK_FILE -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid) {
        $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "[lock] Killing previous run (PID $oldPid)..."
            Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
        } else {
            Write-Host "[lock] Stale lock (PID $oldPid not alive); reclaiming."
        }
    }
    Remove-Item $LOCK_FILE -Force -ErrorAction SilentlyContinue
}
Set-Content -Path $LOCK_FILE -Value $PID

# Bootstrap Python 3.11 inside the image. The base systems differ, so probe:
#   - /usr/bin/python3.11 present + pip works  → use it (qutebrowser, navidrome)
#   - /usr/bin/python3.11 present, pip missing → bootstrap via get-pip.py
#   - no system 3.11 (Debian 11 / teleport)    → extract standalone tarball
# A single probing snippet covers all three so we don't hardcode per case.
$PIP_SETUP = @"
set -e
for d in /usr/local/go/bin /usr/lib/go/bin /usr/lib/go-*/bin /opt/go/bin; do
  [ -d "`$d" ] && export PATH="`$d:`$PATH"
done
WHEELS=/demo/eval/docker/wheels
HARNESS_ROOT=/tmp/demo-harness
HARNESS_PY=`$HARNESS_ROOT/python311/bin/python3.11
HARNESS_VENV=`$HARNESS_ROOT/venv

mkdir -p `$HARNESS_ROOT

if [ -f `$WHEELS/python311-linux.tar.gz ]; then
  if ! `$HARNESS_PY -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; then
    rm -rf `$HARNESS_ROOT/python311
    mkdir -p `$HARNESS_ROOT/python311
    tar -xzf `$WHEELS/python311-linux.tar.gz -C `$HARNESS_ROOT/python311 --strip-components=1
  fi
fi

if [ -f /etc/alpine-release ] && command -v apk >/dev/null 2>&1; then
  if ! `$HARNESS_PY -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; then
    apk add --no-cache gcompat libstdc++ >/dev/null 2>&1 || true
    if [ -e /lib/ld-linux-x86-64.so.2 ] && [ ! -e /lib64/ld-linux-x86-64.so.2 ]; then
      mkdir -p /lib64
      ln -sf /lib/ld-linux-x86-64.so.2 /lib64/ld-linux-x86-64.so.2
    fi
  fi
fi

PYBIN=""
for candidate in "`$HARNESS_PY" /usr/bin/python3.11 /opt/python311/bin/python3.11 "`$(command -v python3 2>/dev/null || true)"; do
  [ -n "`$candidate" ] || continue
  if [ -x "`$candidate" ] && "`$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; then
    PYBIN="`$candidate"
    break
  fi
done

if [ -z "`$PYBIN" ]; then
  echo "No usable Python 3.11 runtime found for harness bootstrap" >&2
  echo "[harness-diagnostics] os-release:" >&2
  cat /etc/os-release >&2 2>/dev/null || true
  echo "[harness-diagnostics] uname=`$(uname -a 2>/dev/null || true)" >&2
  echo "[harness-diagnostics] loader candidates:" >&2
  ls -l /lib/ld-linux* /lib64/ld-linux* /usr/glibc-compat/lib/ld-linux* >&2 2>/dev/null || true
  if [ -e "`$HARNESS_PY" ]; then
    echo "[harness-diagnostics] HARNESS_PY=`$HARNESS_PY" >&2
    file "`$HARNESS_PY" >&2 2>/dev/null || true
    ldd "`$HARNESS_PY" >&2 2>/dev/null || true
    "`$HARNESS_PY" -V >&2 2>/dev/null || true
  else
    echo "[harness-diagnostics] HARNESS_PY missing: `$HARNESS_PY" >&2
  fi
  for candidate in /usr/bin/python3.11 /opt/python311/bin/python3.11 "`$(command -v python3 2>/dev/null || true)"; do
    [ -n "`$candidate" ] || continue
    echo "[harness-diagnostics] candidate=`$candidate" >&2
    "`$candidate" -V >&2 2>/dev/null || true
  done
  exit 127
fi

if [ ! -x `$HARNESS_VENV/bin/python ] || ! `$HARNESS_VENV/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' >/dev/null 2>&1; then
  rm -rf `$HARNESS_VENV
  "`$PYBIN" -m venv `$HARNESS_VENV
fi

PYBIN=`$HARNESS_VENV/bin/python

if ! "`$PYBIN" -m pip --version >/dev/null 2>&1; then
  "`$PYBIN" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi

if ! "`$PYBIN" -m pip --version >/dev/null 2>&1; then
  if [ -f `$WHEELS/get-pip.py ]; then
    "`$PYBIN" `$WHEELS/get-pip.py --no-index --find-links `$WHEELS -q || "`$PYBIN" `$WHEELS/get-pip.py -q
  else
    echo "pip bootstrap failed and `$WHEELS/get-pip.py does not exist" >&2
    exit 127
  fi
fi

if [ ! -d `$WHEELS ] || ! compgen -G "`$WHEELS/*.whl" >/dev/null; then
  echo "wheelhouse is missing; expected predownloaded wheels in `$WHEELS" >&2
  exit 127
fi

"`$PYBIN" -m pip install --no-index --find-links `$WHEELS -q --upgrade pip setuptools wheel
"`$PYBIN" -m pip install --no-index --find-links `$WHEELS -q -r /demo/requirements.lock
echo "PYBIN=`$PYBIN"
"@

if (-not $env:ANTHROPIC_API_KEY) {
    Write-Error "ANTHROPIC_API_KEY is not set. Aborting."
    exit 1
}

if (-not (Test-Path "$DEMO_DIR\eval\docker\wheels")) {
    Write-Error "eval\docker\wheels\ not found. Run eval\docker\setup_wheels.ps1 first."
    exit 1
}

try {
foreach ($num in $ISSUE_NUMS) {
    $metadata_host = "$DEMO_DIR\workdir\swe_issue_${num}\artifacts\instance_metadata.json"
    if (-not (Test-Path $metadata_host)) {
        Write-Warning "issue ${num}: $metadata_host not found; skipping."
        continue
    }

    # Read the image tag from metadata — no hardcoded tags.
    $meta = Get-Content $metadata_host -Raw | ConvertFrom-Json
    $tag  = $meta.dockerhub_tag
    # Some metadata carries only the upstream ECR `image_name` (no dockerhub_tag).
    # The public dockerhub tag is derived from the ECR image ref: take its last
    # path segment "<repo.repo>:<instance>" and join the two halves with "-" to
    # get "<repo.repo>-<instance>". Docker tags are capped at 128 chars, so the
    # derived value is truncated to 128 (verified against the qutebrowser case,
    # whose real dockerhub tag is the 128-char prefix of the derived string).
    if (-not $tag -and $meta.image_name) {
        $lastSeg = ($meta.image_name -split '/')[-1]   # "<repo.repo>:<instance>"
        if ($lastSeg -match ':') {
            $tag = $lastSeg -replace ':', '-'
            if ($tag.Length -gt 128) { $tag = $tag.Substring(0, 128) }
            Write-Host "[tag] issue ${num}: derived dockerhub tag from image_name -> $tag"
        }
    }
    if (-not $tag) {
        Write-Warning "issue ${num}: no 'dockerhub_tag' and no derivable 'image_name' in metadata; skipping."
        continue
    }
    $image = "${DOCKERHUB_USER}/sweap-images:${tag}"

    $metadata_path = "/demo/workdir/swe_issue_${num}/artifacts/instance_metadata.json"
    $output_dir    = "/demo/workdir/swe_issue_${num}/$OUTPUT_DIR_NAME"

    # Write bash script to a file that docker can execute directly.
    # This avoids all multi-line quoting issues when passing $cmd via -c.
    $script_path = "$DEMO_DIR\workdir\swe_issue_${num}\_run_harness.sh"
    $script_content = @"
#!/bin/bash
set -e
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

$PIP_SETUP

echo "[harness-preflight] python=`$("`$PYBIN" -V 2>&1)"
echo "[harness-preflight] pip=`$("`$PYBIN" -m pip --version 2>&1)"
echo "[harness-preflight] PATH=`$PATH"
for tool in git go node npm python3; do
  if command -v "`$tool" >/dev/null 2>&1; then
    echo "[harness-preflight] `$tool=`$(command -v "`$tool")"
  else
    echo "[harness-preflight] `$tool=MISSING"
  fi
done
if [ -d /app ]; then
  git -C /app rev-parse --show-toplevel >/dev/null 2>&1 && echo "[harness-preflight] repo=/app git-ok" || echo "[harness-preflight] repo=/app git-unavailable"
fi
REPO_LANG="$($meta.repo_language)"
if [ "`$REPO_LANG" = "go" ] && ! command -v go >/dev/null 2>&1; then
  echo "Go repository but go toolchain is not visible in the generation container PATH" >&2
  exit 127
fi

# Fake LTM server on port 9030 (no torch/chromadb needed)
"`$PYBIN" - <<'PY' &
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","results":[]}')
    def do_POST(self):
        self.do_GET()
    def log_message(self, *args):
        pass
http.server.HTTPServer(("127.0.0.1", 9030), H).serve_forever()
PY
sleep 1

cd /demo
"`$PYBIN" -m src.main \
  --instance-json $metadata_path \
  --repo-dir /app \
  --output-dir $output_dir$FORCE_RESTART_ARG
"@
    # Write with Unix line endings, no BOM (PowerShell 5.1 utf8 adds BOM which breaks bash shebang)
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $script_lf = $script_content -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText($script_path, $script_lf, $utf8NoBom)

    Write-Host ""
    Write-Host "============================================================"
    Write-Host "  Issue $num  ($($meta.repo_language))  ->  $tag"
    Write-Host "============================================================"

    Write-Host "[pull] $image"
    docker pull $image
    if ($LASTEXITCODE -ne 0) { Write-Warning "pull failed, trying anyway..."; }

    Write-Host "[run] issue $num"
    $memArgs = @()
    if ($Memory) { $memArgs = @("--memory=$Memory", "--memory-swap=$Memory") }
    # Lower EAP to Continue for ALL native docker calls below. Under
    # $ErrorActionPreference='Stop' + the outer *>&1 merge, PowerShell mis-
    # classifies ANY native-command stderr line as a terminating NativeCommandError
    # and aborts the whole script. That bites BOTH the orphan-reclaim `docker rm`
    # (which writes "No such container" to stderr in the normal no-orphan case)
    # and `docker run` (pip's root-user WARNING). Rely on $LASTEXITCODE for real
    # failure detection; restore Stop afterwards.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    # Deterministic container name so a crashed prior run (PS died but `docker
    # run` kept going) leaves a findable ORPHAN we can reclaim — not an anonymous
    # zombie that silently shares /demo, the outputs dir, and the mirrored-network
    # port 9030 with the live run and corrupts evidence.json. Force-remove any
    # leftover with this name before starting.
    $cname = "swe_run_${num}"
    docker rm -f $cname 2>&1 | Out-Null
    $envArgs = @()
    $envArgs = Add-DockerEnvArg $envArgs "MODEL_BACKEND" $env:MODEL_BACKEND
    $envArgs = Add-DockerEnvArg $envArgs "LLM_BACKEND" $env:LLM_BACKEND
    $envArgs = Add-DockerEnvArg $envArgs "ANTHROPIC_API_KEY" $env:ANTHROPIC_API_KEY
    $envArgs = Add-DockerEnvArg $envArgs "ANTHROPIC_BASE_URL" $env:ANTHROPIC_BASE_URL
    $envArgs = Add-DockerEnvArg $envArgs "ANTHROPIC_MODEL" $env:ANTHROPIC_MODEL
    $envArgs = Add-DockerEnvArg $envArgs "OPENAI_API_KEY" $env:OPENAI_API_KEY
    $envArgs = Add-DockerEnvArg $envArgs "OPENAI_BASE_URL" $env:OPENAI_BASE_URL
    $envArgs = Add-DockerEnvArg $envArgs "OPENAI_MODEL" $env:OPENAI_MODEL
    $envArgs = Add-DockerEnvArg $envArgs "OPENAI_API_SURFACE" $env:OPENAI_API_SURFACE
    $envArgs = Add-DockerEnvArg $envArgs "CODEX_PRO_API_KEY" $env:CODEX_PRO_API_KEY
    $envArgs = Add-DockerEnvArg $envArgs "CODEX_PRO_BASE_URL" $env:CODEX_PRO_BASE_URL
    $envArgs = Add-DockerEnvArg $envArgs "CODEX_PRO_MODEL" $env:CODEX_PRO_MODEL
    $envArgs = Add-DockerEnvArg $envArgs "BUZZ_BASE_URL" $env:BUZZ_BASE_URL
    $envArgs = Add-DockerEnvArg $envArgs "NO_PROXY" "*"
    Write-Host "[model] backend=$($env:MODEL_BACKEND) openai_model=$($env:OPENAI_MODEL) anthropic_model=$($env:ANTHROPIC_MODEL) output=$OUTPUT_DIR_NAME"
    # --memory caps the container's RAM so one heavy case gets OOM-killed in
    # isolation instead of dragging the host into swap. --memory-swap = --memory
    # disables swap so the cap is a hard ceiling. Empty $Memory disables the cap.
    docker run --rm --name $cname `
        @memArgs `
        -v "${DEMO_DIR}:/demo" `
        @envArgs `
        $image `
        -c "bash /demo/workdir/swe_issue_${num}/_run_harness.sh" 2>&1 | ForEach-Object { "$_" }
    $ErrorActionPreference = $prevEAP

    if ($LASTEXITCODE -eq 137) {
        Write-Warning "issue ${num}: exit 137 (OOM-killed or SIGKILL). Container hit the --memory=$Memory cap; raise -Memory if the case legitimately needs more."
    } elseif ($LASTEXITCODE -ne 0) {
        Write-Warning "issue $num exited non-zero (exit $LASTEXITCODE). Continuing."
    } else {
        Write-Host "[done] issue $num -> $DEMO_DIR\workdir\swe_issue_${num}\$OUTPUT_DIR_NAME"
    }

    # Remove the pulled image immediately after the container exits to prevent
    # accumulation of 1-5 GB images across hundreds of cases.
    Write-Host "[cleanup] removing image $image"
    $prevEAP2 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    docker rmi $image 2>&1 | Out-Null
    $ErrorActionPreference = $prevEAP2
}
}
finally {
    Remove-Item $LOCK_FILE -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "All issues finished."
