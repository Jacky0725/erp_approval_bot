from __future__ import annotations

import contextlib
import io
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

import pandas as pd
import yaml
from dotenv import load_dotenv

from llm_providers import (
    configured_llm_api_key,
    get_llm_provider,
    provider_base_url,
    provider_default_model,
    provider_options,
)
from category_mapper import (
    category_mapping_summary,
    erp_property_options,
    is_non_writable_rule_category,
    to_erp_property,
    to_rule_category,
)
from reagent_memory import ReagentMemory
from runtime_paths import ensure_runtime_layout, runtime_root, source_root


ensure_runtime_layout()
ROOT_DIR = runtime_root()
SOURCE_ROOT = source_root()
CONFIG_PATH = ROOT_DIR / "config" / "settings.yaml"
ENV_PATH = ROOT_DIR / ".env"
LOG_DIR = ROOT_DIR / "data" / "logs"
REVIEW_QUEUE_PATH = ROOT_DIR / "data" / "review_queue.xlsx"
WEB_RUN_STATE_PATH = LOG_DIR / "web_run_state.yaml"
TODO_TASKS_PATH = LOG_DIR / "todo_tasks.xlsx"
TODO_TASKS_JSON_PATH = LOG_DIR / "todo_tasks.json"


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
    with CONFIG_PATH.open("w", encoding="utf-8") as file:
        yaml.safe_dump(settings, file, allow_unicode=True, sort_keys=False)


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
    def __init__(self, lines: list[str], path: Path, limit: int = 800) -> None:
        self.lines = lines
        self.path = path
        self.limit = limit
        self._lock = threading.RLock()
        self._file = path.open("a", encoding="utf-8")

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not text:
            return 0
        cleaned_text = repair_display_text(text)
        with self._lock:
            self._file.write(cleaned_text)
            self._file.flush()
            for line in cleaned_text.splitlines():
                if line.strip():
                    self.lines.append(line)
            if len(self.lines) > self.limit:
                del self.lines[: len(self.lines) - self.limit]
        return len(text)

    def flush(self) -> None:
        with self._lock:
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()


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
    _lock: threading.RLock = field(default_factory=threading.RLock)

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

    def status(self) -> dict[str, Any]:
        with self._lock:
            if not self.running and not self.action:
                return self._status_from_persisted_state()
            log_tail = self.lines[-160:]
            running = self.running
            success = self.success
            error = self.error
            health = run_health(log_tail, success, error)
            return {
                "running": running,
                "action": self.action,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "success": success,
                "error": error,
                "result_label": result_label(running, success, error, health),
                "result_health": health,
                "log_tail": log_tail,
                "workflow": workflow_summary(log_tail, running=running, success=success, error=error),
            }

    def _run(self, action: str, options: dict[str, str]) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / "web_run_stdout.txt"
        writer = LineBufferWriter(self.lines, log_path)

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
            with self._lock:
                self.running = False
                self.finished_at = datetime.now().isoformat(timespec="seconds")
                self.success = success
                self.error = error
                self._persist_state()

    def _persist_state(self) -> None:
        WEB_RUN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        log_tail = [repair_display_text(line) for line in self.lines[-160:]]
        payload = {
            "running": self.running,
            "action": self.action,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "success": self.success,
            "error": repair_display_text(self.error),
            "log_tail": log_tail,
        }
        with WEB_RUN_STATE_PATH.open("w", encoding="utf-8") as file:
            yaml.safe_dump(payload, file, allow_unicode=True, sort_keys=False)

    def _status_from_persisted_state(self) -> dict[str, Any]:
        if not WEB_RUN_STATE_PATH.exists():
            return {
                "running": False,
                "action": "",
                "started_at": "",
                "finished_at": "",
                "success": None,
                "error": "",
                "result_label": "未运行",
                "log_tail": [],
                "workflow": workflow_summary([], running=False, success=None, error=""),
            }
        try:
            with WEB_RUN_STATE_PATH.open("r", encoding="utf-8") as file:
                payload = yaml.safe_load(file) or {}
        except Exception:
            payload = {}
        log_tail = [repair_display_text(line) for line in (payload.get("log_tail") or [])]
        success = payload.get("success")
        error = repair_display_text(payload.get("error") or "")
        health = run_health(log_tail, success, error)
        return {
            "running": False,
            "action": str(payload.get("action") or ""),
            "started_at": str(payload.get("started_at") or ""),
            "finished_at": str(payload.get("finished_at") or ""),
            "success": success,
            "error": error,
            "result_label": result_label(False, success, error, health),
            "result_health": health,
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
            self._process = process
            if self._stop_requested:
                self._terminate_process_tree(process)
        try:
            assert process.stdout is not None
            for line in process.stdout:
                writer.write(line)
            return process.wait()
        finally:
            with self._lock:
                self._process = None

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
        }
        return {
            key: value
            for key, value in options.items()
            if key in allowed_keys and value is not None
        }


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


