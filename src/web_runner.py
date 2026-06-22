from __future__ import annotations

import contextlib
import io
import os
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
import yaml
from dotenv import load_dotenv

from browser_bot import BrowserBot


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config" / "settings.yaml"
ENV_PATH = ROOT_DIR / ".env"
LOG_DIR = ROOT_DIR / "data" / "logs"
REVIEW_QUEUE_PATH = ROOT_DIR / "data" / "review_queue.xlsx"
WEB_RUN_STATE_PATH = LOG_DIR / "web_run_state.yaml"
TODO_TASKS_PATH = LOG_DIR / "todo_tasks.xlsx"


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
        with self._lock:
            self._file.write(text)
            self._file.flush()
            for line in text.splitlines():
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
        super().close()


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
    _lock: threading.Lock = field(default_factory=threading.Lock)

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
            self._persist_state()
            self._thread = threading.Thread(
                target=self._run,
                args=(action, options),
                name="approval-web-runner",
                daemon=True,
            )
            self._thread.start()
            return {"started": True, "message": f"已启动任务：{action}"}

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
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                print(f"{datetime.now().isoformat(timespec='seconds')} START {action}")
                self._run_bot_action(action, options)
                print(f"{datetime.now().isoformat(timespec='seconds')} END {action}")
            success = True
            error = ""
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
        payload = {
            "running": self.running,
            "action": self.action,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "success": self.success,
            "error": self.error,
            "log_tail": self.lines[-160:],
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
        log_tail = payload.get("log_tail") or []
        success = payload.get("success")
        error = str(payload.get("error") or "")
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

    def _run_bot_action(self, action: str, options: dict[str, str]) -> None:
        load_dotenv(self.root_dir / ".env")
        settings = load_settings()
        bot = BrowserBot(settings=settings, root_dir=self.root_dir)

        with temporary_env(self._env_overrides(options)):
            bot.target_list_number = os.getenv("TARGET_LIST_NUMBER", "").strip()
            bot.target_list_numbers = parse_target_list_numbers(os.getenv("TARGET_LIST_NUMBERS", ""))

            if action == "debug_capture":
                bot.run_debug_capture()
            elif action == "judgement_capture":
                bot.run_reagent_judgement_capture()
            elif action == "todo_export":
                bot.run_todo_tasks_export()
            elif action == "suggestions":
                bot.run_semi_auto_approval_suggestions()
            else:
                raise ValueError(f"Unknown automation action: {action}")

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
        "browser session failed",
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
    if "approval write candidate" in text or "save result for sequence" in text or "apply_approval_write_mode" in text:
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
                "timestamp": first_existing(row, ["timestamp", "时间"]),
                "list_number": first_existing(row, ["试剂清单号", "当前清单号", "清单号", "list_number"]),
                "reagent_name": first_existing(row, ["试剂名称", "chemical_name", "reagent_name"]),
                "cas": first_existing(row, ["cas", "CAS号"]),
                "standard_name": first_existing(row, ["standard_name", "标准化名称"]),
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


def runtime_config_snapshot() -> dict[str, Any]:
    load_dotenv(ENV_PATH)
    settings = load_settings()
    approval = settings.get("approval", {}) or {}
    llm = settings.get("llm", {}) or {}
    return {
        "erp_url_configured": bool(os.getenv("ERP_URL", "").strip()),
        "erp_url": os.getenv("ERP_URL", ""),
        "erp_username_configured": bool(os.getenv("ERP_USERNAME", "").strip()),
        "erp_username": os.getenv("ERP_USERNAME", ""),
        "erp_password_configured": bool(os.getenv("ERP_PASSWORD", "").strip()),
        "siliconflow_api_key_configured": bool(os.getenv("SILICONFLOW_API_KEY", "").strip()),
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
        "llm_provider": llm.get("provider", ""),
        "llm_base_url": llm.get("base_url", ""),
        "llm_model": llm.get("model", ""),
        "llm_timeout_seconds": llm.get("timeout_seconds", 45),
        "llm_max_retries": llm.get("max_retries", 1),
    }


def save_runtime_config(form: dict[str, str]) -> dict[str, Any]:
    env_updates = {
        "ERP_URL": form.get("erp_url", "").strip(),
        "ERP_USERNAME": form.get("erp_username", "").strip(),
        "AUTO_PASS": form.get("auto_pass", "false").strip().lower(),
        "TARGET_LIST_NUMBER": form.get("target_list_number", "").strip(),
        "PROCESS_ALL_TODOS": form.get("process_all_todos", "false").strip().lower(),
        "PROCESS_ALL_TODOS_MAX": form.get("process_all_todos_max", "50").strip() or "50",
        "APPROVAL_WRITE_MODE": form.get("approval_write_mode", "disabled").strip() or "disabled",
        "APPROVAL_WRITE_MIN_CONFIDENCE": form.get("approval_write_min_confidence", "0.8").strip() or "0.8",
        "LLM_PROVIDER": form.get("llm_provider", "siliconflow").strip() or "siliconflow",
        "SILICONFLOW_BASE_URL": form.get("llm_base_url", "").strip(),
        "SILICONFLOW_MODEL": form.get("llm_model", "").strip(),
    }

    erp_password = form.get("erp_password", "").strip()
    if erp_password:
        env_updates["ERP_PASSWORD"] = erp_password

    api_key = form.get("siliconflow_api_key", "").strip()
    if api_key:
        env_updates["SILICONFLOW_API_KEY"] = api_key

    update_env_file(ENV_PATH, env_updates)

    settings = load_settings()
    llm = settings.setdefault("llm", {})
    llm["provider"] = env_updates["LLM_PROVIDER"]
    if env_updates["SILICONFLOW_BASE_URL"]:
        llm["base_url"] = env_updates["SILICONFLOW_BASE_URL"]
    if env_updates["SILICONFLOW_MODEL"]:
        llm["model"] = env_updates["SILICONFLOW_MODEL"]
    llm["timeout_seconds"] = coerce_int(form.get("llm_timeout_seconds", ""), llm.get("timeout_seconds", 45))
    llm["max_retries"] = coerce_int(form.get("llm_max_retries", ""), llm.get("max_retries", 1))

    approval = settings.setdefault("approval", {})
    approval["write_mode"] = env_updates["APPROVAL_WRITE_MODE"]
    approval["write_min_confidence"] = coerce_float(
        env_updates["APPROVAL_WRITE_MIN_CONFIDENCE"],
        approval.get("write_min_confidence", 0.8),
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
