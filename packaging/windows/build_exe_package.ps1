param(
    [string]$Version = "",
    [string]$Python = "python"
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
$PackagePath = Join-Path $ReleaseDir "reagent-approval-bot-$Version-win-$Arch-portable.zip"
$Launcher = Join-Path $RepoRoot "packaging\windows\reagent_approval_bot_launcher.py"
$IconPath = Join-Path $RepoRoot "assets\reagent-approval-bot.ico"
$BrowserRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"

if (!(Test-Path $Launcher)) {
    throw "Launcher not found: $Launcher"
}
if (!(Test-Path (Join-Path $BrowserRoot "chromium-1223"))) {
    Write-Host "Playwright Chromium was not found. Installing chromium..."
    & $Python -m playwright install chromium
}

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
    "$BrowserRoot\chromium-1223;ms-playwright\chromium-1223",
    "$BrowserRoot\chromium_headless_shell-1223;ms-playwright\chromium_headless_shell-1223",
    "$BrowserRoot\ffmpeg-1011;ms-playwright\ffmpeg-1011"
)

$hiddenImports = @(
    "automation_worker",
    "web_app",
    "web_runner",
    "fastapi",
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
    "openpyxl",
    "yaml",
    "dotenv",
    "playwright",
    "playwright.sync_api",
    "openai"
)

$collectAll = @(
    "playwright",
    "pandas",
    "openpyxl",
    "fastapi",
    "starlette",
    "pydantic",
    "uvicorn",
    "jinja2",
    "openai",
    "python_multipart"
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
$args += $Launcher

Write-Host "Building $BuildName $Arch with PyInstaller..."
& $Python @args

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
- Runtime data, logs, review queues, and .env are stored inside this extracted folder.

Notes:
- Keep the _internal folder next to ReagentApprovalBot.exe.
- This package includes Playwright Chromium only.
"@ | Set-Content -Path $Readme -Encoding UTF8

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
