param(
    [string]$Version = "",
    [string]$Python = "python",
    [switch]$SkipFull
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
if (!$Version) {
    $Version = (Get-Content (Join-Path $RepoRoot "VERSION") -Raw).Trim()
}

$ReleaseDir = Join-Path $RepoRoot "dist\releases"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

$Outputs = @()

if (!$SkipFull) {
    & (Join-Path $ScriptDir "build_exe_package.ps1") `
        -Version $Version `
        -Python $Python `
        -BrowserBundle full `
        -PackageSuffix full-portable
    $FullPortable = Join-Path $ReleaseDir "reagent-approval-bot-$Version-win-x64-full-portable.zip"
    $Outputs += $FullPortable

    & (Join-Path $ScriptDir "build_installer.ps1") `
        -Version $Version `
        -Python $Python `
        -PortableZip $FullPortable `
        -InstallerSuffix full-test-setup
    $Outputs += (Join-Path $ReleaseDir "reagent-approval-bot-$Version-win-x64-full-test-setup.exe")
}

& (Join-Path $ScriptDir "build_exe_package.ps1") `
    -Version $Version `
    -Python $Python `
    -BrowserBundle headless `
    -PackageSuffix lite-portable
$LitePortable = Join-Path $ReleaseDir "reagent-approval-bot-$Version-win-x64-lite-portable.zip"

& (Join-Path $ScriptDir "build_installer.ps1") `
    -Version $Version `
    -Python $Python `
    -PortableZip $LitePortable `
    -InstallerSuffix lite-setup
$Outputs += (Join-Path $ReleaseDir "reagent-approval-bot-$Version-win-x64-lite-setup.exe")

Write-Host "Release artifacts:"
foreach ($Output in $Outputs) {
    if (Test-Path $Output) {
        $SizeMb = [math]::Round((Get-Item $Output).Length / 1MB, 1)
        Write-Host " - $Output ($SizeMb MB)"
    } else {
        throw "Expected artifact was not created: $Output"
    }
}
