from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator


@dataclass
class StageLogger:
    prefix: str = "FLOW"
    jsonl_path: Path | None = None
    _stack: list[tuple[str, float]] = field(default_factory=list)

    @contextmanager
    def stage(self, name: str, detail: str = "") -> Iterator[None]:
        started = time.monotonic()
        self._stack.append((name, started))
        suffix = f" - {detail}" if detail else ""
        print(f"{self._stamp()} [{self.prefix}] START {name}{suffix}")
        self._write_event("stage_start", stage=name, detail=detail)
        try:
            yield
        except Exception as error:
            elapsed = time.monotonic() - started
            print(f"{self._stamp()} [{self.prefix}] FAIL  {name} ({elapsed:.1f}s): {error}")
            self._write_event(
                "stage_fail",
                stage=name,
                detail=detail,
                elapsed_seconds=round(elapsed, 3),
                error=str(error),
            )
            raise
        else:
            elapsed = time.monotonic() - started
            print(f"{self._stamp()} [{self.prefix}] END   {name} ({elapsed:.1f}s)")
            self._write_event(
                "stage_end",
                stage=name,
                detail=detail,
                elapsed_seconds=round(elapsed, 3),
            )
        finally:
            if self._stack and self._stack[-1][0] == name:
                self._stack.pop()

    def event(self, message: str) -> None:
        print(f"{self._stamp()} [{self.prefix}] {message}")
        self._write_event("event", message=message)

    def _write_event(self, event_type: str, **payload: object) -> None:
        if not self.jsonl_path:
            return
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "prefix": self.prefix,
            "event": event_type,
            **payload,
        }
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def _stamp() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
