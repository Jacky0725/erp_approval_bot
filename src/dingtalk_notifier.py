from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DINGTALK_STREAM_BINDING_PATH = Path("data") / "dingtalk_stream_binding.json"


def dingtalk_notification_config(settings: dict[str, Any] | None) -> dict[str, Any]:
    configured = ((settings or {}).get("notification", {}) or {}).get("dingtalk", {}) or {}
    return {
        "enabled": coerce_bool(configured.get("enabled")),
        "at_all": coerce_bool(configured.get("at_all", True)),
    }


def dingtalk_stream_notification_config(settings: dict[str, Any] | None) -> dict[str, Any]:
    stream = ((settings or {}).get("dingtalk_stream", {}) or {})
    configured = ((settings or {}).get("notification", {}) or {}).get("dingtalk", {}) or {}
    return {
        "enabled": coerce_bool(stream.get("enabled")),
        "corp_id": str(stream.get("corp_id") or "").strip(),
        "robot_code": str(
            configured.get("stream_robot_code")
            or stream.get("robot_code")
            or stream.get("app_id")
            or stream.get("agent_id")
            or ""
        ).strip(),
        "open_conversation_id": str(
            configured.get("stream_open_conversation_id")
            or stream.get("open_conversation_id")
            or ""
        ).strip(),
        "client_id_env": str(stream.get("client_id_env") or "DINGTALK_STREAM_CLIENT_ID"),
        "client_secret_env": str(stream.get("client_secret_env") or "DINGTALK_STREAM_CLIENT_SECRET"),
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
    root_dir: Path | str | None = None,
    timeout_seconds: int = 8,
) -> dict[str, Any]:
    config = dingtalk_notification_config(settings)
    if not config.get("enabled"):
        return {"sent": False, "skipped": True, "reason": "DingTalk notification is disabled."}
    return send_stream_task_result_notification(
        settings,
        message,
        root_dir=root_dir,
        at_all=bool(config.get("at_all", True)),
        timeout_seconds=timeout_seconds,
    )


def send_stream_task_result_notification(
    settings: dict[str, Any] | None,
    message: DingTalkMessage,
    *,
    root_dir: Path | str | None = None,
    at_all: bool = True,
    timeout_seconds: int = 8,
) -> dict[str, Any]:
    stream_config = dingtalk_stream_notification_config(settings)
    if not stream_config.get("enabled"):
        return {"sent": False, "skipped": False, "error": "DingTalk Stream is not enabled."}

    binding = load_stream_binding(root_dir)
    session_webhook = str(binding.get("sessionWebhook") or binding.get("session_webhook") or "").strip()
    if session_webhook:
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": message.title, "text": message.markdown},
            "at": {"isAtAll": bool(at_all)},
        }
        result = post_json(session_webhook, payload, timeout_seconds)
        if result.get("sent"):
            return {**result, "channel": "stream_session_webhook"}

    open_conversation_id = str(
        binding.get("openConversationId")
        or binding.get("open_conversation_id")
        or stream_config.get("open_conversation_id")
        or ""
    ).strip()
    robot_code = str(binding.get("robotCode") or stream_config.get("robot_code") or "").strip()
    if not open_conversation_id:
        return {"sent": False, "skipped": False, "error": "DingTalk Stream openConversationId is not configured."}
    if not robot_code:
        return {"sent": False, "skipped": False, "error": "DingTalk Stream robotCode is not configured."}

    access_token = get_dingtalk_access_token(stream_config, timeout_seconds)
    if not access_token:
        return {"sent": False, "skipped": False, "error": "Could not get DingTalk access token."}

    content = message.markdown
    if at_all:
        content = "@all\n" + content
    payload = {
        "msgParam": json.dumps({"content": content}, ensure_ascii=False),
        "msgKey": "sampleText",
        "openConversationId": open_conversation_id,
        "robotCode": robot_code,
    }
    result = post_json(
        "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
        payload,
        timeout_seconds,
        headers={"x-acs-dingtalk-access-token": access_token},
    )
    return {**result, "channel": "stream_openapi"}


def get_dingtalk_access_token(config: dict[str, Any], timeout_seconds: int) -> str:
    corp_id = str(config.get("corp_id") or "").strip()
    client_id = os.getenv(str(config.get("client_id_env") or ""), "").strip()
    client_secret = os.getenv(str(config.get("client_secret_env") or ""), "").strip()
    if not corp_id or not client_id or not client_secret:
        return ""

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }
    url = f"https://api.dingtalk.com/v1.0/oauth2/{urllib.parse.quote(corp_id, safe='')}/token"
    result = post_json(url, payload, timeout_seconds)
    if not result.get("sent"):
        return ""
    try:
        body = json.loads(str(result.get("body") or "{}"))
    except json.JSONDecodeError:
        return ""
    return str(body.get("access_token") or "").strip()


def load_stream_binding(root_dir: Path | str | None = None) -> dict[str, Any]:
    path = stream_binding_path(root_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_stream_binding(data: dict[str, Any], root_dir: Path | str | None = None) -> dict[str, Any]:
    path = stream_binding_path(root_dir)
    current = load_stream_binding(root_dir)
    merged = {**current, **{key: value for key, value in data.items() if value}}
    if not merged:
        return {}
    merged["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def stream_binding_path(root_dir: Path | str | None = None) -> Path:
    base = Path(root_dir) if root_dir is not None else Path.cwd()
    return base / DINGTALK_STREAM_BINDING_PATH


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {"sent": True, "status": response.status, "body": body}
    except Exception as exc:  # noqa: BLE001 - notification must not affect automation outcome
        return {"sent": False, "skipped": False, "error": str(exc)}
