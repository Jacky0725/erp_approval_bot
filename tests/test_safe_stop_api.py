import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from task_control import TaskControl


@pytest.fixture
def api(tmp_path, monkeypatch):
    import web_app
    task_id = "a" * 32
    control = TaskControl(tmp_path)
    control.register(task_id, "workbuddy_loop", {}, {"source": "web"}, {})
    control.finish(task_id, "stopped")
    monkeypatch.setattr(web_app, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(web_app.manager, "status", lambda **kw: {"running": True, "task_id": task_id})
    stop = Mock(return_value={"requested": True, "message": "accepted"})
    resume = Mock(return_value={"started": True})
    monkeypatch.setattr(web_app.manager, "request_safe_stop", stop)
    monkeypatch.setattr(web_app.manager, "resume_stopped_task", resume)
    client = TestClient(web_app.app, base_url="http://localhost:8000")
    headers = {"Origin": "http://localhost:8000", "x-reagent-csrf": web_app.security.csrf_token}
    return client, headers, task_id, stop, resume


def test_safe_stop_ticket_is_bound_to_task_and_consumed_once(api):
    client, headers, task_id, stop, _ = api
    ticket = client.post("/api/safe-stop/prepare", headers=headers, json={"task_id": task_id}).json()["ticket"]
    response = client.post("/api/safe-stop", headers=headers, json={"task_id": task_id, "ticket": ticket})
    assert response.status_code == 200
    assert response.json()["requested"]
    assert client.post("/api/safe-stop", headers=headers, json={"task_id": task_id, "ticket": ticket}).status_code == 403
    stop.assert_called_once_with(task_id, {"source": "web"})


def test_changed_task_cannot_use_old_ticket(api):
    client, headers, task_id, stop, _ = api
    ticket = client.post("/api/safe-stop/prepare", headers=headers, json={"task_id": task_id}).json()["ticket"]
    assert client.post("/api/safe-stop", headers=headers, json={"task_id": "b" * 32, "ticket": ticket}).status_code == 403
    stop.assert_not_called()


def test_resume_requires_manual_verification_and_one_time_ticket(api):
    client, headers, task_id, _, resume = api
    assert client.post("/api/resume/prepare", headers=headers, json={"task_id": task_id}).status_code == 400
    payload = {"task_id": task_id, "verified": True}
    ticket = client.post("/api/resume/prepare", headers=headers, json=payload).json()["ticket"]
    payload["ticket"] = ticket
    assert client.post("/api/resume", headers=headers, json=payload).json()["started"]
    assert client.post("/api/resume", headers=headers, json=payload).status_code == 403
    resume.assert_called_once_with(task_id)


def test_new_controls_require_csrf(api):
    client, _, task_id, stop, resume = api
    assert client.post("/api/safe-stop", json={"task_id": task_id}).status_code == 403
    assert client.post("/api/resume", json={"task_id": task_id, "verified": True}).status_code == 403
    stop.assert_not_called()
    resume.assert_not_called()


def test_stopped_task_listing_does_not_expose_actor_or_options(api):
    client, _, _, _, _ = api
    response = client.get("/api/stopped-tasks")
    assert response.status_code == 200
    assert len(response.json()["tasks"]) == 1
    assert "actor" not in response.json()["tasks"][0]
    assert "options" not in response.json()["tasks"][0]
