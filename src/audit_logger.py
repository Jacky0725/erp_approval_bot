from __future__ import annotations

import json
import logging
import hashlib
from threading import Lock
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


_HANDLER_LOCK = Lock()


class CloseAfterEmitFileHandler(logging.FileHandler):
    """Release the Windows file handle after each record while retaining logger locking."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.stream is None:
                self.stream = self._open()
            logging.StreamHandler.emit(self, record)
        finally:
            if self.stream is not None:
                try:
                    self.stream.flush()
                finally:
                    self.stream.close()
                    self.stream = None


@dataclass
class AuditLogger:
    logger: logging.Logger
    log_dir: Path

    @classmethod
    def from_settings(cls, settings: dict[str, Any], root_dir: Path) -> "AuditLogger":
        paths = settings.get("paths", {})
        log_dir = root_dir / paths.get("audit_log_dir", "data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)

        log_file = (log_dir / "bot.log").resolve()
        logger_id = hashlib.sha256(str(log_file).encode("utf-8")).hexdigest()
        logger = logging.getLogger(f"reagent_approval_bot.audit.{logger_id}")
        logger.setLevel(logging.INFO)

        with _HANDLER_LOCK:
            if not logger.handlers:
                handler = CloseAfterEmitFileHandler(log_file, encoding="utf-8", delay=True)
                formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
                handler.setFormatter(formatter)
                logger.addHandler(handler)
        logger.propagate = False

        return cls(logger=logger, log_dir=log_dir)

    def info(self, message: str) -> None:
        self.logger.info(message)

    def warning(self, message: str) -> None:
        self.logger.warning(message)

    def record_decision(self, item_text: str, decision: dict[str, Any], dry_run: bool) -> None:
        record = {
            "event": "approval_decision",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "item_text": item_text,
            "decision": decision,
        }

        self.logger.info(json.dumps(record, ensure_ascii=False))

    def record_execution(self, name: str, success: bool, detail: str, dry_run: bool) -> None:
        record = {
            "event": "execution_result",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "name": name,
            "success": bool(success),
            "detail": detail,
        }
        self.logger.info(json.dumps(record, ensure_ascii=False))
