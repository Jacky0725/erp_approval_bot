param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\ReagentApprovalBot",
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"

$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ZipPath = Join-Path $PackageRoot "ReagentApprovalBot.zip"
if (!(Test-Path $ZipPath)) {
    throw "Installer payload was not found: $ZipPath"
}

$TempExtract = Join-Path $env:TEMP ("ReagentApprovalBotInstall_" + [guid]::NewGuid().ToString("N"))
$BackupRoot = Join-Path $env:TEMP ("ReagentApprovalBotBackup_" + [guid]::NewGuid().ToString("N"))

function Copy-IfExists {
    param([string]$Source, [string]$Destination)
    if (Test-Path $Source) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination) | Out-Null
        Copy-Item -Path $Source -Destination $Destination -Recurse -Force
    }
}

try {
    Write-Host "Installing Reagent Approval Bot to $InstallDir"
    New-Item -ItemType Directory -Force -Path $TempExtract | Out-Null
    Expand-Archive -Path $ZipPath -DestinationPath $TempExtract -Force

    if (Test-Path $InstallDir) {
        New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
        Copy-IfExists (Join-Path $InstallDir ".env") (Join-Path $BackupRoot ".env")
        Copy-IfExists (Join-Path $InstallDir "data") (Join-Path $BackupRoot "data")
        Copy-IfExists (Join-Path $InstallDir "config\settings.yaml") (Join-Path $BackupRoot "config\settings.yaml")
        Copy-IfExists (Join-Path $InstallDir "data\reagent_memory.sqlite") (Join-Path $BackupRoot "data\reagent_memory.sqlite")
        Remove-Item $InstallDir -Recurse -Force
    }

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -Path (Join-Path $TempExtract "*") -Destination $InstallDir -Recurse -Force

    Copy-IfExists (Join-Path $BackupRoot ".env") (Join-Path $InstallDir ".env")
    Copy-IfExists (Join-Path $BackupRoot "data") (Join-Path $InstallDir "data")
    Copy-IfExists (Join-Path $BackupRoot "config\settings.yaml") (Join-Path $InstallDir "config\settings.yaml")
    Copy-IfExists (Join-Path $BackupRoot "data\reagent_memory.sqlite") (Join-Path $InstallDir "data\reagent_memory.sqlite")

    $UninstallScript = Join-Path $InstallDir "uninstall_installed.ps1"
    @"
param([switch]`$KeepData)
`$ErrorActionPreference = "Stop"
`$InstallDir = "$InstallDir"
if (`$KeepData) {
    Get-ChildItem -LiteralPath `$InstallDir -Force | Where-Object { `$_.Name -notin @("data", ".env") } | Remove-Item -Recurse -Force
} elseif (Test-Path `$InstallDir) {
    Remove-Item `$InstallDir -Recurse -Force
}
`$ShortcutName = -join ([char[]](0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
`$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "`$ShortcutName.lnk"
`$StartShortcut = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\`$ShortcutName.lnk"
Remove-Item `$DesktopShortcut -Force -ErrorAction SilentlyContinue
Remove-Item `$StartShortcut -Force -ErrorAction SilentlyContinue
"@ | Set-Content -Path $UninstallScript -Encoding UTF8

    if (!$NoShortcut) {
        $ExePath = Join-Path $InstallDir "ReagentApprovalBot.exe"
        $ShortcutName = -join ([char[]](0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
        $DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "$ShortcutName.lnk"
        $StartMenuDir = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
        $StartShortcut = Join-Path $StartMenuDir "$ShortcutName.lnk"
        $WScript = New-Object -ComObject WScript.Shell
        foreach ($ShortcutPath in @($DesktopShortcut, $StartShortcut)) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ShortcutPath) | Out-Null
            $Shortcut = $WScript.CreateShortcut($ShortcutPath)
            $Shortcut.TargetPath = $ExePath
            $Shortcut.WorkingDirectory = $InstallDir
            $Shortcut.IconLocation = "{0},0" -f $ExePath
            $Shortcut.Description = "Start Reagent Approval Bot local Web UI"
            $Shortcut.Save()
        }
    }

    Write-Host "Installation complete."
    Write-Host "Open Reagent Approval Bot from the desktop shortcut or Start Menu."
} finally {
    Remove-Item $TempExtract -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $BackupRoot -Recurse -Force -ErrorAction SilentlyContinue
}
