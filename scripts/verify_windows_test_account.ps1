param([string]$ReleaseDir = $PSScriptRoot, [switch]$CI)

$ErrorActionPreference = 'Stop'
if ($CI -and $env:GITHUB_ACTIONS -ne 'true') { throw '-CI is only for the disposable GitHub Actions runner.' }
if (!$CI -and $env:USERNAME -ine 'RAB-Test') { throw 'Run this script while signed in as RAB-Test.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (!$CI -and $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Use a non-elevated PowerShell.' }

$setup = Get-ChildItem -LiteralPath $ReleaseDir -Filter 'ReagentApprovalBot-setup-*-windows-amd64.exe' -File
$browser = Get-ChildItem -LiteralPath $ReleaseDir -Filter 'ReagentApprovalBot-browser-chromium-headless-*-windows-amd64.zip' -File
if (@($setup).Count -ne 1 -or @($browser).Count -ne 1) { throw 'Exactly one candidate Setup and browser ZIP are required.' }
$registry = 'HKCU:\Software\ReagentApprovalBot'
$appEntry = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ReagentApprovalBot'
$base = (Resolve-Path -LiteralPath $env:LOCALAPPDATA).Path
$default = Join-Path $base 'Programs\ReagentApprovalBot'
if ((Test-Path -LiteralPath $registry) -or (Test-Path -LiteralPath $appEntry) -or (Test-Path -LiteralPath $default) -or
    (Test-Path -LiteralPath (Join-Path $base 'ReagentApprovalBot'))) { throw 'An existing installation or profile was found; no changes made.' }
$name = -join ([char[]](0x8bd5,0x5242,0x5ba1,0x6279,0x52a9,0x624b))
$uninstall = (-join ([char[]](0x5378,0x8f7d))) + $name
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
$programs = [Environment]::GetFolderPath('Programs')
if (!$desktop -or !$programs) { throw 'The desktop or Start menu folder is unavailable.' }
$links = @((Join-Path $desktop "$name.lnk"), (Join-Path $programs "$name.lnk"), (Join-Path $programs "$uninstall.lnk"))
foreach ($link in $links) { if (Test-Path -LiteralPath $link) { throw "Existing shortcut found: $link" } }

$root = Join-Path $base ('RAB-Accept-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
$target = Join-Path $root ((-join ([char[]](0x8bd5,0x5242))) + ' App')
$runtime = Join-Path $root 'runtime'
$report = Join-Path $env:TEMP ([IO.Path]::GetFileName($root) + '.txt')
$result = [Collections.Generic.List[string]]::new()
$saved = @{}
$testEnv = @{
    REAGENT_APPROVAL_INSTALL_DIR=$target; REAGENT_APPROVAL_RUNTIME_ROOT=$runtime
    REAGENT_APPROVAL_SILENT_INSTALL='1'; REAGENT_APPROVAL_SUPPRESS_INSTALL_MESSAGE='1'
    REAGENT_APPROVAL_WAIT_FOR_PID=$null; REAGENT_APPROVAL_SOURCE_ROOT=$null
    REAGENT_APPROVAL_LAUNCHER_LOG_REDIRECTED=$null; TEMP=$root; TMP=$root
}
$passed = $false
$linkSource = Join-Path $PSScriptRoot '..\packaging\windows\UnicodeShellLink.cs'
if (!(Test-Path -LiteralPath $linkSource -PathType Leaf)) { $linkSource = Join-Path $PSScriptRoot 'UnicodeShellLink.cs' }
Add-Type -LiteralPath $linkSource

function Invoke-Setup {
    $process = Start-Process -FilePath $setup.FullName -PassThru -WindowStyle Hidden
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw "Setup exited with $($process.ExitCode)." }
}

