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
import urllib.request
import zipfile
import ctypes
import hashlib
import json
import re
from queue import Empty, Queue
from typing import Any

try:
    from secure_update import ManifestAsset, atomic_json, rename_staged_path, replace_staged_directory, safe_extract_zip, sha256_file, stage_version, switch_current, verified_download
except ImportError:  # The legacy installer remains usable when built without the secure updater module.
    ManifestAsset = atomic_json = rename_staged_path = replace_staged_directory = safe_extract_zip = sha256_file = stage_version = switch_current = verified_download = None


APP_NAME = "ReagentApprovalBot"
INSTALL_LOG_NAME = "reagent_approval_bot_install.log"
INSTALL_REGISTRY_KEY = r"Software\ReagentApprovalBot"
UNINSTALL_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ReagentApprovalBot"
LAUNCHER_NAME = "ReagentApprovalBot.exe"


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
        ).strip().lower() not in {"1", "true", "yes", "on"} and not install_flag("REAGENT_APPROVAL_SILENT_INSTALL")
        self.root: Any | None = None
        self.message_var: Any | None = None
        self.detail_var: Any | None = None
        self.progress: Any | None = None
        self.window_error: Exception | None = None
        self._determinate = False
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
        except Exception as exc:
            self.enabled = False
            self.root = None
            self.window_error = exc

    def update(self, message: str, detail: str = "", percent: float | None = None) -> None:
        if not self.root:
            return
        if threading.get_ident() != self._main_thread_id:
            self._events.put(("update", (message, detail, percent)))
            return
        self._apply_update(message, detail, percent)

    def _apply_update(self, message: str, detail: str, percent: float | None) -> None:
        try:
            self.message_var.set(message)
            self.detail_var.set(detail)
            if (percent is not None) != self._determinate:
                self.progress.stop()
                self._determinate = percent is not None
                self.progress.configure(mode="determinate" if self._determinate else "indeterminate", value=0)
                if not self._determinate:
                    self.progress.start(12)
            if percent is not None:
                self.progress.configure(value=percent)
            self.root.update_idletasks()
        except Exception:
            # Do not exit the event loop while the worker is modifying files.
            pass

    def download_progress(self, received: int, total: int) -> None:
        percent = 100 * received / total
        self.update("2/5 获取浏览器", f"{received / 1048576:.1f} / {total / 1048576:.1f} MiB ({percent:.0f}%)", percent)

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
                self._apply_update(*payload)
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


def secure_payload_dir() -> Path | None:
    path = bundled_root() / "payload"
    return path if (path / "release-manifest.json").is_file() else None


def install_dir() -> Path:
    override = os.getenv("REAGENT_APPROVAL_INSTALL_DIR")
    if override:
        return Path(override).expanduser()
    candidates = existing_installs()
    if len(candidates) > 1:
        raise ValueError("检测到多个安装，请使用交互安装器选择原目录。")
    return candidates[0] if candidates else default_install_dir()


def default_install_dir() -> Path:
    return Path(os.getenv("LOCALAPPDATA") or str(Path.home())) / "Programs" / APP_NAME


def runtime_data_dir() -> Path:
    configured = os.getenv("REAGENT_APPROVAL_RUNTIME_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    return Path.home() / APP_NAME


def should_prompt_for_install_dir() -> bool:
    return (os.name == "nt" and not os.getenv("REAGENT_APPROVAL_INSTALL_DIR")
            and not install_flag("REAGENT_APPROVAL_START_AFTER_INSTALL")
            and not install_flag("REAGENT_APPROVAL_SILENT_INSTALL"))


def install_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def registered_install() -> Path | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INSTALL_REGISTRY_KEY) as key:
            value, kind = winreg.QueryValueEx(key, "InstallLocation")
        return Path(value) if kind == winreg.REG_SZ and value else None
    except OSError:
        return None


def record_install(target: Path) -> None:
    if os.name != "nt":
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, INSTALL_REGISTRY_KEY) as key:
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(target))


