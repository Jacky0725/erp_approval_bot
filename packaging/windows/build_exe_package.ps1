param(
    [string]$Version = "",
    [string]$Python = "python",
    [ValidateSet("headless", "full")]
    [string]$BrowserBundle = "headless",
    [string]$PackageSuffix = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..\..")
if (!$Version) {
    $Version = (Get-Date -Format "yyyy.MM.dd")
}

$Arch = & $Python -c "import platform; print('x64' if platform.architecture()[0] == '64bit' else 'x86')"
$ReleaseDir = Join-Path $RepoRoot "dist\releases"
$WorkDir = Join-Path $RepoRoot "dist\pyinstaller-work"
$SpecDir = Join-Path $RepoRoot "dist\pyinstaller-spec"
$BuildName = "ReagentApprovalBot"
$DistAppDir = Join-Path $RepoRoot "dist\$BuildName"
if (!$PackageSuffix) {
    $PackageSuffix = if ($BrowserBundle -eq "headless") { "lite-portable" } else { "full-portable" }
}
$PackagePath = Join-Path $ReleaseDir "reagent-approval-bot-$Version-win-$Arch-$PackageSuffix.zip"
$Launcher = Join-Path $RepoRoot "packaging\windows\reagent_approval_bot_launcher.py"
$IconPath = Join-Path $RepoRoot "assets\reagent-approval-bot.ico"
$BrowserRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"

if (!(Test-Path $Launcher)) {
    throw "Launcher not found: $Launcher"
}
function Find-PlaywrightBrowserDir([string]$Pattern) {
    if (!(Test-Path $BrowserRoot)) {
        return $null
    }
    Get-ChildItem $BrowserRoot -Directory -Filter $Pattern |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}

$HeadlessShellDir = Find-PlaywrightBrowserDir "chromium_headless_shell-*"
$FullChromiumDir = Find-PlaywrightBrowserDir "chromium-*"
if (!$HeadlessShellDir -or ($BrowserBundle -eq "full" -and !$FullChromiumDir)) {
    Write-Host "Required Playwright Chromium runtime was not found. Installing chromium package..."
    & $Python -m playwright install chromium
    $HeadlessShellDir = Find-PlaywrightBrowserDir "chromium_headless_shell-*"
    $FullChromiumDir = Find-PlaywrightBrowserDir "chromium-*"
}
if (!$HeadlessShellDir -or !(Test-Path $HeadlessShellDir)) {
    throw "Playwright headless Chromium shell was not found under: $BrowserRoot"
}
if ($BrowserBundle -eq "full" -and (!$FullChromiumDir -or !(Test-Path $FullChromiumDir))) {
    throw "Playwright Chromium browser was not found under: $BrowserRoot"
}
$HeadlessShellName = Split-Path $HeadlessShellDir -Leaf
$FullChromiumName = if ($FullChromiumDir) { Split-Path $FullChromiumDir -Leaf } else { "" }

New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
if (Test-Path $DistAppDir) {
    Remove-Item $DistAppDir -Recurse -Force
}

$addData = @(
    "$RepoRoot\src;app\src",
    "$RepoRoot\config;app\config",
    "$RepoRoot\.env.example;app",
    "$RepoRoot\VERSION;app",
    "$RepoRoot\requirements.txt;app",
    "$RepoRoot\README.md;app",
    "$RepoRoot\AGENTS.md;app",
    "$RepoRoot\assets;app\assets",
    "$RepoRoot\packaging\windows\start_web_ui.ps1;app\packaging\windows",
    "$RepoRoot\packaging\windows\stop_web_ui.ps1;app\packaging\windows",
    "$RepoRoot\packaging\windows\uninstall.ps1;app\packaging\windows",
    "$HeadlessShellDir;ms-playwright\$HeadlessShellName"
)
if ($BrowserBundle -eq "full") {
    $addData += "$FullChromiumDir;ms-playwright\$FullChromiumName"
}

$hiddenImports = @(
    "automation_worker",
    "web_app",
    "web_runner",
    "fastapi",
    "fastapi.responses",
    "fastapi.staticfiles",
    "fastapi.templating",
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "jinja2",
    "pandas",
    "pandas._libs.tslibs.timedeltas",
    "pandas._libs.tslibs.np_datetime",
    "pandas._libs.tslibs.nattype",
    "pandas._libs.tslibs.timezones",
    "openpyxl",
    "openpyxl.cell._writer",
    "openpyxl.styles",
    "openpyxl.styles.builtins",
    "yaml",
    "dotenv",
    "playwright",
    "playwright.sync_api",
    "openai",
    "dingtalk_stream"
)

$collectAll = @(
    "playwright"
)

$excludeModules = @(
    "pytest",
    "unittest",
    "test",
    "tests",
    "matplotlib",
    "PIL",
    "lxml",
    "IPython",
    "jupyter",
    "notebook",
    "scipy",
    "pyarrow",
    "sklearn",
    "tensorflow",
    "torch",
    "cv2",
    "docutils",
    "sphinx"
)

$args = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",
    "--name", $BuildName,
    "--distpath", (Join-Path $RepoRoot "dist"),
    "--workpath", $WorkDir,
    "--specpath", $SpecDir
)

