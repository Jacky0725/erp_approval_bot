import importlib.util
from pathlib import Path
from unittest.mock import patch


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


def test_manual_windows_install_can_prompt(monkeypatch):
    installer = load_installer_module()
    monkeypatch.delenv("REAGENT_APPROVAL_START_AFTER_INSTALL", raising=False)
    monkeypatch.delenv("REAGENT_APPROVAL_SILENT_INSTALL", raising=False)

    with patch.object(installer.os, "name", "nt"):
        assert installer.should_prompt_for_install_dir()


def test_progress_reporter_run_without_ui_executes_worker(monkeypatch):
    installer = load_installer_module()
    monkeypatch.setenv("REAGENT_APPROVAL_SUPPRESS_INSTALL_PROGRESS", "1")
    progress = installer.ProgressReporter()

    assert progress.run(lambda: "done") == "done"