def register_uninstall(target: Path) -> None:
    if os.name != "nt":
        return
    import winreg

    uninstaller = target / "uninstall_installed.ps1"
    if not uninstaller.is_file():
        raise FileNotFoundError(uninstaller)
    pointer = target / "current.json"
    if pointer.is_file():
        version = json.loads(pointer.read_text(encoding="utf-8"))["version"]
    else:
        version = (target / "_internal" / "app" / "VERSION").read_text(encoding="utf-8").strip()
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = subprocess.list2cmdline([str(powershell), "-NoProfile", "-ExecutionPolicy", "RemoteSigned",
                                       "-File", str(uninstaller)])
    values = {"DisplayName": "试剂审批助手", "DisplayVersion": version, "InstallLocation": str(target),
              "DisplayIcon": f"{target / LAUNCHER_NAME},0", "UninstallString": command}
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))
        for name in ("NoModify", "NoRepair"):
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, 1)


def is_product_install(target: Path) -> bool:
    try:
        with (target / LAUNCHER_NAME).open("rb") as launcher:
            if launcher.read(2) != b"MZ":
                return False
        # Install metadata also identifies an installation with a damaged active version.
        for filename, field in (("install.json", "schema"), ("current.json", "path")):
            marker = target / filename
            if not marker.is_file():
                continue
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            version = str(data.get("version", ""))
            if re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", version) and (
                (field == "schema" and data.get(field) == 2) or data.get(field) == f"versions/{version}"
            ):
                return True
        legacy = target / "_internal" / "app"
        return (legacy / "VERSION").is_file() and (legacy / "src" / "templates").is_dir()
    except (OSError, ValueError, AttributeError):
        return False


def existing_installs() -> list[Path]:
    candidates = [registered_install(), default_install_dir()]
    if os.name == "nt":
        for folder in ("DesktopDirectory", "Programs"):
            try:
                shortcut = shortcut_command(folder)
                if shortcut and Path(shortcut).name.casefold() == LAUNCHER_NAME.casefold():
                    candidates.append(Path(shortcut).parent)
            except (OSError, subprocess.SubprocessError):
                continue
    return list(dict.fromkeys(path.resolve() for path in candidates if path and path.is_absolute() and is_product_install(path)))


def validate_install_target(path: Path, runtime: Path, existing: list[Path]) -> Path:
    if not path.is_absolute():
        raise ValueError("请选择本机磁盘上的完整安装路径。")
    path = path.resolve()
    if path == Path(path.anchor) or (os.name == "nt" and ctypes.windll.kernel32.GetDriveTypeW(path.anchor) != 3):
        raise ValueError("请选择本机固定磁盘的子目录，不能使用磁盘根目录。")
    runtime = runtime.resolve()
    protected = [Path(value).resolve() for key in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
                 if (value := os.getenv(key))]
    if path == runtime or path.is_relative_to(runtime) or runtime.is_relative_to(path) or any(
        path.is_relative_to(folder) or folder.is_relative_to(path) for folder in protected
    ):
        raise ValueError("不能使用系统目录、运行数据目录或其上级作为安装目录。")
    if path.exists() and (not path.is_dir() or (any(path.iterdir()) and not is_product_install(path))):
        raise ValueError("安装目录必须为空，或为可识别的试剂审批助手原安装目录。")
    try:
        marker = json.loads((path / "install.json").read_text(encoding="utf-8"))
        recorded_runtime = marker.get("runtime") if isinstance(marker, dict) else None
    except (OSError, ValueError):
        recorded_runtime = None
    if isinstance(recorded_runtime, str) and recorded_runtime and Path(recorded_runtime).resolve() != runtime:
        raise ValueError("此安装使用其他运行数据目录。请以原 Windows 用户和原数据目录运行修复，或选择当前用户的专用新装目录。")
    if existing and path not in existing and not is_product_install(path):
        raise ValueError("已检测到安装，请选用原安装目录；本版不迁移到新目录。")
    return path


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
        if item.name not in {LAUNCHER_NAME, "_internal", "README-WINDOWS.txt", "uninstall_installed.ps1"}:
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()


