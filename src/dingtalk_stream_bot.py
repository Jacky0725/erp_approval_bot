from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dingtalk_state import DingTalkState
from dingtalk_transport import StreamConnection
from dingtalk_notifier import post_with_retry, response_body
from dingtalk_conversation import ChatUnavailable, ConversationStore, chat_config, contains_secret, infer_intent
from audit_logger import AuditLogger
from task_control import STOPPABLE_ACTIONS, TaskControl, can_stop


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
    pipeline: str = "workbuddy"


def parse_stream_command(text: str) -> StreamCommand:
    cleaned = normalize_command_text(text)
    if not cleaned:
        return StreamCommand(kind=COMMAND_UNKNOWN, raw_text=text)
    if cleaned.lower() in {"help", "/help", "帮助", "菜单"}:
        return StreamCommand(kind=COMMAND_HELP, raw_text=text)
    if cleaned in {"状态", "任务状态", "运行状态"} or cleaned.lower() in {"status", "/status"}:
        return StreamCommand(kind=COMMAND_STATUS, raw_text=text)
    match = re.fullmatch(r"(?:执行审批|开始审批|启动审批)\s*(V1|WorkBuddy)?(?:\s+(SJ\d+))?", cleaned, re.IGNORECASE)
    if match:
        pipeline = "v1" if (match.group(1) or "").lower() == "v1" else "workbuddy"
        return StreamCommand(kind=COMMAND_RUN, list_number=(match.group(2) or "").upper(), raw_text=text, pipeline=pipeline)
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
        "sessionWebhookExpiredTime": data.get("sessionWebhookExpiredTime") or "",
        "robotCode": str(data.get("robotCode") or "").strip(),
        "senderStaffId": str(data.get("senderStaffId") or "").strip(),
        "conversationType": str(data.get("conversationType") or "").strip(),
    }


def help_text() -> str:
    return "\n".join(
        [
            "### 试剂审批机器人",
            "- 状态",
            "- 执行审批：WorkBuddy，全量待办",
            "- 执行审批 <清单号>：WorkBuddy，指定清单",
            "- 执行审批V1：V1，全量待办",
            "- 执行审批V1 <清单号>：V1，指定清单",
            "- 帮助",
            "- 停止审批 [任务编号]；确认停止 <确认码>",
            "- 取消：放弃待确认操作；清空对话：清除上下文",
        ]
    )


