$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$TmpDir = Join-Path $Root "tmp"
$PidPath = Join-Path $TmpDir "remote_proxy_tunnel_watcher.pid"
$WatcherPath = Join-Path $Root "scripts\watch_remote_proxy_tunnel.ps1"
$StdoutPath = Join-Path $TmpDir "remote_proxy_tunnel_watcher.log"
$StderrPath = Join-Path $TmpDir "remote_proxy_tunnel_watcher.err.log"

New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null

if (Test-Path -LiteralPath $PidPath) {
    try {
        $ExistingPid = [int](Get-Content -LiteralPath $PidPath -Raw).Trim()
        $ExistingProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$ExistingPid" -ErrorAction SilentlyContinue
        if (
            $null -ne $ExistingProcess -and
            $ExistingProcess.CommandLine -like "*watch_remote_proxy_tunnel.ps1*"
        ) {
            Write-Output "ALREADY_RUNNING PID=$ExistingPid"
            exit 0
        }
    } catch {
        # A stale or malformed PID file is replaced by the new watcher.
    }
}

$PowerShellPath = (Get-Process -Id $PID).Path
$Arguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", ('"' + $WatcherPath + '"'),
    "-PollSeconds", "60",
    "-FailureThreshold", "3"
)
$Process = Start-Process `
    -FilePath $PowerShellPath `
    -ArgumentList $Arguments `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutPath `
    -RedirectStandardError $StderrPath `
    -PassThru

Start-Sleep -Seconds 3
$Started = Get-Process -Id $Process.Id -ErrorAction SilentlyContinue
if ($null -eq $Started) {
    throw "Tunnel watcher exited during startup. See $StderrPath"
}

Write-Output ("STARTED_PID=" + $Process.Id)