def write_uninstaller(target: Path) -> None:
    script = target / "uninstall_installed.ps1"
    install_path = str(target).replace("'", "''")
    script.write_text(
        textwrap.dedent(
            fr"""
            param([switch]$KeepData, [switch]$Quiet)
            $ErrorActionPreference = "Stop"
            $InstallDir = '{install_path}'
            $Record = 'HKCU:\{INSTALL_REGISTRY_KEY}'
            $AppEntry = 'HKCU:\{UNINSTALL_REGISTRY_KEY}'
            $Log = Join-Path $env:TEMP 'reagent_approval_bot_uninstall.log'
            function Write-UninstallLog($Message) {{ Add-Content -LiteralPath $Log -Value "$(Get-Date -Format o) $Message" -Encoding UTF8 }}
            trap {{
                $Message = $_.Exception.Message
                try {{ Write-UninstallLog "Failed: $Message" }} catch {{}}
                if (!$Quiet) {{
                    try {{ [void](New-Object -ComObject WScript.Shell).Popup("$Message`n`nLog: $Log", 0, 'Reagent Approval Bot', 16) }} catch {{}}
                }}
                [Console]::Error.WriteLine($Message)
                exit 1
            }}
            $Recorded = (Get-ItemProperty -LiteralPath $Record -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
            if ($InstallDir -ne $PSScriptRoot -or (Resolve-Path -LiteralPath $InstallDir).Path -ne $InstallDir -or
                ((Get-Item -LiteralPath $InstallDir -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -or
                ($Recorded -ne $InstallDir -and !(Test-Path -LiteralPath (Join-Path $InstallDir '{LAUNCHER_NAME}')))) {{
                throw 'Uninstaller must be run from the original product directory.'
            }}
            $ParentDir = Split-Path -Parent $InstallDir
            Set-Location -LiteralPath $ParentDir
            [Environment]::CurrentDirectory = $ParentDir
            $Prefix = $InstallDir.TrimEnd('\') + '\'
            $ShortcutName = -join ([char[]](0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
            $UninstallShortcutName = -join ([char[]](0x5378, 0x8f7d, 0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
            $Running = @(Get-CimInstance Win32_Process | Where-Object {{
                $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)
            }})
            if ($Running.Count) {{ throw 'Close Reagent Approval Bot and wait for active tasks to finish, then try uninstalling again.' }}
            $Shell = New-Object -ComObject WScript.Shell
            if (!$Quiet -and $Shell.Popup('Uninstall Reagent Approval Bot? Runtime data will be kept.', 0, 'Reagent Approval Bot', 36) -ne 6) {{ exit 0 }}
            Write-UninstallLog "Uninstalling $InstallDir (keep runtime data)"
            foreach ($Name in @('versions', 'shared', '_internal', '{LAUNCHER_NAME}', 'ReagentApprovalBotUpdater.exe',
                                'current.json', 'previous.json', 'browser-current.json', 'install.json',
                                'README-WINDOWS.txt')) {{
                $Item = Join-Path $InstallDir $Name
                if (Test-Path -LiteralPath $Item) {{
                    $Entry = Get-Item -LiteralPath $Item -Force
                    $Resolved = (Resolve-Path -LiteralPath $Item).Path
                    if (!$Resolved.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase) -or
                        ($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {{ throw "Unexpected link in install directory: $Item" }}
                    if ($Entry.PSIsContainer) {{
                        if (Get-ChildItem -LiteralPath $Item -Recurse -Force -Attributes ReparsePoint | Select-Object -First 1) {{
                            throw "Unexpected link under $Item"
                        }}
                        Remove-Item -LiteralPath $Item -Recurse -Force
                    }} else {{ Remove-Item -LiteralPath $Item -Force }}
                }}
            }}
            foreach ($Folder in @('DesktopDirectory', 'Programs')) {{
                $Directory = [Environment]::GetFolderPath([Environment+SpecialFolder]$Folder)
                if (!$Directory) {{ continue }}
                foreach ($Name in @($ShortcutName, $UninstallShortcutName)) {{
                    $Link = Join-Path $Directory "$Name.lnk"
                    if (Test-Path -LiteralPath $Link -PathType Leaf) {{
                        $Shortcut = $Shell.CreateShortcut($Link)
                        if ($Shortcut.WorkingDirectory -eq $InstallDir) {{
                            Remove-Item -LiteralPath $Link -Force
                        }}
                    }}
                }}
            }}
            $EntryLocation = (Get-ItemProperty -LiteralPath $AppEntry -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
            if ($EntryLocation -eq $InstallDir) {{
                Remove-Item -LiteralPath $AppEntry
            }}
            if ($Recorded -eq $InstallDir) {{
                Remove-ItemProperty -LiteralPath $Record -Name InstallLocation
                if (!(Get-Item -LiteralPath $Record).Property -and !(Get-ChildItem -LiteralPath $Record)) {{
                    Remove-Item -LiteralPath $Record
                }}
            }}
            Remove-Item -LiteralPath (Join-Path $InstallDir 'uninstall_installed.ps1') -Force
            if (!(Get-ChildItem -LiteralPath $InstallDir -Force | Select-Object -First 1)) {{
                Remove-Item -LiteralPath $InstallDir -Force
            }}
            Write-UninstallLog 'Uninstall complete; runtime data preserved.'
            if (!$Quiet) {{ [void]$Shell.Popup('Uninstall complete. Runtime data was kept.', 0, 'Reagent Approval Bot', 64) }}
            """
        ).strip()
        + "\n",
        encoding="utf-8-sig",
    )


