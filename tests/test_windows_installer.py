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
