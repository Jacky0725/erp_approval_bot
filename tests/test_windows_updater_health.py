import importlib.util
import io
import json
from pathlib import Path
import sys


UPDATER_PATH = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "reagent_approval_bot_updater.py"


def load_updater():
    spec = importlib.util.spec_from_file_location("reagent_approval_bot_updater_health_test", UPDATER_PATH)
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    return updater


def test_updater_checks_lightweight_health_endpoint(monkeypatch):
    updater = load_updater()
    requested = []
    install_root = Path("C:/ReagentApprovalBot")

    def urlopen(url, timeout):
        requested.append((url, timeout))
        return io.BytesIO(json.dumps({"ok": True, "version": "0.1.24", "install_root": str(install_root)}).encode("utf-8"))

    monkeypatch.setattr(updater.urllib.request, "urlopen", urlopen)
    assert updater.healthy("0.1.24", install_root, timeout=2)
    assert requested == [("http://127.0.0.1:8000/api/health", 1)]


def test_updater_rejects_other_install_on_same_port(monkeypatch):
    updater = load_updater()
    monkeypatch.setattr(updater.time, "monotonic", iter([0, 2]).__next__)
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(
        json.dumps({"ok": True, "version": "0.1.24", "install_root": "C:/OtherInstall"}).encode("utf-8")))
    assert not updater.healthy("0.1.24", Path("C:/ReagentApprovalBot"), timeout=1)


def test_failed_health_restores_old_browser_before_relaunch(tmp_path, monkeypatch):
    updater = load_updater()
    root = tmp_path / "install"
    root.mkdir()
    old_browser = {"revision": "1254", "path": "shared/browsers/1254"}
    pointer = root / "browser-current.json"
    pointer.write_text(json.dumps(old_browser), encoding="utf-8")
    (root / "ReagentApprovalBot.exe").write_bytes(b"MZ")
    core = tmp_path / "core.zip"
    browser = tmp_path / "browser.zip"
    core.write_bytes(b"core")
    browser.write_bytes(b"browser")
    calls = []
    monkeypatch.setattr(sys, "argv", ["updater", "--install-root", str(root), "--core", str(core),
                                    "--version", "0.1.24", "--core-sha256", "core-hash",
                                    "--browser", str(browser), "--browser-sha256", "browser-hash",
                                    "--browser-revision", "1223"])
    monkeypatch.setattr(updater, "sha256_file", lambda path: "core-hash" if path == core else "browser-hash")
    monkeypatch.setattr(updater, "safe_extract_zip", lambda *args: None)
    monkeypatch.setattr(updater, "stage_version", lambda *args: None)
    monkeypatch.setattr(updater, "switch_current", lambda *args: None)
    monkeypatch.setattr(updater, "rollback_current", lambda *args: True)
    monkeypatch.setattr(updater, "healthy", lambda *args: False)
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *args, **kwargs: calls.append(json.loads(pointer.read_text(encoding="utf-8"))))

    assert updater.main() == 1
    assert calls == [{"revision": "1223", "path": "shared/browsers/1223"}, old_browser]
    assert json.loads(pointer.read_text(encoding="utf-8")) == old_browser


def test_failed_staging_restores_browser_pointer(tmp_path, monkeypatch):
    updater = load_updater()
    root = tmp_path / "install"
    root.mkdir()
    pointer = root / "browser-current.json"
    pointer.write_text('{"revision":"1254"}', encoding="utf-8")
    core = tmp_path / "core.zip"
    browser = tmp_path / "browser.zip"
    core.write_bytes(b"core")
    browser.write_bytes(b"browser")
    monkeypatch.setattr(sys, "argv", ["updater", "--install-root", str(root), "--core", str(core),
                                    "--version", "0.1.24", "--core-sha256", "core-hash",
                                    "--browser", str(browser), "--browser-sha256", "browser-hash",
                                    "--browser-revision", "1223"])
    monkeypatch.setattr(updater, "sha256_file", lambda path: "core-hash" if path == core else "browser-hash")
    monkeypatch.setattr(updater, "safe_extract_zip", lambda *args: None)
    monkeypatch.setattr(updater, "stage_version", lambda *args: (_ for _ in ()).throw(RuntimeError("staging failed")))

    assert updater.main() == 1
    assert pointer.read_text(encoding="utf-8") == '{"revision":"1254"}'
