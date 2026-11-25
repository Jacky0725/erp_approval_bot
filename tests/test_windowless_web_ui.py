from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import start_web_ui_windowless as launcher


def test_shortcut_reuses_the_running_project_server(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.socket.socket, "connect_ex", lambda *_: 0)
    monkeypatch.setattr(launcher, "health", lambda: {"install_root": str(tmp_path)})
    open_url = Mock()
    spawn = Mock()
    monkeypatch.setattr(launcher.os, "startfile", open_url, raising=False)
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    launcher.start(tmp_path, Path("pythonw.exe"))
    spawn.assert_not_called()
    open_url.assert_called_once_with(launcher.URL)


def test_background_start_reuses_server_without_opening_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.socket.socket, "connect_ex", lambda *_: 0)
    monkeypatch.setattr(launcher, "health", lambda: {"install_root": str(tmp_path)})
    open_url = Mock()
    monkeypatch.setattr(launcher.os, "startfile", open_url, raising=False)
    launcher.start(tmp_path, Path("pythonw.exe"), open_browser=False)
    open_url.assert_not_called()


def test_shortcut_starts_the_controlled_server_without_a_console(tmp_path, monkeypatch):
    (tmp_path / "python.exe").touch()
    monkeypatch.setattr(launcher.socket.socket, "connect_ex", lambda *_: 1)
    monkeypatch.setattr(launcher, "health", lambda: {"install_root": str(tmp_path)})
    monkeypatch.setattr(launcher.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    open_url = Mock()
    monkeypatch.setattr(launcher.os, "startfile", open_url, raising=False)
    spawn = Mock(return_value=Mock(pid=123))
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    launcher.start(tmp_path, tmp_path / "pythonw.exe")
    assert spawn.call_args.args[0][1:3] == ["-m", "web_server_control"]
    assert spawn.call_args.kwargs["creationflags"] == 0x08000000
    assert (tmp_path / "data/logs/web_ui_server.pid").read_text() == "123"
    open_url.assert_called_once_with(launcher.URL)


def test_shortcut_does_not_open_an_unrelated_service(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.socket.socket, "connect_ex", lambda *_: 0)
    monkeypatch.setattr(launcher, "health", lambda: {"install_root": str(tmp_path / "other")})
    open_url = Mock()
    monkeypatch.setattr(launcher.os, "startfile", open_url, raising=False)
    with pytest.raises(RuntimeError, match="could not be verified"):
        launcher.start(tmp_path, Path("pythonw.exe"))
    open_url.assert_not_called()
