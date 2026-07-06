$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidPath = Join-Path $Root "tmp\remote_proxy_tunnel.pid"
$RemoteHost = "user@172.28.8.77"

if (Test-Path $PidPath) {
    $TunnelPid = Get-Content $PidPath -ErrorAction SilentlyContinue
    Write-Output ("PID=" + $TunnelPid)
    if ($TunnelPid) {
        Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, StartTime
    }
} else {
    Write-Output "PID_MISSING"
}

& ssh.exe $RemoteHost "sh -lc 'ss -ltn | grep ""127.0.0.1:7897"" || true'" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Output "REMOTE_TUNNEL_CHECK_FAILED"
}
