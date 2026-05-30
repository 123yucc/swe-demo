#!/usr/bin/env pwsh
# Pre-download Linux wheels + standalone Python 3.11 on Windows (proxy works here).
# Run once before run_docker_issues.ps1.

$ErrorActionPreference = "Stop"
$DEMO_DIR = "D:\demo"
$WHEELS_DIR = "$DEMO_DIR\eval\docker\wheels"

# Use local proxy on Windows
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
$env:HTTP_PROXY  = "http://127.0.0.1:7897"

New-Item -ItemType Directory -Force -Path $WHEELS_DIR | Out-Null

Write-Host "[1/2] Downloading Linux wheels for Python 3.11 (manylinux_2_17_x86_64)..."
# Use the lock file (exact pinned versions) to avoid pip backtracking.
# Download wheels targeting Linux/cp311 so they work inside the containers.
pip download `
    --dest $WHEELS_DIR `
    --platform manylinux_2_17_x86_64 `
    --python-version 311 `
    --implementation cp `
    --abi cp311 `
    --only-binary ":all:" `
    -i https://mirrors.aliyun.com/pypi/simple/ `
    -r "$DEMO_DIR\requirements.lock"

if ($LASTEXITCODE -ne 0) {
    Write-Error "pip download failed. Check proxy and requirements.txt."
    exit 1
}

Write-Host "[2/2] Downloading standalone Python 3.11 for Debian (issue 009)..."
$PY_URL  = "https://github.com/indygreg/python-build-standalone/releases/download/20240814/cpython-3.11.9+20240814-x86_64-unknown-linux-gnu-install_only.tar.gz"
$PY_DEST = "$WHEELS_DIR\python311-linux.tar.gz"

if (Test-Path $PY_DEST) {
    Write-Host "  Already exists, skipping download."
} else {
    Invoke-WebRequest -Uri $PY_URL -OutFile $PY_DEST -Proxy "http://127.0.0.1:7897"
    Write-Host "  Saved to $PY_DEST"
}

Write-Host ""
Write-Host "Done. Wheels in: $WHEELS_DIR"
Write-Host "Run eval\docker\run_docker_issues.ps1 next."
