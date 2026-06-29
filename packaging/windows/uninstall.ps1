param(
    [string]$InstallDir = "$env:LOCALAPPDATA\ReagentApprovalBot",
    [switch]$KeepData
)

$ErrorActionPreference = "Stop"

$StopScript = Join-Path $InstallDir "packaging\windows\stop_web_ui.ps1"
if (Test-Path $StopScript) {
    powershell -ExecutionPolicy Bypass -File $StopScript
}

$ShortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Reagent Approval Bot.lnk"
if (Test-Path $ShortcutPath) {
    Remove-Item $ShortcutPath -Force
}

if (Test-Path $InstallDir) {
    if ($KeepData) {
        Get-ChildItem $InstallDir -Force |
            Where-Object { $_.Name -ne "data" -and $_.Name -ne ".env" } |
            Remove-Item -Recurse -Force
        Write-Host "Application files removed. Preserved data directory and .env."
    } else {
        Remove-Item $InstallDir -Recurse -Force
        Write-Host "Application removed: $InstallDir"
    }
} else {
    Write-Host "Install directory was not found: $InstallDir"
}
