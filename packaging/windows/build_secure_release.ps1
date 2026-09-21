param(
    [string]$Version = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..\..")
if (!$Version) { $Version = (Get-Content (Join-Path $RepoRoot "VERSION") -Raw).Trim() }
$ReleaseDir = Join-Path $RepoRoot "dist\releases"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

# The core package deliberately excludes browser binaries; the browser is a separately verified asset.
& (Join-Path $PSScriptRoot "build_exe_package.ps1") -Version $Version -Python $Python -BrowserBundle none -PackageSuffix core
$Core = Join-Path $ReleaseDir "ReagentApprovalBot-core-$Version-windows-amd64.zip"

$BrowserRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"
$Browser = Get-ChildItem $BrowserRoot -Directory -Filter "chromium_headless_shell-*" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (!$Browser) { throw "Install Playwright Chromium headless shell before building the browser package." }
$Revision = $Browser.Name.Replace("chromium_headless_shell-", "")
$BrowserStage = Join-Path $RepoRoot "dist\browser-stage\$Revision"
Remove-Item $BrowserStage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $BrowserStage | Out-Null
Copy-Item $Browser.FullName -Destination (Join-Path $BrowserStage $Browser.Name) -Recurse -Force
Get-ChildItem $BrowserStage -Directory -Recurse -Filter locales | ForEach-Object {
    Get-ChildItem $_.FullName -File -Filter *.pak | Where-Object { $_.BaseName -notin @("zh-CN", "en-US") } | Remove-Item -Force
}
$BrowserPackage = Join-Path $ReleaseDir "ReagentApprovalBot-browser-chromium-headless-$Revision-windows-amd64.zip"
Remove-Item $BrowserPackage -Force -ErrorAction SilentlyContinue
Push-Location $BrowserStage
try { tar.exe -a -c -f $BrowserPackage * } finally { Pop-Location }

$Bootstrap = Join-Path $ReleaseDir "ReagentApprovalBotBootstrap.exe"
$Updater = Join-Path $ReleaseDir "ReagentApprovalBotUpdater.exe"
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name ReagentApprovalBotBootstrap --distpath $ReleaseDir --workpath (Join-Path $RepoRoot "dist\bootstrap-work") --specpath (Join-Path $RepoRoot "dist\bootstrap-spec") (Join-Path $PSScriptRoot "reagent_approval_bot_bootstrap.py")
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name ReagentApprovalBotUpdater --distpath $ReleaseDir --workpath (Join-Path $RepoRoot "dist\updater-work") --specpath (Join-Path $RepoRoot "dist\updater-spec") --paths (Join-Path $RepoRoot "src") --hidden-import secure_update (Join-Path $PSScriptRoot "reagent_approval_bot_updater.py")

$Manifest = Join-Path $ReleaseDir "release-manifest.json"
$Repository = $env:REAGENT_APPROVAL_GITHUB_REPO
if (!$Repository) { $Repository = "Jacky0725/erp_approval_bot" }
& $Python (Join-Path $PSScriptRoot "create_release_manifest.py") --version $Version --repository $Repository --core $Core --browser $BrowserPackage --browser-revision $Revision --output $Manifest

foreach ($item in @(@("core", $Core), @("browser", $BrowserPackage))) {
    & $Python (Join-Path $PSScriptRoot "check_package_size.py") --kind $item[0] --artifact $item[1] --report ("$item[1].size.json")
}
Write-Host "Core: $Core"
Write-Host "Browser: $BrowserPackage"
Write-Host "Manifest: $Manifest"
