$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidPath = Join-Path $Root "tmp\remote_proxy_tunnel.pid"
$RemoteHost = "user@172.28.8.77"
$SshArgs = @(
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-N", "-T",
    "-R", "7897:127.0.0.1:7897",
    $RemoteHost
)

New-Item -ItemType Directory -Force -Path (Split-Path $PidPath) | Out-Null

if (-not (Get-NetTCPConnection -LocalPort 7897 -State Listen -ErrorAction SilentlyContinue)) {
    throw "Local port 7897 is not listening. Start the local proxy first."
}

function Test-RemoteTunnel {
    & ssh.exe $RemoteHost "sh -lc 'ss -ltn | grep -q ""127.0.0.1:7897""'" | Out-Null
    return $LASTEXITCODE -eq 0
}

if (Test-Path $PidPath) {
    $ExistingTunnelPid = Get-Content $PidPath -ErrorAction SilentlyContinue
    if ($ExistingTunnelPid) {
        $ExistingProc = Get-Process -Id $ExistingTunnelPid -ErrorAction SilentlyContinue
        if ($ExistingProc -and (Test-RemoteTunnel)) {
            Write-Output "ALREADY_RUNNING PID=$ExistingTunnelPid"
            exit 0
        }
        if ($ExistingProc) {
            Stop-Process -Id $ExistingTunnelPid -Force -ErrorAction SilentlyContinue
        }
        Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
    }
}

$Proc = Start-Process -FilePath "ssh.exe" -ArgumentList $SshArgs -WindowStyle Hidden -PassThru
$Proc.Id | Set-Content -Encoding ascii $PidPath
Start-Sleep -Seconds 3

$StartedProc = Get-Process -Id $Proc.Id -ErrorAction SilentlyContinue
if (-not $StartedProc) {
    Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
    throw "Reverse tunnel process exited immediately."
}

if (-not (Test-RemoteTunnel)) {
    Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue
    Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
    throw "Remote port 7897 is not listening after tunnel startup."
}

Write-Output ("STARTED_PID=" + $Proc.Id)
