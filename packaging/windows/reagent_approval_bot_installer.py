from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import textwrap
import time
import zipfile
import ctypes
from queue import Empty, Queue
from typing import Any


APP_NAME = "ReagentApprovalBot"
INSTALL_LOG_NAME = "reagent_approval_bot_install.log"


def ui_text(*codepoints: int) -> str:
    return "".join(chr(value) for value in codepoints)


APP_TITLE = ui_text(0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x81ea, 0x52a8, 0x5316)
INSTALLING_TITLE = ui_text(0x6b63, 0x5728, 0x5b89, 0x88c5)
INSTALL_DONE_TITLE = ui_text(0x5b89, 0x88c5, 0x5b8c, 0x6210)
INSTALL_FAILED_TITLE = ui_text(0x5b89, 0x88c5, 0x5931, 0x8d25)


class ProgressReporter:
    def __init__(self) -> None:
        self.enabled = os.name == "nt" and os.getenv(
            "REAGENT_APPROVAL_SUPPRESS_INSTALL_PROGRESS", ""
        ).strip().lower() not in {"1", "true", "yes", "on"}
        self.root: Any | None = None
        self.message_var: Any | None = None
        self.detail_var: Any | None = None
        self.progress: Any | None = None
        self._main_thread_id = threading.get_ident()
        self._events: Queue[tuple[str, Any]] = Queue()
        if self.enabled:
            self._create_window()

    def _create_window(self) -> None:
        try:
            import tkinter as tk
            from tkinter import ttk

            root = tk.Tk()
            root.title(f"{APP_TITLE} - {INSTALLING_TITLE}")
            root.resizable(False, False)
            root.geometry("460x170")
            root.attributes("-topmost", True)
            root.protocol("WM_DELETE_WINDOW", lambda: None)

            frame = ttk.Frame(root, padding=18)
            frame.pack(fill="both", expand=True)
            self.message_var = tk.StringVar(value=ui_text(0x6b63, 0x5728, 0x51c6, 0x5907, 0x5b89, 0x88c5, 0x2026))
            self.detail_var = tk.StringVar(value="")
            ttk.Label(frame, textvariable=self.message_var, font=("Microsoft YaHei UI", 11, "bold")).pack(
                anchor="w", pady=(0, 8)
            )
            ttk.Label(frame, textvariable=self.detail_var, wraplength=410).pack(anchor="w", pady=(0, 14))
            self.progress = ttk.Progressbar(frame, mode="indeterminate", length=410)
            self.progress.pack(fill="x")
            self.progress.start(12)
            root.update()
            self.root = root
        except Exception:
            self.enabled = False
            self.root = None

    def update(self, message: str, detail: str = "") -> None:
        if not self.root:
            return
        if threading.get_ident() != self._main_thread_id:
            self._events.put(("update", (message, detail)))
            return
        self._apply_update(message, detail)

    def _apply_update(self, message: str, detail: str = "") -> None:
        try:
            self.message_var.set(message)
            self.detail_var.set(detail)
            self.root.update_idletasks()
        except Exception:
            self.close()

    def run(self, worker: Any) -> Any:
        if not self.root:
            return worker()

        result: dict[str, Any] = {}

        def run_worker() -> None:
            try:
                result["value"] = worker()
            except BaseException as exc:  # noqa: BLE001 - surface worker failure after UI loop exits
                result["error"] = exc
            finally:
                self._events.put(("done", None))

        thread = threading.Thread(target=run_worker, name="installer-worker", daemon=True)
        thread.start()
        self._schedule_drain()
        self.root.mainloop()
        thread.join(timeout=1)
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _schedule_drain(self) -> None:
        if not self.root:
            return
        try:
            self.root.after(100, self._drain_events)
        except Exception:
            self.close()

    def _drain_events(self) -> None:
        done = False
        while True:
            try:
                event, payload = self._events.get_nowait()
            except Empty:
                break
            if event == "update":
                message, detail = payload
                self._apply_update(message, detail)
            elif event == "done":
                done = True
        if done:
            try:
                self.root.quit()
            except Exception:
                self.close()
            return
        self._schedule_drain()

    def close(self) -> None:
        if not self.root:
            return
        try:
            if self.progress:
                self.progress.stop()
            self.root.destroy()
        except Exception:
            pass
        finally:
            self.root = None


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


