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

$ErrorActionPreference = "Stop"

$DEMO_DIR = "D:\demo"
$DOCKERHUB_USER = "jefzda"

# Just the issue numbers — everything else is derived from instance_metadata.json.
# 009/010/013 are the Go cases whose build gate only runs with a real toolchain.
$ISSUE_NUMS = @("008", "009", "010", "013")

# Bootstrap Python 3.11 inside the image. The base systems differ, so probe:
#   - /usr/bin/python3.11 present + pip works  → use it (qutebrowser, navidrome)
#   - /usr/bin/python3.11 present, pip missing → bootstrap via get-pip.py
#   - no system 3.11 (Debian 11 / teleport)    → extract standalone tarball
# A single probing snippet covers all three so we don't hardcode per case.
$PIP_SETUP = @"
set -e
WHEELS=/demo/eval/docker/wheels
if [ -x /usr/bin/python3.11 ]; then
  PYBIN=/usr/bin/python3.11
  if ! \$PYBIN -m pip --version >/dev/null 2>&1; then
    \$PYBIN \$WHEELS/get-pip.py --break-system-packages --no-index --find-links \$WHEELS -q
  fi
  \$PYBIN -m pip install --break-system-packages --find-links \$WHEELS --no-index -q -r /demo/requirements.lock
else
  if [ ! -x /opt/python311/bin/python3.11 ]; then
    mkdir -p /opt/python311
    tar -xzf \$WHEELS/python311-linux.tar.gz -C /opt/python311 --strip-components=1
  fi
  PYBIN=/opt/python311/bin/python3.11
  \$PYBIN -m pip install --find-links \$WHEELS --no-index -q -r /demo/requirements.lock
fi
echo "PYBIN=\$PYBIN"
"@

if (-not $env:ANTHROPIC_API_KEY) {
    Write-Error "ANTHROPIC_API_KEY is not set. Aborting."
    exit 1
}

if (-not (Test-Path "$DEMO_DIR\eval\docker\wheels")) {
    Write-Error "eval\docker\wheels\ not found. Run eval\docker\setup_wheels.ps1 first."
    exit 1
}

foreach ($num in $ISSUE_NUMS) {
    $metadata_host = "$DEMO_DIR\workdir\swe_issue_${num}\artifacts\instance_metadata.json"
    if (-not (Test-Path $metadata_host)) {
        Write-Warning "issue ${num}: $metadata_host not found; skipping."
        continue
    }

    # Read the image tag from metadata — no hardcoded tags.
    $meta = Get-Content $metadata_host -Raw | ConvertFrom-Json
    $tag  = $meta.dockerhub_tag
    if (-not $tag) {
        Write-Warning "issue ${num}: no 'dockerhub_tag' in metadata; skipping."
        continue
    }
    $image = "${DOCKERHUB_USER}/sweap-images:${tag}"

    $metadata_path = "/demo/workdir/swe_issue_${num}/artifacts/instance_metadata.json"
    $output_dir    = "/demo/workdir/swe_issue_${num}/outputs"

    # Write bash script to a file that docker can execute directly.
    # This avoids all multi-line quoting issues when passing $cmd via -c.
    $script_path = "$DEMO_DIR\workdir\swe_issue_${num}\_run_harness.sh"
    $script_content = @"
#!/bin/bash
set -e
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY

# Fake LTM server on port 9030 (no torch/chromadb needed)
python3 -c "import http.server; h=type('H',(http.server.BaseHTTPRequestHandler,),{'do_GET':lambda s:(s.send_response(200),s.send_header('Content-Type','application/json'),s.end_headers(),s.wfile.write(b'{\"status\":\"ok\",\"results\":[]}')),  'log_message':lambda *a:None}); http.server.HTTPServer(('127.0.0.1',9030),h).serve_forever()" &
sleep 2

$PIP_SETUP

cd /demo
"\$PYBIN" -m src.main \
  --instance-json $metadata_path \
  --repo-dir /app \
  --output-dir $output_dir
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
    docker run --rm `
        -v "${DEMO_DIR}:/demo" `
        -e "ANTHROPIC_API_KEY=$env:ANTHROPIC_API_KEY" `
        -e "NO_PROXY=*" `
        $image `
        -c "bash /demo/workdir/swe_issue_${num}/_run_harness.sh"

    if ($LASTEXITCODE -ne 0) {
        Write-Warning "issue $num exited non-zero (exit $LASTEXITCODE). Continuing."
    } else {
        Write-Host "[done] issue $num -> $DEMO_DIR\workdir\swe_issue_${num}\outputs"
    }
}

Write-Host ""
Write-Host "All issues finished."
