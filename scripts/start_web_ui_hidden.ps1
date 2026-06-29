$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LogDir = Join-Path $Root "data\logs"
$PidFile = Join-Path $LogDir "web_ui_server.pid"
$Stdout = Join-Path $LogDir "web_ui_server.log"
$Stderr = Join-Path $LogDir "web_ui_server.err.log"
$Url = "http://127.0.0.1:8000"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-WebUiListening {
    $connection = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @("127.0.0.1", "0.0.0.0", "::", "::1") } |
        Select-Object -First 1
    return $null -ne $connection
}

if (Test-WebUiListening) {
    Start-Process $Url | Out-Null
    exit 0
}

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    Add-Content -Path $Stderr -Encoding UTF8 -Value "$(Get-Date -Format s) python command was not found in PATH."
    Start-Process "powershell.exe" -ArgumentList @(
        "-NoExit",
        "-NoProfile",
        "-Command",
        "Write-Host 'python command was not found. Please add Python to PATH first.'; Set-Location '$Root'"
    )
    exit 1
}

$Process = Start-Process -FilePath $PythonCommand.Source `
    -ArgumentList @("-m", "uvicorn", "web_app:app", "--app-dir", "src", "--host", "127.0.0.1", "--port", "8000") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $Stdout `
    -RedirectStandardError $Stderr `
    -PassThru

Set-Content -Path $PidFile -Encoding ASCII -Value $Process.Id

for ($i = 0; $i -lt 40; $i++) {
    if (Test-WebUiListening) {
        Start-Process $Url | Out-Null
        exit 0
    }
    Start-Sleep -Milliseconds 250
}

Start-Process "powershell.exe" -ArgumentList @(
    "-NoExit",
    "-NoProfile",
    "-Command",
    "Write-Host 'Web UI startup timed out. Please check data\logs\web_ui_server.err.log'; Set-Location '$Root'"
)
exit 1