class DingTalkCommandService:
    def __init__(
        self,
        *,
        job_manager: Any,
        run_options_builder: Callable[[str, str], dict[str, str]],
        status_provider: Callable[[], dict[str, Any]],
        settings_loader: Callable[[], dict[str, Any]] | None = None,
        intent_resolver: Callable[..., dict[str, str]] = infer_intent,
        clock: Callable[[], float] = time.monotonic,
        audit: Callable[[dict[str, Any]], None] | None = None,
        root_dir: Path | str | None = None,
    ) -> None:
        self.job_manager = job_manager
        self.run_options_builder = run_options_builder
        self.status_provider = status_provider
        self.settings_loader = settings_loader or (lambda: {})
        self.intent_resolver = intent_resolver
        self.clock = clock
        self.audit = audit or (lambda event: None)
        self.conversations = ConversationStore(clock=clock)
        self._guard = threading.RLock()
        self._pending: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._pending_stop: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
        self._deliveries: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()
        self.state = DingTalkState(root_dir) if root_dir is not None else None
        self.task_control = TaskControl(root_dir) if root_dir is not None else None

    def handle_message(self, data: dict[str, Any]) -> str:
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                return "消息格式无法识别，本次未执行。"
        if not isinstance(data, dict):
            return "消息格式无法识别，本次未执行。"
        conversation = str(data.get("conversationId") or "")
        sender = str(data.get("senderStaffId") or data.get("senderId") or "")
        message_id = str(data.get("msgId") or "")
        text = extract_message_text(data)
        effectful = parse_stream_command(text).kind == COMMAND_RUN or bool(re.fullmatch(r"确认(?:停止)?\s+[0-9a-fA-F]{6}", normalize_command_text(text)))
        if self.state and effectful and (not conversation or not message_id):
            return "消息缺少可靠标识，本次未执行审批；请在钉钉中重新发送。"
        delivery = (conversation, message_id)
        if message_id:
            with self._guard:
                now = self.clock()
                for key, (created, _) in list(self._deliveries.items()):
                    if now - created > 600:
                        del self._deliveries[key]
                if delivery in self._deliveries:
                    return self._deliveries[delivery][1]
                if self.state:
                    try:
                        if not self.state.claim(conversation, message_id, effectful=effectful):
                            return "该消息已接收，不会重复执行。请发送“状态”核对结果；如需新任务，请重新发送新的审批指令。"
                    except Exception:
                        if effectful:
                            return "消息去重记录无法保存，本次未执行；请检查本地数据目录。"
                        self._audit((conversation, sender), "dedup_unavailable_readonly")
                self._deliveries[delivery] = (now, "该消息正在处理中，请勿重复提交。")
                while len(self._deliveries) > 1024:
                    self._deliveries.popitem(last=False)
        try:
            target = stream_binding_from_message(data) if str(data.get("conversationType")) == "2" else None
            actor = {"source": "dingtalk", "corp_id": str(data.get("senderCorpId") or data.get("chatbotCorpId") or dingtalk_stream_config(self.settings_loader()).get("corp_id") or ""),
                     "user_id": sender, "conversation_id": conversation, "conversation_type": str(data.get("conversationType") or "")}
            response = self.handle_text(text, session_key=(conversation, sender), notification_target=target, actor=actor)
        except Exception:
            response = "消息处理异常，请使用“状态”核对任务结果；不要重复执行审批。"
        if message_id:
            with self._guard:
                self._deliveries[delivery] = (self.clock(), response)
            if self.state:
                try:
                    self.state.complete(conversation, message_id)
                except Exception:
                    self._audit((conversation, sender), "dedup_completion_failed")
        return response

    def handle_text(self, text: str, *, session_key: tuple[str, str] = ("", ""),
                    notification_target: dict[str, Any] | None = None, actor: dict[str, str] | None = None) -> str:
        command = parse_stream_command(text)
        cleaned = normalize_command_text(text)
        stop = re.fullmatch(r"停止审批(?:\s+([0-9a-fA-F]{12}|[0-9a-fA-F]{32}))?", cleaned)
        if stop:
            return self.propose_stop(session_key, actor or {}, (stop.group(1) or "").lower())
        stop_confirmation = re.fullmatch(r"确认停止\s+([0-9a-fA-F]{6})", cleaned)
        if stop_confirmation:
            return self.confirm_stop(session_key, actor or {}, stop_confirmation.group(1).upper())
        if cleaned.startswith(("停止审批", "确认停止")):
            return "停止指令格式有误，请使用“停止审批”或“确认停止 <确认码>”。"
        confirmation = re.fullmatch(r"确认\s+([0-9a-fA-F]{6})", cleaned)
        if confirmation:
            return self.confirm_approval(session_key, confirmation.group(1).upper(), notification_target=notification_target, actor=actor)
        if cleaned in {"取消", "清空对话"}:
            with self.conversations.lock:
                self.conversations.clear(session_key)
                with self._guard:
                    self._pending.pop(session_key, None)
                    self._pending_stop.pop(session_key, None)
            return "已清除待确认操作" + ("和对话上下文" if cleaned == "清空对话" else "") + "；不会停止已运行的任务。"
        if command.kind == COMMAND_HELP:
            return help_text()
        if command.kind == COMMAND_STATUS:
            return self.status_text()
        if command.kind == COMMAND_RUN:
            with self._guard:
                self._pending.pop(session_key, None)
            response = self.start_approval(command.list_number, command.pipeline, notification_target=notification_target, actor=actor)
            self._audit(session_key, "exact_run", pipeline=command.pipeline, list_number=command.list_number,
                        started=response.startswith("已启动审批任务"))
            return response
        # A malformed precise command must not silently become an LLM request.
        if re.match(r"^(执行审批|开始审批|启动审批)", cleaned):
            return "指令格式有误，本次未执行。\n\n" + help_text()
        return self.chat(cleaned, session_key, notification_target=notification_target, actor=actor)

    def propose_stop(self, key: tuple[str, str], actor: dict[str, str], requested: str) -> str:
        status = self.status_provider() or {}
        task_id = str(status.get("task_id") or "")
        if not status.get("running") or not task_id or requested and not task_id.startswith(requested.lower()):
            return "没有匹配的运行中任务，本次未停止任何任务。"
        if not self.task_control:
            return "安全停止控制未配置，本次未执行。"
        task = self.task_control.get(task_id)
        allowed = can_stop(task, actor, self.settings_loader())
        self._audit(key, "safe_stop_proposal", task_id=task_id, allowed=allowed)
        if not allowed:
            return "仅任务发起人或已配置管理员可在授权群停止此任务。"
        action = task["action"]
        if action not in STOPPABLE_ACTIONS:
            return "当前任务不支持安全停止。"
        if task["state"] == "stop_requested":
            return "停止请求已受理，正在等待安全停止。"
        if task["state"] != "running":
            return "任务状态已变化，本次未生成停止确认。"
        code = secrets.token_hex(3).upper()
        with self.conversations.lock:
            self.conversations.clear(key)
            with self._guard:
                self._pending.pop(key, None)
                self._pending_stop[key] = {"task_id": task_id, "code": code, "expires": self.clock() + 120, "actor": dict(actor)}
                self._pending_stop.move_to_end(key)
                while len(self._pending_stop) > 128:
                    self._pending_stop.popitem(last=False)
        details = task.get("details") or {}
        current_list = status.get("current_list_number") or details.get("current_list") or "-"
        return (
            f"待确认停止，任务仍在运行\n- 任务：{task_id[:12]}\n"
            f"- 链路：{'V1' if action == 'suggestions' else 'WorkBuddy'}\n"
            f"- 当前清单：{current_list}\n- 当前阶段：{task.get('phase') or '-'}\n"
            f"- 已保存结果不会撤销。\n请本人在本会话2分钟内发送“确认停止 {code}”；发送“取消”放弃。"
        )

    def confirm_stop(self, key: tuple[str, str], actor: dict[str, str], code: str) -> str:
        with self._guard:
            pending = self._pending_stop.get(key)
            if not pending or pending["expires"] <= self.clock():
                self._pending_stop.pop(key, None)
                return "停止确认已过期或不存在，请重新请求。"
            if not secrets.compare_digest(pending["code"], code) or pending["actor"] != actor:
                return "确认码或身份不匹配，本次未执行。"
            self._pending_stop.pop(key)
        result = self.job_manager.request_safe_stop(pending["task_id"], actor)
        self._audit(key, "safe_stop_confirmed", task_id=pending["task_id"], requested=bool(result.get("requested")))
        return str(result.get("message") or "停止请求未生效。")

    def _audit(self, key: tuple[str, str], event: str, **fields: Any) -> None:
        try:
            identity = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]
            self.audit({"event": "dingtalk_command", "session": identity, "operation": event, **fields})
        except Exception:
            logging.getLogger(__name__).warning("Could not write DingTalk command audit record")

    def chat(self, text: str, key: tuple[str, str], *, notification_target: dict[str, Any] | None = None, actor: dict[str, str] | None = None) -> str:
        settings = self.settings_loader()
        config = chat_config(settings)
        if not config["enabled"]:
            return "自然语言对话未启用。\n\n" + help_text()
        if not all(key):
            return "无法识别当前会话或发送人，请在钉钉中重新发送消息。"
        if not text or len(text) > 2000:
            return "请将消息控制在 2000 字以内。"
        if contains_secret(text):
            return "请勿发送密码或 API Key；本条消息未提交给对话模型。已泄露的凭证请及时更换。"
        try:
            history, generation = self.conversations.begin(key, config)
        except ChatUnavailable as error:
            return str(error)
        response = ""
        remember = False
        with self._guard:
            self._pending.pop(key, None)
        try:
            result = self.intent_resolver(settings, text, history, self.status_provider() or {})
            with self.conversations.lock:
                if not self.conversations.is_current(key, generation):
                    return "该次对话请求已取消，本次未执行。"
            intent = result.get("intent")
            if intent == "status":
                response = self.status_text()
            elif intent == "help":
                response = help_text()
            elif intent == "run":
                pipeline = result.get("pipeline", "workbuddy")
                number = result.get("list_number", "").upper()
                if pipeline not in {"workbuddy", "v1"} or (number and (not re.fullmatch(r"SJ\d+", number) or number not in text.upper())):
                    response = "未能可靠识别审批参数，请明确链路和清单号，或使用精确指令。"
                else:
                    with self.conversations.lock:
                        if not self.conversations.is_current(key, generation):
                            return "该次对话请求已取消，本次未执行。"
                        response = self.propose_approval(key, number, pipeline, notification_target=notification_target, actor=actor)
            elif intent == "chat":
                reply = str(result.get("reply") or "请说明你要查询状态、执行审批还是了解操作。").strip()[:1200]
                response = "对话回复（未执行任务）：\n" + reply if not contains_secret(reply) else "回复包含疑似敏感信息，已拦截，本次未执行。"
                remember = not contains_secret(reply)
            else:
                response = "无法识别请求，本次未执行；请使用“帮助”查看指令。"
            self._audit(key, "natural_language", intent=intent)
            return response
        except ChatUnavailable as error:
            return str(error)
        except Exception:
            return "对话处理失败，本次未执行；精确指令仍可使用。"
        finally:
            self.conversations.finish(key, text, response, remember=remember, generation=generation)

    def propose_approval(self, key: tuple[str, str], number: str, pipeline: str, *,
                          notification_target: dict[str, Any] | None = None, actor: dict[str, str] | None = None) -> str:
        if (self.status_provider() or {}).get("running"):
            return "当前已有审批任务正在运行，请稍后再试。"
        try:
            options = self.run_options_builder(number, pipeline)
        except (ValueError, RuntimeError) as error:
            return f"无法准备审批：{error}"
        code = secrets.token_hex(3).upper()
        with self._guard:
            now = self.clock()
            for old_key, pending in list(self._pending.items()):
                if pending["expires"] <= now:
                    del self._pending[old_key]
            self._pending[key] = {"code": code, "expires": now + 120, "number": number, "pipeline": pipeline, "options": dict(options)}
            self._pending[key]["notification_target"] = dict(notification_target) if notification_target is not None else None
            self._pending[key]["actor"] = dict(actor) if actor is not None else None
            self._pending.move_to_end(key)
            while len(self._pending) > 128:
                self._pending.popitem(last=False)
        scope = number or "本次首次读取的全部待办（固定范围）"
        trial = "；试运行，不回写 ERP" if options.get("DINGTALK_TRIAL_MODE") == "true" else ""
        return f"待确认，尚未启动\n- 链路：{'WorkBuddy' if pipeline == 'workbuddy' else 'V1'}\n- 范围：{scope}\n- 写入模式：{options.get('APPROVAL_WRITE_MODE', 'disabled')}{trial}\n请由本人在本会话 2 分钟内发送“确认 {code}”；发送“取消”放弃。"

    def confirm_approval(self, key: tuple[str, str], code: str, *, notification_target: dict[str, Any] | None = None, actor: dict[str, str] | None = None) -> str:
        if not all(key):
            return "无法识别会话身份，本次未执行。"
        if not chat_config(self.settings_loader())["enabled"]:
            with self._guard:
                self._pending.pop(key, None)
            return "自然语言对话已关闭，待确认操作已失效。"
        with self._guard:
            pending = self._pending.get(key)
            if not pending or pending["expires"] <= self.clock():
                self._pending.pop(key, None)
                return "确认已过期或不存在，请重新提出审批请求。"
            if not secrets.compare_digest(pending["code"], code):
                return "确认码不正确，本次未执行。"
            if pending.get("actor") != actor:
                return "确认身份已变化，本次未执行。"
            self._pending.pop(key)
        try:
            current = self.run_options_builder(pending["number"], pending["pipeline"])
        except (ValueError, RuntimeError) as error:
            return f"审批任务未启动：{error}"
        if current != pending["options"]:
            return "审批配置已变化，本次未执行，请重新提出请求并核对参数。"
        target = pending.get("notification_target")
        if target is not None and notification_target is not None and target.get("conversationId") == notification_target.get("conversationId"):
            target = notification_target
        response = self.start_approval(pending["number"], pending["pipeline"], options=current, notification_target=target, actor=actor)
        self._audit(key, "confirmed_run", pipeline=pending["pipeline"], list_number=pending["number"],
                    started=response.startswith("已启动审批任务"))
        return response

    def status_text(self) -> str:
        status = self.status_provider() or {}
        running = bool(status.get("running"))
        lines = [
            "### 当前任务状态",
            f"- 状态：{'运行中' if running else status.get('result_label') or '空闲'}",
            f"- 当前任务：{status.get('action_label') or status.get('action') or '-'}",
            f"- 开始时间：{status.get('started_at') or '-'}",
            f"- 结束时间：{status.get('finished_at') or '-'}",
        ]
        if running and status.get("action") in {"suggestions", "workbuddy_loop"}:
            lines.append(f"- 审批链路：{'WorkBuddy' if status.get('action') == 'workbuddy_loop' else 'V1'}")
            list_number = str(status.get("current_list_number") or "").strip()
            label = "当前批次涉及清单" if status.get("action") == "workbuddy_loop" else "正在审批清单号"
            lines.append(f"- {label}：{list_number or '正在获取待办清单'}")
            if status.get("action") == "workbuddy_loop":
                for line in reversed(status.get("log_tail") or []):
                    if "[workbuddy progress]" in line:
                        lines.append("- 当前阶段：" + line.split("[workbuddy progress]", 1)[1].strip())
                        break
        if status.get("task_id"):
            lines.append(f"- 任务编号：{status['task_id'][:12]}")
        control = status.get("control") or {}
        if control:
            lines.append(f"- 控制状态：{control.get('label') or '-'}")
            lines.append(f"- 操作阶段：{control.get('phase') or '-'}")
        error = str(status.get("error") or "").strip()
        if error:
            lines.append(f"- 错误：{error[:220]}")
        return "\n".join(lines)

    def start_approval(self, list_number: str = "", pipeline: str = "workbuddy", *, options: dict[str, str] | None = None,
                       notification_target: dict[str, Any] | None = None, actor: dict[str, str] | None = None) -> str:
        if (self.status_provider() or {}).get("running"):
            return "当前已有审批任务正在运行，请稍后再试。"
        try:
            options = options if options is not None else self.run_options_builder(list_number, pipeline)
        except (ValueError, RuntimeError) as error:
            return f"审批任务未启动：{error}"
        kwargs = {"notification_target": dict(notification_target)} if notification_target is not None else {}
        if actor is not None:
            kwargs["actor"] = dict(actor)
        result = self.job_manager.start("workbuddy_loop" if pipeline == "workbuddy" else "suggestions", options, **kwargs)
        if result.get("started"):
            scope = f"清单 {list_number}" if list_number else "本次首次读取的 ERP 全部待办清单（固定范围）"
            mode = options.get("APPROVAL_WRITE_MODE", "disabled")
            trial = options.get("DINGTALK_TRIAL_MODE") == "true"
            trial_text = "\n- 试运行：不回写 ERP" + ("；WorkBuddy 仅执行单批。" if pipeline == "workbuddy" else "。") if trial else ""
            return f"已启动审批任务\n- 链路：{'WorkBuddy' if pipeline == 'workbuddy' else 'V1'}\n- 范围：{scope}\n- 写入模式：{mode}" + trial_text
        return str(result.get("message") or "审批任务未启动。")


