param(
    [string]$InstallDir = "$env:LOCALAPPDATA\ReagentApprovalBot",
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    $commands = @("py -3", "python")
    foreach ($command in $commands) {
        try {
            $parts = $command.Split(" ")
            $exe = $parts[0]
            $args = $parts[1..($parts.Length - 1)]
            if ($parts.Length -eq 1) { $args = @() }
            $version = & $exe @args -c "import sys; print(sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $version) {
                return @{ Exe = $exe; Args = $args }
            }
        } catch {
            continue
        }
    }
    throw "Python 3 was not found. Please install Python 3.11+ from https://www.python.org/downloads/windows/ and run this installer again."
}

$PackageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppSource = Join-Path $PackageRoot "app"
if (!(Test-Path $AppSource)) {
    throw "Package is incomplete: app directory was not found next to install.ps1."
}

Write-Host "Installing Reagent Approval Bot to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

Write-Host "Copying application files..."
Copy-Item -Path (Join-Path $AppSource "*") -Destination $InstallDir -Recurse -Force

$Python = Resolve-Python
$VenvDir = Join-Path $InstallDir ".venv"
if (!(Test-Path $VenvDir)) {
    Write-Host "Creating Python virtual environment..."
    & $Python.Exe @($Python.Args + @("-m", "venv", $VenvDir))
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
Write-Host "Installing Python dependencies..."
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $InstallDir "requirements.txt")

Write-Host "Installing Playwright Chromium browser..."
& $VenvPython -m playwright install chromium

$EnvPath = Join-Path $InstallDir ".env"
if (!(Test-Path $EnvPath)) {
    Copy-Item -Path (Join-Path $InstallDir ".env.example") -Destination $EnvPath
    Write-Host "Created .env from .env.example. Open it or use Web UI settings to fill credentials."
}

if (!$NoShortcut) {
    $Desktop = [Environment]::GetFolderPath("Desktop")
    $ShortcutPath = Join-Path $Desktop "Reagent Approval Bot.lnk"
    $StartScript = Join-Path $InstallDir "packaging\windows\start_web_ui.ps1"
    $WScript = New-Object -ComObject WScript.Shell
    $Shortcut = $WScript.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = "powershell.exe"
    $Shortcut.Arguments = "-ExecutionPolicy Bypass -File `"$StartScript`""
    $Shortcut.WorkingDirectory = $InstallDir
    $Shortcut.IconLocation = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe,0"
    $Shortcut.Save()
    Write-Host "Created desktop shortcut: $ShortcutPath"
}

Write-Host ""
Write-Host "Installation complete."
Write-Host "Start Web UI:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$InstallDir\packaging\windows\start_web_ui.ps1`""
