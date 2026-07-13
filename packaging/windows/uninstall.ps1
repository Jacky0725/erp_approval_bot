param(
    [string]$InstallDir = "$env:LOCALAPPDATA\ReagentApprovalBot",
    [switch]$KeepData
)

$ErrorActionPreference = "Stop"

$StopScript = Join-Path $InstallDir "packaging\windows\stop_web_ui.ps1"
if (Test-Path $StopScript) {
    powershell -ExecutionPolicy Bypass -File $StopScript
}

$ShortcutName = -join ([char[]](0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
$ShortcutPaths = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "$ShortcutName.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Reagent Approval Bot.lnk")
)
foreach ($ShortcutPath in $ShortcutPaths) {
    if (Test-Path $ShortcutPath) {
        Remove-Item $ShortcutPath -Force
    }
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
