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
LOG_DIR = ROOT_DIR / "data" / "logs"


def load_settings() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


class LineBufferWriter(io.TextIOBase):
    def __init__(self, lines: list[str], path: Path, limit: int = 800) -> None:
        self.lines = lines
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
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
            return {
                "running": self.running,
                "action": self.action,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "success": self.success,
                "error": self.error,
                "log_tail": self.lines[-160:],
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

    def _run_bot_action(self, action: str, options: dict[str, str]) -> None:
        load_dotenv(self.root_dir / ".env")
        settings = load_settings()
        bot = BrowserBot(settings=settings, root_dir=self.root_dir)

        with temporary_env(self._env_overrides(options)):
            bot.target_list_number = os.getenv("TARGET_LIST_NUMBER", "").strip()

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


def runtime_config_snapshot() -> dict[str, Any]:
    load_dotenv(ROOT_DIR / ".env")
    settings = load_settings()
    return {
        "erp_url_configured": bool(os.getenv("ERP_URL", "").strip()),
        "erp_username_configured": bool(os.getenv("ERP_USERNAME", "").strip()),
        "auto_pass": os.getenv("AUTO_PASS", "false"),
        "target_list_number": os.getenv("TARGET_LIST_NUMBER", ""),
        "process_all_todos": os.getenv("PROCESS_ALL_TODOS", "false"),
        "approval_write_mode": os.getenv(
            "APPROVAL_WRITE_MODE",
            str((settings.get("approval", {}) or {}).get("write_mode", "disabled")),
        ),
        "llm_provider": (settings.get("llm", {}) or {}).get("provider", ""),
        "llm_model": (settings.get("llm", {}) or {}).get("model", ""),
    }


manager = AutomationJobManager()
