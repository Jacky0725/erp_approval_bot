import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import workbuddy_discovery as discovery


def test_detects_unique_existing_paths_and_filters_invalid_entries(tmp_path, monkeypatch):
    first = tmp_path / "Programs" / "WorkBuddy.exe"
    second = tmp_path / "安装 目录" / "WorkBuddy.exe"
    for path in (first, second):
        path.parent.mkdir()
        path.touch()
    paths = [(str(first), "user_registry"), (str(second), "machine_registry"),
             (str(tmp_path / "missing" / "WorkBuddy.exe"), "user_registry")]
    monkeypatch.setattr(discovery, "running_paths", lambda: iter([str(first)]))
    monkeypatch.setattr(discovery, "registered_paths", lambda: iter(paths))
    if discovery.os.name != "nt":
        pytest.skip("Windows-only discovery")
    assert discovery.discover_clients() == [
        {"path": str(first), "source": "running"}, {"path": str(second), "source": "machine_registry"},
    ]
    monkeypatch.setattr(discovery, "running_paths", lambda: iter(()))
    paths.clear()
    assert discovery.discover_clients() == []


def test_save_changes_only_desktop_configuration(tmp_path):
    executable = tmp_path / "WorkBuddy.exe"
    executable.touch()
    settings = {"workbuddy": {"write_enabled": False, "batch_dir": "data/workbuddy"}}
    with pytest.raises(discovery.WorkBuddyConfigError, match="完整路径"):
        discovery.update_client_settings(settings, {"workbuddy_executable": "WorkBuddy.exe", "workbuddy_desktop_enabled": "true"})
    with pytest.raises(discovery.WorkBuddyConfigError, match="项目名称"):
        discovery.update_client_settings(settings, {"workbuddy_executable": str(executable), "workbuddy_desktop_enabled": "true"})
    discovery.update_client_settings(settings, {"workbuddy_executable": str(executable), "workbuddy_project_name": "试剂审批",
                                               "workbuddy_desktop_enabled": "true"})
    assert settings["workbuddy"] == {"executable": str(executable), "project_name": "试剂审批", "desktop_enabled": True,
                                    "write_enabled": False, "batch_dir": "data/workbuddy"}
    discovery.update_client_settings(settings, {"workbuddy_executable": ""})
    assert settings["workbuddy"]["executable"] == ""
    assert not settings["workbuddy"]["desktop_enabled"]


def test_detection_requires_csrf_and_does_not_save(monkeypatch):
    import web_app

    result = [{"path": "D:/Apps/WorkBuddy.exe", "source": "running"}]
    monkeypatch.setattr(web_app, "discover_clients", lambda: result)
    monkeypatch.setattr(web_app, "save_runtime_config", lambda _: pytest.fail("detection must not save"))
    with TestClient(web_app.app, base_url="http://localhost:8000") as client:
        headers = {"Origin": "http://localhost:8000"}
        assert client.post("/api/settings/workbuddy/detect", headers=headers).status_code == 403
        headers["X-Reagent-CSRF"] = web_app.security.csrf_token
        assert client.post("/api/settings/workbuddy/detect", headers=headers).json() == {"candidates": result}


def test_workbuddy_settings_reject_changes_while_task_running(monkeypatch):
    import web_app

    monkeypatch.setattr(web_app.manager, "status", lambda: {"running": True})
    with TestClient(web_app.app, base_url="http://localhost:8000") as client:
        response = client.post("/api/settings", data={"workbuddy_config_present": "true", "workbuddy_executable": "D:/Apps/WorkBuddy.exe"},
                               headers={"Origin": "http://localhost:8000", "X-Reagent-CSRF": web_app.security.csrf_token})
    assert response.status_code == 409


def test_settings_candidate_selection_in_desktop_and_mobile_browser(monkeypatch):
    from playwright.sync_api import sync_playwright
    import web_app

    candidates = [{"path": r"D:\应用程序\WorkBuddy\WorkBuddy.exe", "source": "running"},
                  {"path": r"C:\Program Files\WorkBuddy\WorkBuddy.exe", "source": "machine_registry"}]
    monkeypatch.setattr(web_app, "discover_clients", lambda: candidates)
    with TestClient(web_app.app, base_url="http://localhost:8000") as client, sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Playwright Chromium is not installed")
        browser = playwright.chromium.launch()
        for width in (1280, 390):
            page = browser.new_page(viewport={"width": width, "height": 840})
            errors = []
            saves = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def fulfill(route):
                path = urlsplit(route.request.url).path
                if path == "/api/settings/workbuddy/detect":
                    response = client.post(path, headers=route.request.all_headers())
                elif (path == "/settings" and route.request.method == "GET") or path.startswith("/static/"):
                    response = client.get(path)
                else:
                    if path == "/api/settings" and route.request.method == "POST":
                        saves.append(path)
                    route.fulfill(json={})
                    return
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

            page.route("**/*", fulfill)
            page.goto("http://localhost:8000/settings")
            section = page.locator(".settings-section").filter(has=page.get_by_role("heading", name="WorkBuddy 本机配置"))
            section.get_by_role("button", name="展开").click()
            section.get_by_role("button", name="检测客户端").click()
            section.get_by_label("WorkBuddy 客户端候选").select_option(candidates[0]["path"])
            assert section.get_by_label("客户端路径").input_value() == candidates[0]["path"]
            assert "尚未保存" in section.get_by_role("status").inner_text()
            bounds = section.get_by_label("WorkBuddy 客户端候选").bounding_box()
            assert bounds and 0 <= bounds["x"] and bounds["x"] + bounds["width"] <= width + 1
            assert not saves and not errors
            page.close()
        browser.close()
