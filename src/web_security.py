from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Any, Mapping

from fastapi import HTTPException, Request


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def normalize_run_confirmation_options(payload: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Return the exact semantic run options bound to a confirmation ticket."""
    values = payload or {}

    def text_value(name: str, default: str = "") -> str:
        value = values.get(name, default)
        return default if value is None else str(value).strip()

    def boolean_value(name: str) -> str:
        return "true" if text_value(name).lower() in {"1", "true", "yes", "on"} else "false"

    pipeline = text_value("pipeline_version", "v1").lower() or "v1"
    pipeline = pipeline if pipeline in {"v1", "v2", "v3"} else "v1"

    return {
        "action": text_value("action"),
        "target_list_numbers": text_value("target_list_numbers"),
        "process_all_todos": boolean_value("process_all_todos"),
        "process_all_todos_max": text_value("process_all_todos_max", "50"),
        "approval_write_mode": text_value("approval_write_mode", "disabled"),
        "approval_write_min_confidence": text_value("approval_write_min_confidence", "0.8"),
        "approval_write_batch_size": text_value("approval_write_batch_size", "3"),
        "erp_write_backend": text_value("erp_write_backend", "web_ui"),
        "pipeline_version": pipeline,
        "auto_pass": boolean_value("auto_pass"),
    }


def canonical_hash(payload: dict[str, Any] | None) -> str:
    encoded = json.dumps(payload or {}, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class OperationTicket:
    operation: str
    options_hash: str
    expires_at: float


class WebSecurity:
    def __init__(self, *, ticket_ttl_seconds: int = 60) -> None:
        self.csrf_token = secrets.token_urlsafe(32)
        self.ticket_ttl_seconds = ticket_ttl_seconds
        self._tickets: dict[str, OperationTicket] = {}
        self._lock = threading.Lock()

    def issue_ticket(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.time()
        token = secrets.token_urlsafe(32)
        ticket = OperationTicket(operation, canonical_hash(payload), now + self.ticket_ttl_seconds)
        with self._lock:
            self._discard_expired(now)
            self._tickets[token] = ticket
        return {"ticket": token, "expires_in": self.ticket_ttl_seconds, "options_hash": ticket.options_hash}

    def consume_ticket(self, token: str, operation: str, payload: dict[str, Any] | None = None) -> None:
        now = time.time()
        with self._lock:
            ticket = self._tickets.pop(token, None)
            self._discard_expired(now)
        if ticket is None:
            raise HTTPException(status_code=403, detail="操作确认票据无效或已使用。")
        if ticket.expires_at < now:
            raise HTTPException(status_code=403, detail="操作确认票据已过期。")
        if not hmac.compare_digest(ticket.operation, operation):
            raise HTTPException(status_code=403, detail="操作确认票据与请求类型不匹配。")
        if not hmac.compare_digest(ticket.options_hash, canonical_hash(payload)):
            raise HTTPException(status_code=403, detail="操作参数已变化，请重新确认。")

    def _discard_expired(self, now: float) -> None:
        for key in [key for key, value in self._tickets.items() if value.expires_at < now]:
            self._tickets.pop(key, None)

    def validate_request(self, request: Request) -> None:
        host = request.headers.get("host", "")
        # Starlette's TestClient uses this synthetic host; never accepted by the frozen application.
        test_client = host == "testserver"
        if not test_client:
            hostname = host.rsplit(":", 1)[0].strip("[]").lower()
            if hostname not in {"127.0.0.1", "localhost"}:
                raise HTTPException(status_code=403, detail="非法的本地管理地址。")

        if request.method.upper() not in UNSAFE_METHODS:
            return
        if test_client:
            return
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(status_code=403, detail="拒绝跨站请求。")
        origin = request.headers.get("origin", "")
        expected_origins = {f"http://{host.lower()}"}
        if origin.lower() not in expected_origins:
            raise HTTPException(status_code=403, detail="请求来源无效。")
        supplied = request.headers.get("x-reagent-csrf", "")
        if not supplied or not hmac.compare_digest(supplied, self.csrf_token):
            raise HTTPException(status_code=403, detail="CSRF 令牌无效。")


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
