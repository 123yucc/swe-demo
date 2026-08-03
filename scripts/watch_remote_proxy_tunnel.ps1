param(
    [int]$PollSeconds = 60,
    [int]$FailureThreshold = 3,
    [switch]$Once,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"

if ($PollSeconds -lt 5 -and -not $Once) {
    throw "PollSeconds must be at least 5."
}
if ($FailureThreshold -lt 1) {
    throw "FailureThreshold must be at least 1."
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TmpDir = Join-Path $Root "tmp"
$TunnelPidPath = Join-Path $TmpDir "remote_proxy_tunnel.pid"
$WatcherPidPath = Join-Path $TmpDir "remote_proxy_tunnel_watcher.pid"
$StatePath = Join-Path $TmpDir "remote_proxy_tunnel_watcher.state.json"
$RemoteHost = "user@172.28.8.77"
$ProxyHelper = (Join-Path $Root "scripts\http_connect_proxy.py").Replace("\", "/")
$ProxyCommand = "ProxyCommand=python $ProxyHelper %h %p --proxy-port 7897"
$StartScript = Join-Path $Root "scripts\start_remote_proxy_tunnel.ps1"
$MutexName = "Local\DemoRemoteProxyTunnelWatcher"

New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null

function Get-TunnelPid {
    if (-not (Test-Path -LiteralPath $TunnelPidPath)) {
        return $null
    }
    try {
        return [int](Get-Content -LiteralPath $TunnelPidPath -Raw).Trim()
    } catch {
        return $null
    }
}

function Test-LocalProxy {
    return $null -ne (
        Get-NetTCPConnection -LocalPort 7897 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    )
}

function Test-RemoteTunnel {
    & ssh.exe `
        "-o" $ProxyCommand `
        "-o" "ConnectTimeout=15" `
        "-o" "ServerAliveInterval=15" `
        "-o" "ServerAliveCountMax=2" `
        $RemoteHost `
        "sh -lc 'ss -ltn | grep -q ""127.0.0.1:7897""'" 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Write-WatcherState {
    param(
        [string]$Status,
        [string]$Message,
        [bool]$LocalProxy,
        [bool]$RemoteTunnel,
        [int]$ConsecutiveFailures
    )
    $TunnelPid = Get-TunnelPid
    $TunnelProcessAlive = $false
    if ($null -ne $TunnelPid) {
        $TunnelProcessAlive = $null -ne (
            Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue
        )
    }
    $Payload = [ordered]@{
        schema_version = 1
        updated_at = [DateTimeOffset]::Now.ToString("o")
        status = $Status
        message = $Message
        watcher_pid = $PID
        tunnel_pid = $TunnelPid
        tunnel_process_alive = $TunnelProcessAlive
        local_proxy_ready = $LocalProxy
        remote_tunnel_ready = $RemoteTunnel
        consecutive_failures = $ConsecutiveFailures
        failure_threshold = $FailureThreshold
        poll_seconds = $PollSeconds
    }
    $Temporary = "$StatePath.tmp"
    $Json = $Payload | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText(
        $Temporary,
        $Json,
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $Temporary -Destination $StatePath -Force
}

$Mutex = [Threading.Mutex]::new($false, $MutexName)
$Acquired = $false
try {
    $Acquired = $Mutex.WaitOne(0)
    if (-not $Acquired) {
        Write-Output "TUNNEL_WATCHER_ALREADY_RUNNING"
        exit 0
    }

    $PID | Set-Content -LiteralPath $WatcherPidPath -Encoding ascii
    $ConsecutiveFailures = 0

    while ($true) {
        $LocalProxyReady = Test-LocalProxy
        $RemoteTunnelReady = $false
        if ($LocalProxyReady) {
            $RemoteTunnelReady = Test-RemoteTunnel
        }

        if ($LocalProxyReady -and $RemoteTunnelReady) {
            $ConsecutiveFailures = 0
            Write-WatcherState `
                -Status "healthy" `
                -Message "local proxy and remote reverse listener are ready" `
                -LocalProxy $true `
                -RemoteTunnel $true `
                -ConsecutiveFailures $ConsecutiveFailures
        } elseif (-not $LocalProxyReady) {
            $ConsecutiveFailures = 0
            Write-WatcherState `
                -Status "waiting_for_local_proxy" `
                -Message "local port 7897 is not listening" `
                -LocalProxy $false `
                -RemoteTunnel $false `
                -ConsecutiveFailures $ConsecutiveFailures
        } else {
            $ConsecutiveFailures += 1
            if ($CheckOnly -or $ConsecutiveFailures -lt $FailureThreshold) {
                Write-WatcherState `
                    -Status "degraded" `
                    -Message "remote reverse listener check failed" `
                    -LocalProxy $true `
                    -RemoteTunnel $false `
                    -ConsecutiveFailures $ConsecutiveFailures
            } else {
                Write-WatcherState `
                    -Status "restarting" `
                    -Message "failure threshold reached; restarting exact tunnel process" `
                    -LocalProxy $true `
                    -RemoteTunnel $false `
                    -ConsecutiveFailures $ConsecutiveFailures
                try {
                    $StartOutput = & $StartScript 2>&1 | Out-String
                    $RemoteTunnelReady = Test-RemoteTunnel
                    if ($RemoteTunnelReady) {
                        $ConsecutiveFailures = 0
                        Write-WatcherState `
                            -Status "healthy" `
                            -Message ("tunnel restarted: " + $StartOutput.Trim()) `
                            -LocalProxy $true `
                            -RemoteTunnel $true `
                            -ConsecutiveFailures $ConsecutiveFailures
                    } else {
                        Write-WatcherState `
                            -Status "restart_failed" `
                            -Message ("launcher returned without a healthy listener: " + $StartOutput.Trim()) `
                            -LocalProxy $true `
                            -RemoteTunnel $false `
                            -ConsecutiveFailures $ConsecutiveFailures
                    }
                } catch {
                    Write-WatcherState `
                        -Status "restart_failed" `
                        -Message $_.Exception.Message `
                        -LocalProxy $true `
                        -RemoteTunnel $false `
                        -ConsecutiveFailures $ConsecutiveFailures
                }
            }
        }

        if ($Once) {
            break
        }
        Start-Sleep -Seconds $PollSeconds
    }
} finally {
    if ($Acquired) {
        $Mutex.ReleaseMutex()
    }
    $Mutex.Dispose()
}