New-Item -ItemType Directory -Path $root | Out-Null
try {
    foreach ($key in $testEnv.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $testEnv[$key], 'Process')
    }
    Invoke-Setup
    $launcher = Join-Path $target 'ReagentApprovalBot.exe'
    if (!(Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'Stable launcher missing.' }
    if ((Get-ItemProperty -LiteralPath $registry -Name InstallLocation).InstallLocation -ine $target) { throw 'HKCU installation record mismatch.' }
    foreach ($link in $links) {
        if (!(Test-Path -LiteralPath $link -PathType Leaf)) { throw "Missing shortcut: $link" }
        $entry = [UnicodeShellLink]::Read($link)
        if ($entry.WorkingDirectory -ine $target -or ($link -ne $links[2] -and $entry.TargetPath -ine $launcher)) { throw "Unexpected shortcut target: $link" }
    }
    $result.Add('PASS: real offline Setup, HKCU install record, Desktop and Start menu shortcuts.')
    $current = Get-Content -LiteralPath (Join-Path $target 'current.json') -Raw | ConvertFrom-Json
    if ($current.version -notmatch '^\d+\.\d+\.\d+$' -or $current.path -cne "versions/$($current.version)") { throw 'Unexpected version pointer.' }
    $worker = Join-Path $target "versions\$($current.version)\ReagentApprovalBot.exe"
    $process = Start-Process -FilePath $worker -ArgumentList @('-m','automation_worker','--help') -PassThru -WindowStyle Hidden
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) { throw 'Frozen worker help failed.' }
    $log = Join-Path $runtime 'data\logs\launcher.log'
    if (!(Select-String -LiteralPath $log -Pattern 'usage: automation_worker' -Quiet)) { throw 'Frozen worker help output missing.' }
    $result.Add('PASS: frozen worker help; no ERP action.')
    $sentinel = Join-Path $runtime 'acceptance-marker.txt'
    [IO.File]::WriteAllText($sentinel, 'preserve this runtime file')
    $hash = (Get-FileHash -LiteralPath $sentinel).Hash
    Invoke-Setup
    if ((Get-FileHash -LiteralPath $sentinel).Hash -ne $hash) { throw 'Repair changed the runtime marker.' }
    $result.Add('PASS: same-version repair preserved runtime data.')
    $arp = Get-ItemProperty -LiteralPath $appEntry
    $uninstaller = Join-Path $target 'uninstall_installed.ps1'
    $engine = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $expectedArguments = '-NoProfile -ExecutionPolicy RemoteSigned -File "{0}"' -f $uninstaller
    $expectedName = $name
    if ($arp.InstallLocation -ine $target -or $arp.DisplayVersion -cne $current.version -or
        $arp.DisplayName -cne $expectedName -or $arp.UninstallString -cne "$engine $expectedArguments") {
        throw 'Windows Installed Apps registration mismatch.'
    }
    $uninstallLink = [UnicodeShellLink]::Read($links[2])
    if ($uninstallLink.TargetPath -ine $engine -or $uninstallLink.Arguments -cne $expectedArguments) {
        throw 'Start menu and Windows Installed Apps do not use the same uninstaller.'
    }
    $process = Start-Process -FilePath $engine -ArgumentList "$expectedArguments -Quiet" -WorkingDirectory $target -PassThru -WindowStyle Hidden
    $process.WaitForExit()
    if ($process.ExitCode -ne 0 -or (Test-Path -LiteralPath $target) -or
        (Test-Path -LiteralPath $appEntry) -or @($links | Where-Object { Test-Path -LiteralPath $_ }).Count -or
        (Get-FileHash -LiteralPath $sentinel).Hash -ne $hash) { throw 'Registered uninstall failed to remove its entries or preserve runtime data.' }
    $result.Add('PASS: registered uninstaller removed program, shortcuts and Windows entry; runtime data preserved.')
    $passed = $true
} catch {
    $result.Add("FAIL: $($_.Exception.Message)")
    $log = Join-Path $root 'reagent_approval_bot_install.log'
    if (Test-Path -LiteralPath $log) { foreach ($line in (Get-Content -LiteralPath $log -Tail 12)) { $result.Add($line) } }
    $log = Join-Path $root 'reagent_approval_bot_uninstall.log'
    if (Test-Path -LiteralPath $log) { foreach ($line in (Get-Content -LiteralPath $log -Tail 12)) { $result.Add($line) } }
} finally {
    foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process') }
    try {
        foreach ($link in $links) {
            if ((Test-Path -LiteralPath $link) -and [UnicodeShellLink]::Read($link).WorkingDirectory -ieq $target) {
                Remove-Item -LiteralPath $link -Force
            }
        }
        $entry = Get-ItemProperty -LiteralPath $registry -Name InstallLocation -ErrorAction SilentlyContinue
        if ($entry.InstallLocation -ieq $target) { Remove-ItemProperty -LiteralPath $registry -Name InstallLocation }
        if ((Test-Path -LiteralPath $registry) -and !(Get-Item -LiteralPath $registry).Property -and
            !(Get-ChildItem -LiteralPath $registry)) { Remove-Item -LiteralPath $registry }
        $entry = Get-ItemProperty -LiteralPath $appEntry -Name InstallLocation -ErrorAction SilentlyContinue
        if ($entry.InstallLocation -ieq $target) { Remove-Item -LiteralPath $appEntry }
        $actual = (Resolve-Path -LiteralPath $root).Path
        if ($actual -ine $root -or (Split-Path -Parent $actual) -ine $base -or
            (Split-Path -Leaf $actual) -notmatch '^RAB-Accept-[a-f0-9]{8}$' -or
            ((Get-Item -LiteralPath $actual -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            (Get-ChildItem -LiteralPath $actual -Force -Recurse -Attributes ReparsePoint | Select-Object -First 1)) {
            throw "Cleanup refused for unexpected path: $actual"
        }
        Remove-Item -LiteralPath $actual -Recurse -Force
        if ((Test-Path -LiteralPath $actual) -or @($links | Where-Object { Test-Path -LiteralPath $_ }).Count -or
            (Test-Path -LiteralPath $registry) -or (Test-Path -LiteralPath $appEntry)) { throw 'A test artifact remains.' }
        $result.Add('PASS: cleanup verified; test install, runtime, shortcuts, and HKCU record removed.')
    } catch {
        $passed = $false
        $result.Add("FAIL: cleanup: $($_.Exception.Message)")
    }
    $result | Set-Content -LiteralPath $report -Encoding UTF8
    $result | Write-Output
    Write-Output "Report: $report"
}
if (!$passed) { exit 1 }
