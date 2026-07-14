from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap
import zipfile
import ctypes


APP_NAME = "ReagentApprovalBot"
INSTALL_LOG_NAME = "reagent_approval_bot_install.log"


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
    default_parent = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "Programs"
    if should_prompt_for_install_dir():
        selected_parent = choose_install_parent(default_parent)
        if selected_parent:
            return selected_parent / APP_NAME
    return default_parent / APP_NAME


def should_prompt_for_install_dir() -> bool:
    if os.getenv("REAGENT_APPROVAL_START_AFTER_INSTALL", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if os.getenv("REAGENT_APPROVAL_SILENT_INSTALL", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    return os.name == "nt"


def choose_install_parent(default_parent: Path) -> Path | None:
    default_parent.mkdir(parents=True, exist_ok=True)
    script = textwrap.dedent(
        f"""
        $shell = New-Object -ComObject Shell.Application
        $folder = $shell.BrowseForFolder(0, '请选择安装位置。程序会安装到所选目录下的 ReagentApprovalBot 文件夹。', 0, '{default_parent}')
        if ($folder -ne $null) {{ $folder.Self.Path }}
        """
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        text=True,
        capture_output=True,
        check=False,
        **hidden_subprocess_kwargs(),
    )
    selected = result.stdout.strip()
    return Path(selected).expanduser().resolve() if selected else None


def hidden_subprocess_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


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
            $ShortcutName = -join ([char[]](0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
            $UninstallShortcutName = -join ([char[]](0x5378, 0x8f7d, 0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
            Get-CimInstance Win32_Process | Where-Object {{
                ($_.Name -eq "ReagentApprovalBot.exe") -or
                ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase))
            }} | ForEach-Object {{
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }}
            Start-Sleep -Milliseconds 700
            if ($KeepData) {{
                Get-ChildItem -LiteralPath $InstallDir -Force | Where-Object {{ $_.Name -notin @("data", ".env") }} | Remove-Item -Recurse -Force
            }} elseif (Test-Path $InstallDir) {{
                Remove-Item $InstallDir -Recurse -Force
            }}
            $DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "$ShortcutName.lnk"
            $StartMenuDir = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
            $StartShortcut = Join-Path $StartMenuDir "$ShortcutName.lnk"
            $UninstallStartShortcut = Join-Path $StartMenuDir "$UninstallShortcutName.lnk"
            foreach ($ShortcutPath in @($DesktopShortcut, $StartShortcut, $UninstallStartShortcut)) {{
                Remove-Item $ShortcutPath -Force -ErrorAction SilentlyContinue
            }}
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
        $ShortcutName = -join ([char[]](0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
        $UninstallShortcutName = -join ([char[]](0x5378, 0x8f7d, 0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
        $StartMenuDir = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
        $ShortcutPaths = @(
            (Join-Path ([Environment]::GetFolderPath("Desktop")) "$ShortcutName.lnk"),
            (Join-Path $StartMenuDir "$ShortcutName.lnk")
        )
        $WScript = New-Object -ComObject WScript.Shell
        foreach ($ShortcutPath in $ShortcutPaths) {{
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ShortcutPath) | Out-Null
            $Shortcut = $WScript.CreateShortcut($ShortcutPath)
            $Shortcut.TargetPath = $ExePath
            $Shortcut.WorkingDirectory = $WorkDir
            $Shortcut.IconLocation = "{{0}},0" -f $ExePath
            $Shortcut.Description = "Start Reagent Approval Bot local Web UI"
            $Shortcut.Save()
        }}
        $UninstallShortcutPath = Join-Path $StartMenuDir "$UninstallShortcutName.lnk"
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $UninstallShortcutPath) | Out-Null
        $UninstallShortcut = $WScript.CreateShortcut($UninstallShortcutPath)
        $UninstallShortcut.TargetPath = "powershell.exe"
        $UninstallShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"{target}\\uninstall_installed.ps1`""
        $UninstallShortcut.WorkingDirectory = "{target}"
        $UninstallShortcut.IconLocation = "$env:SystemRoot\\System32\\shell32.dll,31"
        $UninstallShortcut.Description = "Uninstall Reagent Approval Bot"
        $UninstallShortcut.Save()
        """
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        check=True,
        **hidden_subprocess_kwargs(),
    )


def start_installed_app(target: Path) -> None:
    exe = target / "ReagentApprovalBot.exe"
    if not exe.exists():
        return
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [str(exe)],
        cwd=str(target),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )


def wait_for_previous_process() -> None:
    value = os.getenv("REAGENT_APPROVAL_WAIT_FOR_PID", "").strip()
    if not value:
        return
    try:
        pid = int(value)
    except ValueError:
        return
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        wait_for_windows_process_exit(pid)
        return
    wait_for_process_exit_portable(pid)


def stop_existing_app_processes(target: Path) -> None:
    if os.name != "nt":
        return
    script = textwrap.dedent(
        f"""
        $InstallDir = "{target}"
        Get-CimInstance Win32_Process | Where-Object {{
            ($_.Name -eq "ReagentApprovalBot.exe") -or
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase))
        }} | ForEach-Object {{
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }}
        """
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=False,
        **hidden_subprocess_kwargs(),
    )
    time.sleep(0.7)


def wait_for_windows_process_exit(pid: int, timeout_seconds: int = 60) -> None:
    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return
    try:
        remaining_ms = max(1, timeout_seconds) * 1000
        while remaining_ms > 0:
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, min(1000, remaining_ms))
            if result == 0:
                return
            remaining_ms -= 1000
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def wait_for_process_exit_portable(pid: int, timeout_seconds: int = 60) -> None:
    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(1)


def main() -> int:
    log_path = Path(os.getenv("TEMP", str(Path.home()))) / INSTALL_LOG_NAME
    try:
        target = install_dir()
        log_path = target.parent / INSTALL_LOG_NAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        write_install_log(log_path, f"Installing Reagent Approval Bot to: {target}")
        wait_for_previous_process()
        stop_existing_app_processes(target)
        with tempfile.TemporaryDirectory(prefix="ReagentApprovalBotInstall_") as tmp:
            temp_root = Path(tmp)
            extracted = temp_root / "payload"
            backup = temp_root / "backup"
            extracted.mkdir(parents=True, exist_ok=True)
            backup.mkdir(parents=True, exist_ok=True)

            if target.exists():
                write_install_log(log_path, "Preserving existing settings and runtime data...")
                preserve_existing(target, backup)
                shutil.rmtree(target)

            write_install_log(log_path, "Extracting application files...")
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
            if os.getenv("REAGENT_APPROVAL_START_AFTER_INSTALL", "").strip().lower() in {"1", "true", "yes", "on"}:
                start_installed_app(target)

        write_install_log(log_path, "Installation complete.")
        show_message("试剂审批自动化", f"安装完成：\n{target}")
        return 0
    except Exception as exc:  # noqa: BLE001 - show install failures to desktop users
        write_install_log(log_path, f"Installation failed: {exc}")
        show_message("试剂审批自动化安装失败", f"{exc}\n\n日志：{log_path}", error=True)
        return 1


def write_install_log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(message.rstrip() + "\n")


def show_message(title: str, message: str, *, error: bool = False) -> None:
    if os.getenv("REAGENT_APPROVAL_SUPPRESS_INSTALL_MESSAGE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    if os.name == "nt":
        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(None, message, title, flags)
        return
    print(f"{title}: {message}")


if __name__ == "__main__":
    raise SystemExit(main())
