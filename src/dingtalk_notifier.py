from __future__ import annotations

import json
import os
import time
import tempfile
import urllib.request
import urllib.error
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
            or os.getenv(str(stream.get("client_id_env") or "DINGTALK_STREAM_CLIENT_ID"), "")
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
    stopped: bool = False,
    stop_details: dict[str, Any] | None = None,
) -> DingTalkMessage:
    action_label = action_labels().get(action, action or "自动化任务")
    status = "已人工停止" if stopped else "成功" if success else "失败"
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

    approval = {} if stopped else approval or {}
    if approval.get("exists"):
        lines.append(f"- 审批建议：{approval.get('rows', 0)} 条")
        lines.append(f"- 需人工复核：{approval.get('manual_review', 0)} 条")

    review_queue = {} if stopped else review_queue or {}
    if review_queue.get("exists"):
        lines.append(f"- 复核队列待处理：{review_queue.get('pending', 0)} 条")

    if error:
        lines.append(f"- 错误：{truncate(error, 220)}")
    if stopped:
        lines.append("- 已保存结果不会撤销，未完成记录已保留。")
        if (stop_details or {}).get("task_id"):
            lines.append(f"- 任务编号：{stop_details['task_id'][:12]}")
        if (stop_details or {}).get("phase"):
            lines.append(f"- 停止位置：{stop_details['phase']}")
        for key, label in (("saved", "已保存"), ("pending_library", "待生成试剂库"), ("remaining", "未处理"), ("uncertain", "待核对")):
            if key in (stop_details or {}):
                if key in {"remaining", "uncertain", "pending_library"} and stop_details.get("count_scope") == "batch":
                    label = "本批" + label
                lines.append(f"- {label}：{stop_details[key]} 条")

    return DingTalkMessage(title=title, markdown="\n".join(lines))


def action_labels() -> dict[str, str]:
    return {
        "suggestions": "生成审批建议",
        "workbuddy_loop": "WorkBuddy 审批批次循环",
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
    binding: dict[str, Any] | None = None,
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
        binding=binding,
    )


def send_stream_task_result_notification(
    settings: dict[str, Any] | None,
    message: DingTalkMessage,
    *,
    root_dir: Path | str | None = None,
    at_all: bool = True,
    timeout_seconds: int = 8,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stream_config = dingtalk_stream_notification_config(settings)
    if not stream_config.get("enabled"):
        return {"sent": False, "skipped": False, "error": "DingTalk Stream is not enabled."}

    binding = default_notification_binding(settings, root_dir) if binding is None else dict(binding)
    session_webhook = str(binding.get("sessionWebhook") or binding.get("session_webhook") or "").strip()
    try:
        expires = float(binding.get("sessionWebhookExpiredTime") or 0)
    except (ValueError, TypeError):
        expires = 0
    if session_webhook and (not expires or expires > time.time() * 1000 + 5000):
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": message.title, "text": message.markdown},
            "at": {"isAtAll": bool(at_all)},
        }
        result = post_with_retry(session_webhook, payload, timeout_seconds)
        if result.get("sent"):
            body = response_body(result)
            if str(body.get("errcode")) == "0":
                return {**result, "channel": "stream_session_webhook", "at_all_requested": at_all}
            return {"sent": False, "delivery_unknown": True, "error": "Session reply acknowledgement is unrecognized; not resent."}
        if result.get("delivery_unknown") or result.get("retryable"):
            return {**result, "channel": "stream_session_webhook"}

    open_conversation_id = str(
        binding.get("openConversationId")
        or binding.get("open_conversation_id")
        or ""
    ).strip()
    robot_code = str(binding.get("robotCode") or "").strip()
    if not open_conversation_id:
        return {"sent": False, "skipped": False, "error": "DingTalk Stream openConversationId is not configured."}
    if not robot_code:
        return {"sent": False, "skipped": False, "error": "DingTalk Stream robotCode is not configured."}

    access_token = get_dingtalk_access_token(stream_config, timeout_seconds)
    if not access_token:
        return {"sent": False, "skipped": False, "error": "Could not get DingTalk access token."}

    payload = {
        "msgParam": json.dumps({"title": message.title, "text": message.markdown}, ensure_ascii=False),
        "msgKey": "sampleMarkdown",
        "openConversationId": open_conversation_id,
        "robotCode": robot_code,
    }
    result = post_with_retry(
        "https://api.dingtalk.com/v1.0/robot/groupMessages/send",
        payload,
        timeout_seconds,
        headers={"x-acs-dingtalk-access-token": access_token},
    )
    receipt = response_body(result).get("processQueryKey")
    if result.get("sent") and (not isinstance(receipt, str) or not receipt.strip()):
        return {"sent": False, "delivery_unknown": True, "error": "Group message acknowledgement is unrecognized; not resent."}
    warnings = ["OpenAPI fallback accepted the summary, but @all is not supported by the verified group-message request schema."] if at_all and result.get("sent") else []
    return {**result, "channel": "stream_openapi", "warnings": warnings, "at_all_requested": at_all, "at_all_supported": False}