def shortcut_command(folder: str, target: Path | None = None, *, uninstall: bool = False) -> str:
    ps = r"""
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $directory = [Environment]::GetFolderPath([Environment+SpecialFolder]$env:RAB_SHORTCUT_FOLDER)
    if (!$directory) { throw 'Windows known folder is unavailable.' }
    $name = -join ([char[]](0x8bd5, 0x5242, 0x5ba1, 0x6279, 0x52a9, 0x624b))
    if ($env:RAB_UNINSTALL_LINK -eq '1') { $name = (-join ([char[]](0x5378, 0x8f7d))) + $name }
    $link = Join-Path $directory "$name.lnk"
    $shell = New-Object -ComObject WScript.Shell
    if (!$env:RAB_SHORTCUT_ROOT) {
        if (Test-Path -LiteralPath $link -PathType Leaf) { $shell.CreateShortcut($link).TargetPath }
        exit
    }
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $shortcut = $shell.CreateShortcut($link)
    $exe = Join-Path $env:RAB_SHORTCUT_ROOT 'ReagentApprovalBot.exe'
    if ($env:RAB_UNINSTALL_LINK -eq '1') {
        $exe = Join-Path $PSHOME 'powershell.exe'
        $shortcut.Arguments = '-NoProfile -ExecutionPolicy RemoteSigned -File "' + (Join-Path $env:RAB_SHORTCUT_ROOT 'uninstall_installed.ps1') + '"'
    } else {
        $shortcut.Arguments = ''
    }
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = $env:RAB_SHORTCUT_ROOT
    $shortcut.IconLocation = "$exe,0"
    $shortcut.Save()
    if (!(Test-Path -LiteralPath $link -PathType Leaf) -or $shell.CreateShortcut($link).TargetPath -ne $exe) {
        throw 'Shortcut target verification failed.'
    }
    $link
    """
    env = {**os.environ, "RAB_SHORTCUT_FOLDER": folder, "RAB_SHORTCUT_ROOT": str(target) if target else "",
           "RAB_UNINSTALL_LINK": "1" if uninstall else "0"}
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
        capture_output=True, encoding="utf-8", errors="replace", timeout=15, **hidden_subprocess_kwargs(),
    )
    if result.returncode:
        raise OSError(result.stderr.strip() or f"PowerShell exit code {result.returncode}")
    return result.stdout.strip()


def create_shortcuts(target: Path, desktop: bool, log_path: Path) -> dict[str, str | None]:
    outcomes: dict[str, str | None] = {}
    entries = [("开始菜单", "Programs", False)]
    if (target / "uninstall_installed.ps1").is_file():
        entries.append(("卸载入口", "Programs", True))
    if desktop:
        entries.insert(0, ("桌面", "DesktopDirectory", False))
    else:
        outcomes["桌面"] = None
    for label, folder, uninstall in entries:
        try:
            link = shortcut_command(folder, target, uninstall=uninstall)
            outcomes[label] = ""
            write_install_log(log_path, f"Shortcut {label}: {link}")
        except (OSError, subprocess.SubprocessError) as exc:
            outcomes[label] = str(exc)
            write_install_log(log_path, f"Shortcut {label} failed: {exc}")
    return outcomes


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
    with tempfile.NamedTemporaryFile(prefix=".install-write-", dir=target):
        pass


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


