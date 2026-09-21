import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

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
