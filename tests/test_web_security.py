import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web_security import WebSecurity, normalize_run_confirmation_options


def request(method: str, headers: dict[str, str]) -> Request:
    return Request({"type": "http", "method": method, "headers": [(key.encode(), value.encode()) for key, value in headers.items()]})


def test_rejects_cross_site_write_with_valid_token():
    security = WebSecurity()
    with pytest.raises(HTTPException, match="跨站"):
        security.validate_request(request("POST", {
            "host": "127.0.0.1:8000",
            "origin": "http://127.0.0.1:8000",
            "x-reagent-csrf": security.csrf_token,
            "sec-fetch-site": "cross-site",
        }))


def test_rejects_bad_origin_and_accepts_local_write():
    security = WebSecurity()
    with pytest.raises(HTTPException, match="来源"):
        security.validate_request(request("POST", {
            "host": "localhost:8000", "origin": "http://evil.example", "x-reagent-csrf": security.csrf_token,
        }))
    security.validate_request(request("POST", {
        "host": "localhost:8000", "origin": "http://localhost:8000", "x-reagent-csrf": security.csrf_token,
    }))


def test_operation_ticket_is_single_use_and_payload_bound():
    security = WebSecurity()
    ticket = security.issue_ticket("run", {"action": "suggestions"})["ticket"]
    with pytest.raises(HTTPException, match="参数"):
        security.consume_ticket(ticket, "run", {"action": "erp_smoke"})
    with pytest.raises(HTTPException, match="无效"):
        security.consume_ticket(ticket, "run", {"action": "suggestions"})


def test_run_ticket_treats_missing_unchecked_flags_as_false():
    security = WebSecurity()
    prepared = normalize_run_confirmation_options(
        {
            "action": "suggestions",
            "target_list_numbers": "SJ1",
            "approval_write_mode": "multi_page",
        }
    )
    submitted = normalize_run_confirmation_options(
        {
            "action": "suggestions",
            "target_list_numbers": "SJ1",
            "process_all_todos": "false",
            "approval_write_mode": "multi_page",
            "auto_pass": "false",
        }
    )

    assert prepared == submitted
    ticket = security.issue_ticket("run", prepared)["ticket"]
    security.consume_ticket(ticket, "run", submitted)


def test_run_confirmation_normalization_is_fixed_to_v1():
    assert normalize_run_confirmation_options({"pipeline_version": "v3"})["pipeline_version"] == "v1"


def test_web_middleware_returns_403_for_missing_csrf():
    from web_app import app

    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.post("/api/stop", headers={"Origin": "http://localhost:8000"})
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF 令牌无效。"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_run_prepare_rejects_explicit_retired_pipeline():
    from web_app import app, security

    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.post(
            "/api/run/prepare",
            json={"action": "suggestions", "pipeline_version": "v3"},
            headers={
                "Origin": "http://localhost:8000",
                "x-reagent-csrf": security.csrf_token,
            },
        )
    assert response.status_code == 400
    assert "V2/V3 已停用" in response.json()["detail"]


def test_run_endpoint_rejects_explicit_retired_pipeline():
    from web_app import app, security

    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.post(
            "/api/run",
            data={"action": "suggestions", "pipeline_version": "v2"},
            headers={
                "Origin": "http://localhost:8000",
                "x-reagent-csrf": security.csrf_token,
            },
        )
    assert response.status_code == 400
    assert "V2/V3 已停用" in response.json()["detail"]


def test_settings_endpoint_rejects_retired_pipeline_value():
    from web_app import app, security

    with patch("web_app.manager.status", return_value={"running": False}), TestClient(
        app, base_url="http://localhost:8000"
    ) as client:
        response = client.post(
            "/api/settings",
            data={"approval_pipeline_version": "v3"},
            headers={
                "Origin": "http://localhost:8000",
                "x-reagent-csrf": security.csrf_token,
            },
        )
    assert response.status_code == 400
    assert "V2/V3 已停用" in response.json()["detail"]


def test_retired_pipeline_diagnostic_routes_are_removed():
    from web_app import app

    with TestClient(app, base_url="http://localhost:8000") as client:
        assert client.get("/api/v3/invalid_results").status_code == 404
        assert client.get("/api/enrichment_shadow").status_code == 404


def test_ui_exposes_only_v1_and_keeps_identity_enrichment_settings():
    from web_app import app

    with TestClient(app, base_url="http://localhost:8000") as client:
        run_page = client.get("/run")
        settings_page = client.get("/settings")
        logs_page = client.get("/logs")
    assert run_page.status_code == settings_page.status_code == logs_page.status_code == 200
    assert "name=\"pipeline_version\"" not in run_page.text
    assert "V2 物化信息链路" not in settings_page.text
    assert "V3 百炼" not in settings_page.text
    assert "name=\"identity_enrichment_api_key\"" in settings_page.text
    assert "V3 无效结果" not in logs_page.text