def perform_install(progress: ProgressReporter, state: dict[str, Any]) -> Path:
    progress.update("1/5 准备安装")
    runtime = runtime_data_dir()
    target = validate_install_target(state.get("target") or install_dir(), runtime, existing_installs())
    log_path = state["log_path"]
    write_install_log(log_path, f"Installing Reagent Approval Bot to: {target}")
    write_install_log(log_path, f"Runtime data directory: {runtime}")

    progress.update("1/5 准备安装", "等待原程序退出")
    waited_pid = wait_for_requested_previous_process()
    if waited_pid is not None:
        write_install_log(log_path, f"Waited for previous process pid={waited_pid}")

    progress.update("1/5 准备安装", str(target))
    running = running_app_processes(target)
    if running:
        details = "; ".join(f"pid={pid} {path}" for pid, path in running[:5])
        raise RuntimeError(f"Reagent Approval Bot is still running. Close it and run installer again. {details}")

    ensure_install_dir_writable(target)

    secure_payload = secure_payload_dir()
    if secure_payload is not None:
        if is_product_install(target):
            perform_versioned_install(progress, target, runtime, log_path, secure_payload)
        else:
            with tempfile.TemporaryDirectory(prefix=".s-", dir=target) as staging:
                staged = Path(staging)
                perform_versioned_install(progress, staged, runtime, log_path, secure_payload, display_target=target)
                publish_fresh_install(staged, target)
        finish_install(progress, state, target)
        return target

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

    finish_install(progress, state, target)
    return target


def finish_install(progress: ProgressReporter, state: dict[str, Any], target: Path) -> None:
    progress.update("5/5 创建入口", str(target))
    state["outcomes"] = repair_install_entries(target, state.get("desktop", True), state["log_path"])
    write_install_log(state["log_path"], "Installation complete.")


def publish_fresh_install(staged: Path, target: Path) -> None:
    moved = []
    try:
        for entry in sorted(staged.iterdir(), key=lambda entry: entry.name == "current.json"):
            destination = target / entry.name
            if destination.exists():
                raise FileExistsError(destination)
            rename_staged_path(entry, destination)
            moved.append((destination, entry))
    except OSError:
        for destination, entry in reversed(moved):
            rename_staged_path(destination, entry)
        raise


def repair_install_entries(target: Path, desktop: bool, log_path: Path) -> dict[str, str | None]:
    warnings: dict[str, str | None] = {}
    for label, operation in (("安装记录", record_install), ("卸载脚本", write_uninstaller), ("Windows 应用列表", register_uninstall)):
        try:
            operation(target)
        except (OSError, ValueError, KeyError) as exc:
            warnings[label] = str(exc)
            write_install_log(log_path, f"{label} failed: {exc}")
    return {**create_shortcuts(target, desktop, log_path), **warnings}


