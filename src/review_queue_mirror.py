from __future__ import annotations

import sqlite3
import json
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from review_queue import canonicalize_review_queue_columns


@dataclass
class ReviewQueueMirror:
    root_dir: Path
    excel_path: Path
    sqlite_path: Path

    @classmethod
    def from_settings(cls, root_dir: Path, settings: dict[str, Any] | None = None) -> "ReviewQueueMirror":
        settings = settings or {}
        paths = settings.get("paths", {}) or {}
        return cls(
            root_dir=Path(root_dir),
            excel_path=Path(root_dir) / paths.get("review_queue_excel", "data/review_queue.xlsx"),
            sqlite_path=Path(root_dir) / paths.get("review_queue_mirror_sqlite", "data/review_queue_mirror.sqlite"),
        )

    def refresh(self) -> dict[str, Any]:
        self._ensure_schema()
        if not self.excel_path.exists():
            return {"refreshed": False, "rows": 0, "message": "review_queue.xlsx does not exist."}
        frame = canonicalize_review_queue_columns(pd.read_excel(self.excel_path, dtype=str).fillna(""))
        now = datetime.now().isoformat(timespec="seconds")
        rows = [self._row_payload(index, row, now) for index, row in frame.iterrows()]
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("DELETE FROM review_queue_mirror")
                conn.executemany(
                    """
                    INSERT INTO review_queue_mirror (
                        row_index, updated_at, list_number, sequence, reagent_name, cas,
                        cleaned_name, standard_name, status, final_category, reason, payload
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        return {"refreshed": True, "rows": len(rows), "path": str(self.sqlite_path)}

    def page(
        self,
        *,
        list_number: str = "",
        status: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._ensure_schema()
        clauses = []
        params: list[Any] = []
        if list_number:
            clauses.append("list_number = ?")
            params.append(list_number)
        if status:
            clauses.append("LOWER(status) = ?")
            params.append(status.lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        safe_limit = max(1, min(100, int(limit or 20)))
        safe_offset = max(0, int(offset or 0))
        with closing(self._connect()) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM review_queue_mirror {where}", params).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT *
                FROM review_queue_mirror
                {where}
                ORDER BY updated_at DESC, row_index DESC
                LIMIT ? OFFSET ?
                """,
                [*params, safe_limit, safe_offset],
            ).fetchall()
        return {"total": int(total), "rows": [dict(row) for row in rows], "limit": safe_limit, "offset": safe_offset}

    def _row_payload(self, index: int, row: pd.Series, updated_at: str) -> tuple[Any, ...]:
        row_dict = {str(key): str(value or "") for key, value in row.to_dict().items()}
        return (
            int(index),
            updated_at,
            self._first(row, "试剂清单号", "当前清单号", "清单号", "list_number"),
            self._first(row, "序号", "sequence", "index"),
            self._first(row, "试剂名称", "chemical_name", "reagent_name"),
            self._first(row, "cas", "CAS号"),
            self._first(row, "cleaned_name", "清洗后名称"),
            self._first(row, "standard_name", "标准化名称"),
            self._first(row, "status", "状态", "处理状态"),
            self._first(row, "manual_result", "final_category", "suggested_category"),
            self._first(row, "reason", "原因", "复核原因", "manual_review_reason"),
            json.dumps(row_dict, ensure_ascii=False, sort_keys=True),
        )

    @staticmethod
    def _first(row: pd.Series, *columns: str) -> str:
        for column in columns:
            if column in row.index:
                value = str(row.get(column) or "").strip()
                if value:
                    return value
        return ""

    def _ensure_schema(self) -> None:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS review_queue_mirror (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        row_index INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        list_number TEXT NOT NULL DEFAULT '',
                        sequence TEXT NOT NULL DEFAULT '',
                        reagent_name TEXT NOT NULL DEFAULT '',
                        cas TEXT NOT NULL DEFAULT '',
                        cleaned_name TEXT NOT NULL DEFAULT '',
                        standard_name TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT '',
                        final_category TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        payload TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_review_mirror_list ON review_queue_mirror(list_number)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_review_mirror_status ON review_queue_mirror(status)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn
