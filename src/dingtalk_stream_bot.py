from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dingtalk_notifier import save_stream_binding


COMMAND_HELP = "help"
COMMAND_RUN = "run"
COMMAND_STATUS = "status"
COMMAND_UNKNOWN = "unknown"


def dingtalk_stream_defaults() -> dict[str, Any]:
    return {
        "enabled": False,
        "corp_id": "",
        "agent_id": "",
        "robot_code": "",
        "open_conversation_id": "",
        "client_id_env": "DINGTALK_STREAM_CLIENT_ID",
        "client_secret_env": "DINGTALK_STREAM_CLIENT_SECRET",
        "api_token_env": "DINGTALK_STREAM_API_TOKEN",
        "require_at_in_group": True,
    }


def dingtalk_stream_config(settings: dict[str, Any] | None) -> dict[str, Any]:
    configured = (settings or {}).get("dingtalk_stream", {}) or {}
    result = dingtalk_stream_defaults()
    result.update(configured)
    result["enabled"] = coerce_bool(result.get("enabled"))
    result["require_at_in_group"] = coerce_bool(result.get("require_at_in_group", True))
    return result


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class StreamCommand:
    kind: str
    list_number: str = ""
    raw_text: str = ""


def parse_stream_command(text: str) -> StreamCommand:
    cleaned = normalize_command_text(text)
    if not cleaned:
        return StreamCommand(kind=COMMAND_UNKNOWN, raw_text=text)
    if cleaned.lower() in {"help", "/help", "帮助", "菜单"}:
        return StreamCommand(kind=COMMAND_HELP, raw_text=text)
    if cleaned in {"状态", "任务状态", "运行状态"} or cleaned.lower() in {"status", "/status"}:
        return StreamCommand(kind=COMMAND_STATUS, raw_text=text)
    for prefix in ("执行审批", "开始审批", "启动审批"):
        if cleaned == prefix:
            return StreamCommand(kind=COMMAND_RUN, raw_text=text)
        if cleaned.startswith(prefix + " "):
            list_number = cleaned[len(prefix) :].strip().split()[0]
            return StreamCommand(kind=COMMAND_RUN, list_number=list_number, raw_text=text)
    return StreamCommand(kind=COMMAND_UNKNOWN, raw_text=text)


def normalize_command_text(text: str) -> str:
    value = str(text or "").replace("\u3000", " ").strip()
    value = re.sub(r"^\s*@\S+\s*", "", value)
    return re.sub(r"\s+", " ", value).strip()


def extract_message_text(data: Any) -> str:
    if isinstance(data, str):
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(data)
    if not isinstance(data, dict):
        return ""
    text = data.get("text")
    if isinstance(text, dict):
        return str(text.get("content") or "").strip()
    if isinstance(text, str):
        return text.strip()
    content = data.get("content")
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "").strip()
    return str(content or "").strip()


def should_process_message(data: Any, config: dict[str, Any]) -> bool:
    if isinstance(data, str):
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(data)
    if not isinstance(data, dict):
        return False
    if str(data.get("conversationType") or "") == "2" and config.get("require_at_in_group", True):
        return coerce_bool(data.get("isInAtList"))
    return True


def stream_binding_from_message(data: Any) -> dict[str, Any]:
    if isinstance(data, str):
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(data)
    if not isinstance(data, dict):
        return {}
    return {
        "conversationId": str(data.get("conversationId") or "").strip(),
        "openConversationId": str(data.get("openConversationId") or data.get("conversationId") or "").strip(),
        "sessionWebhook": str(data.get("sessionWebhook") or "").strip(),
        "robotCode": str(data.get("robotCode") or "").strip(),
        "senderStaffId": str(data.get("senderStaffId") or "").strip(),
        "conversationType": str(data.get("conversationType") or "").strip(),
    }


def help_text() -> str:
    return "\n".join(
        [
            "### 试剂审批机器人",
            "- 状态",
            "- 执行审批",
            "- 执行审批 <清单号>",
            "- 帮助",
        ]
    )


class DingTalkCommandService:
    def __init__(
        self,
        *,
        job_manager: Any,
        run_options_builder: Callable[[str], dict[str, str]],
        status_provider: Callable[[], dict[str, Any]],
    ) -> None:
        self.job_manager = job_manager
        self.run_options_builder = run_options_builder
        self.status_provider = status_provider

    def handle_text(self, text: str) -> str:
        command = parse_stream_command(text)
        if command.kind == COMMAND_HELP:
            return help_text()
        if command.kind == COMMAND_STATUS:
            return self.status_text()
        if command.kind == COMMAND_RUN:
            return self.start_approval(command.list_number)
        return "未识别命令。\n\n" + help_text()

    def status_text(self) -> str:
        status = self.status_provider() or {}
        running = bool(status.get("running"))
        lines = [
            "### 当前任务状态",
            f"- 状态：{'运行中' if running else status.get('result_label') or '空闲'}",
            f"- 当前任务：{status.get('action') or '-'}",
            f"- 开始时间：{status.get('started_at') or '-'}",
            f"- 结束时间：{status.get('finished_at') or '-'}",
        ]
        error = str(status.get("error") or "").strip()
        if error:
            lines.append(f"- 错误：{error[:220]}")
        return "\n".join(lines)

    def start_approval(self, list_number: str = "") -> str:
        if (self.status_provider() or {}).get("running"):
            return "当前已有审批任务正在运行，请稍后再试。"
        options = self.run_options_builder(list_number)
        result = self.job_manager.start("suggestions", options)
        if result.get("started"):
            scope = f"清单 {list_number}" if list_number else "ERP 当前全部待办清单"
            return f"已启动审批任务：{scope}。"
        return str(result.get("message") or "审批任务未启动。")


class DingTalkStreamBot:
    def __init__(
        self,
        *,
        settings_loader: Callable[[], dict[str, Any]],
        job_manager: Any,
        run_options_builder: Callable[[str], dict[str, str]],
        status_provider: Callable[[], dict[str, Any]],
        root_dir: Path | str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings_loader = settings_loader
        self.command_service = DingTalkCommandService(
            job_manager=job_manager,
            run_options_builder=run_options_builder,
            status_provider=status_provider,
        )
        self.logger = logger or logging.getLogger(__name__)
        self.root_dir = Path(root_dir) if root_dir is not None else None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._lock = threading.RLock()
        self._running = False
        self._last_error = ""
        self._started_at = ""

    def start(self) -> None:
        config = dingtalk_stream_config(self.settings_loader())
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._last_error = ""
            if not config.get("enabled"):
                self._running = False
                return
            self._thread = threading.Thread(target=self._run, name="dingtalk-stream-bot", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            client = self._client
            self._running = False
        close = getattr(client, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()

    def reload(self) -> None:
        self.stop()
        self.start()

    def status(self) -> dict[str, Any]:
        config = dingtalk_stream_config(self.settings_loader())
        with self._lock:
            alive = bool(self._thread and self._thread.is_alive())
            return {
                "enabled": bool(config.get("enabled")),
                "running": alive,
                "started_at": self._started_at,
                "last_error": self._last_error,
                "client_id_configured": bool(os.getenv(str(config.get("client_id_env")), "").strip()),
                "client_secret_configured": bool(os.getenv(str(config.get("client_secret_env")), "").strip()),
                "api_token_configured": bool(os.getenv(str(config.get("api_token_env")), "").strip()),
            }

    def _run(self) -> None:
        try:
            import dingtalk_stream
            from dingtalk_stream import AckMessage
        except Exception as exc:  # noqa: BLE001
            self._set_error(f"dingtalk-stream SDK 未安装或无法导入：{exc}")
            return

        config = dingtalk_stream_config(self.settings_loader())
        client_id = os.getenv(str(config.get("client_id_env")), "").strip()
        client_secret = os.getenv(str(config.get("client_secret_env")), "").strip()
        api_token = os.getenv(str(config.get("api_token_env")), "").strip()
        credential_secret = client_secret or api_token
        if not client_id or not credential_secret:
            self._set_error("钉钉 Stream 缺少 Client ID 或 Client Secret/API Token。")
            return

        service = self.command_service
        logger = self.logger
        root_dir = self.root_dir

        class ApprovalBotHandler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback: Any):  # noqa: ANN001
                data = callback.data
                if not should_process_message(data, config):
                    return AckMessage.STATUS_OK, "OK"
                binding = stream_binding_from_message(data)
                if binding:
                    try:
                        save_stream_binding(binding, root_dir)
                    except Exception as error:  # noqa: BLE001
                        logger.warning("Could not save DingTalk Stream group binding: %s", error)
                text = extract_message_text(data)
                response = service.handle_text(text)
                try:
                    incoming_message = dingtalk_stream.ChatbotMessage.from_dict(data)
                    self.reply_text(response, incoming_message)
                except Exception as error:  # noqa: BLE001
                    logger.exception("DingTalk Stream reply failed: %s", error)
                return AckMessage.STATUS_OK, "OK"

        try:
            credential = dingtalk_stream.Credential(client_id, credential_secret)
            client = dingtalk_stream.DingTalkStreamClient(credential)
            client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, ApprovalBotHandler())
            with self._lock:
                self._client = client
                self._running = True
                self._started_at = datetime.now().isoformat(timespec="seconds")
            client.start_forever()
        except Exception as exc:  # noqa: BLE001
            self._set_error(str(exc) or exc.__class__.__name__)
        finally:
            with self._lock:
                self._running = False
                self._client = None

    def _set_error(self, message: str) -> None:
        self.logger.error("DingTalk Stream bot error: %s", message)
        with self._lock:
            self._last_error = message
            self._running = False
