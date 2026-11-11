"""Read-only discovery; a candidate is never permission to launch it."""

from __future__ import annotations

import csv
import os
from pathlib import Path
import subprocess
from typing import Iterator


class WorkBuddyConfigError(ValueError):
    pass


def client_path(value: str) -> Path | None:
    if not value.strip():
        return None
    path = Path(os.path.expandvars(value.strip().strip('"')))
    try:
        if path.is_absolute() and not path.drive.startswith("\\\\") and path.name.casefold() == "workbuddy.exe" and path.is_file():
            return path.resolve(strict=True)
    except (OSError, ValueError):
        pass
    return None


def running_paths() -> Iterator[str]:
    from pywinauto.application import process_module
    from workbuddy_desktop import WorkBuddyDesktop

    current = WorkBuddyDesktop._security_context(os.getpid())
    output = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq WorkBuddy.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="mbcs", errors="replace", timeout=5, check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    for row in csv.reader(output.splitlines()):
        if len(row) < 2 or row[0].casefold() != "workbuddy.exe":
            continue
        try:
            pid = int(row[1])
            identity = WorkBuddyDesktop._security_context(pid)
            if all(identity[key] == current[key] for key in ("user_sid", "session_id")):
                yield process_module(pid)
        except Exception:
            # A process can exit or be inaccessible between enumeration and inspection.
            continue


def registered_paths() -> Iterator[tuple[str, str]]:
    import winreg

    key = r"Software\Microsoft\Windows\CurrentVersion\App Paths\WorkBuddy.exe"
    for hive, label in ((winreg.HKEY_CURRENT_USER, "user_registry"), (winreg.HKEY_LOCAL_MACHINE, "machine_registry")):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as entry:
                    value, kind = winreg.QueryValueEx(entry, "")
                    if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                        yield value, label
            except OSError:
                continue


def discover_clients() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(value: str, source: str) -> None:
        path = client_path(value)
        if path is not None and (key := os.path.normcase(str(path))) not in seen:
            seen.add(key)
            candidates.append({"path": str(path), "source": source})

    try:
        for path in running_paths():
            add(path, "running")
    except (OSError, RuntimeError, ImportError, subprocess.SubprocessError):
        pass
    for path, source in registered_paths():
        add(path, source)
    return candidates


def update_client_settings(settings: dict, form: dict[str, str]) -> None:
    if "workbuddy_executable" not in form:
        return
    config = settings.setdefault("workbuddy", {})
    executable = form["workbuddy_executable"].strip()
    project = form.get("workbuddy_project_name", "").strip()
    enabled = form.get("workbuddy_desktop_enabled") == "true"
    changed = (executable != str(config.get("executable") or "") or
               project != str(config.get("project_name") or "") or
               enabled != bool(config.get("desktop_enabled")))
    if not changed:
        return
    path = client_path(executable)
    if (enabled or (executable and executable != config.get("executable"))) and path is None:
        raise WorkBuddyConfigError("请选择本机存在的 WorkBuddy.exe 完整路径。")
    if enabled and not project:
        raise WorkBuddyConfigError("启用 WorkBuddy 桌面联动前，请填写实际项目名称。")
    config.update(executable=str(path) if path else executable, project_name=project, desktop_enabled=enabled)
