from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator


@dataclass
class StageLogger:
    prefix: str = "FLOW"
    _stack: list[tuple[str, float]] = field(default_factory=list)

    @contextmanager
    def stage(self, name: str, detail: str = "") -> Iterator[None]:
        started = time.monotonic()
        self._stack.append((name, started))
        suffix = f" - {detail}" if detail else ""
        print(f"{self._stamp()} [{self.prefix}] START {name}{suffix}")
        try:
            yield
        except Exception as error:
            elapsed = time.monotonic() - started
            print(f"{self._stamp()} [{self.prefix}] FAIL  {name} ({elapsed:.1f}s): {error}")
            raise
        else:
            elapsed = time.monotonic() - started
            print(f"{self._stamp()} [{self.prefix}] END   {name} ({elapsed:.1f}s)")
        finally:
            if self._stack and self._stack[-1][0] == name:
                self._stack.pop()

    def event(self, message: str) -> None:
        print(f"{self._stamp()} [{self.prefix}] {message}")

    @staticmethod
    def _stamp() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
