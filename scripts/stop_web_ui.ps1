$ErrorActionPreference = "Continue"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir = Join-Path $Root "data\logs"
$PidFile = Join-Path $LogDir "web_ui_server.pid"

function Stop-ProcessTree {
    param([int]$ProcessId)
    if ($ProcessId -le 0) {
        return
    }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $process) {
        return
    }
    & taskkill.exe /PID $ProcessId /T /F | Out-Null
}

if (Test-Path $PidFile) {
    $storedPid = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $parsedPid = 0
    if ([int]::TryParse($storedPid, [ref]$parsedPid)) {
        Stop-ProcessTree -ProcessId $parsedPid
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

$listeners = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    Stop-ProcessTree -ProcessId ([int]$listener.OwningProcess)
}

Write-Host "Web UI background service stopped."
