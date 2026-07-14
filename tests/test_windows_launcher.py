import importlib.util
from pathlib import Path
from unittest.mock import patch


def load_launcher_module():
    path = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "reagent_approval_bot_launcher.py"
    spec = importlib.util.spec_from_file_location("reagent_approval_bot_launcher", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_resolve_web_ui_port_uses_preferred_when_free():
    launcher = load_launcher_module()
    with patch.object(launcher, "port_is_open", return_value=False):
        assert launcher.resolve_web_ui_port("127.0.0.1", 8000) == (8000, False)


def test_resolve_web_ui_port_skips_port_occupied_by_other_app():
    launcher = load_launcher_module()

    def port_is_open(host, port):
        return port == 8000

    with (
        patch.object(launcher, "port_is_open", side_effect=port_is_open),
        patch.object(launcher, "is_reagent_web_ui", return_value=False),
    ):
        assert launcher.resolve_web_ui_port("127.0.0.1", 8000) == (8001, False)


def test_resolve_web_ui_port_reuses_existing_reagent_web_ui():
    launcher = load_launcher_module()
    with (
        patch.object(launcher, "port_is_open", return_value=True),
        patch.object(launcher, "is_reagent_web_ui", return_value=True),
    ):
        assert launcher.resolve_web_ui_port("127.0.0.1", 8000) == (8000, True)


def test_worker_failure_does_not_open_launcher_log(tmp_path, monkeypatch):
    launcher = load_launcher_module()
    monkeypatch.setattr(launcher.sys, "argv", ["ReagentApprovalBot.exe", "-m", "automation_worker", "suggestions"])
    monkeypatch.setenv("REAGENT_APPROVAL_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("REAGENT_APPROVAL_SOURCE_ROOT", str(tmp_path))

    with (
        patch.object(launcher, "bundled_root", return_value=tmp_path),
        patch.object(launcher, "runpy") as runpy_mock,
        patch.object(launcher.webbrowser, "open") as open_mock,
    ):
        runpy_mock.run_module.side_effect = RuntimeError("worker failed")
        assert launcher.main() == 1
        open_mock.assert_not_called()
