from __future__ import annotations

import contextlib
import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from collections.abc import Callable

import pandas as pd
import yaml
from dotenv import load_dotenv

from approval_suggestion_metrics import aggregate_suggestion_summaries
from llm_providers import (
    configured_llm_api_key,
    get_llm_provider,
    provider_base_url,
    provider_default_model,
    provider_options,
)
from dingtalk_notifier import (
    build_task_result_message,
    dingtalk_notification_config,
    send_task_result_notification,
)
from dingtalk_stream_bot import dingtalk_stream_config
from category_mapper import (
    category_mapping_summary,
    erp_property_options,
    is_non_writable_rule_category,
    to_erp_property,
    to_rule_category,
)
from app_info import app_version
from memory_sync import MemorySyncError, MemorySyncService, memory_sync_config
from reagent_memory import ReagentMemory
from review_queue import migrate_pending_review_reasons, review_display_summary_from_row
from runtime_paths import ensure_runtime_layout, runtime_root, source_root
from scheduler import scheduler_config


ensure_runtime_layout()
ROOT_DIR = runtime_root()
SOURCE_ROOT = source_root()
CONFIG_PATH = ROOT_DIR / "config" / "settings.yaml"
ENV_PATH = ROOT_DIR / ".env"
LOG_DIR = ROOT_DIR / "data" / "logs"
RUN_LOG_DIR = LOG_DIR / "runs"
REVIEW_QUEUE_PATH = ROOT_DIR / "data" / "review_queue.xlsx"
WEB_RUN_STATE_PATH = LOG_DIR / "web_run_state.yaml"
TODO_TASKS_PATH = LOG_DIR / "todo_tasks.xlsx"
TODO_TASKS_JSON_PATH = LOG_DIR / "todo_tasks.json"
ALLOWED_WEB_WRITE_MODES = {"disabled", "multi_page", "generate_library"}

ACTION_LABELS = {
    "suggestions": "审批流程",
    "todo_export": "待办清单刷新",
    "debug_capture": "首页采集",
    "judgement_capture": "试剂判定页采集",
}

WEB_WRITE_MODE_LABELS = {
    "disabled": "禁用网页写入",
    "multi_page": "全清单分页保存",
    "generate_library": "保存并生成试剂库",
}
RETIRED_WEB_WRITE_MODES = {"test_one", "save_one", "single_page", ""}


WORKFLOW_STEPS = [
    {"id": "login", "label": "登录 ERP"},
    {"id": "judgement", "label": "进入试剂判定"},
    {"id": "auto_match", "label": "一键匹配"},
    {"id": "sort_read", "label": '排序读取 "-"'},
    {"id": "search", "label": "网站查询"},
    {"id": "llm", "label": "大模型整理"},
    {"id": "rule", "label": "规则判定"},
    {"id": "write", "label": "网页写入"},
]

_FILE_CACHE_LOCK = threading.Lock()
_FILE_CACHE: dict[tuple[str, str], tuple[tuple[bool, int, int], Any]] = {}


def normalize_web_write_mode(value: Any, *, default: str = "disabled") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in ALLOWED_WEB_WRITE_MODES:
        return normalized
    if normalized in RETIRED_WEB_WRITE_MODES:
        return "disabled"
    return default if default in ALLOWED_WEB_WRITE_MODES else "disabled"


BLOCKING_REVIEW_STATUSES = {
    "",
    "pending",
    "manual_review",
    "open",
    "todo",
    "待处理",
    "待复核",
    "人工复核",
    "需人工复核",
}


def load_settings() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def save_settings(settings: dict[str, Any]) -> None:
    payload = yaml.safe_dump(settings, allow_unicode=True, sort_keys=False)
    atomic_write_text(CONFIG_PATH, payload)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


MOJIBAKE_MARKERS = (
    "\ufffd",
    "\u00c2",
    "\u00c3",
    "\u93b4",
    "\u6fb6",
    "\u9427",
    "\u93c8",
    "\u934f",
    "\u74c7",
    "\u6d93",
)


def repair_display_text(value: Any) -> str:
    text = str(value or "")
    if not text or not any(marker in text for marker in MOJIBAKE_MARKERS):
        return text
    candidates = [text]
    for encoding in ("latin1", "cp1252", "gbk", "cp936"):
        try:
            candidates.append(text.encode(encoding).decode("utf-8"))
        except UnicodeError:
            continue
    return min(candidates, key=mojibake_score)


def mojibake_score(text: str) -> int:
    return sum(text.count(marker) for marker in MOJIBAKE_MARKERS)


class LineBufferWriter(io.TextIOBase):
    def __init__(self, lines: list[str], paths: Path | list[Path], limit: int = 800) -> None:
        self.lines = lines
        self.paths = [paths] if isinstance(paths, Path) else list(paths)
        self.limit = limit
        self.failure_reason = ""
        self._lock = threading.RLock()
        self._files = []
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._files.append(path.open("a", encoding="utf-8"))

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not text:
            return 0
        cleaned_text = repair_display_text(text)
        with self._lock:
            for file in self._files:
                file.write(cleaned_text)
                file.flush()
            for line in cleaned_text.splitlines():
                if line.strip():
                    self.lines.append(line)
                    if not self.failure_reason:
                        self.failure_reason = automation_failure_reason([line])
            if len(self.lines) > self.limit:
                del self.lines[: len(self.lines) - self.limit]
        return len(text)

    def flush(self) -> None:
        with self._lock:
            for file in self._files:
                file.flush()

    def close(self) -> None:
        with self._lock:
            for file in self._files:
                if not file.closed:
                    file.close()