def perform_versioned_install(
    progress: ProgressReporter,
    target: Path,
    runtime: Path,
    log_path: Path,
    payload: Path,
    *,
    display_target: Path | None = None,
) -> Path:
    if not all((ManifestAsset, atomic_json, replace_staged_directory, safe_extract_zip, sha256_file, stage_version, switch_current, verified_download)):
        raise RuntimeError("Secure installer payload is incomplete.")
    manifest = json.loads((payload / "release-manifest.json").read_text(encoding="utf-8"))
    version = str(manifest.get("release_version") or "")
    assets = {str(item.get("kind") or ""): item for item in manifest.get("assets") or []}
    core = assets.get("core")
    browser = assets.get("browser")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", version) or not core or not browser:
        raise RuntimeError("Secure installer manifest is missing core or browser assets.")
    revision = str(browser.get("browser_revision") or "")
    core_name = f"ReagentApprovalBot-core-{version}-windows-amd64.zip"
    browser_name = f"ReagentApprovalBot-browser-chromium-headless-{revision}-windows-amd64.zip"
    if not re.fullmatch(r"[0-9]+", revision) or core.get("name") != core_name or browser.get("name") != browser_name:
        raise RuntimeError("Secure installer asset names do not match the manifest.")
    core_path = payload / core_name
    browser_dir = target / "shared" / "browsers" / revision
    if any(not (target / relative).resolve().is_relative_to(target.resolve())
           for relative in ("versions", "shared/browsers", f"shared/browsers/{revision}")):
        raise ValueError("安装目录中的版本或浏览器路径指向了安装目录外部。")
    browser_path = payload / browser_name
    if browser_path.is_file():
        progress.update("2/5 获取浏览器", "使用内置浏览器文件")
    else:
        browser_path = download_browser_package(browser, runtime, version, progress)
    progress.update("3/5 校验", "核对核心包、浏览器包的大小和 SHA-256")
    for asset, path in ((core, core_path), (browser, browser_path)):
        if (asset.get("arch") != "amd64" or not path.is_file() or path.stat().st_size != asset.get("size")
                or sha256_file(path).lower() != str(asset.get("sha256") or "").lower()):
            raise RuntimeError(f"Installer payload verification failed: {path.name}")

    if target.exists():
        progress.update("4/5 解压安装", "保留并检查已有数据")
        migrate_legacy_data(target, runtime, log_path)
    target.mkdir(parents=True, exist_ok=True)
    bootstrap = payload / "ReagentApprovalBotBootstrap.exe"
    updater = payload / "ReagentApprovalBotUpdater.exe"
    if not bootstrap.is_file() or not updater.is_file():
        raise RuntimeError("Secure installer is missing bootstrap or updater executable.")
    progress.update("4/5 解压安装", str(display_target or target))
    browser_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".b-", dir=target) as staging:
        extracted = Path(staging)
        safe_extract_zip(browser_path, extracted)
        if not any(item.is_file() for item in extracted.glob(f"chromium_headless_shell-{revision}/**/chrome-headless-shell.exe")):
            raise RuntimeError("Browser archive does not contain the expected Chromium headless executable.")
        replace_staged_directory(extracted, browser_dir)
    stage_version(core_path, target, version, repair=True)
    shutil.copy2(bootstrap, target / "ReagentApprovalBot.exe")
    shutil.copy2(updater, target / "ReagentApprovalBotUpdater.exe")
    atomic_json(target / "browser-current.json", {"revision": revision, "path": f"shared/browsers/{revision}"})
    atomic_json(target / "install.json", {"schema": 2, "version": version, "runtime": str(runtime)})
    switch_current(target, version, revision)
    return target


def download_browser_package(asset: dict[str, Any], runtime: Path, version: str, progress: ProgressReporter) -> Path:
    """Use an adjacent offline asset first; otherwise fetch the exact stable Release asset."""
    name = str(asset.get("name") or "")
    sibling = Path(sys.executable).resolve().parent / name
    if sibling.is_file():
        progress.update("2/5 获取浏览器", f"使用本地文件：{name}")
        return sibling
    progress.update("2/5 获取浏览器", "正在连接 GitHub")
    repository = os.getenv("REAGENT_APPROVAL_GITHUB_REPO", "Jacky0725/erp_approval_bot")
    url = f"https://github.com/{repository}/releases/download/v{version}/{name}"
    manifest_asset = ManifestAsset(
        name=name,
        kind="browser",
        arch="amd64",
        size=int(asset.get("size") or 0),
        sha256=str(asset.get("sha256") or ""),
        url=url,
        browser_revision=str(asset.get("browser_revision") or ""),
    )
    return verified_download(manifest_asset, runtime / "data" / "updates", timeout_seconds=600,
                             progress=progress.download_progress)


def choose_install_options(existing: list[Path]) -> tuple[Path, bool] | None:
    import tkinter as tk
    from tkinter import filedialog, ttk

    root = tk.Tk()
    root.title(f"{APP_TITLE} - 安装向导")
    root.minsize(540, 0)
    frame = ttk.Frame(root, padding=20)
    frame.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    location = tk.StringVar(value=str(existing[0] if len(existing) == 1 else default_install_dir() if not existing else ""))
    desktop = tk.BooleanVar(value=True)
    message = tk.StringVar()
    selected: tuple[Path, bool] | None = None

    def browse() -> None:
        directory = filedialog.askdirectory(parent=root, title="选择完整的安装目录")
        if directory:
            location.set(directory)

    def accept() -> None:
        nonlocal selected
        try:
            target = validate_install_target(Path(location.get().strip()).expanduser(), runtime_data_dir(), existing)
        except (OSError, ValueError) as exc:
            message.set(str(exc))
            return
        selected = (target, desktop.get())
        root.destroy()

    ttk.Label(frame, text="安装目录（完整路径）").grid(row=0, column=0, sticky="w")
    ttk.Combobox(frame, textvariable=location, values=[str(path) for path in existing], width=55).grid(
        row=1, column=0, sticky="ew", pady=6)
    ttk.Button(frame, text="浏览…", command=browse).grid(row=1, column=1, padx=(8, 0))
    ttk.Checkbutton(frame, text="创建桌面快捷方式", variable=desktop).grid(row=2, column=0, sticky="w")
    ttk.Label(frame, textvariable=message, wraplength=475).grid(row=3, columnspan=2, sticky="w", pady=8)
    if existing:
        message.set("检测到已有安装。请选择原目录进行升级或修复。")
    buttons = ttk.Frame(frame)
    buttons.grid(row=4, columnspan=2, sticky="e")
    ttk.Button(buttons, text="取消", command=root.destroy).pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="安装 / 修复", command=accept).pack(side="left")
    root.mainloop()
    return selected


