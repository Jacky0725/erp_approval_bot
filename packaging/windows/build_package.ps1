param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "..\..")
if (!$Version) {
    $Version = (Get-Date -Format "yyyy.MM.dd")
}

$DistDir = Join-Path $RepoRoot "dist\releases"
$StageDir = Join-Path $RepoRoot "dist\stage\reagent-approval-bot-$Version-win"
$PackagePath = Join-Path $DistDir "reagent-approval-bot-$Version-win.zip"

if (Test-Path $StageDir) {
    Remove-Item $StageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "app") | Out-Null
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

$AppDest = Join-Path $StageDir "app"
$IncludePaths = @(
    ".env.example",
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "requirements.txt",
    "start_web_ui_hidden.bat",
    "stop_web_ui.bat",
    "config",
    "scripts",
    "src",
    "tests",
    "packaging"
)

foreach ($Relative in $IncludePaths) {
    $Source = Join-Path $RepoRoot $Relative
    if (!(Test-Path $Source)) { continue }
    $Target = Join-Path $AppDest $Relative
    $Parent = Split-Path -Parent $Target
    New-Item -ItemType Directory -Force -Path $Parent | Out-Null
    Copy-Item -Path $Source -Destination $Target -Recurse -Force
}

$RemovePatterns = @(
    ".pytest_cache",
    "__pycache__",
    "*.pyc",
    "data",
    ".venv",
    "dist",
    ".env",
    "config\rule_candidates.xlsx"
)

foreach ($Pattern in $RemovePatterns) {
    Get-ChildItem -Path $AppDest -Recurse -Force -Filter $Pattern -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

Copy-Item -Path (Join-Path $RepoRoot "packaging\windows\install.ps1") -Destination (Join-Path $StageDir "install.ps1")
Copy-Item -Path (Join-Path $RepoRoot "packaging\windows\uninstall.ps1") -Destination (Join-Path $StageDir "uninstall.ps1")

$ReadmePath = Join-Path $StageDir "INSTALL_README.txt"
@"
Reagent Approval Bot Windows Package
Version: $Version

Requirements:
- Windows 10 or newer.
- Python 3.11+ installed and available as `python` or `py -3`.
- Internet access during installation, unless Python dependencies and Playwright Chromium are already cached.

Install:
1. Extract this zip.
2. Right-click PowerShell and choose "Run as administrator" only if your policy requires it.
3. Run:
   powershell -ExecutionPolicy Bypass -File .\install.ps1

Start Web UI after installation:
   powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\ReagentApprovalBot\packaging\windows\start_web_ui.ps1"

Open:
   http://127.0.0.1:8000/

Uninstall:
   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1

Notes:
- The installer does not include real credentials. Fill ERP and LLM settings in the Web UI or .env.
- This package is architecture-neutral at the project level. Python/Playwright dependencies follow the installed Python architecture.
"@ | Set-Content -Path $ReadmePath -Encoding UTF8

if (Test-Path $PackagePath) {
    Remove-Item $PackagePath -Force
}
Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $PackagePath -Force
Write-Host $PackagePath