def result_label(running: bool, success: bool | None, error: str, health: str = "") -> str:
    if running:
        return "运行中"
    if success is True:
        if health == "warning":
            return "需检查"
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
    artifacts = []
    for path in sorted(log_dir.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.suffix.lower() not in wanted_suffixes:
            continue
        stat = path.stat()
        artifacts.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "download_url": f"/artifacts/{path.name}",
            }
        )
    return artifacts[:24]


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
    path = root_dir / "data" / "logs" / "todo_tasks.xlsx"
    if not path.exists():
        return {"exists": False, "rows": 0, "tasks": [], "modified": ""}

    try:
        frame = pd.read_excel(path, dtype=str).fillna("")
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "error": str(exc), "rows": 0, "tasks": [], "modified": ""}

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


def clear_todo_task_cache(root_dir: Path = ROOT_DIR) -> None:
    for path in (
        TODO_TASKS_PATH if root_dir == ROOT_DIR else root_dir / "data" / "logs" / "todo_tasks.xlsx",
        TODO_TASKS_JSON_PATH if root_dir == ROOT_DIR else root_dir / "data" / "logs" / "todo_tasks.json",
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            print(f"Could not clear old todo cache {path}: {error}")


def review_queue_summary(root_dir: Path = ROOT_DIR) -> dict[str, Any]:
    path = root_dir / "data" / "review_queue.xlsx"
    if not path.exists():
        return {"exists": False, "rows": 0, "pending": 0, "preview": [], "list_numbers": []}

    try:
        frame = pd.read_excel(path, dtype=str).fillna("")
    except Exception as exc:  # noqa: BLE001
        return {"exists": True, "error": str(exc), "rows": 0, "pending": 0, "preview": [], "list_numbers": []}

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
    for _, row in pending_frame.head(120).iterrows():
        reason = first_existing(row, ["reason", "原因", "复核原因", "manual_review_reason"])
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
            }
        )

    return {
        "exists": True,
        "rows": int(len(frame)),
        "pending": int(len(pending_frame)),
        "preview": preview,
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

    review_key = str(payload.get("review_key") or "").strip()
    matched_index: int | None = None
    if review_key:
        matches = frame.index[frame["_review_key"].astype(str) == review_key].tolist()
        if matches:
            matched_index = matches[-1]

    if matched_index is None:
        matched_index = match_review_item_by_fields(frame, payload)

    if matched_index is None:
        return {"confirmed": False, "message": "没有找到对应的人工复核记录，可能已被处理或文件已刷新。"}

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
        frame.at[matched_index, column] = value

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
    return {
        "confirmed": True,
        "memory_added": bool(memory_added),
        "message": message,
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
    llm = settings.get("llm", {}) or {}
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
        "approval_write_mode": os.getenv(
            "APPROVAL_WRITE_MODE",
            str(approval.get("write_mode", "disabled")),
        ),
        "approval_write_min_confidence": os.getenv(
            "APPROVAL_WRITE_MIN_CONFIDENCE",
            str(approval.get("write_min_confidence", 0.8)),
        ),
        "approval_parallel_workers": os.getenv(
            "APPROVAL_PARALLEL_WORKERS",
            str(approval.get("parallel_workers", 3)),
        ),
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
        "APPROVAL_WRITE_MODE": form.get("approval_write_mode", "disabled").strip() or "disabled",
        "APPROVAL_WRITE_MIN_CONFIDENCE": form.get("approval_write_min_confidence", "0.8").strip() or "0.8",
        "LLM_PROVIDER": provider.id,
        "LLM_BASE_URL": llm_base_url,
        "LLM_MODEL": llm_model,
    }
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

    update_env_file(ENV_PATH, env_updates)

    settings = load_settings()
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
    approval["parallel_workers"] = coerce_int(
        form.get("approval_parallel_workers", ""),
        approval.get("parallel_workers", 3),
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

    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
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
