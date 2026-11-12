import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
from unittest.mock import patch
import zipfile

import pytest


def load_installer_module():
    path = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "reagent_approval_bot_installer.py"
    spec = importlib.util.spec_from_file_location("reagent_approval_bot_installer", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_install_dir_uses_explicit_override(tmp_path, monkeypatch):
    installer = load_installer_module()
    target = tmp_path / "custom"
    monkeypatch.setenv("REAGENT_APPROVAL_INSTALL_DIR", str(target))

    assert installer.install_dir() == target.resolve()


def test_online_update_install_does_not_prompt_for_folder(monkeypatch):
    installer = load_installer_module()
    monkeypatch.setenv("REAGENT_APPROVAL_START_AFTER_INSTALL", "1")

    assert not installer.should_prompt_for_install_dir()


def test_manual_windows_install_prompts_by_default(monkeypatch):
    installer = load_installer_module()
    monkeypatch.delenv("REAGENT_APPROVAL_START_AFTER_INSTALL", raising=False)
    monkeypatch.delenv("REAGENT_APPROVAL_SILENT_INSTALL", raising=False)
    monkeypatch.delenv("REAGENT_APPROVAL_PROMPT_INSTALL_DIR", raising=False)
    monkeypatch.delenv("REAGENT_APPROVAL_INSTALL_DIR", raising=False)

    with patch.object(installer.os, "name", "nt"):
        assert installer.should_prompt_for_install_dir()


@pytest.mark.parametrize("flag", ["REAGENT_APPROVAL_SILENT_INSTALL", "REAGENT_APPROVAL_INSTALL_DIR"])
def test_override_and_silent_install_skip_wizard(monkeypatch, flag):
    installer = load_installer_module()
    monkeypatch.delenv("REAGENT_APPROVAL_START_AFTER_INSTALL", raising=False)
    monkeypatch.delenv("REAGENT_APPROVAL_SILENT_INSTALL", raising=False)
    monkeypatch.setenv(flag, "1")

    with patch.object(installer.os, "name", "nt"):
        assert not installer.should_prompt_for_install_dir()


def test_cancel_before_install_does_not_write(tmp_path, monkeypatch):
    installer = load_installer_module()
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(installer, "should_prompt_for_install_dir", lambda: True)
    monkeypatch.setattr(installer, "existing_installs", list)
    monkeypatch.setattr(installer, "choose_install_options", lambda _: None)
    assert installer.main() == 0
    assert not list(tmp_path.iterdir())


def test_target_validation_separates_data_and_refuses_move(tmp_path):
    installer = load_installer_module()
    runtime = tmp_path / "runtime"
    target = tmp_path / "试剂 审批"
    assert installer.validate_install_target(target, runtime, []) == target
    for invalid in (Path(tmp_path.anchor), runtime, runtime / "child", tmp_path):
        with pytest.raises(ValueError):
            installer.validate_install_target(invalid, runtime, [])
    with pytest.raises(ValueError, match="原安装目录"):
        installer.validate_install_target(target, runtime, [tmp_path / "old"])
    target.mkdir()
    (target / "unrelated.txt").write_text("preserve")
    with pytest.raises(ValueError, match="必须为空"):
        installer.validate_install_target(target, runtime, [])


def test_detects_original_install_and_refuses_ambiguous_unattended_repair(tmp_path, monkeypatch):
    installer = load_installer_module()
    previous = tmp_path / "old"
    previous.mkdir()
    (previous / installer.LAUNCHER_NAME).write_bytes(b"MZ")
    (previous / "install.json").write_text('{"schema":2,"version":"0.1.22"}')
    monkeypatch.delenv("REAGENT_APPROVAL_INSTALL_DIR", raising=False)
    monkeypatch.setattr(installer, "registered_install", lambda: previous)
    monkeypatch.setattr(installer, "default_install_dir", lambda: tmp_path / "default")
    monkeypatch.setattr(installer, "shortcut_command", lambda _: "")
    assert installer.install_dir() == previous
    monkeypatch.setattr(installer, "existing_installs", lambda: [previous, tmp_path / "other"])
    with pytest.raises(ValueError, match="多个安装"):
        installer.install_dir()


def test_refuses_repair_of_a_different_runtime_profile(tmp_path):
    installer = load_installer_module()
    installed = tmp_path / "program"
    installed.mkdir()
    (installed / installer.LAUNCHER_NAME).write_bytes(b"MZ")
    (installed / "install.json").write_text(json.dumps({"schema": 2, "version": "0.1.22",
                                                       "runtime": str(tmp_path / "other-profile")}))
    with pytest.raises(ValueError, match="其他运行数据目录"):
        installer.validate_install_target(installed, tmp_path / "current-profile", [installed])


def test_shortcut_failure_does_not_skip_start_menu_and_can_retry(tmp_path, monkeypatch):
    installer = load_installer_module()
    calls = []

    def command(folder, target, **kwargs):
        calls.append((folder, target))
        if folder == "DesktopDirectory" and len(calls) == 1:
            raise PermissionError("redirected desktop is unavailable")
        return str(tmp_path / "shortcut.lnk")

    monkeypatch.setattr(installer, "shortcut_command", command)
    log = tmp_path / "install.log"
    results = installer.create_shortcuts(tmp_path, True, log)
    assert results == {"桌面": "redirected desktop is unavailable", "开始菜单": ""}
    assert calls[1] == ("Programs", tmp_path)
    assert installer.create_shortcuts(tmp_path, True, log) == {"桌面": "", "开始菜单": ""}
    assert installer.create_shortcuts(tmp_path, False, log)["桌面"] is None


def test_offline_install_checks_hash_and_repairs_original_version(tmp_path, monkeypatch):
    installer = load_installer_module()
    version, revision = "0.1.22", "1254"
    payload, target, runtime = (tmp_path / name for name in ("payload", "自定义 程序", "runtime"))
    payload.mkdir()
    for name in ("ReagentApprovalBotBootstrap.exe", "ReagentApprovalBotUpdater.exe"):
        (payload / name).write_bytes(b"MZbootstrap")
    core = payload / f"ReagentApprovalBot-core-{version}-windows-amd64.zip"
    browser = tmp_path / f"ReagentApprovalBot-browser-chromium-headless-{revision}-windows-amd64.zip"
    for path, entry in ((core, installer.LAUNCHER_NAME), (browser, f"chromium_headless_shell-{revision}/win64/chrome-headless-shell.exe")):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(entry, b"MZverified")
            if path == browser:
                archive.writestr(f"chromium_headless_shell-{revision}/chrome-headless-shell-win64/PrivacySandboxAttestationsPreloaded/manifest.json", "{}")
    manifest = {"release_version": version, "assets": [
        {"kind": kind, "arch": "amd64", "name": path.name, "browser_revision": revision,
         "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for kind, path in (("core", core), ("browser", browser))]}
    (payload / "release-manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(installer.sys, "executable", str(tmp_path / "setup.exe"))
    monkeypatch.setenv("REAGENT_APPROVAL_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("REAGENT_APPROVAL_SUPPRESS_INSTALL_PROGRESS", "1")
    monkeypatch.setattr(installer, "secure_payload_dir", lambda: payload)
    monkeypatch.setattr(installer, "existing_installs", list)
    monkeypatch.setattr(installer, "running_app_processes", lambda _: [])
    monkeypatch.setattr(installer, "repair_install_entries", lambda *args: {})
    state = {"target": target, "log_path": tmp_path / "install.log"}
    progress = installer.ProgressReporter()
    package = browser.read_bytes()
    browser.write_bytes(b"bad archive")
    with pytest.raises(RuntimeError, match="verification failed"):
        installer.perform_install(progress, state)
    assert not list(target.iterdir())
    browser.write_bytes(package)
    assert installer.perform_install(progress, state) == target
    current = target / "current.json"
    pointer = current.read_bytes()
    exe = target / "versions" / version / installer.LAUNCHER_NAME
    exe.write_bytes(b"damaged")
    runtime.mkdir(exist_ok=True)
    (runtime / ".env").write_text("keep=true")
    installer.perform_install(progress, state)
    assert exe.read_bytes() == b"MZverified"
    assert current.read_bytes() == pointer
    assert (runtime / ".env").read_text() == "keep=true"
    browser_dir = target / "shared" / "browsers" / revision
    assert installer.browser_install_complete(browser_dir, revision, manifest["assets"][1]["sha256"])
    monkeypatch.setattr(installer, "download_browser_package", lambda *args: (_ for _ in ()).throw(AssertionError("browser downloaded again")))
    installer.perform_install(progress, state)
    assert installer.browser_install_complete(browser_dir, revision, manifest["assets"][1]["sha256"])


def test_browser_reuse_requires_matching_verified_marker(tmp_path):
    installer = load_installer_module()
    browser_dir = tmp_path / "browser"
    executable = browser_dir / "chromium_headless_shell-1254" / "win64" / "chrome-headless-shell.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZbrowser")
    (browser_dir / ".installed.json").write_text('{"revision":"1254","sha256":"correct"}', encoding="utf-8")
    assert installer.browser_install_complete(browser_dir, "1254", "correct")
    assert not installer.browser_install_complete(browser_dir, "1254", "wrong")
    executable.unlink()
    assert not installer.browser_install_complete(browser_dir, "1254", "correct")


def test_progress_reporter_run_without_ui_executes_worker(monkeypatch):
    installer = load_installer_module()
    monkeypatch.setenv("REAGENT_APPROVAL_SUPPRESS_INSTALL_PROGRESS", "1")
    progress = installer.ProgressReporter()

    assert progress.run(lambda: "done") == "done"


def test_runtime_data_dir_defaults_to_local_app_data(tmp_path, monkeypatch):
    installer = load_installer_module()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.delenv("REAGENT_APPROVAL_RUNTIME_ROOT", raising=False)

    assert installer.runtime_data_dir() == tmp_path / "LocalAppData" / "ReagentApprovalBot"


def test_migrate_legacy_data_copies_to_runtime_without_overwrite(tmp_path):
    installer = load_installer_module()
    target = tmp_path / "Programs" / "ReagentApprovalBot"
    runtime = tmp_path / "LocalAppData" / "ReagentApprovalBot"
    log_path = tmp_path / "install.log"
    (target / "data").mkdir(parents=True)
    (target / "config").mkdir()
    (target / ".env").write_text("ERP_USERNAME=old", encoding="utf-8")
    (target / "data" / "reagent_memory.sqlite").write_bytes(b"db")
    (target / "config" / "settings.yaml").write_text("approval: old", encoding="utf-8")
    (runtime / "config").mkdir(parents=True)
    (runtime / "config" / "settings.yaml").write_text("approval: keep", encoding="utf-8")

    installer.migrate_legacy_data(target, runtime, log_path)

    assert (runtime / ".env").read_text(encoding="utf-8") == "ERP_USERNAME=old"
    assert (runtime / "data" / "reagent_memory.sqlite").read_bytes() == b"db"
    assert (runtime / "config" / "settings.yaml").read_text(encoding="utf-8") == "approval: keep"


def test_remove_program_files_keeps_legacy_data_until_runtime_migration(tmp_path):
    installer = load_installer_module()
    target = tmp_path / "ReagentApprovalBot"
    (target / "_internal").mkdir(parents=True)
    (target / "data").mkdir()
    (target / ".env").write_text("secret", encoding="utf-8")
    (target / "ReagentApprovalBot.exe").write_bytes(b"exe")

    installer.remove_program_files(target)

    assert (target / "data").exists()
    assert (target / ".env").exists()
    assert not (target / "_internal").exists()
    assert not (target / "ReagentApprovalBot.exe").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires the native Windows Shell Link COM API")
def test_native_shell_link_round_trips_unicode_paths_and_arguments(tmp_path):
    installer = load_installer_module()
    target = tmp_path / "\u8bd5\u5242 App"
    target.mkdir()
    exe = target / installer.LAUNCHER_NAME
    exe.write_bytes(b"MZ")
    link = tmp_path / "\u5378\u8f7d\u8bd5\u5242.lnk"
    script = installer.shell_link_powershell() + r"""
    $ErrorActionPreference = 'Stop'
    $argsText = '-File "' + (Join-Path $env:RAB_TEST_ROOT 'uninstall_installed.ps1') + '"'
    [UnicodeShellLink]::Save($env:RAB_TEST_LINK, $env:RAB_TEST_EXE, $env:RAB_TEST_ROOT, $argsText)
    $entry = [UnicodeShellLink]::Read($env:RAB_TEST_LINK)
    if ($entry.TargetPath -cne $env:RAB_TEST_EXE -or $entry.WorkingDirectory -cne $env:RAB_TEST_ROOT -or
        $entry.Arguments -cne $argsText) { throw 'Unicode shortcut round trip failed.' }
    """
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"],
        input=script + "\n", text=True, capture_output=True, timeout=30,
        env={**os.environ, "RAB_TEST_LINK": str(link), "RAB_TEST_ROOT": str(target), "RAB_TEST_EXE": str(exe)},
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr, result.stderr
    assert link.is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell locks its working directory")
def test_uninstaller_can_remove_directory_when_started_from_its_shortcut(tmp_path):
    installer = load_installer_module()
    target = tmp_path / "\u8bd5\u5242 App With Spaces"
    target.mkdir()
    (target / installer.LAUNCHER_NAME).write_bytes(b"MZ")
    (target / "versions").mkdir()
    data = tmp_path / "runtime.txt"
    data.write_text("keep", encoding="utf-8")
    installer.write_uninstaller(target)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File",
         str(target / "uninstall_installed.ps1"), "-Quiet"],
        cwd=target, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not target.exists()
    assert data.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows HKCU and PowerShell")
def test_registered_uninstall_removes_only_matching_test_keys(tmp_path, monkeypatch):
    import winreg

    installer = load_installer_module()
    record = "Software\\RAB-Installer-Test-" + uuid.uuid4().hex
    entry = record + "-Uninstall"
    monkeypatch.setattr(installer, "INSTALL_REGISTRY_KEY", record)
    monkeypatch.setattr(installer, "UNINSTALL_REGISTRY_KEY", entry)
    target = tmp_path / "App With Spaces"
    target.mkdir()
    (target / installer.LAUNCHER_NAME).write_bytes(b"MZ")
    (target / "current.json").write_text('{"version":"0.1.22"}', encoding="utf-8")
    installer.write_uninstaller(target)
    try:
        installer.record_install(target)
        installer.register_uninstall(target)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, entry) as key:
            command, _ = winreg.QueryValueEx(key, "UninstallString")
            assert winreg.QueryValueEx(key, "DisplayVersion")[0] == "0.1.22"
            assert winreg.QueryValueEx(key, "InstallLocation")[0] == str(target)
        result = subprocess.run(command + " -Quiet", cwd=target, capture_output=True, timeout=30,
                                env={**os.environ, "TEMP": str(tmp_path)})
        assert result.returncode == 0, result.stderr
        assert not target.exists()
        assert "runtime data preserved" in (tmp_path / "reagent_approval_bot_uninstall.log").read_text(encoding="utf-8-sig")
        for path in (record, entry):
            with pytest.raises(FileNotFoundError):
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    finally:
        for path in (record, entry):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except FileNotFoundError:
                pass


def test_perform_install_fails_when_app_is_running(tmp_path, monkeypatch):
    installer = load_installer_module()
    monkeypatch.setenv("REAGENT_APPROVAL_SUPPRESS_INSTALL_PROGRESS", "1")
    monkeypatch.setenv("REAGENT_APPROVAL_INSTALL_DIR", str(tmp_path / "install"))
    monkeypatch.setenv("REAGENT_APPROVAL_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(installer, "running_app_processes", lambda target: [(123, str(target / "ReagentApprovalBot.exe"))])

    progress = installer.ProgressReporter()
    try:
        try:
            installer.perform_install(progress, {"log_path": tmp_path / "install.log"})
        except RuntimeError as exc:
            assert "still running" in str(exc)
        else:
            raise AssertionError("perform_install should fail when app is running")
    finally:
        progress.close()


def test_wait_for_requested_previous_process_polls_until_exit(monkeypatch):
    installer = load_installer_module()
    monkeypatch.setenv("REAGENT_APPROVAL_WAIT_FOR_PID", "456")
    states = iter([True, True, False])
    seen: list[int] = []

    def fake_process_exists(pid: int) -> bool:
        seen.append(pid)
        return next(states)

    monkeypatch.setattr(installer, "process_exists", fake_process_exists)
    monkeypatch.setattr(installer.time, "sleep", lambda seconds: None)

    assert installer.wait_for_requested_previous_process(timeout_seconds=5) == 456
    assert seen == [456, 456, 456]
