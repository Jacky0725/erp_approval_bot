from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


STATE_FILENAME = "scheduler_state.yaml"


def normalize_checkbox(value: Any) -> str:
    return "true" if str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"} else "false"


def scheduler_defaults() -> dict[str, Any]:
    return {
        "enabled": False,
        "mode": "interval",
        "interval_hours": 6,
        "daily_time": "16:00",
        "use_default_run_policy": True,
        "process_all_todos_max": 50,
        "approval_write_mode": "multi_page",
        "approval_write_min_confidence": "0.8",
        "auto_pass": False,
        "skip_manual_review_lists": True,
    }


def scheduler_config(settings: dict[str, Any] | None) -> dict[str, Any]:
    settings = settings or {}
    configured = settings.get("scheduler", {}) or {}
    result = scheduler_defaults()
    result.update(configured)
    result["enabled"] = coerce_bool(result.get("enabled"))
    result["mode"] = str(result.get("mode") or "interval").strip().lower()
    if result["mode"] not in {"interval", "daily"}:
        result["mode"] = "interval"
    result["interval_hours"] = max(1, coerce_int(result.get("interval_hours"), 6))
    result["daily_time"] = normalize_daily_time(result.get("daily_time"))
    result["use_default_run_policy"] = coerce_bool(result.get("use_default_run_policy", True))
    if result["use_default_run_policy"]:
        approval = settings.get("approval", {}) or {}
        result["process_all_todos_max"] = max(
            1,
            coerce_int(os.getenv("PROCESS_ALL_TODOS_MAX") or result.get("process_all_todos_max"), 50),
        )
        result["approval_write_mode"] = (
            os.getenv("APPROVAL_WRITE_MODE")
            or str(approval.get("write_mode") or result.get("approval_write_mode") or "multi_page")
        ).strip() or "multi_page"
        result["approval_write_min_confidence"] = (
            os.getenv("APPROVAL_WRITE_MIN_CONFIDENCE")
            or str(approval.get("write_min_confidence") or result.get("approval_write_min_confidence") or "0.8")
        ).strip() or "0.8"
        result["auto_pass"] = coerce_bool(os.getenv("AUTO_PASS") or result.get("auto_pass"))
    else:
        result["process_all_todos_max"] = max(1, coerce_int(result.get("process_all_todos_max"), 50))
        result["approval_write_mode"] = str(result.get("approval_write_mode") or "multi_page").strip() or "multi_page"
        result["approval_write_min_confidence"] = str(result.get("approval_write_min_confidence") or "0.8").strip() or "0.8"
        result["auto_pass"] = coerce_bool(result.get("auto_pass"))
    result["skip_manual_review_lists"] = coerce_bool(result.get("skip_manual_review_lists", True))
    return result


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def coerce_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def normalize_daily_time(value: Any) -> str:
    text = str(value or "16:00").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = min(23, max(0, int(hour_text)))
        minute = min(59, max(0, int(minute_text)))
    except (TypeError, ValueError):
        hour, minute = 16, 0
    return f"{hour:02d}:{minute:02d}"


