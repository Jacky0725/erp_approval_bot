param(
    [int]$Port = 8000
)

$ErrorActionPreference = "SilentlyContinue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Resolve-Path (Join-Path $ScriptDir "..\..")
$PidFile = Join-Path $AppDir "data\logs\web_ui.pid"
$Stopped = $false

if (Test-Path $PidFile) {
    $Pid = Get-Content $PidFile | Select-Object -First 1
    if ($Pid) {
        $Process = Get-Process -Id $Pid
        if ($Process) {
            Stop-Process -Id $Pid -Force
            $Stopped = $true
        }
    }
    Remove-Item $PidFile -Force
}

$Connections = Get-NetTCPConnection -LocalPort $Port -State Listen
foreach ($Connection in $Connections) {
    if ($Connection.OwningProcess) {
        Stop-Process -Id $Connection.OwningProcess -Force
        $Stopped = $true
    }
}

if ($Stopped) {
    Write-Host "Web UI stopped."
} else {
    Write-Host "Web UI was not running."
}