@dataclass
class AutomationJobManager:
    root_dir: Path = ROOT_DIR
    running: bool = False
    action: str = ""
    started_at: str = ""
    finished_at: str = ""
    success: bool | None = None
    error: str = ""
    lines: list[str] = field(default_factory=list)
    _thread: threading.Thread | None = None
    _process: subprocess.Popen[str] | None = None
    _stop_requested: bool = False
    _last_action: str = ""
    _last_options: dict[str, str] = field(default_factory=dict)
    _run_log_path: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _completion_callbacks: list[Callable[..., None]] = field(default_factory=list)

    def start(self, action: str, options: dict[str, str] | None = None) -> dict[str, Any]:
        options = options or {}
        with self._lock:
            if self.running:
                return {"started": False, "message": "已有任务正在运行。"}

            self.running = True
            self.action = action
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self.finished_at = ""
            self.success = None
            self.error = ""
            self.lines = []
            self._stop_requested = False
            self._last_action = action
            self._last_options = dict(options)
            self._run_log_path = str(new_run_log_path(action))
            self._persist_state()
            self._thread = threading.Thread(
                target=self._run,
                args=(action, options),
                name="approval-web-runner",
                daemon=True,
            )
            self._thread.start()
            return {"started": True, "message": f"已启动任务：{action}"}

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self.running:
                return {"stopped": False, "message": "No automation task is running."}
            self._stop_requested = True
            process = self._process

        self._terminate_process_tree(process)

        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10)
        stopped = not (thread and thread.is_alive())
        return {
            "stopped": stopped,
            "message": "Current automation task stopped." if stopped else "Stop requested; waiting for task cleanup.",
        }

    def status(self, *, light: bool = False) -> dict[str, Any]:
        with self._lock:
            if not self.running and not self.action:
                return self._status_from_persisted_state(light=light)
            log_tail = self.lines[-160:]
            running = self.running
            success = self.success
            error = self.error
            if light:
                health = run_health(log_tail, success, error)
                return {
                    "running": running,
                    "action": self.action,
                    "action_label": action_label(self.action),
                    "started_at": self.started_at,
                    "finished_at": self.finished_at,
                    "success": success,
                    "error": error,
                    "result_label": result_label(running, success, error, health, action=self.action),
                    "result_health": health,
                    "run_log_path": self._run_log_path,
                    "summary": light_run_summary(
                        log_tail,
                        action=self.action,
                        options=self._last_options,
                        running=running,
                        success=success,
                        error=error,
                    ),
                    "log_tail": log_tail,
                    "workflow": workflow_summary(log_tail, running=running, success=success, error=error),
                }
            summary_lines = current_run_lines(self._run_log_path, fallback=self.lines)
            health = run_health(summary_lines, success, error)
            summary = run_summary(
                summary_lines,
                action=self.action,
                options=self._last_options,
                running=running,
                success=success,
                error=error,
                root_dir=self.root_dir,
            )
            return {
                "running": running,
                "action": self.action,
                "action_label": action_label(self.action),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "success": success,
                "error": error,
                "result_label": result_label(running, success, error, health, action=self.action),
                "result_health": health,
                "run_log_path": self._run_log_path,
                "summary": summary,
                "log_tail": log_tail,
                "workflow": workflow_summary(log_tail, running=running, success=success, error=error),
            }

    def _run(self, action: str, options: dict[str, str]) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        aggregate_log_path = LOG_DIR / "web_run_stdout.txt"
        run_log_path = Path(self._run_log_path) if self._run_log_path else new_run_log_path(action)
        self._run_log_path = str(run_log_path)
        writer = LineBufferWriter(self.lines, [aggregate_log_path, run_log_path])
        memory_signature_before = memory_file_signature(self.root_dir) if action == "suggestions" else None

        try:
            if action == "todo_export":
                clear_todo_task_cache()
            writer.write(f"{datetime.now().isoformat(timespec='seconds')} START {action}\n")
            return_code = self._run_worker_process(action, options, writer)
            if self._stop_requested:
                success = False
                error = "用户停止运行"
                writer.write(f"{datetime.now().isoformat(timespec='seconds')} STOPPED {action}\n")
            elif return_code == 0:
                failure_reason = writer.failure_reason or automation_failure_reason(self.lines)
                if failure_reason:
                    success = False
                    error = failure_reason
                    writer.write(
                        f"{datetime.now().isoformat(timespec='seconds')} END {action} WITH WARNINGS: "
                        f"{failure_reason}\n"
                    )
                else:
                    success = True
                    error = ""
                    writer.write(f"{datetime.now().isoformat(timespec='seconds')} END {action}\n")
            else:
                success = False
                error = f"Automation worker exited with code {return_code}"
        except Exception as exc:  # noqa: BLE001 - surfaced in web UI for local operator diagnosis
            success = False
            error = str(exc)
            writer.write(traceback.format_exc())
        finally:
            writer.close()
            if (
                action == "suggestions"
                and success
                and memory_signature_before != memory_file_signature(self.root_dir)
            ):
                self._auto_upload_memory_after_success()
            with self._lock:
                self.running = False
                self.finished_at = datetime.now().isoformat(timespec="seconds")
                self.success = success
                self.error = error
                self._persist_state()
            self._notify_completion_callbacks(action=action, success=success, error=error, options=options)
            self._notify_task_result(action=action, success=success, error=error)

    def add_completion_callback(self, callback: Callable[..., None]) -> None:
        with self._lock:
            if callback not in self._completion_callbacks:
                self._completion_callbacks.append(callback)

    def _notify_completion_callbacks(
        self,
        *,
        action: str,
        success: bool,
        error: str,
        options: dict[str, str],
    ) -> None:
        with self._lock:
            callbacks = list(self._completion_callbacks)
        for callback in callbacks:
            try:
                callback(action=action, success=success, error=error, options=dict(options))
            except Exception as exc:  # noqa: BLE001 - callbacks must not block automation cleanup
                self._record_notification_failure(f"Automation completion callback failed: {exc}")

    def _persist_state(self) -> None:
        WEB_RUN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_tail = [repair_display_text(line) for line in self.lines[-160:]]
        payload = {
            "running": self.running,
            "action": self.action,
            "action_label": action_label(self.action),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "success": self.success,
            "error": repair_display_text(self.error),
            "run_log_path": self._run_log_path,
            "options": dict(self._last_options),
            "log_tail": log_tail,
        }
        atomic_write_text(
            WEB_RUN_STATE_PATH,
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        )

    def _status_from_persisted_state(self, *, light: bool = False) -> dict[str, Any]:
        if not WEB_RUN_STATE_PATH.exists():
            return {
                "running": False,
                "action": "",
                "action_label": "",
                "started_at": "",
                "finished_at": "",
                "success": None,
                "error": "",
                "result_label": "未运行",
                "run_log_path": "",
                "summary": {},
                "log_tail": [],
                "workflow": workflow_summary([], running=False, success=None, error=""),
            }
        try:
            with WEB_RUN_STATE_PATH.open("r", encoding="utf-8") as file:
                payload = yaml.safe_load(file) or {}
        except Exception:
            payload = {}
        log_tail = [repair_display_text(line) for line in (payload.get("log_tail") or [])]
        run_log_path = str(payload.get("run_log_path") or "")
        success = payload.get("success")
        error = repair_display_text(payload.get("error") or "")
        action = str(payload.get("action") or "")
        if light:
            health = run_health(log_tail, success, error)
            return {
                "running": False,
                "action": action,
                "action_label": action_label(action),
                "started_at": str(payload.get("started_at") or ""),
                "finished_at": str(payload.get("finished_at") or ""),
                "success": success,
                "error": error,
                "result_label": result_label(False, success, error, health, action=action),
                "result_health": health,
                "run_log_path": run_log_path,
                "summary": light_run_summary(
                    log_tail,
                    action=action,
                    options=payload.get("options") or {},
                    running=False,
                    success=success,
                    error=error,
                ),
                "log_tail": log_tail,
                "workflow": workflow_summary(log_tail, running=False, success=success, error=error),
            }
        summary_lines = current_run_lines(run_log_path, fallback=log_tail)
        health = run_health(summary_lines, success, error)
        summary = run_summary(
            summary_lines,
            action=action,
            options=payload.get("options") or {},
            running=False,
            success=success,
            error=error,
            root_dir=self.root_dir,
        )
        return {
            "running": False,
            "action": action,
            "action_label": action_label(action),
            "started_at": str(payload.get("started_at") or ""),
            "finished_at": str(payload.get("finished_at") or ""),
            "success": success,
            "error": error,
            "result_label": result_label(False, success, error, health, action=action),
            "result_health": health,
            "run_log_path": run_log_path,
            "summary": summary,
            "log_tail": log_tail,
            "workflow": workflow_summary(log_tail, running=False, success=success, error=error),
        }

    def _run_worker_process(self, action: str, options: dict[str, str], writer: LineBufferWriter) -> int:
        env = os.environ.copy()
        overrides = self._env_overrides(options)
        for key, value in overrides.items():
            if value == "":
                env.pop(key, None)
            else:
                env[key] = value
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")

        with self._lock:
            if self._stop_requested:
                return 130
            popen_kwargs: dict[str, Any] = {}
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0
                popen_kwargs["startupinfo"] = startupinfo
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "automation_worker", action],
                cwd=str(SOURCE_ROOT / "src"),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **popen_kwargs,
            )
            job_handle = self._assign_windows_kill_on_close_job(process)
            self._process = process
            if self._stop_requested:
                self._terminate_process_tree(process)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                writer.write(line)
            return process.wait()
        finally:
            self._close_windows_job_handle(job_handle)
            with self._lock:
                self._process = None

    @staticmethod
    def _assign_windows_kill_on_close_job(process: subprocess.Popen[str]) -> int | None:
        if os.name != "nt":
            return None
        try:
            kernel32 = ctypes.windll.kernel32
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return None

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                kernel32.CloseHandle(job)
                return None
            if not kernel32.AssignProcessToJobObject(job, int(process._handle)):  # noqa: SLF001 - Windows handle
                kernel32.CloseHandle(job)
                return None
            return int(job)
        except Exception:
            return None

    @staticmethod
    def _close_windows_job_handle(job_handle: int | None) -> None:
        if os.name != "nt" or not job_handle:
            return
        try:
            ctypes.windll.kernel32.CloseHandle(job_handle)
        except Exception:
            return

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str] | None) -> None:
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def _env_overrides(self, options: dict[str, str]) -> dict[str, str]:
        allowed_keys = {
            "TARGET_LIST_NUMBER",
            "PROCESS_ALL_TODOS",
            "PROCESS_ALL_TODOS_MAX",
            "TARGET_LIST_NUMBERS",
            "APPROVAL_WRITE_MODE",
            "APPROVAL_WRITE_MIN_CONFIDENCE",
            "AUTO_PASS",
            "SCHEDULED_RUN",
            "SCHEDULED_SKIP_MANUAL_REVIEW_LISTS",
        }
        return {
            key: value
            for key, value in options.items()
            if key in allowed_keys and value is not None
        }

    def _notify_task_result(self, *, action: str, success: bool, error: str) -> None:
        try:
            settings = load_settings()
            load_dotenv(ENV_PATH)
            message = build_task_result_message(
                action=action,
                success=success,
                started_at=self.started_at,
                finished_at=self.finished_at,
                error=error,
                approval=approval_summary(self.root_dir),
                review_queue=review_queue_summary(self.root_dir),
            )
            result = send_task_result_notification(settings, message, root_dir=self.root_dir)
            if result.get("sent") or result.get("skipped"):
                return
            self._record_notification_failure(f"DingTalk notification failed: {result.get('error') or result}")
        except Exception as exc:  # noqa: BLE001 - notification must not block automation
            self._record_notification_failure(f"DingTalk notification failed: {exc}")

    def _auto_upload_memory_after_success(self) -> None:
        try:
            result = auto_upload_memory_if_configured(self.root_dir)
            if result.get("skipped"):
                return
            message = result.get("message") or "试剂记忆库自动上传完成。"
            self._record_notification_failure(f"Memory sync: {message}")
        except Exception as exc:  # noqa: BLE001 - memory sync must not block automation cleanup
            try:
                service = memory_sync_service(self.root_dir)
                service.update_state(last_error=f"自动上传失败：{exc}")
            except Exception:
                pass
            self._record_notification_failure(f"Memory sync auto upload failed: {exc}")

    def _record_notification_failure(self, message: str) -> None:
        self.lines.append(message)
        for path in [LOG_DIR / "web_run_stdout.txt", Path(self._run_log_path) if self._run_log_path else None]:
            if path is None:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as file:
                    file.write(message + "\n")
            except OSError:
                pass
        self._persist_state()


