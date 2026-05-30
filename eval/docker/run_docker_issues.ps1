#!/usr/bin/env pwsh
# Run issues 008-010 sequentially inside their SWE-bench Pro Docker images.
# Requires: ANTHROPIC_API_KEY set, and eval\docker\setup_wheels.ps1 run first.

$ErrorActionPreference = "Stop"

$DEMO_DIR = "D:\demo"
$DOCKERHUB_USER = "jefzda"

$ISSUES = @(
    @{
        num = "008"
        tag = "qutebrowser.qutebrowser-qutebrowser__qutebrowser-f631cd4422744160d9dcf7a0455da532ce973315-v35616345bb8052ea303186706cec663146f0f"
        # Has /usr/bin/python3.11; pip missing — bootstrap with get-pip.py
        python_bin = "/usr/bin/python3.11"
        pip_setup  = @"
/usr/bin/python3.11 /demo/eval/docker/wheels/get-pip.py --break-system-packages --no-index --find-links /demo/eval/docker/wheels -q
/usr/bin/python3.11 -m pip install --break-system-packages --find-links /demo/eval/docker/wheels --no-index -q -r /demo/requirements.lock
"@
    },
    @{
        num = "009"
        tag = "gravitational.teleport-gravitational__teleport-3fa6904377c006497169945428e8197158667910-v626ec2a48416b10a88641359a169d99e935ff03"
        # Debian 11, Python 3.9 only — extract standalone Python 3.11 tarball
        python_bin = "/opt/python311/bin/python3.11"
        pip_setup  = @"
if [ ! -x /opt/python311/bin/python3.11 ]; then
  mkdir -p /opt/python311
  tar -xzf /demo/eval/docker/wheels/python311-linux.tar.gz -C /opt/python311 --strip-components=1
fi
/opt/python311/bin/python3.11 -m pip install --find-links /demo/eval/docker/wheels --no-index -q -r /demo/requirements.lock
"@
    },
    @{
        num = "010"
        tag = "navidrome.navidrome-navidrome__navidrome-7073d18b54da7e53274d11c9e2baef1242e8769e"
        # Python 3.11, pip 23, PEP 668 enforced
        python_bin = "/usr/bin/python3.11"
        pip_setup  = @"
/usr/bin/python3.11 -m pip install --break-system-packages --find-links /demo/eval/docker/wheels --no-index -q -r /demo/requirements.lock
"@
    }
)

if (-not $env:ANTHROPIC_API_KEY) {
    Write-Error "ANTHROPIC_API_KEY is not set. Aborting."
    exit 1
}

if (-not (Test-Path "$DEMO_DIR\eval\docker\wheels")) {
    Write-Error "eval\docker\wheels\ not found. Run eval\docker\setup_wheels.ps1 first."
    exit 1
}

foreach ($issue in $ISSUES) {
    $num     = $issue.num
    $tag     = $issue.tag
    $image   = "${DOCKERHUB_USER}/sweap-images:${tag}"
    $python  = $issue.python_bin
    $pip_setup = $issue.pip_setup

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

$pip_setup

cd /demo
$python -m src.main \
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
    Write-Host "  Issue $num"
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