def next_run_at(config: dict[str, Any], now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    if not config.get("enabled"):
        return None
    if config.get("mode") == "daily":
        hour_text, minute_text = str(config.get("daily_time") or "16:00").split(":", 1)
        candidate = now.replace(hour=int(hour_text), minute=int(minute_text), second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    return now + timedelta(hours=max(1, coerce_int(config.get("interval_hours"), 6)))


def scheduled_run_options(config: dict[str, Any]) -> dict[str, str]:
    return {
        "TARGET_LIST_NUMBER": "",
        "TARGET_LIST_NUMBERS": "",
        "PROCESS_ALL_TODOS": "true",
        "PROCESS_ALL_TODOS_MAX": str(config.get("process_all_todos_max") or 50),
        "APPROVAL_WRITE_MODE": str(config.get("approval_write_mode") or "multi_page"),
        "APPROVAL_WRITE_MIN_CONFIDENCE": str(config.get("approval_write_min_confidence") or "0.8"),
        "AUTO_PASS": normalize_checkbox(config.get("auto_pass")),
        "SCHEDULED_RUN": "true",
        "SCHEDULED_SKIP_MANUAL_REVIEW_LISTS": normalize_checkbox(config.get("skip_manual_review_lists", True)),
    }


@dataclass
class ApprovalScheduler:
    root_dir: Path
    settings_loader: Any
    job_manager: Any
    state_path: Path | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _thread: threading.Thread | None = None
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _config: dict[str, Any] = field(default_factory=scheduler_defaults)
    _next_run_at: datetime | None = None
    _last_run_at: str = ""
    _last_result: str = ""
    _last_message: str = ""

    def __post_init__(self) -> None:
        if self.state_path is None:
            self.state_path = self.root_dir / "data" / STATE_FILENAME
        self._load_state()
        self.reload()
        add_callback = getattr(self.job_manager, "add_completion_callback", None)
        if callable(add_callback):
            add_callback(self.record_job_result)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="approval-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def reload(self) -> None:
        config = scheduler_config(self.settings_loader())
        with self._lock:
            self._config = config
            self._next_run_at = next_run_at(config)
            self._persist_state()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": bool(self._config.get("enabled")),
                "mode": self._config.get("mode", "interval"),
                "interval_hours": self._config.get("interval_hours", 6),
                "daily_time": self._config.get("daily_time", "16:00"),
                "use_default_run_policy": normalize_checkbox(self._config.get("use_default_run_policy", True)),
                "process_all_todos_max": self._config.get("process_all_todos_max", 50),
                "approval_write_mode": self._config.get("approval_write_mode", "multi_page"),
                "approval_write_min_confidence": self._config.get("approval_write_min_confidence", "0.8"),
                "auto_pass": normalize_checkbox(self._config.get("auto_pass")),
                "skip_manual_review_lists": normalize_checkbox(self._config.get("skip_manual_review_lists", True)),
                "next_run_at": self._next_run_at.isoformat(timespec="seconds") if self._next_run_at else "",
                "last_run_at": self._last_run_at,
                "last_result": self._last_result,
                "last_message": self._last_message,
            }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            should_run = False
            with self._lock:
                should_run = bool(
                    self._config.get("enabled")
                    and self._next_run_at is not None
                    and datetime.now() >= self._next_run_at
                )
            if should_run:
                self.trigger_due_run()
            self._stop_event.wait(15)

    def trigger_due_run(self) -> dict[str, Any]:
        with self._lock:
            config = dict(self._config)
            self._last_run_at = datetime.now().isoformat(timespec="seconds")

        if not config.get("enabled"):
            result = {"started": False, "message": "Scheduler is disabled."}
        elif self.job_manager.status().get("running"):
            result = {"started": False, "message": "Skipped scheduled run because another task is running."}
        else:
            result = self.job_manager.start("suggestions", scheduled_run_options(config))

        with self._lock:
            self._last_result = "started" if result.get("started") else "skipped"
            self._last_message = str(result.get("message") or "")
            self._next_run_at = next_run_at(config)
            self._persist_state()
        return result

    def record_job_result(
        self,
        *,
        action: str,
        success: bool,
        error: str = "",
        options: dict[str, str] | None = None,
    ) -> None:
        options = options or {}
        if normalize_checkbox(options.get("SCHEDULED_RUN")) != "true":
            return
        with self._lock:
            self._last_result = "success" if success else "failed"
            self._last_message = "" if success else str(error or "Scheduled automation failed.")
            self._persist_state()

    def _load_state(self) -> None:
        path = self.state_path
        if not path or not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as file:
                payload = yaml.safe_load(file) or {}
        except Exception:
            return
        self._last_run_at = str(payload.get("last_run_at") or "")
        self._last_result = str(payload.get("last_result") or "")
        self._last_message = str(payload.get("last_message") or "")

    def _persist_state(self) -> None:
        path = self.state_path
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_run_at": self._last_run_at,
            "last_result": self._last_result,
            "last_message": self._last_message,
            "next_run_at": self._next_run_at.isoformat(timespec="seconds") if self._next_run_at else "",
        }
        with path.open("w", encoding="utf-8") as file:
            yaml.safe_dump(payload, file, allow_unicode=True, sort_keys=False)