def new_run_log_path(action: str) -> Path:
    safe_action = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(action or "run"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return RUN_LOG_DIR / f"{stamp}_{safe_action}.log"


def current_run_lines(run_log_path: str | Path | None, *, fallback: list[str] | None = None) -> list[str]:
    if run_log_path:
        try:
            path = Path(run_log_path)
            if path.exists():
                return [repair_display_text(line.rstrip("\n")) for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
        except OSError:
            pass
    return [repair_display_text(line) for line in (fallback or [])]


def memory_file_signature(root_dir: Path = ROOT_DIR) -> tuple[bool, int, int]:
    try:
        memory = ReagentMemory.from_settings(load_settings(), root_dir)
        path = memory.path
        if not path.exists():
            return (False, 0, 0)
        stat = path.stat()
        return (True, int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        return (False, 0, 0)


def action_label(action: str) -> str:
    return ACTION_LABELS.get(str(action or ""), str(action or ""))


def run_summary(
    lines: list[str],
    *,
    action: str,
    options: dict[str, Any] | None,
    running: bool,
    success: bool | None,
    error: str,
    root_dir: Path = ROOT_DIR,
) -> dict[str, Any]:
    text = "\n".join(str(line) for line in lines)
    lower_text = text.lower()
    options = options or {}
    todo = todo_tasks_summary(root_dir)
    suggestion_metrics = aggregate_suggestion_summaries(lines)

    write_success = sum(
        1
        for line in lines
        if "save verified for sequence" in line.lower() or "save result for sequence" in line.lower()
    )
    write_failed = sum(
        1
        for line in lines
        if "could not select" in line.lower()
        or "failed save operation" in line.lower()
        or "save verification failed" in line.lower()
    )
    deferred_write_count = sum(1 for line in lines if "deferred pending write candidate" in line.lower())
    not_found_after_reread_count = sum(
        1
        for line in lines
        if "not_found_after_reread" in line.lower()
        or "not found after re-read" in line.lower()
        or "pending write candidate(s) not found" in line.lower()
    )
    dropdown_failures = dropdown_failure_details(lines)
    llm_seconds = 0.0
    llm_batches = 0
    for line in lines:
        lower = line.lower()
        if "llm extraction completed" in lower and "s" in lower:
            llm_batches += 1
            seconds = extract_number_before(line, "s")
            if seconds is not None:
                llm_seconds += seconds
    page_count = len(
        {
            marker
            for marker in (
                extract_after(line, "Current page:")
                or extract_after(line, "Read reagent page")
                or extract_after(line, "Read todo page")
                for line in lines
            )
            if marker
        }
    )
    target_lists = parse_target_list_numbers(str(options.get("TARGET_LIST_NUMBERS") or options.get("target_list_numbers") or ""))
    if not target_lists:
        for line in lines:
            for token in line.replace("'", " ").replace('"', " ").replace(",", " ").split():
                if token.startswith("SJ") and token[2:].isdigit() and token not in target_lists:
                    target_lists.append(token)

    if running:
        outcome = "运行中"
    elif success is True and action == "todo_export":
        outcome = "待办清单刷新成功"
    elif success is True and action == "suggestions":
        outcome = "审批流程完成"
    elif success is True:
        outcome = f"{action_label(action)}成功"
    elif success is False:
        outcome = repair_display_text(error) or "执行失败"
    else:
        outcome = "未运行"

    return {
        "action": action,
        "action_label": action_label(action),
        "outcome": outcome,
        "target_list_numbers": target_lists,
        "todo_count": int(todo.get("rows") or 0),
        "suggestion_count": int(suggestion_metrics.get("suggestion_total") or 0),
        "manual_review_count": int(suggestion_metrics.get("manual_review_candidate_count") or 0),
        "processed_pages": page_count,
        "write_success_count": write_success,
        "write_failure_count": write_failed,
        "deferred_write_count": deferred_write_count,
        "not_found_after_reread_count": not_found_after_reread_count,
        "dropdown_failure_count": len(dropdown_failures),
        "dropdown_failures": dropdown_failures[:12],
        "page_suggestion_count": int(suggestion_metrics.get("suggestion_total") or 0),
        "writable_candidate_count": int(suggestion_metrics.get("writable_candidate_count") or 0),
        "manual_review_candidate_count": int(suggestion_metrics.get("manual_review_candidate_count") or 0),
        "low_confidence_count": int(suggestion_metrics.get("low_confidence_count") or 0),
        "search_failure_count": int(suggestion_metrics.get("search_failure_count") or 0),
        "memory_hit_count": int(suggestion_metrics.get("memory_hit_count") or 0),
        "llm_knowledge_fallback_count": int(suggestion_metrics.get("llm_knowledge_fallback_count") or 0),
        "llm_batch_count": llm_batches,
        "llm_seconds": round(llm_seconds, 1),
        "skipped_candidate_count": int(suggestion_metrics.get("skipped_candidate_count") or 0),
        "skip_reasons": suggestion_metrics.get("skip_reasons") or {},
        "has_traceback": "traceback" in lower_text,
        "has_write_warning": write_failed > 0,
    }


def light_run_summary(
    lines: list[str],
    *,
    action: str,
    options: dict[str, Any] | None,
    running: bool,
    success: bool | None,
    error: str,
) -> dict[str, Any]:
    options = options or {}
    target_lists = parse_target_list_numbers(str(options.get("TARGET_LIST_NUMBERS") or options.get("target_list_numbers") or ""))
    suggestion_metrics = aggregate_suggestion_summaries(lines)
    write_success = sum(
        1
        for line in lines
        if "save verified for sequence" in line.lower() or "save result for sequence" in line.lower()
    )
    write_failed = sum(
        1
        for line in lines
        if "could not select" in line.lower()
        or "failed save operation" in line.lower()
        or "save verification failed" in line.lower()
    )
    dropdown_failures = dropdown_failure_details(lines)
    if running:
        outcome = "运行中"
    elif success is True and action == "todo_export":
        outcome = "待办清单刷新成功"
    elif success is True and action == "suggestions":
        outcome = "审批流程完成"
    elif success is True:
        outcome = f"{action_label(action)}成功"
    elif success is False:
        outcome = repair_display_text(error) or "执行失败"
    else:
        outcome = "未运行"
    return {
        "action": action,
        "action_label": action_label(action),
        "outcome": outcome,
        "target_list_numbers": target_lists,
        "todo_count": 0,
        "suggestion_count": int(suggestion_metrics.get("suggestion_total") or 0),
        "manual_review_count": int(suggestion_metrics.get("manual_review_candidate_count") or 0),
        "processed_pages": 0,
        "write_success_count": write_success,
        "write_failure_count": write_failed,
        "deferred_write_count": sum(1 for line in lines if "deferred pending write candidate" in line.lower()),
        "not_found_after_reread_count": sum(1 for line in lines if "not found after re-read" in line.lower()),
        "dropdown_failure_count": len(dropdown_failures),
        "dropdown_failures": dropdown_failures[:12],
        "page_suggestion_count": int(suggestion_metrics.get("suggestion_total") or 0),
        "writable_candidate_count": int(suggestion_metrics.get("writable_candidate_count") or 0),
        "manual_review_candidate_count": int(suggestion_metrics.get("manual_review_candidate_count") or 0),
        "low_confidence_count": int(suggestion_metrics.get("low_confidence_count") or 0),
        "search_failure_count": int(suggestion_metrics.get("search_failure_count") or 0),
        "memory_hit_count": int(suggestion_metrics.get("memory_hit_count") or 0),
        "llm_knowledge_fallback_count": int(suggestion_metrics.get("llm_knowledge_fallback_count") or 0),
        "llm_batch_count": 0,
        "llm_seconds": 0.0,
        "skipped_candidate_count": int(suggestion_metrics.get("skipped_candidate_count") or 0),
        "skip_reasons": suggestion_metrics.get("skip_reasons") or {},
        "has_traceback": any("traceback" in line.lower() for line in lines),
        "has_write_warning": write_failed > 0,
    }


def extract_after(line: str, marker: str) -> str:
    if marker not in line:
        return ""
    tail = line.split(marker, 1)[1].strip()
    return tail.split()[0].strip(" .,:;")


def extract_between(line: str, start_marker: str, end_marker: str) -> str:
    if start_marker not in line or end_marker not in line:
        return ""
    value = line.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
    return repair_display_text(value)


def dropdown_failure_details(lines: list[str]) -> list[dict[str, str]]:
    failures = []
    for line in lines:
        lower = line.lower()
        if "could not select physicochemical property" not in lower:
            continue
        failures.append(
            {
                "line": repair_display_text(line),
                "sequence": extract_after(line, "sequence:"),
                "category": extract_between(line, "property", "for sequence:"),
            }
        )
    return failures


def extract_number_before(line: str, marker: str) -> float | None:
    if marker not in line:
        return None
    before = line.split(marker, 1)[0].strip()
    token = before.split()[-1] if before.split() else ""
    try:
        return float(token)
    except ValueError:
        return None


@contextlib.contextmanager
def temporary_env(overrides: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value == "":
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def workflow_summary(
    lines: list[str],
    *,
    running: bool,
    success: bool | None,
    error: str,
) -> dict[str, Any]:
    active_step = active_workflow_step(lines)
    seen_order = highest_seen_workflow_index(lines)
    active_index = workflow_step_index(active_step)

    if running and active_index < 0:
        active_step = "login"
        active_index = 0

    if running and active_index >= 0:
        completed_index = active_index - 1
    elif success:
        completed_index = seen_order
    else:
        completed_index = seen_order

    steps = []
    for index, step in enumerate(WORKFLOW_STEPS):
        state = "waiting"
        if index <= completed_index:
            state = "done"
        if running and index == active_index:
            state = "active"
        elif error and not running and index == active_index:
            state = "failed"
        steps.append({**step, "state": state})

    current = WORKFLOW_STEPS[active_index] if active_index >= 0 else None
    return {
        "current_step": current["id"] if current else "",
        "current_label": current["label"] if current else ("已完成" if success else "未运行"),
        "steps": steps,
    }


def result_label(running: bool, success: bool | None, error: str, health: str = "", action: str = "") -> str:
    if running:
        return "运行中"
    if success is True:
        if health == "warning":
            return "需检查"
        if action == "todo_export":
            return "待办清单刷新成功"
        if action == "suggestions":
            return "审批流程完成"
        return "成功"
    if success is False or error:
        return "失败"
    return "未运行"


def run_health(lines: list[str], success: bool | None, error: str) -> str:
    if success is False or error:
        return "failed"
    text = "\n".join(str(line) for line in lines).lower()
    warning_tokens = (
        "failed save operation",
        "could not select",
        "could not open technical judgement",
        "multi-page mode stopped because",
        "pagination check stopped",
        "traceback",
    )
    if any(token in text for token in warning_tokens):
        return "warning"
    if success is True:
        return "ok"
    return "unknown"


def automation_failure_reason(lines: list[str]) -> str:
    text = "\n".join(str(line) for line in lines).lower()
    failure_patterns = (
        ("could not select physicochemical property", "存在物化特性下拉选择失败"),
        ("could not open technical judgement", "存在技术判定入口打开失败"),
        ("save verification failed", "存在网页保存后校验失败"),
        ("failed save operation", "存在保存操作失败"),
        ("multi-page mode stopped because the reagent detail page could not be stabilized", "写入失败后详情页未能恢复稳定"),
        ("multi-page mode stopped because next-page navigation could not be verified", "分页切换未能确认"),
        ("multi-page mode stopped because sorting after", "排序或翻页后的页面状态异常"),
        ("traceback", "运行过程中出现异常堆栈"),
    )
    for pattern, reason in failure_patterns:
        if pattern in text:
            return reason
    return ""


def parse_target_list_numbers(value: str) -> list[str]:
    numbers = []
    for part in str(value or "").replace("\n", ",").replace(";", ",").split(","):
        item = part.strip()
        if item and item not in numbers:
            numbers.append(item)
    return numbers


def workflow_step_index(step_id: str) -> int:
    for index, step in enumerate(WORKFLOW_STEPS):
        if step["id"] == step_id:
            return index
    return -1


def active_workflow_step(lines: list[str]) -> str:
    active_stage = ""
    for line in lines:
        stage = parse_flow_stage(line, "START")
        if stage:
            active_stage = stage
            continue
        stage = parse_flow_stage(line, "END")
        if stage and stage == active_stage:
            active_stage = ""
    if active_stage:
        return stage_to_workflow_step(active_stage)

    for line in reversed(lines):
        step = line_to_workflow_step(line)
        if step:
            return step
    return ""


def highest_seen_workflow_index(lines: list[str]) -> int:
    highest = -1
    for line in lines:
        step = line_to_workflow_step(line)
        index = workflow_step_index(step)
        if index > highest:
            highest = index
    return highest


def parse_flow_stage(line: str, marker: str) -> str:
    needle = f"[FLOW] {marker}"
    if needle not in line:
        return ""
    tail = line.split(needle, 1)[1].strip()
    if not tail:
        return ""
    return tail.split(" - ", 1)[0].split(" (", 1)[0].strip()


def line_to_workflow_step(line: str) -> str:
    for marker in ("START", "END"):
        stage = parse_flow_stage(line, marker)
        if stage:
            mapped = stage_to_workflow_step(stage)
            if mapped:
                return mapped

    text = line.lower()
    if "opening menu" in text or "opening page" in text or "opening target task detail" in text or "todo list refresh" in text:
        return "judgement"
    if "auto-match" in text or "一键匹配" in line:
        return "auto_match"
    if "physicochemical property header" in text or "read_current_page_unmatched" in text or "sorting considered successful" in text:
        return "sort_read"
    if "chemsrc" in text or "chemicalbook" in text or "chemical_search" in text:
        return "search"
    if "llm_extract" in text or "大模型" in line:
        return "llm"
    if "rule_classify" in text or "规则判定" in line:
        return "rule"
    if (
        "approval write candidate" in text
        or "save result for sequence" in text
        or "save verified for sequence" in text
        or "apply_approval_write_mode" in text
    ):
        return "write"
    if "start " in text:
        return "login"
    return ""


def stage_to_workflow_step(stage: str) -> str:
    normalized = stage.strip().lower()
    if normalized in {"perform_auto_match"}:
        return "auto_match"
    if normalized in {"wait_reagent_table_ready", "read_detail_info", "sort_property_column", "read_current_page_unmatched"}:
        return "sort_read"
    if normalized in {"chemical_search"}:
        return "search"
    if normalized in {"llm_extract"}:
        return "llm"
    if normalized in {"rule_classify", "record_rule_candidate", "add_manual_review_item"}:
        return "rule"
    if normalized in {"apply_approval_write_mode"}:
        return "write"
    return ""


def artifact_summary(root_dir: Path = ROOT_DIR) -> list[dict[str, Any]]:
    log_dir = root_dir / "data" / "logs"
    if not log_dir.exists():
        return []
    wanted_suffixes = {".xlsx", ".png", ".html", ".txt"}
    excluded_names = {"web_run_stdout.txt"}
    excluded_suffixes = {".sqlite", ".db"}
    artifacts = []
    for path in sorted(log_dir.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        lowered_name = path.name.lower()
        if path.is_dir() or lowered_name in excluded_names or "backup" in lowered_name:
            continue
        if path.suffix.lower() in excluded_suffixes:
            continue
        if path.suffix.lower() not in wanted_suffixes:
            continue
        stat = path.stat()
        if stat.st_size > 20 * 1024 * 1024:
            continue
        artifacts.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "download_url": f"/artifacts/{path.name}",
            }
        )
    return artifacts[:24]


def file_signature(path: Path) -> tuple[bool, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (False, 0, 0)
    return (True, int(stat.st_size), int(stat.st_mtime_ns))


def cached_file_payload(cache_name: str, path: Path, loader: Callable[[], Any]) -> Any:
    signature = file_signature(path)
    key = (cache_name, str(path))
    with _FILE_CACHE_LOCK:
        cached = _FILE_CACHE.get(key)
        if cached and cached[0] == signature:
            return cached[1]
    payload = loader()
    with _FILE_CACHE_LOCK:
        _FILE_CACHE[key] = (signature, payload)
    return payload


def approval_summary(root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    path = root_dir / "data" / "logs" / "approval_suggestions.xlsx"
    if not path.exists():
        return {"exists": False, "rows": 0, "categories": {}, "manual_review": 0, "preview": []}

    try:
        frame = pd.read_excel(path, dtype=str).fillna("")
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "error": str(exc), "rows": 0, "categories": {}, "manual_review": 0, "preview": []}

    category_column = "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"
    manual_column = "\u9700\u4eba\u5de5\u590d\u6838"
    categories = frame[category_column].value_counts().to_dict() if category_column in frame.columns else {}
    manual_review = 0
    if manual_column in frame.columns:
        manual_review = int(frame[manual_column].astype(str).str.lower().isin(["true", "1", "yes"]).sum())

    preview_columns = [
        "\u5e8f\u53f7",
        "\u8bd5\u5242\u540d\u79f0",
        "CAS\u53f7",
        "\u6807\u51c6\u5316\u540d\u79f0",
        "\u67e5\u8be2\u6765\u6e90",
        "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b",
        "\u7f6e\u4fe1\u5ea6",
        "\u9700\u4eba\u5de5\u590d\u6838",
    ]
    present_columns = [column for column in preview_columns if column in frame.columns]
    preview = frame[present_columns].head(12).to_dict(orient="records") if present_columns else []

    return {
        "exists": True,
        "rows": int(len(frame)),
        "categories": categories,
        "manual_review": manual_review,
        "preview": preview,
        "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def todo_tasks_summary(root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    json_path = root_dir / "data" / "logs" / "todo_tasks.json"
    if json_path.exists():
        try:
            return cached_file_payload("todo_tasks_json", json_path, lambda: build_todo_tasks_json_payload(json_path))
        except Exception:
            pass

    path = root_dir / "data" / "logs" / "todo_tasks.xlsx"
    if not path.exists():
        return {"exists": False, "rows": 0, "tasks": [], "modified": ""}

    try:
        return cached_file_payload("todo_tasks_xlsx", path, lambda: build_todo_tasks_excel_payload(path))
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "error": str(exc), "rows": 0, "tasks": [], "modified": ""}


def build_todo_tasks_json_payload(json_path: Path) -> dict[str, Any]:
    rows = json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows.get("tasks", [])
    tasks = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        list_number = first_dict_value(row, ["试剂清单号", "清单号", "list_number"])
        if not list_number:
            continue
        tasks.append(
            {
                "list_number": list_number,
                "customer_id": first_dict_value(row, ["客户编号", "customer_id"]),
                "customer_name": first_dict_value(row, ["客户名称", "customer_name"]),
                "progress": first_dict_value(row, ["技术审批进度", "progress"]),
                "status": first_dict_value(row, ["技术审批状态", "状态", "status"]),
                "salesman": first_dict_value(row, ["业务员", "salesman"]),
                "applicant": first_dict_value(row, ["申请人", "applicant"]),
                "contact": first_dict_value(row, ["联系人", "contact"]),
            }
        )
    return {
        "exists": True,
        "rows": int(len(tasks)),
        "tasks": tasks,
        "modified": datetime.fromtimestamp(json_path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def build_todo_tasks_excel_payload(path: Path) -> dict[str, Any]:
    frame = pd.read_excel(path, dtype=str).fillna("")

    def first_existing(row: pd.Series, columns: list[str]) -> str:
        for column in columns:
            if column in row.index:
                value = str(row.get(column, "")).strip()
                if value:
                    return value
        return ""

    tasks = []
    for _, row in frame.iterrows():
        list_number = first_existing(row, ["试剂清单号", "清单号", "list_number"])
        if not list_number:
            continue
        tasks.append(
            {
                "list_number": list_number,
                "customer_id": first_existing(row, ["客户编号"]),
                "customer_name": first_existing(row, ["客户名称"]),
                "progress": first_existing(row, ["技术审批进度"]),
                "status": first_existing(row, ["技术审批状态", "状态"]),
                "salesman": first_existing(row, ["业务员"]),
                "applicant": first_existing(row, ["申请人"]),
                "contact": first_existing(row, ["联系人"]),
            }
        )

    return {
        "exists": True,
        "rows": int(len(tasks)),
        "tasks": tasks,
        "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def first_dict_value(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return ""


def clear_todo_task_cache(root_dir: Path = ROOT_DIR) -> None:
    for path in (
        TODO_TASKS_PATH if root_dir == ROOT_DIR else root_dir / "data" / "logs" / "todo_tasks.xlsx",
        TODO_TASKS_JSON_PATH if root_dir == ROOT_DIR else root_dir / "data" / "logs" / "todo_tasks.json",
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            print(f"Could not clear old todo cache {path}: {error}")


def review_queue_summary(
    root_dir: Path = ROOT_DIR,
    *,
    page: int = 1,
    per_page: int = 20,
    list_number: str = "",
    sort_direction: str = "desc",
) -> dict[str, Any]:
    path = root_dir / "data" / "review_queue.xlsx"
    if not path.exists():
        return {"exists": False, "rows": 0, "pending": 0, "preview": [], "list_numbers": []}

    try:
        migrate_pending_review_reasons(path)
        payload = cached_file_payload("review_queue", path, lambda: build_review_queue_payload(path))
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "error": str(exc), "rows": 0, "pending": 0, "preview": [], "list_numbers": []}

    rows = list(payload.get("pending_rows") or [])
    if list_number:
        rows = [row for row in rows if str(row.get("list_number") or "") == list_number]
    reverse = sort_direction != "asc"
    rows.sort(key=lambda row: DateSortKey.from_text(str(row.get("timestamp") or "")), reverse=reverse)
    page = max(1, int(page or 1))
    per_page = max(1, min(100, int(per_page or 20)))
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    preview = rows[start : start + per_page]

    return {
        "exists": True,
        "rows": int(payload.get("rows") or 0),
        "pending": int(payload.get("pending") or 0),
        "filtered": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "preview": preview,
        "list_numbers": payload.get("list_numbers") or [],
        "modified": payload.get("modified") or "",
    }


@dataclass(frozen=True)
class DateSortKey:
    value: float

    @classmethod
    def from_text(cls, value: str) -> "DateSortKey":
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return cls(0.0)
        return cls(float(parsed.timestamp()))

    def __lt__(self, other: "DateSortKey") -> bool:
        return self.value < other.value


def build_review_queue_payload(path: Path) -> dict[str, Any]:
    frame = pd.read_excel(path, dtype=str).fillna("")
    settings = load_settings()

    def first_existing(row: pd.Series, columns: list[str]) -> str:
        for column in columns:
            if column in row.index:
                value = str(row.get(column, "")).strip()
                if value:
                    return value
        return ""

    status_column = next((column for column in ("status", "状态", "处理状态") if column in frame.columns), "")
    if status_column:
        normalized = frame[status_column].astype(str).str.strip().str.lower()
        pending_frame = frame[normalized.isin(BLOCKING_REVIEW_STATUSES)].copy()
    else:
        pending_frame = frame.copy()

    def compact_reason(value: str, limit: int = 260) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "..."

    def natural_reason(value: str) -> str:
        text = " ".join(str(value or "").split())
        lowered = text.lower()
        if not text:
            return "缺少足够可信的物性证据，需要人工确认后再写入网页。"
        if "duplicate search url" in lowered:
            return "检索到的网页与其他试剂重复，可能没有匹配到当前试剂的专属页面，需要人工确认检索结果。"
        if "chemsrc" in lowered and "chemicalbook" in lowered and ("失败" in text or "无有效结果" in text):
            return "Chemsrc 和 ChemicalBook 都没有查到可信结果，需要人工核对试剂名称或补充物性资料。"
        if "lookup failed" in lowered or "query failed" in lowered or "查询失败" in text:
            return "化学资料查询失败，需要人工确认试剂名称、CAS 号或补充可靠资料来源。"
        if "similarity" in lowered or "relevance" in lowered or "相似" in text:
            return "检索结果与当前试剂名称相似度不足，可能不是同一种试剂，需要人工确认。"
        if "confidence" in lowered or "置信度" in text:
            return "规则判定置信度不足，需要人工复核后再决定物化特性。"
        if "llm" in lowered or "大模型" in text:
            return "大模型整理出的物性信息不够明确，需要人工复核证据。"
        if "pubchem cid" in lowered or "molecularformula" in lowered or "iupacname" in lowered:
            return "查询到了化学资料，但资料没有被规则稳定归类，需要人工核对物性证据后再处理。"
        if "缺少" in text or "无足够" in text or "证据" in text:
            return "缺少足够可信的物性证据，需要人工确认后再写入网页。"
        return compact_reason(text)

    def sort_value(row: pd.Series) -> pd.Timestamp:
        raw_time = first_existing(row, ["timestamp", "时间"])
        parsed = pd.to_datetime(raw_time, errors="coerce")
        if pd.isna(parsed):
            return pd.Timestamp.min
        return parsed

    if not pending_frame.empty:
        pending_frame["_sort_time"] = pending_frame.apply(sort_value, axis=1)
        pending_frame["_list_number"] = pending_frame.apply(
            lambda row: first_existing(row, ["试剂清单号", "当前清单号", "清单号", "list_number"]),
            axis=1,
        )
        pending_frame["_reagent_name"] = pending_frame.apply(
            lambda row: first_existing(row, ["试剂名称", "chemical_name", "reagent_name"]),
            axis=1,
        )
        pending_frame["_cas"] = pending_frame.apply(lambda row: first_existing(row, ["cas", "CAS号"]), axis=1)
        pending_frame["_standard_name"] = pending_frame.apply(
            lambda row: first_existing(row, ["standard_name", "标准化名称"]),
            axis=1,
        )
        pending_frame["_review_key"] = (
            pending_frame["_list_number"].astype(str)
            + "|"
            + pending_frame["_cas"].astype(str)
            + "|"
            + pending_frame["_reagent_name"].astype(str)
            + "|"
            + pending_frame["_standard_name"].astype(str)
        )
        pending_frame = (
            pending_frame.sort_values("_sort_time", ascending=True)
            .drop_duplicates("_review_key", keep="last")
            .sort_values("_sort_time", ascending=False)
        )

    list_numbers = []
    if not pending_frame.empty and "_list_number" in pending_frame.columns:
        list_numbers = sorted(value for value in pending_frame["_list_number"].dropna().astype(str).unique() if value)

    preview: list[dict[str, str]] = []
    for _, row in pending_frame.iterrows():
        reason = first_existing(row, ["reason", "原因", "复核原因", "manual_review_reason"])
        display_summary = review_display_summary_from_row(row, reason=reason)
        suggested_category = first_existing(row, ["suggested_category"])
        mapped_suggested_category = to_erp_property(suggested_category, settings) or suggested_category
        preview.append(
            {
                "review_key": first_existing(row, ["_review_key"]),
                "timestamp": first_existing(row, ["timestamp", "时间"]),
                "list_number": first_existing(row, ["试剂清单号", "当前清单号", "清单号", "list_number"]),
                "sequence": first_existing(row, ["序号", "sequence", "index"]),
                "reagent_name": first_existing(row, ["试剂名称", "chemical_name", "reagent_name"]),
                "cas": first_existing(row, ["cas", "CAS号"]),
                "standard_name": first_existing(row, ["standard_name", "标准化名称"]),
                "cleaned_name": first_existing(row, ["cleaned_name", "清洗后名称"]),
                "specification": first_existing(row, ["specification", "规格"]),
                "unit": first_existing(row, ["unit", "规格单位"]),
                "reason": natural_reason(reason),
                "reason_full": reason,
                "status": first_existing(row, ["status", "状态", "处理状态"]) or "pending",
                "suggested_category": mapped_suggested_category,
                "classification_confidence": first_existing(row, ["classification_confidence"]),
                "property_summary": first_existing(row, ["property_summary"]),
                "evidence_source_type": first_existing(row, ["evidence_source_type"]),
                "source_confidence": first_existing(row, ["source_confidence"]),
                "llm_confidence": first_existing(row, ["llm_confidence"]),
                "evidence_quality": first_existing(row, ["evidence_quality"]),
                "source_url": first_existing(row, ["source_url"]),
                "flash_point": first_existing(row, ["flash_point"]),
                "boiling_point": first_existing(row, ["boiling_point"]),
                "toxicity": first_existing(row, ["toxicity"]),
                "corrosive": first_existing(row, ["corrosive"]),
                "oxidizing": first_existing(row, ["oxidizing"]),
                "flammable": first_existing(row, ["flammable"]),
                "water_reactive": first_existing(row, ["water_reactive"]),
                "explosive_risk": first_existing(row, ["explosive_risk"]),
                "used_llm_knowledge_fallback": first_existing(row, ["used_llm_knowledge_fallback"]),
                "used_llm_rule_fallback": first_existing(row, ["used_llm_rule_fallback"]),
                "used_llm_manual_review_advice": first_existing(row, ["used_llm_manual_review_advice"]),
                "llm_rule_confidence": first_existing(row, ["llm_rule_confidence"]),
                "llm_rule_reason": first_existing(row, ["llm_rule_reason"]),
                "llm_rule_matched_rule": first_existing(row, ["llm_rule_matched_rule"]),
                "llm_rule_evidence_type": first_existing(row, ["llm_rule_evidence_type"]),
                "llm_rule_must_manual_review": first_existing(row, ["llm_rule_must_manual_review"]),
                "llm_advisory_category": first_existing(row, ["llm_advisory_category"]),
                "llm_advisory_summary_cn": first_existing(row, ["llm_advisory_summary_cn"]),
                "llm_advisory_reason_cn": first_existing(row, ["llm_advisory_reason_cn"]),
                "llm_advisory_rule_cn": first_existing(row, ["llm_advisory_rule_cn"]),
                "llm_advisory_uncertainties_cn": first_existing(row, ["llm_advisory_uncertainties_cn"]),
                "llm_advisory_confidence": first_existing(row, ["llm_advisory_confidence"]),
                "llm_advisory_evidence_basis": first_existing(row, ["llm_advisory_evidence_basis"]),
                "llm_advisory_only": first_existing(row, ["llm_advisory_only"]),
                "llm_advisory_high_risk": first_existing(row, ["llm_advisory_high_risk"]),
                "llm_model": first_existing(row, ["llm_model"]),
                "llm_provider": first_existing(row, ["llm_provider"]),
                "llm_generated_at": first_existing(row, ["llm_generated_at"]),
                "llm_rules_fingerprint": first_existing(row, ["llm_rules_fingerprint"]),
                "review_advice": first_existing(row, ["review_advice"]),
                "original_erp_cas": first_existing(row, ["original_erp_cas"]),
                "corrected_cas": first_existing(row, ["corrected_cas"]),
                "cas_name_conflict": first_existing(row, ["cas_name_conflict"]),
                "cas_correction_applied": first_existing(row, ["cas_correction_applied"]),
                "cas_correction_reason": first_existing(row, ["cas_correction_reason"]),
                "cas_correction_source": first_existing(row, ["cas_correction_source"]),
                "cas_correction_url": first_existing(row, ["cas_correction_url"]),
                "display_suggestion": first_existing(row, ["display_suggestion"]) or display_summary["display_suggestion"],
                "display_reason": first_existing(row, ["display_reason"]) or display_summary["display_reason"],
                "evidence_status": first_existing(row, ["evidence_status"]) or display_summary["evidence_status"],
                "detail_summary": first_existing(row, ["detail_summary"]) or display_summary["detail_summary"],
                "allow_suggestion_preselect": first_existing(row, ["allow_suggestion_preselect"])
                or str(display_summary["allow_suggestion_preselect"]),
            }
        )

    return {
        "rows": int(len(frame)),
        "pending": int(len(pending_frame)),
        "pending_rows": preview,
        "list_numbers": list_numbers,
        "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def confirm_review_item(payload: dict[str, Any], root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    path = root_dir / "data" / "review_queue.xlsx"
    if not path.exists():
        return {"confirmed": False, "message": "review_queue.xlsx does not exist."}

    frame = pd.read_excel(path, dtype=str).fillna("")
    if frame.empty:
        return {"confirmed": False, "message": "review_queue.xlsx is empty."}

    final_category = str(payload.get("final_category") or "").strip()
    if not final_category:
        return {"confirmed": False, "message": "请先选择人工确认后的物化特性。"}
    settings = load_settings()
    options = erp_property_options(settings)
    rule_category = to_rule_category(final_category, settings, root_dir)
    non_writable_decision = is_non_writable_rule_category(rule_category, settings, root_dir)
    if non_writable_decision:
        final_category = rule_category
    elif final_category not in options:
        mapped_category = to_erp_property(final_category, settings)
        if not mapped_category:
            return {
                "confirmed": False,
                "message": f"物化特性类别未映射到 ERP 下拉选项：{final_category}",
            }
        final_category = mapped_category

    if "_review_key" not in frame.columns:
        frame["_review_key"] = frame.apply(review_queue_row_key, axis=1)

    status_column = next((column for column in ("status", "状态", "处理状态") if column in frame.columns), "")
    if status_column:
        normalized_status = frame[status_column].astype(str).str.strip().str.lower()
        blocking_indices = set(frame.index[normalized_status.isin(BLOCKING_REVIEW_STATUSES)].tolist())
    else:
        blocking_indices = set(frame.index.tolist())

    review_key = str(payload.get("review_key") or "").strip()
    matched_indices: list[int] = []
    if review_key:
        matched_indices = frame.index[frame["_review_key"].astype(str) == review_key].tolist()

    if not matched_indices:
        matched_index = match_review_item_by_fields(frame, payload)
        if matched_index is not None:
            matched_indices = [matched_index]

    if not matched_indices:
        return {"confirmed": False, "message": "没有找到对应的人工复核记录，可能已被处理或文件已刷新。"}

    pending_matched_indices = [index for index in matched_indices if index in blocking_indices]
    if not pending_matched_indices:
        pending_matched_indices = [matched_indices[-1]]
    matched_index = pending_matched_indices[-1]

    row = frame.loc[matched_index]
    memory = ReagentMemory.from_settings(settings, root_dir)
    memory_added = memory.add_record(
        raw_name=first_existing_value(row, ["试剂名称", "chemical_name", "reagent_name"])
        or str(payload.get("reagent_name") or ""),
        cleaned_name=first_existing_value(row, ["cleaned_name", "清洗后名称"])
        or str(payload.get("cleaned_name") or ""),
        standard_name=first_existing_value(row, ["standard_name", "标准化名称"])
        or str(payload.get("standard_name") or ""),
        cas=first_existing_value(row, ["cas", "CAS号"]) or str(payload.get("cas") or ""),
        final_category=final_category,
        confidence=0.0 if non_writable_decision else 1.0,
        reason=str(payload.get("reason") or "人工复核确认后加入高可信试剂记忆库。"),
        source="manual_review_web_ui",
        specification=first_existing_value(row, ["specification", "规格"]) or str(payload.get("specification") or ""),
        unit=first_existing_value(row, ["unit", "规格单位"]) or str(payload.get("unit") or ""),
        need_manual_review=False,
        manual_verified=True,
        track_conflicts=not non_writable_decision,
    )

    now = datetime.now().isoformat(timespec="seconds")
    for column, value in {
        "status": "confirmed",
        "manual_result": final_category,
        "confirmed_at": now,
        "confirmed_by": "web_ui",
        "memory_added": str(bool(memory_added)),
    }.items():
        if column not in frame.columns:
            frame[column] = ""
        for index in pending_matched_indices:
            frame.at[index, column] = value

    frame = frame.drop(columns=["_review_key"], errors="ignore")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_excel(path, index=False)
    if non_writable_decision:
        message = "已确认人工复核项，并入库为不可自动写网页的拒收/复核决策。"
    else:
        message = (
            "已确认人工复核项，并写入高可信试剂记忆库。"
            if memory_added
            else "已确认人工复核项；存在冲突或限制，未设为可自动复用。"
        )
    sync_result: dict[str, Any] | None = None
    if memory_added:
        try:
            sync_result = auto_upload_memory_if_configured(root_dir)
        except Exception as error:  # noqa: BLE001 - review confirmation should survive sync failures
            sync_result = {"ok": False, "message": f"试剂记忆库自动上传失败：{error}"}
    return {
        "confirmed": True,
        "memory_added": bool(memory_added),
        "message": message,
        "memory_sync": sync_result,
    }


def delete_review_item(payload: dict[str, Any], root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    path = root_dir / "data" / "review_queue.xlsx"
    if not path.exists():
        return {"deleted": False, "message": "review_queue.xlsx does not exist."}

    frame = pd.read_excel(path, dtype=str).fillna("")
    if frame.empty:
        return {"deleted": False, "message": "review_queue.xlsx is empty."}

    if "_review_key" not in frame.columns:
        frame["_review_key"] = frame.apply(review_queue_row_key, axis=1)

    review_key = str(payload.get("review_key") or "").strip()
    matched_index: int | None = None
    if review_key:
        matches = frame.index[frame["_review_key"].astype(str) == review_key].tolist()
        if matches:
            matched_index = matches[-1]

    if matched_index is None:
        matched_index = match_review_item_by_fields(frame, payload)

    if matched_index is None:
        return {"deleted": False, "message": "没有找到对应的人工复核记录，可能已被处理或文件已刷新。"}

    frame = frame.drop(index=matched_index).drop(columns=["_review_key"], errors="ignore")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_excel(path, index=False)
    return {"deleted": True, "message": "已删除该人工复核项。"}


def match_review_item_by_fields(frame: pd.DataFrame, payload: dict[str, Any]) -> int | None:
    list_number = str(payload.get("list_number") or "").strip()
    reagent_name = str(payload.get("reagent_name") or "").strip()
    cas = str(payload.get("cas") or "").strip()
    sequence = str(payload.get("sequence") or "").strip()
    matches: list[int] = []
    for index, row in frame.iterrows():
        if list_number and first_existing_value(row, ["试剂清单号", "当前清单号", "清单号", "list_number"]) != list_number:
            continue
        if reagent_name and first_existing_value(row, ["试剂名称", "chemical_name", "reagent_name"]) != reagent_name:
            continue
        if cas and first_existing_value(row, ["cas", "CAS号"]) != cas:
            continue
        if sequence and first_existing_value(row, ["序号", "sequence", "index"]) != sequence:
            continue
        matches.append(index)
    return matches[-1] if matches else None


def review_queue_row_key(row: pd.Series) -> str:
    return "|".join(
        [
            first_existing_value(row, ["试剂清单号", "当前清单号", "清单号", "list_number"]),
            first_existing_value(row, ["cas", "CAS号"]),
            first_existing_value(row, ["试剂名称", "chemical_name", "reagent_name"]),
            first_existing_value(row, ["standard_name", "标准化名称"]),
        ]
    )


def first_existing_value(row: pd.Series, columns: list[str]) -> str:
    for column in columns:
        if column in row.index:
            value = str(row.get(column, "")).strip()
            if value:
                return value
    return ""


def memory_summary(
    *,
    query: str = "",
    category: str = "",
    reusable: str = "",
    conflict: str = "",
    limit: int = 20,
    page: int = 1,
    per_page: int | None = None,
    root_dir: Path = ROOT_DIR,
) -> dict[str, Any]:
    settings = load_settings()
    memory = ReagentMemory.from_settings(settings, root_dir)
    mapping = category_mapping_summary(settings, root_dir)
    safe_per_page = max(1, min(100, int(per_page or limit or 20)))
    total = memory.count_records(
        query=query,
        category=category,
        reusable=reusable,
        conflict=conflict,
    )
    pages = max(1, (total + safe_per_page - 1) // safe_per_page)
    safe_page = max(1, min(pages, int(page or 1)))
    offset = (safe_page - 1) * safe_per_page
    rows = memory.list_records(
        query=query,
        category=category,
        reusable=reusable,
        conflict=conflict,
        limit=safe_per_page,
        offset=offset,
    )
    categories = memory.list_categories()
    return {
        "exists": memory.path.exists(),
        "path": str(memory.path),
        "rows": total,
        "page_rows": len(rows),
        "page": safe_page,
        "pages": pages,
        "per_page": safe_per_page,
        "categories": categories,
        "erp_property_options": mapping["erp_property_options"],
        "review_decision_options": mapping["review_decision_options"],
        "category_mappings": mapping["mappings"],
        "unmapped_rule_categories": mapping["unmapped_rule_categories"],
        "non_writable_rule_categories": mapping["non_writable_rule_categories"],
        "preview": rows,
    }


def update_memory_record(record_id: int, payload: dict[str, Any], root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    settings = load_settings()
    payload = dict(payload)
    if truthy_value(payload.get("reusable")):
        payload["reusable"] = True
        payload["conflict"] = False
        payload["need_manual_review"] = False
        payload["manual_verified"] = True
    final_category = str(payload.get("final_category") or "").strip()
    if final_category:
        options = erp_property_options(settings)
        rule_category = to_rule_category(final_category, settings, root_dir)
        if is_non_writable_rule_category(rule_category, settings, root_dir):
            payload = dict(payload)
            payload["final_category"] = rule_category
            payload["reusable"] = False
        elif final_category not in options:
            mapped_category = to_erp_property(final_category, settings)
            if not mapped_category:
                raise ValueError(f"物化特性类别未映射到 ERP 下拉选项：{final_category}")
            payload = dict(payload)
            payload["final_category"] = mapped_category
    memory = ReagentMemory.from_settings(settings, root_dir)
    updated = memory.update_record(record_id, payload)
    return {"updated": True, "record": updated}


def delete_memory_record(record_id: int, root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    settings = load_settings()
    memory = ReagentMemory.from_settings(settings, root_dir)
    deleted = memory.delete_record(record_id)
    return {"deleted": deleted}


def delete_conflicting_memory(root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    settings = load_settings()
    memory = ReagentMemory.from_settings(settings, root_dir)
    delete_count = memory.count_conflicting_records()
    backup_path = ""
    if delete_count and memory.path.exists():
        log_dir = root_dir / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        backup = log_dir / f"reagent_memory_backup_before_delete_conflicting_{datetime.now():%Y%m%d_%H%M%S}.sqlite"
        shutil.copy2(memory.path, backup)
        backup_path = str(backup)
    deleted = memory.delete_conflicting_records()
    return {
        "deleted": deleted,
        "candidate_count": delete_count,
        "backup": backup_path,
    }


def memory_sync_service(root_dir: Path = ROOT_DIR) -> MemorySyncService:
    load_dotenv(ENV_PATH, override=True)
    return MemorySyncService(root_dir=root_dir, settings=load_settings())


def memory_sync_status(root_dir: Path = ROOT_DIR, *, check_remote: bool | None = None) -> dict[str, Any]:
    service = memory_sync_service(root_dir)
    if check_remote is None:
        check_remote = bool(service.config.get("check_remote_on_startup"))
    return service.status(check_remote=bool(check_remote))


def test_memory_sync_connection(root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    try:
        return memory_sync_service(root_dir).test_connection()
    except MemorySyncError as error:
        error.payload.setdefault("ok", False)
        error.payload.setdefault("message", str(error))
        raise


def upload_memory_sync(root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    return memory_sync_service(root_dir).upload()


def download_memory_sync(root_dir: Path = ROOT_DIR, *, force: bool = False) -> dict[str, Any]:
    return memory_sync_service(root_dir).download(force=force)


def memory_sync_versions(root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    return memory_sync_service(root_dir).versions()


def auto_upload_memory_if_configured(root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    service = memory_sync_service(root_dir)
    if not service.config.get("enabled") or not service.config.get("auto_upload_after_memory_change"):
        return {"ok": True, "skipped": True, "message": "试剂库自动上传未启用。"}
    memory_path = service.memory_path
    if not memory_path.exists():
        return {"ok": True, "skipped": True, "message": "本地试剂记忆库不存在，跳过自动上传。"}
    state = service.read_state()
    last_upload = str(state.get("last_upload_at") or "")
    local = service.local_summary()
    if last_upload and str(local.get("updated_at") or "") <= last_upload:
        return {"ok": True, "skipped": True, "message": "试剂记忆库无新增变更，跳过自动上传。"}
    return service.upload()


def import_approval_suggestions_to_memory(root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    settings = load_settings()
    memory = ReagentMemory.from_settings(settings, root_dir)
    log_dir = root_dir / "data" / "logs"
    paths = sorted(
        log_dir.glob("approval_suggestions*.xlsx"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    stats: dict[str, Any] = {
        "imported": 0,
        "existing": 0,
        "conflicts": 0,
        "skipped_manual_review": 0,
        "skipped_low_confidence": 0,
        "candidate_manual_review": 0,
        "candidate_low_confidence": 0,
        "skipped_missing_category": 0,
        "candidate_unmapped_category": 0,
        "skipped_missing_identity": 0,
        "skipped_duplicate_source_row": 0,
        "skipped_memory_source": 0,
        "errors": [],
        "files": [],
        "scanned": 0,
    }
    seen_source_rows: set[tuple[str, str, str, str, str]] = set()
    for path in paths:
        if not path.exists():
            continue
        stats["files"].append(str(path))
        try:
            frame = pd.read_excel(path, dtype=str).fillna("")
        except Exception as error:  # noqa: BLE001
            stats["errors"].append(f"{path.name}: {error}")
            continue

        for _, row in frame.iterrows():
            stats["scanned"] += 1
            suggestion = row.to_dict()
            final_category = str(suggestion.get("最终建议类别") or "").strip()
            confidence = parse_float(suggestion.get("置信度"), 0.0)
            manual_review = truthy_value(suggestion.get("需人工复核"))
            raw_name = str(suggestion.get("试剂名称") or "").strip()
            cleaned_name = str(suggestion.get("清洗后名称") or "").strip()
            standard_name = str(suggestion.get("标准化名称") or "").strip()
            cas = str(suggestion.get("CAS号") or "").strip()
            query_source = str(suggestion.get("\u67e5\u8be2\u6765\u6e90") or "").strip()
            if query_source == "reagent_memory":
                stats["skipped_memory_source"] += 1
                continue
            source_key = (cas.lower(), standard_name.lower(), cleaned_name.lower(), raw_name.lower(), final_category)

            if source_key in seen_source_rows:
                stats["skipped_duplicate_source_row"] += 1
                continue
            seen_source_rows.add(source_key)

            if not final_category:
                stats["skipped_missing_category"] += 1
                continue
            if not any((cas, standard_name, cleaned_name, raw_name)):
                stats["skipped_missing_identity"] += 1
                continue
            rule_category = to_rule_category(final_category, settings, root_dir)
            if is_non_writable_rule_category(rule_category, settings, root_dir):
                erp_category = rule_category
                category_mapped = False
            else:
                erp_category = to_erp_property(final_category, settings) or final_category
                category_mapped = erp_category in erp_property_options(settings)

            existing = memory.find_any(
                cas=cas,
                standard_name=standard_name,
                cleaned_name=cleaned_name,
                raw_name=raw_name,
                final_category=erp_category,
            )
            if existing:
                stats["existing"] += 1
                continue

            if manual_review or confidence < memory.min_confidence or not category_mapped:
                memory.add_record(
                    raw_name=raw_name,
                    cleaned_name=cleaned_name,
                    standard_name=standard_name,
                    cas=cas,
                    final_category=erp_category,
                    confidence=confidence,
                    reason=str(suggestion.get("规则原因") or suggestion.get("证据") or "").strip()
                    or (
                        f"规则类别 {final_category} 未映射到 ERP 下拉选项"
                        if not category_mapped
                        else "人工复核历史候选"
                        if manual_review
                        else "低置信度历史候选"
                    ),
                    source="approval_suggestions_candidate",
                    url=str(suggestion.get("查询URL") or "").strip(),
                    specification=str(suggestion.get("规格") or "").strip(),
                    unit=str(suggestion.get("规格单位") or "").strip(),
                    need_manual_review=True,
                    manual_verified=False,
                    track_conflicts=False,
                )
                if manual_review:
                    stats["candidate_manual_review"] += 1
                    stats["skipped_manual_review"] += 1
                elif not category_mapped:
                    stats["candidate_unmapped_category"] += 1
                else:
                    stats["candidate_low_confidence"] += 1
                    stats["skipped_low_confidence"] += 1
                continue

            suggestion = dict(suggestion)
            suggestion["最终建议类别"] = erp_category
            imported = memory.remember_suggestion(suggestion)
            if imported:
                stats["imported"] += 1
            else:
                stats["conflicts"] += 1
    if stats["imported"] > 0:
        try:
            sync_result = auto_upload_memory_if_configured(root_dir)
            if not sync_result.get("skipped"):
                stats["memory_sync"] = sync_result
        except Exception as error:  # noqa: BLE001 - import result should survive sync failures
            stats["memory_sync"] = {"ok": False, "message": f"试剂记忆库自动上传失败：{error}"}
    return stats


def truthy_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "是", "需人工复核"}


def parse_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def runtime_config_snapshot() -> dict[str, Any]:
    load_dotenv(ENV_PATH)
    settings = load_settings()
    approval = settings.get("approval", {}) or {}
    schedule = scheduler_config(settings)
    dingtalk = dingtalk_notification_config(settings)
    dingtalk_stream = dingtalk_stream_config(settings)
    llm = settings.get("llm", {}) or {}
    app_settings = settings.get("app", {}) or {}
    sync_config = memory_sync_config(settings)
    mapping = category_mapping_summary(settings, ROOT_DIR)
    provider = get_llm_provider(os.getenv("LLM_PROVIDER") or llm.get("provider") or "siliconflow")
    configured_base_url = os.getenv("LLM_BASE_URL") or (
        os.getenv("SILICONFLOW_BASE_URL") if provider.id == "siliconflow" else ""
    ) or llm.get("base_url", "")
    configured_model = (
        os.getenv("LLM_MODEL")
        or (os.getenv("SILICONFLOW_MODEL") if provider.id == "siliconflow" else "")
        or llm.get("model", "")
        or provider_default_model(provider.id)
    )
    return {
        "app_version": app_version(),
        "app_frozen": bool(getattr(sys, "frozen", False)),
        "app_dry_run": "true" if app_settings.get("dry_run") else "false",
        "update_token_configured": bool(
            os.getenv("REAGENT_APPROVAL_UPDATE_TOKEN", "").strip()
            or os.getenv("GITHUB_TOKEN", "").strip()
            or os.getenv("GH_TOKEN", "").strip()
        ),
        "erp_url_configured": bool(os.getenv("ERP_URL", "").strip()),
        "erp_url": os.getenv("ERP_URL", ""),
        "erp_username_configured": bool(os.getenv("ERP_USERNAME", "").strip()),
        "erp_username": os.getenv("ERP_USERNAME", ""),
        "erp_password_configured": bool(os.getenv("ERP_PASSWORD", "").strip()),
        "llm_api_key_configured": configured_llm_api_key(provider.id),
        "siliconflow_api_key_configured": configured_llm_api_key(provider.id),
        "auto_pass": os.getenv("AUTO_PASS", "false"),
        "target_list_number": os.getenv("TARGET_LIST_NUMBER", ""),
        "process_all_todos": os.getenv("PROCESS_ALL_TODOS", "false"),
        "process_all_todos_max": os.getenv("PROCESS_ALL_TODOS_MAX", "50"),
        "approval_write_mode": normalize_web_write_mode(
            os.getenv(
                "APPROVAL_WRITE_MODE",
                str(approval.get("write_mode", "disabled")),
            )
        ),
        "approval_write_min_confidence": os.getenv(
            "APPROVAL_WRITE_MIN_CONFIDENCE",
            str(approval.get("write_min_confidence", 0.8)),
        ),
        "approval_write_batch_size": os.getenv(
            "APPROVAL_WRITE_BATCH_SIZE",
            str(approval.get("write_batch_size", 3)),
        ),
        "approval_parallel_workers": os.getenv(
            "APPROVAL_PARALLEL_WORKERS",
            str(approval.get("parallel_workers", 3)),
        ),
        "scheduler_enabled": "true" if schedule.get("enabled") else "false",
        "scheduler_mode": schedule.get("mode", "interval"),
        "scheduler_interval_hours": str(schedule.get("interval_hours", 6)),
        "scheduler_daily_time": schedule.get("daily_time", "16:00"),
        "scheduler_use_default_run_policy": "true" if schedule.get("use_default_run_policy", True) else "false",
        "scheduler_process_all_todos_max": str(schedule.get("process_all_todos_max", 50)),
        "scheduler_approval_write_mode": normalize_web_write_mode(str(schedule.get("approval_write_mode", "disabled"))),
        "scheduler_approval_write_min_confidence": str(schedule.get("approval_write_min_confidence", "0.8")),
        "scheduler_auto_pass": "true" if schedule.get("auto_pass") else "false",
        "scheduler_skip_manual_review_lists": "true" if schedule.get("skip_manual_review_lists", True) else "false",
        "dingtalk_notification_enabled": "true" if dingtalk.get("enabled") else "false",
        "dingtalk_at_all": "true" if dingtalk.get("at_all", True) else "false",
        "dingtalk_stream_enabled": "true" if dingtalk_stream.get("enabled") else "false",
        "dingtalk_stream_corp_id": dingtalk_stream.get("corp_id", ""),
        "dingtalk_stream_agent_id": dingtalk_stream.get("agent_id", ""),
        "dingtalk_stream_robot_code": dingtalk_stream.get("robot_code", ""),
        "dingtalk_stream_open_conversation_id": dingtalk_stream.get("open_conversation_id", ""),
        "dingtalk_stream_client_id": os.getenv(str(dingtalk_stream.get("client_id_env")), ""),
        "dingtalk_stream_client_secret_configured": bool(
            os.getenv(str(dingtalk_stream.get("client_secret_env")), "").strip()
        ),
        "dingtalk_stream_api_token_configured": bool(
            os.getenv(str(dingtalk_stream.get("api_token_env")), "").strip()
        ),
        "memory_sync_enabled": "true" if sync_config.get("enabled") else "false",
        "memory_sync_base_url": sync_config.get("base_url", ""),
        "memory_sync_remote_dir": sync_config.get("remote_dir", ""),
        "memory_sync_username": os.getenv(str(sync_config.get("username_env") or ""), ""),
        "memory_sync_password_configured": bool(os.getenv(str(sync_config.get("password_env") or ""), "").strip()),
        "memory_sync_auto_upload_after_memory_change": (
            "true" if sync_config.get("auto_upload_after_memory_change") else "false"
        ),
        "memory_sync_check_remote_on_startup": "true" if sync_config.get("check_remote_on_startup") else "false",
        "memory_sync_keep_versions": str(sync_config.get("keep_versions", 10)),
        "llm_provider": provider.id,
        "llm_provider_label": provider.label,
        "llm_provider_options": provider_options(),
        "llm_base_url": provider_base_url(provider.id, configured_base_url),
        "llm_model": configured_model,
        "llm_timeout_seconds": llm.get("timeout_seconds", 45),
        "llm_max_retries": llm.get("max_retries", 1),
        "erp_property_options": mapping["erp_property_options"],
        "review_decision_options": mapping["review_decision_options"],
        "category_mappings": mapping["mappings"],
        "unmapped_rule_categories": mapping["unmapped_rule_categories"],
        "non_writable_rule_categories": mapping["non_writable_rule_categories"],
    }


def save_runtime_config(form: dict[str, str]) -> dict[str, Any]:
    provider_id = form.get("llm_provider", "siliconflow").strip() or "siliconflow"
    provider = get_llm_provider(provider_id)
    llm_base_url = provider_base_url(provider.id, form.get("llm_base_url", "").strip())
    llm_model = form.get("llm_model", "").strip() or provider_default_model(provider.id)
    env_updates = {
        "ERP_URL": form.get("erp_url", "").strip(),
        "ERP_USERNAME": form.get("erp_username", "").strip(),
        "AUTO_PASS": form.get("auto_pass", "false").strip().lower(),
        "TARGET_LIST_NUMBER": form.get("target_list_number", "").strip(),
        "PROCESS_ALL_TODOS": form.get("process_all_todos", "false").strip().lower(),
        "PROCESS_ALL_TODOS_MAX": form.get("process_all_todos_max", "50").strip() or "50",
        "APPROVAL_WRITE_MODE": normalize_web_write_mode(form.get("approval_write_mode", "")),
        "APPROVAL_WRITE_MIN_CONFIDENCE": form.get("approval_write_min_confidence", "0.8").strip() or "0.8",
        "APPROVAL_WRITE_BATCH_SIZE": form.get("approval_write_batch_size", "3").strip() or "3",
        "LLM_PROVIDER": provider.id,
        "LLM_BASE_URL": llm_base_url,
        "LLM_MODEL": llm_model,
    }
    update_token = form.get("update_token", "").strip()
    if update_token:
        env_updates["REAGENT_APPROVAL_UPDATE_TOKEN"] = update_token
    if provider.id == "siliconflow":
        env_updates["SILICONFLOW_BASE_URL"] = llm_base_url
        env_updates["SILICONFLOW_MODEL"] = llm_model

    erp_password = form.get("erp_password", "").strip()
    if erp_password:
        env_updates["ERP_PASSWORD"] = erp_password

    api_key = (form.get("llm_api_key", "") or form.get("siliconflow_api_key", "")).strip()
    if api_key:
        env_updates["LLM_API_KEY"] = api_key
        env_updates[provider.api_key_env] = api_key

    dingtalk_stream_client_id = form.get("dingtalk_stream_client_id", "").strip()
    if dingtalk_stream_client_id:
        env_updates["DINGTALK_STREAM_CLIENT_ID"] = dingtalk_stream_client_id
    dingtalk_stream_client_secret = form.get("dingtalk_stream_client_secret", "").strip()
    if dingtalk_stream_client_secret:
        env_updates["DINGTALK_STREAM_CLIENT_SECRET"] = dingtalk_stream_client_secret
    dingtalk_stream_api_token = form.get("dingtalk_stream_api_token", "").strip()
    if dingtalk_stream_api_token:
        env_updates["DINGTALK_STREAM_API_TOKEN"] = dingtalk_stream_api_token

    memory_sync_username = form.get("memory_sync_username", "").strip()
    if memory_sync_username:
        env_updates["JIANGUOYUN_WEBDAV_USER"] = memory_sync_username
    memory_sync_password = form.get("memory_sync_password", "").strip()
    if memory_sync_password:
        env_updates["JIANGUOYUN_WEBDAV_PASSWORD"] = memory_sync_password

    update_env_file(ENV_PATH, env_updates)

    settings = load_settings()
    app = settings.setdefault("app", {})
    app["dry_run"] = form.get("app_dry_run", "false").strip().lower() == "true"

    llm = settings.setdefault("llm", {})
    llm["provider"] = provider.id
    llm["base_url"] = llm_base_url
    llm["model"] = llm_model
    llm["timeout_seconds"] = coerce_int(form.get("llm_timeout_seconds", ""), llm.get("timeout_seconds", 45))
    llm["max_retries"] = coerce_int(form.get("llm_max_retries", ""), llm.get("max_retries", 1))

    approval = settings.setdefault("approval", {})
    approval["write_mode"] = env_updates["APPROVAL_WRITE_MODE"]
    approval["write_min_confidence"] = coerce_float(
        env_updates["APPROVAL_WRITE_MIN_CONFIDENCE"],
        approval.get("write_min_confidence", 0.8),
    )
    approval["write_batch_size"] = coerce_int(
        env_updates["APPROVAL_WRITE_BATCH_SIZE"],
        approval.get("write_batch_size", 3),
    )
    approval["parallel_workers"] = coerce_int(
        form.get("approval_parallel_workers", ""),
        approval.get("parallel_workers", 3),
    )

    scheduler = settings.setdefault("scheduler", {})
    scheduler["enabled"] = form.get("scheduler_enabled", "false").strip().lower() == "true"
    scheduler["mode"] = form.get("scheduler_mode", "interval").strip() or "interval"
    scheduler["interval_hours"] = coerce_int(
        form.get("scheduler_interval_hours", ""),
        scheduler.get("interval_hours", 6),
    )
    scheduler["daily_time"] = form.get("scheduler_daily_time", "").strip() or scheduler.get("daily_time", "16:00")
    scheduler["use_default_run_policy"] = (
        form.get("scheduler_use_default_run_policy", "false").strip().lower() == "true"
    )
    scheduler["process_all_todos_max"] = coerce_int(
        form.get("scheduler_process_all_todos_max", ""),
        scheduler.get("process_all_todos_max", 50),
    )
    scheduler["approval_write_mode"] = normalize_web_write_mode(
        form.get("scheduler_approval_write_mode", ""),
        default=normalize_web_write_mode(str(scheduler.get("approval_write_mode", "disabled"))),
    )
    scheduler["approval_write_min_confidence"] = (
        form.get("scheduler_approval_write_min_confidence", "").strip()
        or scheduler.get("approval_write_min_confidence", "0.8")
    )
    scheduler["auto_pass"] = form.get("scheduler_auto_pass", "false").strip().lower() == "true"
    scheduler["skip_manual_review_lists"] = (
        form.get("scheduler_skip_manual_review_lists", "false").strip().lower() == "true"
    )

    notification = settings.setdefault("notification", {})
    dingtalk = notification.setdefault("dingtalk", {})
    dingtalk["enabled"] = form.get("dingtalk_notification_enabled", "false").strip().lower() == "true"
    dingtalk["at_all"] = form.get("dingtalk_at_all", "false").strip().lower() == "true"

    stream = settings.setdefault("dingtalk_stream", {})
    stream["enabled"] = form.get("dingtalk_stream_enabled", "false").strip().lower() == "true"
    stream["corp_id"] = form.get("dingtalk_stream_corp_id", "").strip()
    stream["agent_id"] = form.get("dingtalk_stream_agent_id", "").strip()
    stream["robot_code"] = form.get("dingtalk_stream_robot_code", "").strip()
    stream["open_conversation_id"] = form.get("dingtalk_stream_open_conversation_id", "").strip()
    stream["client_id_env"] = "DINGTALK_STREAM_CLIENT_ID"
    stream["client_secret_env"] = "DINGTALK_STREAM_CLIENT_SECRET"
    stream["api_token_env"] = "DINGTALK_STREAM_API_TOKEN"
    stream["require_at_in_group"] = True

    memory_sync = settings.setdefault("memory_sync", {})
    memory_sync["enabled"] = form.get("memory_sync_enabled", "false").strip().lower() == "true"
    memory_sync["provider"] = "webdav"
    memory_sync["base_url"] = (
        form.get("memory_sync_base_url", "").strip()
        or memory_sync.get("base_url")
        or "https://dav.jianguoyun.com/dav/"
    )
    memory_sync["remote_dir"] = (
        form.get("memory_sync_remote_dir", "").strip()
        or memory_sync.get("remote_dir")
        or "reagent-approval-bot"
    )
    memory_sync["username_env"] = "JIANGUOYUN_WEBDAV_USER"
    memory_sync["password_env"] = "JIANGUOYUN_WEBDAV_PASSWORD"
    memory_sync["keep_versions"] = coerce_int(
        form.get("memory_sync_keep_versions", ""),
        memory_sync.get("keep_versions", 10),
    )
    memory_sync["auto_upload_after_memory_change"] = (
        form.get("memory_sync_auto_upload_after_memory_change", "false").strip().lower() == "true"
    )
    memory_sync["check_remote_on_startup"] = (
        form.get("memory_sync_check_remote_on_startup", "false").strip().lower() == "true"
    )

    save_settings(settings)

    return runtime_config_snapshot()


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue

        key = line.split("=", 1)[0].strip()
        if key in updates:
            output.append(f"{key}={escape_env_value(updates[key])}")
            seen.add(key)
        else:
            output.append(line)

    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={escape_env_value(value)}")

    atomic_write_text(path, "\n".join(output).rstrip() + "\n")
    load_dotenv(path, override=True)


def escape_env_value(value: str) -> str:
    if any(char.isspace() for char in value) or "#" in value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def coerce_int(value: str, fallback: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def coerce_float(value: str, fallback: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


manager = AutomationJobManager()
