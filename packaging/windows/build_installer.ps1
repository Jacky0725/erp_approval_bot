param(
    [string]$Version = "",
    [string]$PortableZip = "",
    [string]$Python = "python",
    [string]$InstallerSuffix = "setup"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..\..")
if (!$Version) {
    $Version = (Get-Date -Format "yyyy.MM.dd")
}

$ReleaseDir = Join-Path $RepoRoot "dist\releases"
if (!$PortableZip) {
    $PortableZip = Join-Path $ReleaseDir "reagent-approval-bot-$Version-win-x64-lite-portable.zip"
}
if (!(Test-Path $PortableZip)) {
    throw "Portable zip not found: $PortableZip"
}

$StageDir = Join-Path $RepoRoot "dist\installer-stage\reagent-approval-bot-$Version-win-x64-$InstallerSuffix"
$WorkDir = Join-Path $RepoRoot "dist\pyinstaller-installer-work"
$SpecDir = Join-Path $RepoRoot "dist\pyinstaller-installer-spec"
$OneFileOut = Join-Path $RepoRoot "dist\ReagentApprovalBotInstaller.exe"
$InstallerPath = Join-Path $ReleaseDir "reagent-approval-bot-$Version-win-x64-$InstallerSuffix.exe"
$InstallerScript = Join-Path $RepoRoot "packaging\windows\reagent_approval_bot_installer.py"
$IconPath = Join-Path $RepoRoot "assets\reagent-approval-bot.ico"

if (!(Test-Path $InstallerScript)) {
    throw "Installer launcher not found: $InstallerScript"
}
if (Test-Path $StageDir) {
    Remove-Item $StageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "payload") | Out-Null
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
Copy-Item -Path $PortableZip -Destination (Join-Path $StageDir "payload\ReagentApprovalBot.zip") -Force

if (Test-Path $OneFileOut) {
    Remove-Item $OneFileOut -Force
}
if (Test-Path $InstallerPath) {
    Remove-Item $InstallerPath -Force
}

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name ReagentApprovalBotInstaller `
    --distpath (Join-Path $RepoRoot "dist") `
    --workpath $WorkDir `
    --specpath $SpecDir `
    --add-data "$StageDir\payload;payload" `
    --hidden-import tkinter `
    --hidden-import tkinter.ttk `
    --icon $IconPath `
    $InstallerScript

if (!(Test-Path $OneFileOut)) {
    throw "Installer was not created: $OneFileOut"
}
Move-Item -Path $OneFileOut -Destination $InstallerPath -Force
Write-Host $InstallerPath