def runtime_data_dir() -> Path:
    configured = os.getenv("REAGENT_APPROVAL_RUNTIME_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    return Path.home() / APP_NAME


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
        $folder = $shell.BrowseForFolder(0, 'Choose install location. The app will be installed into a ReagentApprovalBot folder under the selected directory.', 0, '{default_parent}')
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


def migrate_legacy_data(target: Path, runtime: Path, log_path: Path) -> None:
    runtime.mkdir(parents=True, exist_ok=True)
    for relative in (".env", "data", "config/settings.yaml", "config/name_aliases.yaml"):
        source = target / relative
        destination = runtime / relative
        if not source.exists() or destination.exists():
            continue
        write_install_log(log_path, f"Migrating legacy user data: {relative}")
        copy_if_exists(source, destination)


def remove_program_files(target: Path) -> None:
    if not target.exists():
        return
    for item in target.iterdir():
        if item.name in {"data", ".env"}:
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()


def write_uninstaller(target: Path) -> None:
    script = target / "uninstall_installed.ps1"
    runtime = runtime_data_dir()
    script.write_text(
        textwrap.dedent(
            f"""
            param([switch]$KeepData)
            $ErrorActionPreference = "Stop"
            $InstallDir = "{target}"
            $RuntimeDir = "{runtime}"
            $ShortcutName = -join ([char[]](0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
            $UninstallShortcutName = -join ([char[]](0x5378, 0x8f7d, 0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
            Get-CimInstance Win32_Process | Where-Object {{
                ($_.Name -eq "ReagentApprovalBot.exe") -or
                ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase))
            }} | ForEach-Object {{
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }}
            Start-Sleep -Milliseconds 700
            if (Test-Path $InstallDir) {{
                Remove-Item $InstallDir -Recurse -Force
            }}
            if (!$KeepData -and (Test-Path $RuntimeDir)) {{
                Remove-Item $RuntimeDir -Recurse -Force
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


def running_app_processes(target: Path) -> list[tuple[int, str]]:
    if os.name != "nt":
        return []
    script = textwrap.dedent(
        f"""
        $InstallDir = "{target}"
        Get-CimInstance Win32_Process | Where-Object {{
            ($_.Name -eq "ReagentApprovalBot.exe") -or
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallDir, [StringComparison]::OrdinalIgnoreCase))
        }} | ForEach-Object {{
            "$($_.ProcessId)`t$($_.ExecutablePath)"
        }}
        """
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=False,
        text=True,
        capture_output=True,
        **hidden_subprocess_kwargs(),
    )
    processes: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        pid_text, _, path = line.partition("\t")
        try:
            processes.append((int(pid_text.strip()), path.strip()))
        except ValueError:
            continue
    return processes


def ensure_install_dir_writable(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    probe = target / ".install_write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        probe.unlink(missing_ok=True)


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
            ],
            text=True,
            capture_output=True,
            **hidden_subprocess_kwargs(),
        )
        return result.returncode == 0
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_for_requested_previous_process(timeout_seconds: float = 90.0) -> int | None:
    raw = os.getenv("REAGENT_APPROVAL_WAIT_FOR_PID", "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process_exists(pid):
            return pid
        time.sleep(0.5)
    return pid




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


def perform_install(progress: ProgressReporter, state: dict[str, Path]) -> Path:
    progress.update("Preparing installation...", "If a folder picker opens, choose the install location.")
    target = install_dir()
    runtime = runtime_data_dir()
    log_path = target.parent / INSTALL_LOG_NAME
    state["log_path"] = log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    write_install_log(log_path, f"Installing Reagent Approval Bot to: {target}")
    write_install_log(log_path, f"Runtime data directory: {runtime}")

    progress.update("Waiting for previous application to exit...", "")
    waited_pid = wait_for_requested_previous_process()
    if waited_pid is not None:
        write_install_log(log_path, f"Waited for previous process pid={waited_pid}")

    progress.update("Checking running application...", str(target))
    running = running_app_processes(target)
    if running:
        details = "; ".join(f"pid={pid} {path}" for pid, path in running[:5])
        raise RuntimeError(f"Reagent Approval Bot is still running. Close it and run installer again. {details}")

    progress.update("Checking install directory permissions...", str(target))
    ensure_install_dir_writable(target)

    with tempfile.TemporaryDirectory(prefix="ReagentApprovalBotInstall_") as tmp:
        temp_root = Path(tmp)
        extracted = temp_root / "payload"
        extracted.mkdir(parents=True, exist_ok=True)

        if target.exists():
            progress.update("Migrating legacy local data...", str(runtime))
            migrate_legacy_data(target, runtime, log_path)
            progress.update("Removing old application files...", str(target))
            remove_program_files(target)

        write_install_log(log_path, "Extracting application files...")
        progress.update("Extracting new application files...", "This can take a few minutes during antivirus scanning.")
        with zipfile.ZipFile(payload_zip()) as zf:
            zf.extractall(extracted)

        progress.update("Copying application files...", str(target))
        target.mkdir(parents=True, exist_ok=True)
        for item in extracted.iterdir():
            destination = target / item.name
            if item.is_dir():
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(item, destination)
            else:
                shutil.copy2(item, destination)

        progress.update("Creating shortcuts and uninstall entry...", "")
        write_uninstaller(target)
        create_shortcuts(target)

    write_install_log(log_path, "Installation complete.")
    return target


def main() -> int:
    state = {"log_path": Path(os.getenv("TEMP", str(Path.home()))) / INSTALL_LOG_NAME}
    progress = ProgressReporter()
    try:
        target = progress.run(lambda: perform_install(progress, state))
        progress.close()
        show_message(APP_TITLE, f"{INSTALL_DONE_TITLE}\n{target}")
        return 0
    except Exception as exc:  # noqa: BLE001 - show install failures to desktop users
        progress.close()
        log_path = state["log_path"]
        write_install_log(log_path, f"Installation failed: {exc}")
        show_message(APP_TITLE, f"{INSTALL_FAILED_TITLE}\n{exc}\n\nLog: {log_path}", error=True)
        return 1
    finally:
        progress.close()


if __name__ == "__main__":
    raise SystemExit(main())
