param(
    [int]$Port = 8000,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Resolve-Path (Join-Path $ScriptDir "..\..")
$PidFile = Join-Path $AppDir "data\logs\web_ui.pid"
$LogDir = Join-Path $AppDir "data\logs"
$LogFile = Join-Path $LogDir "web_ui_server.log"
$ErrFile = Join-Path $LogDir "web_ui_server.err.log"
$Python = Join-Path $AppDir ".venv\Scripts\python.exe"

if (!(Test-Path $Python)) {
    throw "Virtual environment was not found. Run install.ps1 first."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (Test-Path $PidFile) {
    $ExistingPid = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ExistingPid) {
        $Existing = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
        if ($Existing) {
            Write-Host "Web UI already running on http://127.0.0.1:$Port/"
            if (!$NoBrowser) { Start-Process "http://127.0.0.1:$Port/" }
            exit 0
        }
    }
}

$Args = @(
    "-m", "uvicorn",
    "web_app:app",
    "--app-dir", "src",
    "--host", "127.0.0.1",
    "--port", "$Port"
)

Write-Host "Starting Reagent Approval Bot Web UI..."
$Process = Start-Process `
    -FilePath $Python `
    -ArgumentList $Args `
    -WorkingDirectory $AppDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError $ErrFile `
    -PassThru

Set-Content -Path $PidFile -Value $Process.Id
Start-Sleep -Seconds 2

if (!$NoBrowser) {
    Start-Process "http://127.0.0.1:$Port/"
}

Write-Host "Web UI started: http://127.0.0.1:$Port/"
