from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class AuditLogger:
    logger: logging.Logger
    log_dir: Path

    @classmethod
    def from_settings(cls, settings: dict[str, Any], root_dir: Path) -> "AuditLogger":
        paths = settings.get("paths", {})
        log_dir = root_dir / paths.get("audit_log_dir", "data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger("reagent_approval_bot")
        logger.setLevel(logging.INFO)

        if not logger.handlers:
            log_file = log_dir / "bot.log"
            handler = logging.FileHandler(log_file, encoding="utf-8")
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            handler.setFormatter(formatter)
            logger.addHandler(handler)

        return cls(logger=logger, log_dir=log_dir)

    def info(self, message: str) -> None:
        self.logger.info(message)

    def warning(self, message: str) -> None:
        self.logger.warning(message)

    def record_decision(self, item_text: str, decision: dict[str, Any], dry_run: bool) -> None:
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "item_text": item_text,
            "decision": decision,
        }

        self.logger.info(json.dumps(record, ensure_ascii=False))

