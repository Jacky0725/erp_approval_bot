from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile


APP_NAME = "ReagentApprovalBot"
SHORTCUT_NAME = "Reagent Approval Bot.lnk"


def bundled_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def payload_zip() -> Path:
    path = bundled_root() / "payload" / "ReagentApprovalBot.zip"
    if not path.exists():
        raise FileNotFoundError(f"Installer payload not found: {path}")
    return path


def install_dir() -> Path:
    override = os.getenv("REAGENT_APPROVAL_INSTALL_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Programs" / APP_NAME


def copy_if_exists(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def preserve_existing(target: Path, backup: Path) -> None:
    copy_if_exists(target / ".env", backup / ".env")
    copy_if_exists(target / "data", backup / "data")
    copy_if_exists(target / "config" / "settings.yaml", backup / "config" / "settings.yaml")
    copy_if_exists(target / "data" / "reagent_memory.sqlite", backup / "data" / "reagent_memory.sqlite")


def restore_existing(target: Path, backup: Path) -> None:
    copy_if_exists(backup / ".env", target / ".env")
    copy_if_exists(backup / "data", target / "data")
    copy_if_exists(backup / "config" / "settings.yaml", target / "config" / "settings.yaml")
    copy_if_exists(backup / "data" / "reagent_memory.sqlite", target / "data" / "reagent_memory.sqlite")


def write_uninstaller(target: Path) -> None:
    script = target / "uninstall_installed.ps1"
    script.write_text(
        textwrap.dedent(
            f"""
            param([switch]$KeepData)
            $ErrorActionPreference = "Stop"
            $InstallDir = "{target}"
            if ($KeepData) {{
                Get-ChildItem -LiteralPath $InstallDir -Force | Where-Object {{ $_.Name -notin @("data", ".env") }} | Remove-Item -Recurse -Force
            }} elseif (Test-Path $InstallDir) {{
                Remove-Item $InstallDir -Recurse -Force
            }}
            $DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "{SHORTCUT_NAME}"
            $StartShortcut = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\\{SHORTCUT_NAME}"
            Remove-Item $DesktopShortcut -Force -ErrorAction SilentlyContinue
            Remove-Item $StartShortcut -Force -ErrorAction SilentlyContinue
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def create_shortcuts(target: Path) -> None:
    exe = target / "ReagentApprovalBot.exe"
    ps = textwrap.dedent(
        f"""
        $ErrorActionPreference = "Stop"
        $ExePath = "{exe}"
        $WorkDir = "{target}"
        $ShortcutPaths = @(
            (Join-Path ([Environment]::GetFolderPath("Desktop")) "{SHORTCUT_NAME}"),
            (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\\{SHORTCUT_NAME}")
        )
        $WScript = New-Object -ComObject WScript.Shell
        foreach ($ShortcutPath in $ShortcutPaths) {{
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ShortcutPath) | Out-Null
            $Shortcut = $WScript.CreateShortcut($ShortcutPath)
            $Shortcut.TargetPath = $ExePath
            $Shortcut.WorkingDirectory = $WorkDir
            $Shortcut.IconLocation = "$ExePath,0"
            $Shortcut.Save()
        }}
        """
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        check=True,
    )


def main() -> int:
    target = install_dir()
    print(f"Installing Reagent Approval Bot to: {target}")
    with tempfile.TemporaryDirectory(prefix="ReagentApprovalBotInstall_") as tmp:
        temp_root = Path(tmp)
        extracted = temp_root / "payload"
        backup = temp_root / "backup"
        extracted.mkdir(parents=True, exist_ok=True)
        backup.mkdir(parents=True, exist_ok=True)

        if target.exists():
            print("Preserving existing settings and runtime data...")
            preserve_existing(target, backup)
            shutil.rmtree(target)

        print("Extracting application files...")
        with zipfile.ZipFile(payload_zip()) as zf:
            zf.extractall(extracted)

        target.mkdir(parents=True, exist_ok=True)
        for item in extracted.iterdir():
            destination = target / item.name
            if item.is_dir():
                shutil.copytree(item, destination)
            else:
                shutil.copy2(item, destination)

        restore_existing(target, backup)
        write_uninstaller(target)
        create_shortcuts(target)

    print("Installation complete.")
    print("Open Reagent Approval Bot from the desktop shortcut or Start Menu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