def get_dingtalk_access_token(config: dict[str, Any], timeout_seconds: int) -> str:
    client_id = os.getenv(str(config.get("client_id_env") or ""), "").strip()
    client_secret = os.getenv(str(config.get("client_secret_env") or ""), "").strip()
    if not client_id or not client_secret:
        return ""

    payload = {
        "appKey": client_id,
        "appSecret": client_secret,
    }
    url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
    result = post_with_retry(url, payload, timeout_seconds, safe_to_repeat=True)
    if not result.get("sent"):
        return ""
    try:
        body = json.loads(str(result.get("body") or "{}"))
    except json.JSONDecodeError:
        return ""
    token = body.get("accessToken")
    return token.strip() if isinstance(token, str) else ""


def load_stream_binding(root_dir: Path | str | None = None) -> dict[str, Any]:
    path = stream_binding_path(root_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_stream_binding(data: dict[str, Any], root_dir: Path | str | None = None) -> dict[str, Any]:
    path = stream_binding_path(root_dir)
    current = load_stream_binding(root_dir)
    old_id = current.get("openConversationId") or current.get("conversationId")
    new_id = data.get("openConversationId") or data.get("conversationId")
    if new_id and old_id != new_id:
        current = {}
    merged = {**current, **{key: value for key, value in data.items() if value}}
    if not merged:
        return {}
    merged["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(json.dumps(merged, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()
    return merged


def default_notification_binding(settings: dict[str, Any] | None, root_dir: Path | str | None = None) -> dict[str, Any]:
    config = dingtalk_stream_notification_config(settings)
    saved = load_stream_binding(root_dir)
    if str(saved.get("conversationType") or "2") != "2":
        saved = {}
    configured_id = config["open_conversation_id"]
    saved_id = saved.get("openConversationId") or saved.get("conversationId") or ""
    if configured_id and configured_id != saved_id:
        saved = {}
    return {**saved, "openConversationId": configured_id or saved_id,
            "robotCode": config["robot_code"] or saved.get("robotCode", "")}


def task_notification_binding(settings: dict[str, Any] | None, supplied: dict[str, Any] | None,
                              root_dir: Path | str | None = None) -> dict[str, Any]:
    config = dingtalk_stream_notification_config(settings)
    binding = default_notification_binding(settings, root_dir) if supplied is None else dict(supplied)
    keys = ("conversationId", "openConversationId", "sessionWebhook", "sessionWebhookExpiredTime", "robotCode", "conversationType")
    target = {key: binding[key] for key in keys if binding.get(key)}
    if target.get("conversationType") not in {None, "2", 2}:
        return {}
    target["robotCode"] = target.get("robotCode") or config.get("robot_code") or ""
    return target


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
            return classify_response(response.status, body)
    except urllib.error.HTTPError as error:
        with error:
            body = error.read().decode("utf-8", errors="replace")
        return classify_response(error.code, body)
    except Exception:  # A timeout may happen after the server accepted a message.
        return {"sent": False, "delivery_unknown": True, "retryable": False,
                "error": "DingTalk transport failed; delivery outcome is unknown."}


def response_body(result: dict[str, Any]) -> dict[str, Any]:
    try:
        body = json.loads(str(result.get("body") or "{}"))
        return body if isinstance(body, dict) else {}
    except (ValueError, TypeError):
        return {}


def classify_response(status: int, text: str) -> dict[str, Any]:
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        body = None
    if not isinstance(body, dict):
        return {"sent": False, "status": status, "delivery_unknown": 200 <= status < 300 or status >= 500,
                "retryable": status == 429, "error": f"DingTalk returned an invalid JSON acknowledgement (HTTP {status})."}
    code = body.get("errcode", body.get("code"))
    success = 200 <= status < 300 and (code is None or str(code).lower() in {"0", "200", "ok", "success"}) and body.get("success") is not False
    if success:
        return {"sent": True, "status": status, "body": text}
    safe_code = "".join(c for c in str(code or "http_error")[:80] if c.isalnum() or c in "_.-")
    retryable = status == 429 or str(code) in {"90018", "130101", "Throttling", "TooManyRequests"} or str(code).startswith("Throttling.")
    return {"sent": False, "status": status, "code": safe_code, "retryable": retryable,
            "delivery_unknown": status >= 500 and not retryable, "error": f"DingTalk rejected request: {safe_code} (HTTP {status})."}


def post_with_retry(url: str, payload: dict[str, Any], timeout_seconds: int, *,
                    headers: dict[str, str] | None = None, safe_to_repeat: bool = False) -> dict[str, Any]:
    for attempt in range(1, 4):
        result = post_json(url, payload, timeout_seconds, headers=headers) if headers else post_json(url, payload, timeout_seconds)
        if result.get("sent") or not (result.get("retryable") or safe_to_repeat and result.get("delivery_unknown")):
            return {**result, "attempts": attempt}
        if attempt < 3:
            time.sleep(2 ** (attempt - 1))
    return {**result, "attempts": 3}
