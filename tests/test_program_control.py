from pathlib import Path
import sys
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def control(monkeypatch):
    import web_app

    shutdown = Mock()
    begin = Mock(return_value=True)
    restart = Mock()
    monkeypatch.setattr(web_app.manager, "begin_shutdown", begin)
    monkeypatch.setattr(web_app.manager, "cancel_shutdown", Mock())
    monkeypatch.setattr(web_app.manager, "_unsettled_workbuddy_startup", Mock(return_value=False))
    monkeypatch.setattr(web_app, "_pending_workbuddy_shutdown", Mock(return_value=False))
    monkeypatch.setattr(web_app.manager, "status", Mock(return_value={"running": False}))
    monkeypatch.setattr(web_app, "schedule_web_ui_restart", restart)

    class Timer:
        def __init__(self, delay, callback):
            self.callback = callback

        def start(self):
            self.callback()

    monkeypatch.setattr(web_app.threading, "Timer", Timer)
    web_app.app.state.request_server_shutdown = shutdown
    client = TestClient(web_app.app, base_url="http://localhost:8000")
    headers = {"Origin": "http://localhost:8000", "x-reagent-csrf": web_app.security.csrf_token}
    yield web_app, client, headers, begin, restart, shutdown
    web_app.app.state.request_server_shutdown = None


def test_running_task_blocks_restart_and_exit_without_force_stop(control, monkeypatch):
    app, client, headers, begin, restart, shutdown = control
    monkeypatch.setattr(app.manager, "status", Mock(return_value={"running": True}))
    stop = Mock()
    monkeypatch.setattr(app.manager, "stop", stop)
    assert client.post("/api/restart", headers=headers).status_code == 409
    assert client.post("/api/exit/prepare", headers=headers).status_code == 409
    begin.assert_not_called()
    stop.assert_not_called()
    restart.assert_not_called()
    shutdown.assert_not_called()


def test_unresolved_workbuddy_blocks_program_control(control, monkeypatch):
    app, client, headers, begin, restart, shutdown = control
    monkeypatch.setattr(app, "_pending_workbuddy_shutdown", Mock(return_value=True))
    assert client.post("/api/restart", headers=headers).status_code == 409
    assert client.post("/api/exit/prepare", headers=headers).status_code == 409
    begin.assert_not_called()
    restart.assert_not_called()
    shutdown.assert_not_called()


def test_exit_requires_ticket_and_closes_idle_server_once(control):
    _, client, headers, begin, restart, shutdown = control
    ticket = client.post("/api/exit/prepare", headers=headers).json()["ticket"]
    assert client.post("/api/exit", headers=headers, json={"ticket": ticket}).json()["exiting"]
    assert client.post("/api/exit", headers=headers, json={"ticket": ticket}).status_code == 403
    begin.assert_called_once()
    restart.assert_not_called()
    shutdown.assert_called_once()


def test_restart_does_not_report_success_when_helper_fails(control):
    _, client, headers, begin, restart, shutdown = control
    restart.side_effect = OSError("cannot launch")
    response = client.post("/api/restart", headers=headers)
    assert response.status_code == 500
    begin.assert_called_once()
    shutdown.assert_not_called()


def test_reload_mode_disables_web_shutdown(control):
    app, client, headers, begin, _, _ = control
    app.app.state.request_server_shutdown = None
    assert client.post("/api/restart", headers=headers).status_code == 409
    assert client.post("/api/exit/prepare", headers=headers).status_code == 409
    begin.assert_not_called()


def test_exit_requires_local_csrf(control):
    _, client, _, begin, _, _ = control
    assert client.post("/api/exit/prepare").status_code == 403
    assert client.post("/api/exit", json={"ticket": "x"}).status_code == 403
    begin.assert_not_called()
