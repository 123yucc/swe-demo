$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidPath = Join-Path $Root "tmp\remote_proxy_tunnel.pid"
$WatcherPidPath = Join-Path $Root "tmp\remote_proxy_tunnel_watcher.pid"
$WatcherStatePath = Join-Path $Root "tmp\remote_proxy_tunnel_watcher.state.json"
$RemoteHost = "user@172.28.8.77"
$ProxyHelper = (Join-Path $Root "scripts\http_connect_proxy.py").Replace("\", "/")
$ProxyCommand = "ProxyCommand=python $ProxyHelper %h %p --proxy-port 7897"

if (Test-Path $PidPath) {
    $TunnelPid = Get-Content $PidPath -ErrorAction SilentlyContinue
    Write-Output ("PID=" + $TunnelPid)
    if ($TunnelPid) {
        Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, StartTime
    }
} else {
    Write-Output "PID_MISSING"
}

& ssh.exe "-o" $ProxyCommand $RemoteHost "sh -lc 'ss -ltn | grep ""127.0.0.1:7897"" || true'" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Output "REMOTE_TUNNEL_CHECK_FAILED"
}

if (Test-Path -LiteralPath $WatcherPidPath) {
    $WatcherPid = Get-Content -LiteralPath $WatcherPidPath -ErrorAction SilentlyContinue
    $WatcherProcess = $null
    if ($WatcherPid) {
        $WatcherProcess = Get-Process -Id $WatcherPid -ErrorAction SilentlyContinue
    }
    Write-Output (
        "WATCHER_PID=" + $WatcherPid +
        " WATCHER_ALIVE=" + ($null -ne $WatcherProcess).ToString().ToLowerInvariant()
    )
}
if (Test-Path -LiteralPath $WatcherStatePath) {
    $WatcherState = Get-Content -LiteralPath $WatcherStatePath -Raw | ConvertFrom-Json
    Write-Output (
        "WATCHER_STATUS=" + $WatcherState.status +
        " UPDATED_AT=" + $WatcherState.updated_at +
        " FAILURES=" + $WatcherState.consecutive_failures
    )
}
