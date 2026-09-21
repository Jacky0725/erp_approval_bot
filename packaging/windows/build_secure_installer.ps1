param(
    [string]$Version = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..\..")
if (!$Version) { $Version = (Get-Content (Join-Path $RepoRoot "VERSION") -Raw).Trim() }
$ReleaseDir = Join-Path $RepoRoot "dist\releases"
$Core = Join-Path $ReleaseDir "ReagentApprovalBot-core-$Version-windows-amd64.zip"
$Manifest = Join-Path $ReleaseDir "release-manifest.json"
if (!(Test-Path $Core) -or !(Test-Path $Manifest)) { throw "Build the secure release assets before building Setup." }
$ManifestData = Get-Content $Manifest -Raw | ConvertFrom-Json
$BrowserName = ($ManifestData.assets | Where-Object kind -eq "browser").name
$Browser = Join-Path $ReleaseDir $BrowserName
if (!(Test-Path $Browser)) { throw "Browser package not found: $Browser" }

$Stage = Join-Path $RepoRoot "dist\secure-installer-stage"
Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "payload") | Out-Null
Copy-Item $Core, $Browser, $Manifest -Destination (Join-Path $Stage "payload") -Force
Copy-Item (Join-Path $ReleaseDir "ReagentApprovalBotBootstrap.exe") -Destination (Join-Path $Stage "payload\ReagentApprovalBotBootstrap.exe") -Force
Copy-Item (Join-Path $ReleaseDir "ReagentApprovalBotUpdater.exe") -Destination (Join-Path $Stage "payload\ReagentApprovalBotUpdater.exe") -Force
$Output = Join-Path $ReleaseDir "ReagentApprovalBot-setup-$Version-windows-amd64.exe"
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name ReagentApprovalBotSetup --distpath (Join-Path $RepoRoot "dist") --workpath (Join-Path $RepoRoot "dist\secure-installer-work") --specpath (Join-Path $RepoRoot "dist\secure-installer-spec") --paths (Join-Path $RepoRoot "src") --hidden-import secure_update --add-data "$Stage\payload;payload" --hidden-import tkinter --hidden-import tkinter.ttk --icon (Join-Path $RepoRoot "assets\reagent-approval-bot.ico") (Join-Path $PSScriptRoot "reagent_approval_bot_installer.py")
Move-Item (Join-Path $RepoRoot "dist\ReagentApprovalBotSetup.exe") $Output -Force
& $Python (Join-Path $PSScriptRoot "check_package_size.py") --kind setup --artifact $Output --report "$Output.size.json"
Write-Host $Output