class DingTalkStreamBot:
    def __init__(
        self,
        *,
        settings_loader: Callable[[], dict[str, Any]],
        job_manager: Any,
        run_options_builder: Callable[[str, str], dict[str, str]],
        status_provider: Callable[[], dict[str, Any]],
        root_dir: Path | str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings_loader = settings_loader
        self.command_service = DingTalkCommandService(
            job_manager=job_manager,
            run_options_builder=run_options_builder,
            status_provider=status_provider,
            settings_loader=settings_loader,
            audit=self._audit_event,
            root_dir=root_dir,
        )
        self.logger = logger or logging.getLogger(__name__)
        self.root_dir = Path(root_dir) if root_dir is not None else None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._lock = threading.RLock()
        self._running = False
        self._last_error = ""
        self._started_at = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connection: StreamConnection | None = None
        self._stop_requested = threading.Event()
        self._restart_requested = False
        self._connected = False
        self._connection_state = "stopped"
        self._last_connected_at = ""
        self._last_received_at = ""
        self._reconnect_count = 0

    def _audit_event(self, event: dict[str, Any]) -> None:
        root = self.root_dir
        if root is not None:
            AuditLogger.from_settings(self.settings_loader(), root).info(json.dumps(event, ensure_ascii=False))

    def start(self) -> None:
        config = dingtalk_stream_config(self.settings_loader())
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._last_error = ""
            if not config.get("enabled"):
                self._running = False
                self._connected = False
                self._connection_state = "disabled"
                return
            self._stop_requested.clear()
            self._connection_state = "starting"
            self._thread = threading.Thread(target=self._run, name="dingtalk-stream-bot", daemon=True)
            self._thread.start()

    def stop(self, *, restart: bool = False) -> bool:
        self._stop_requested.set()
        with self._lock:
            self._restart_requested = restart
            loop, connection, thread = self._loop, self._connection, self._thread
            self._connection_state = "stopping" if thread and thread.is_alive() else "stopped"
        if loop and connection and loop.is_running():
            coroutine = connection.stop()
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except RuntimeError:
                coroutine.close()
            else:
                with contextlib.suppress(Exception):
                    future.result(timeout=4)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=15)
        return not (thread and thread.is_alive())

    def reload(self) -> None:
        if self.stop(restart=True):
            with self._lock:
                self._restart_requested = False
            self.start()

    def _observe_connection(self, state: str, *, error: str = "") -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            if state == "received":
                self._last_received_at = now
                return
            if state == "message_error":
                self._last_error = f"Stream message handling failed ({error})."
                return
            self._connection_state = state
            self._connected = state == "connected"
            if self._connected:
                self._last_connected_at = now
                self._last_error = ""
            if state == "reconnecting":
                self._reconnect_count += 1
            if error:
                self._last_error = f"Stream connection failed ({error}); reconnecting."

    def status(self) -> dict[str, Any]:
        settings = self.settings_loader()
        config = dingtalk_stream_config(settings)
        chat = chat_config(settings)
        with self._lock:
            alive = bool(self._thread and self._thread.is_alive())
            return {
                "enabled": bool(config.get("enabled")),
                "running": alive,
                "connected": self._connected and alive,
                "connection_state": "stopping" if self._stop_requested.is_set() and alive else self._connection_state,
                "last_connected_at": self._last_connected_at,
                "last_received_at": self._last_received_at,
                "reconnect_count": self._reconnect_count,
                "reload_pending": self._restart_requested,
                "started_at": self._started_at,
                "last_error": self._last_error,
                "client_id_configured": bool(os.getenv(str(config.get("client_id_env")), "").strip()),
                "client_secret_configured": bool(os.getenv(str(config.get("client_secret_env")), "").strip()),
                "api_token_configured": bool(os.getenv(str(config.get("api_token_env")), "").strip()),
                "chat_enabled": chat["enabled"],
                "chat_model": chat["model"],
                "chat_configured": bool(chat["base_url"] and chat["model"] and (chat["api_key"] or not chat["requires_api_key"])),
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
        class ApprovalBotHandler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback: Any):  # noqa: ANN001
                data = callback.data
                if not should_process_message(data, config):
                    return AckMessage.STATUS_OK, "OK"
                response = await asyncio.to_thread(service.handle_message, data)
                try:
                    incoming_message = dingtalk_stream.ChatbotMessage.from_dict(data)
                    reply = await asyncio.to_thread(post_with_retry, incoming_message.session_webhook,
                        {"msgtype": "text", "text": {"content": response}, "at": {"atUserIds": [incoming_message.sender_staff_id]}}, 8)
                    if not reply.get("sent") or str(response_body(reply).get("errcode")) != "0":
                        logger.warning("DingTalk command reply was rejected or unconfirmed; approval was not retried.")
                except Exception:  # noqa: BLE001
                    logger.warning("DingTalk Stream reply failed; approval was not retried.")
                return AckMessage.STATUS_OK, "OK"

        try:
            credential = dingtalk_stream.Credential(client_id, credential_secret)
            client = dingtalk_stream.DingTalkStreamClient(credential, logger=logger)
            client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, ApprovalBotHandler())
            with self._lock:
                self._client = client
                self._running = True
                self._started_at = datetime.now().isoformat(timespec="seconds")
            async def run_connection() -> None:
                connection = StreamConnection(client, self._observe_connection)
                with self._lock:
                    self._loop = asyncio.get_running_loop()
                    self._connection = connection
                if self._stop_requested.is_set():
                    await connection.stop()
                await connection.run()
            asyncio.run(run_connection())
        except Exception as exc:  # noqa: BLE001
            self._set_error(f"Stream stopped unexpectedly ({type(exc).__name__}).")
        finally:
            with self._lock:
                self._running = False
                self._connected = False
                self._client = None
                self._loop = None
                self._connection = None
                self._connection_state = "stopped"
                restart = self._restart_requested
                self._restart_requested = False
                if self._thread is threading.current_thread():
                    self._thread = None
            if restart:
                self.start()

    def _set_error(self, message: str) -> None:
        self.logger.error("DingTalk Stream bot error: %s", message)
        with self._lock:
            self._last_error = message
            self._running = False
            self._connected = False
            self._connection_state = "error"