if (Test-Path $IconPath) {
    $args += @("--icon", $IconPath)
}

foreach ($item in $addData) {
    $args += @("--add-data", $item)
}
foreach ($item in $hiddenImports) {
    $args += @("--hidden-import", $item)
}
foreach ($item in $collectAll) {
    $args += @("--collect-all", $item)
}
foreach ($item in $excludeModules) {
    $args += @("--exclude-module", $item)
}
$args += $Launcher

Write-Host "Building $BuildName $Arch with PyInstaller..."
& $Python @args

function Remove-IfExists([string]$Path) {
    if (Test-Path $Path) {
        Remove-Item $Path -Recurse -Force
    }
}

function Remove-ChildDirectoriesByName([string]$Root, [string[]]$Names) {
    if (!(Test-Path $Root)) {
        return
    }
    Get-ChildItem $Root -Directory -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { $Names -contains $_.Name } |
        Sort-Object FullName -Descending |
        ForEach-Object { Remove-IfExists $_.FullName }
}

function Remove-ChildFilesByPattern([string]$Root, [string[]]$Patterns) {
    if (!(Test-Path $Root)) {
        return
    }
    foreach ($pattern in $Patterns) {
        Get-ChildItem $Root -File -Recurse -Force -Filter $pattern -ErrorAction SilentlyContinue |
            ForEach-Object { Remove-Item $_.FullName -Force }
    }
}

function Remove-BrowserLocales([string]$BrowserDir) {
    if (!(Test-Path $BrowserDir)) {
        return
    }
    Get-ChildItem $BrowserDir -Directory -Recurse -Force -Filter "locales" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Get-ChildItem $_.FullName -File -Filter "*.pak" -ErrorAction SilentlyContinue |
                Where-Object { $_.BaseName -notin @("zh-CN", "en-US") } |
                Remove-Item -Force
        }
}

Remove-IfExists (Join-Path $DistAppDir "_internal\matplotlib")
Remove-IfExists (Join-Path $DistAppDir "_internal\PIL")
Remove-IfExists (Join-Path $DistAppDir "_internal\lxml")
Remove-IfExists (Join-Path $DistAppDir "_internal\app\tests")
Remove-IfExists (Join-Path $DistAppDir "_internal\app\.pytest_cache")
Remove-ChildDirectoriesByName $DistAppDir @("__pycache__", "tests", "test", "testing", "examples", "doc", "docs")
Remove-ChildFilesByPattern $DistAppDir @("*.pyc", "*.pyo", "*.pyd.map", "*.pdb")

$BundledBrowserRoot = Join-Path $DistAppDir "_internal\ms-playwright"
if ($BrowserBundle -eq "headless") {
    Get-ChildItem $BundledBrowserRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike "chromium_headless_shell-*" } |
        Remove-Item -Recurse -Force
} else {
    Get-ChildItem $BundledBrowserRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike "chromium_headless_shell-*" -and $_.Name -notlike "chromium-*" } |
        Remove-Item -Recurse -Force
}
Get-ChildItem $BundledBrowserRoot -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-BrowserLocales $_.FullName }

$Readme = Join-Path $DistAppDir "README-WINDOWS.txt"
@"
Reagent Approval Bot $Version ($Arch)

This is a portable Windows package. Python is bundled by PyInstaller; the target
computer does not need to install Python.

Requirements:
- Windows 10 or newer.
- Network access for ERP, chemical websites, and the configured LLM provider.

Start:
1. Extract the zip.
2. Double-click ReagentApprovalBot.exe.
3. The Web UI opens at http://127.0.0.1:8000/.

Configuration:
- Real credentials are not included.
- Configure ERP and LLM credentials in the Web UI settings page.
- Runtime data, logs, review queues, .env, and the reagent memory database are
  stored outside the program folder under %LOCALAPPDATA%\ReagentApprovalBot.
  Upgrades replace program files only and keep local data intact.

Notes:
- Keep the _internal folder next to ReagentApprovalBot.exe.
"@ | Set-Content -Path $Readme -Encoding UTF8

if ($BrowserBundle -eq "headless") {
    @"
 - This lightweight package includes Playwright headless Chromium shell only.
 - The packaged runtime forces browser.headless=true.
"@ | Add-Content -Path $Readme -Encoding UTF8
} else {
    @"
 - This full package includes Playwright Chromium and headless Chromium shell.
 - It supports headed browser debugging when config/settings.yaml sets browser.headless=false.
"@ | Add-Content -Path $Readme -Encoding UTF8
}

if (Test-Path $PackagePath) {
    Remove-Item $PackagePath -Force
}
Push-Location $DistAppDir
try {
    tar.exe -a -c -f $PackagePath *
} finally {
    Pop-Location
}

Write-Host $PackagePath
