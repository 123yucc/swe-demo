$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidPath = Join-Path $Root "tmp\remote_proxy_tunnel.pid"

if (-not (Test-Path $PidPath)) {
    Write-Output "PID_MISSING"
    exit 0
}

$TunnelPid = Get-Content $PidPath -ErrorAction SilentlyContinue
if ($TunnelPid) {
    Stop-Process -Id $TunnelPid -Force -ErrorAction SilentlyContinue
}
Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
Write-Output "STOPPED"