def outcome_text(outcomes: dict[str, str | None]) -> str:
    return "\n".join(f"{name}：" + ("已跳过" if error is None else "已创建" if not error else f"未创建：{error.splitlines()[0]}")
                     for name, error in outcomes.items())


def show_completion(target: Path, state: dict[str, Any], *, interactive: bool) -> None:
    if install_flag("REAGENT_APPROVAL_SUPPRESS_INSTALL_MESSAGE") or install_flag("REAGENT_APPROVAL_SILENT_INSTALL"):
        return
    outcomes = state.get("outcomes", {})
    if not interactive:
        show_message(APP_TITLE, f"程序已安装\n{target}\n\n{outcome_text(outcomes)}\n\n日志：{state['log_path']}")
        return
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(f"{APP_TITLE} - {INSTALL_DONE_TITLE}")
    frame = ttk.Frame(root, padding=20)
    frame.pack(fill="both", expand=True)
    status = tk.StringVar()
    ttk.Label(frame, text=f"程序已安装：{target}", wraplength=500).pack(anchor="w")
    ttk.Label(frame, textvariable=status, wraplength=500).pack(anchor="w", pady=8)
    ttk.Label(frame, text=f"日志：{state['log_path']}", wraplength=500).pack(anchor="w")
    buttons = ttk.Frame(frame)
    buttons.pack(anchor="e", pady=(12, 0))

    def render(results: dict[str, str | None]) -> None:
        status.set(outcome_text(results))
        retry.configure(state="normal" if any(results.values()) else "disabled")

    def retry_entries() -> None:
        retry.configure(state="disabled")
        root.update_idletasks()
        render(repair_install_entries(target, state.get("desktop", True), state["log_path"]))

    retry = ttk.Button(buttons, text="重试创建入口", command=retry_entries)
    retry.pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="完成", command=root.destroy).pack(side="left")
    render(outcomes)
    root.mainloop()


def main() -> int:
    state: dict[str, Any] = {"log_path": Path(os.getenv("TEMP", str(Path.home()))) / INSTALL_LOG_NAME}
    progress = None
    interactive = should_prompt_for_install_dir()
    try:
        if interactive:
            options = choose_install_options(existing_installs())
            if options is None:
                return 0
            state["target"], state["desktop"] = options
        progress = ProgressReporter()
        if progress.window_error:
            raise RuntimeError(f"安装进度窗口无法初始化：{progress.window_error}")
        target = progress.run(lambda: perform_install(progress, state))
        progress.close()
        try:
            show_completion(target, state, interactive=interactive)
        except Exception as exc:
            write_install_log(state["log_path"], f"Completion window failed: {exc}")
            show_completion(target, state, interactive=False)
        return 0
    except Exception as exc:  # noqa: BLE001 - show install failures to desktop users
        if progress:
            progress.close()
        log_path = state["log_path"]
        write_install_log(log_path, f"Installation failed: {exc}")
        if not install_flag("REAGENT_APPROVAL_SILENT_INSTALL"):
            show_message(APP_TITLE, f"{INSTALL_FAILED_TITLE}\n{exc}\n\nLog: {log_path}", error=True)
        return 1
    finally:
        if progress:
            progress.close()


if __name__ == "__main__":
    raise SystemExit(main())
