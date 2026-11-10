from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class DingTalkState:
    """Durable admission ledger; ambiguous deliveries are never replayed."""

    def __init__(self, root_dir: Path | str) -> None:
        self.path = Path(root_dir) / "data" / "dingtalk_state.sqlite3"

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE IF NOT EXISTS deliveries (message_key TEXT PRIMARY KEY, state TEXT NOT NULL, effectful INTEGER NOT NULL, created_at REAL NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS task_targets (task_id TEXT PRIMARY KEY, binding TEXT NOT NULL, created_at REAL NOT NULL)")
            connection.commit()
            yield connection
            connection.commit()
        finally:
            connection.close()

    def claim(self, conversation: str, message_id: str, *, effectful: bool) -> bool:
        key = hashlib.sha256(json.dumps([conversation, message_id], ensure_ascii=True).encode()).hexdigest()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM deliveries WHERE effectful=0 AND created_at < ?", (time.time() - 30 * 86400,))
            cursor = connection.execute("INSERT OR IGNORE INTO deliveries VALUES (?, 'received', ?, ?)", (key, int(effectful), time.time()))
            return cursor.rowcount == 1

    def complete(self, conversation: str, message_id: str) -> None:
        key = hashlib.sha256(json.dumps([conversation, message_id], ensure_ascii=True).encode()).hexdigest()
        with self.connection() as connection:
            connection.execute("UPDATE deliveries SET state='completed' WHERE message_key=?", (key,))

    def save_target(self, task_id: str, binding: dict[str, Any]) -> None:
        with self.connection() as connection:
            connection.execute("INSERT INTO task_targets VALUES (?, ?, ?)", (task_id, json.dumps(binding, ensure_ascii=False), time.time()))

    def load_target(self, task_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT binding FROM task_targets WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else {}
