from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


def dingtalk_notification_config(settings: dict[str, Any] | None) -> dict[str, Any]:
    configured = ((settings or {}).get("notification", {}) or {}).get("dingtalk", {}) or {}
    return {
        "enabled": coerce_bool(configured.get("enabled")),
        "webhook_env": str(configured.get("webhook_env") or "DINGTALK_ROBOT_WEBHOOK"),
        "secret_env": str(configured.get("secret_env") or "DINGTALK_ROBOT_SECRET"),
        "at_all": coerce_bool(configured.get("at_all", True)),
    }


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class DingTalkMessage:
    title: str
    markdown: str


def build_task_result_message(
    *,
    action: str,
    success: bool,
    started_at: str = "",
    finished_at: str = "",
    error: str = "",
    approval: dict[str, Any] | None = None,
    review_queue: dict[str, Any] | None = None,
) -> DingTalkMessage:
    action_label = action_labels().get(action, action or "自动化任务")
    status = "成功" if success else "失败"
    title = f"试剂审批任务{status}"
    lines = [
        f"### {title}",
        f"- 任务：{action_label}",
        f"- 状态：{status}",
    ]
    if started_at:
        lines.append(f"- 开始：{started_at}")
    if finished_at:
        lines.append(f"- 结束：{finished_at}")

    approval = approval or {}
    if approval.get("exists"):
        lines.append(f"- 审批建议：{approval.get('rows', 0)} 条")
        lines.append(f"- 需人工复核：{approval.get('manual_review', 0)} 条")

    review_queue = review_queue or {}
    if review_queue.get("exists"):
        lines.append(f"- 复核队列待处理：{review_queue.get('pending', 0)} 条")

    if error:
        lines.append(f"- 错误：{truncate(error, 220)}")

    return DingTalkMessage(title=title, markdown="\n".join(lines))


def action_labels() -> dict[str, str]:
    return {
        "suggestions": "生成审批建议",
        "todo_export": "导出待办清单",
        "debug_capture": "调试截图",
        "judgement_capture": "审批页截图",
    }


def truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


def send_task_result_notification(
    settings: dict[str, Any] | None,
    message: DingTalkMessage,
    *,
    timeout_seconds: int = 8,
) -> dict[str, Any]:
    config = dingtalk_notification_config(settings)
    if not config.get("enabled"):
        return {"sent": False, "skipped": True, "reason": "DingTalk notification is disabled."}

    webhook = os.getenv(config["webhook_env"], "").strip()
    if not webhook:
        return {"sent": False, "skipped": False, "error": "DingTalk webhook is not configured."}

    secret = os.getenv(config["secret_env"], "").strip()
    if secret:
        webhook = signed_dingtalk_url(webhook, secret)

    payload = {
        "msgtype": "markdown",
        "markdown": {"title": message.title, "text": message.markdown},
        "at": {"isAtAll": bool(config.get("at_all", True))},
    }
    return post_json(webhook, payload, timeout_seconds)


def signed_dingtalk_url(webhook: str, secret: str) -> str:
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    separator = "&" if "?" in webhook else "?"
    return f"{webhook}{separator}timestamp={timestamp}&sign={sign}"


def post_json(url: str, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {"sent": True, "status": response.status, "body": body}
    except Exception as exc:  # noqa: BLE001 - notification must not affect automation outcome
        return {"sent": False, "skipped": False, "error": str(exc)}
