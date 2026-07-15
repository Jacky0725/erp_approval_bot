import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import update_checker
from update_checker import (
    choose_portable_asset,
    choose_setup_asset,
    choose_update_asset,
    check_for_update,
    is_newer_version,
    normalize_tag,
    parse_version,
)


def test_parse_version_ignores_prefixes_and_suffixes():
    assert parse_version("v0.1.4") == (0, 1, 4)
    assert parse_version("release-1.2.3-win-x64") == (1, 2, 3)


def test_is_newer_version_compares_padded_parts():
    assert is_newer_version("0.1.4", "0.1.3")
    assert is_newer_version("0.2", "0.1.9")
    assert not is_newer_version("0.1.3", "0.1.3")
    assert not is_newer_version("0.1.3", "0.1.4")


def test_normalize_tag_removes_v_prefix():
    assert normalize_tag("v0.1.4") == "0.1.4"
    assert normalize_tag("V1.0.0") == "1.0.0"


def test_choose_setup_asset_prefers_windows_x64_setup():
    asset = choose_setup_asset(
        [
            {"name": "reagent-approval-bot-0.1.4-win-x64-portable.zip", "browser_download_url": "zip"},
            {"name": "reagent-approval-bot-0.1.4-win-x64-setup.exe", "browser_download_url": "exe", "size": 123},
        ]
    )
    assert asset is not None
    assert asset.name.endswith("-win-x64-setup.exe")
    assert asset.url == "exe"
    assert asset.size == 123


def test_choose_setup_asset_prefers_lite_setup_over_full_test():
    asset = choose_setup_asset(
        [
            {
                "name": "reagent-approval-bot-0.1.7-win-x64-full-test-setup.exe",
                "browser_download_url": "full",
                "size": 300,
            },
            {
                "name": "reagent-approval-bot-0.1.7-win-x64-lite-setup.exe",
                "browser_download_url": "lite",
                "size": 200,
            },
        ]
    )
    assert asset is not None
    assert asset.name.endswith("-lite-setup.exe")
    assert asset.url == "lite"


def test_choose_portable_asset_prefers_lite_portable():
    asset = choose_portable_asset(
        [
            {
                "name": "reagent-approval-bot-0.1.7-win-x64-full-portable.zip",
                "browser_download_url": "full",
                "size": 300,
            },
            {
                "name": "reagent-approval-bot-0.1.7-win-x64-lite-portable.zip",
                "browser_download_url": "lite",
                "size": 200,
            },
        ]
    )
    assert asset is not None
    assert asset.name.endswith("-lite-portable.zip")
    assert asset.url == "lite"


def test_choose_update_asset_prefers_portable_over_setup():
    asset = choose_update_asset(
        [
            {
                "name": "reagent-approval-bot-0.1.7-win-x64-lite-setup.exe",
                "browser_download_url": "setup",
                "size": 200,
            },
            {
                "name": "reagent-approval-bot-0.1.7-win-x64-lite-portable.zip",
                "browser_download_url": "portable",
                "size": 180,
            },
        ]
    )
    assert asset is not None
    assert asset.name.endswith("-lite-portable.zip")
    assert asset.url == "portable"


def test_check_for_update_uses_public_release_before_api(monkeypatch):
    calls = []

    def public_release(timeout_seconds=20):
        calls.append("public")
        return {
            "tag_name": "v0.1.5",
            "html_url": "https://github.example/release",
            "assets": [
                {
                    "name": "reagent-approval-bot-0.1.5-win-x64-lite-portable.zip",
                    "browser_download_url": "https://github.example/portable.zip",
                    "size": 100,
                }
            ],
        }

    def api_release(timeout_seconds=20):
        calls.append("api")
        raise AssertionError("GitHub API should not be called when public release check succeeds")

    monkeypatch.setattr(update_checker, "fetch_latest_release_public", public_release)
    monkeypatch.setattr(update_checker, "fetch_latest_release", api_release)

    result = check_for_update(current_version="0.1.4")

    assert result.ok
    assert result.latest_version == "0.1.5"
    assert result.update_available
    assert calls == ["public"]
    assert result.asset is not None
    assert result.asset.name.endswith("-lite-portable.zip")


def test_installer_launch_copy_uses_temp_directory(tmp_path, monkeypatch):
    source_dir = tmp_path / "install" / "data" / "updates"
    source_dir.mkdir(parents=True)
    source = source_dir / "reagent-approval-bot-0.1.8-win-x64-lite-setup.exe"
    source.write_bytes(b"installer")
    temp_dir = tmp_path / "temp"
    monkeypatch.setattr(update_checker.tempfile, "gettempdir", lambda: str(temp_dir))

    copied = update_checker.installer_launch_copy(source)

    assert copied == temp_dir / "ReagentApprovalBotUpdates" / source.name
    assert copied.read_bytes() == b"installer"
    assert copied != source


def test_launch_installer_runs_temp_copy_and_waits_for_current_pid(tmp_path, monkeypatch):
    source = tmp_path / "install" / "data" / "updates" / "setup.exe"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"installer")
    temp_dir = tmp_path / "temp"
    popen_calls = []

    class DummyPopen:
        def __init__(self, args, **kwargs):
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(update_checker.tempfile, "gettempdir", lambda: str(temp_dir))
    monkeypatch.setattr(update_checker.subprocess, "Popen", DummyPopen)

    update_checker.launch_installer(source)

    assert len(popen_calls) == 1
    args, kwargs = popen_calls[0]
    assert Path(args[0]).parent == temp_dir / "ReagentApprovalBotUpdates"
    assert kwargs["cwd"] == str(temp_dir / "ReagentApprovalBotUpdates")
    assert kwargs["env"]["REAGENT_APPROVAL_START_AFTER_INSTALL"] == "1"
    assert kwargs["env"]["REAGENT_APPROVAL_WAIT_FOR_PID"] == str(update_checker.os.getpid())


def test_launch_portable_updater_generates_script(tmp_path, monkeypatch):
    source = tmp_path / "install" / "data" / "updates" / "portable.zip"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"zip")
    temp_dir = tmp_path / "temp"
    runtime_dir = tmp_path / "runtime"
    executable = tmp_path / "app" / "ReagentApprovalBot.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    popen_calls = []

    class DummyPopen:
        def __init__(self, args, **kwargs):
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(update_checker.tempfile, "gettempdir", lambda: str(temp_dir))
    monkeypatch.setattr(update_checker, "runtime_root", lambda: runtime_dir)
    monkeypatch.setattr(update_checker.sys, "executable", str(executable))
    monkeypatch.setattr(update_checker.subprocess, "Popen", DummyPopen)

    script = update_checker.launch_portable_updater(source)

    assert script == temp_dir / "ReagentApprovalBotUpdates" / "apply_reagent_update.ps1"
    script_text = script.read_text(encoding="utf-8")
    assert "Expand-Archive" in script_text
    assert "Get-Process -Id" in script_text
    assert "Start-Process -FilePath" in script_text
    assert len(popen_calls) == 1
