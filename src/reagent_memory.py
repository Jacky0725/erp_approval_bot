from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from reagent_name_rules import UNKNOWN_CATEGORY, unknown_reagent_name_reason


RAW_NAME_KEY = "\u8bd5\u5242\u540d\u79f0"
CAS_KEY = "CAS\u53f7"
FINAL_CATEGORY_KEY = "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"
CONFIDENCE_KEY = "\u7f6e\u4fe1\u5ea6"
MANUAL_REVIEW_KEY = "\u9700\u4eba\u5de5\u590d\u6838"
RULE_REASON_KEY = "\u89c4\u5219\u539f\u56e0"
SOURCE_KEY = "\u67e5\u8be2\u6765\u6e90"
URL_KEY = "\u67e5\u8be2URL"
STANDARD_NAME_KEY = "\u6807\u51c6\u5316\u540d\u79f0"
CLEANED_NAME_KEY = "\u6e05\u6d17\u540e\u540d\u79f0"
SPECIFICATION_KEY = "\u89c4\u683c"
UNIT_KEY = "\u89c4\u683c\u5355\u4f4d"


@dataclass
class ReagentMemory:
    root_dir: Path
    settings: dict[str, Any] | None = None

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None, root_dir: Path) -> "ReagentMemory":
        return cls(root_dir=Path(root_dir), settings=settings or {})

    @property
    def path(self) -> Path:
        paths = (self.settings or {}).get("paths", {}) or {}
        configured = paths.get("reagent_memory_sqlite", "data/reagent_memory.sqlite")
        return self.root_dir / configured

    def lookup(
        self,
        *,
        cas: str = "",
        standard_name: str = "",
        cleaned_name: str = "",
        raw_name: str = "",
    ) -> dict[str, Any] | None:
        self._ensure_schema()
        values = {
            "cas": self._norm(cas),
            "standard_name": self._norm(standard_name),
            "cleaned_name": self._norm(cleaned_name),
            "raw_name": self._norm(raw_name),
        }
        clauses = []
        params: list[str] = []
        for column, value in values.items():
            if value:
                clauses.append(f"{column}_key = ?")
                params.append(value)
        if not clauses:
            return None

        sql = f"""
            SELECT *
            FROM reagent_memory
            WHERE reusable = 1
              AND need_manual_review = 0
              AND conflict = 0
              AND ({' OR '.join(clauses)})
            ORDER BY manual_verified DESC, confidence DESC, updated_at DESC, id DESC
            LIMIT 1
        """
        with closing(self._connect()) as conn:
            with conn:
                row = conn.execute(sql, params).fetchone()
                if not row:
                    return None
                conn.execute(
                    """
                    UPDATE reagent_memory
                    SET use_count = use_count + 1, last_used_at = ?
                    WHERE id = ?
                    """,
                    (self._now(), row["id"]),
                )
                return dict(row)

    def find_any(
        self,
        *,
        cas: str = "",
        standard_name: str = "",
        cleaned_name: str = "",
        raw_name: str = "",
        final_category: str = "",
    ) -> dict[str, Any] | None:
        self._ensure_schema()
        clauses = []
        params: list[Any] = []
        for column, value in {
            "cas": self._norm(cas),
            "standard_name": self._norm(standard_name),
            "cleaned_name": self._norm(cleaned_name),
            "raw_name": self._norm(raw_name),
        }.items():
            if value:
                clauses.append(f"{column}_key = ?")
                params.append(value)
        if not clauses:
            return None
        if final_category:
            clauses.append("final_category = ?")
            params.append(final_category)
        sql = f"""
            SELECT *
            FROM reagent_memory
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
        """
        with closing(self._connect()) as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def find_by_id(self, record_id: int) -> dict[str, Any] | None:
        self._ensure_schema()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM reagent_memory WHERE id = ?",
                (int(record_id),),
            ).fetchone()
            return dict(row) if row else None

    def remember_suggestion(self, suggestion: dict[str, Any]) -> bool:
        final_category = str(suggestion.get(FINAL_CATEGORY_KEY) or "").strip()
        if not final_category:
            return False
        unknown_reason = unknown_reagent_name_reason(
            suggestion.get(RAW_NAME_KEY, ""),
            suggestion.get(CLEANED_NAME_KEY, ""),
            suggestion.get(STANDARD_NAME_KEY, ""),
        )
        if unknown_reason:
            return self.add_record(
                raw_name=str(suggestion.get(RAW_NAME_KEY) or "").strip(),
                cleaned_name=str(suggestion.get(CLEANED_NAME_KEY) or "").strip(),
                standard_name=str(suggestion.get(STANDARD_NAME_KEY) or "").strip(),
                cas=str(suggestion.get(CAS_KEY) or "").strip(),
                final_category=UNKNOWN_CATEGORY,
                confidence=1.0,
                reason=unknown_reason,
                source=str(suggestion.get(SOURCE_KEY) or "").strip() or "approval_flow",
                url=str(suggestion.get(URL_KEY) or "").strip(),
                specification=str(suggestion.get(SPECIFICATION_KEY) or "").strip(),
                unit=str(suggestion.get(UNIT_KEY) or "").strip(),
                need_manual_review=False,
                manual_verified=True,
            )
        if self._truthy(suggestion.get(MANUAL_REVIEW_KEY)):
            return False
        confidence = self._float(suggestion.get(CONFIDENCE_KEY), 0.0)
        if confidence < self.min_confidence:
            return False

        return self.add_record(
            raw_name=str(suggestion.get(RAW_NAME_KEY) or "").strip(),
            cleaned_name=str(suggestion.get(CLEANED_NAME_KEY) or "").strip(),
            standard_name=str(suggestion.get(STANDARD_NAME_KEY) or "").strip(),
            cas=str(suggestion.get(CAS_KEY) or "").strip(),
            final_category=final_category,
            confidence=confidence,
            reason=str(suggestion.get(RULE_REASON_KEY) or "").strip(),
            source=str(suggestion.get(SOURCE_KEY) or "").strip() or "approval_flow",
            url=str(suggestion.get(URL_KEY) or "").strip(),
            specification=str(suggestion.get(SPECIFICATION_KEY) or "").strip(),
            unit=str(suggestion.get(UNIT_KEY) or "").strip(),
            need_manual_review=False,
            manual_verified=False,
        )

    def list_records(
        self,
        *,
        query: str = "",
        category: str = "",
        reusable: str = "",
        conflict: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        where_sql, params = self._record_filter_sql(
            query=query,
            category=category,
            reusable=reusable,
            conflict=conflict,
        )
        safe_limit = max(1, min(1000, int(limit or 200)))
        safe_offset = max(0, int(offset or 0))
        sql = f"""
            SELECT *
            FROM reagent_memory
            WHERE {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            OFFSET ?
        """
        params.extend([safe_limit, safe_offset])
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def count_records(
        self,
        *,
        query: str = "",
        category: str = "",
        reusable: str = "",
        conflict: str = "",
    ) -> int:
        self._ensure_schema()
        where_sql, params = self._record_filter_sql(
            query=query,
            category=category,
            reusable=reusable,
            conflict=conflict,
        )
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM reagent_memory WHERE {where_sql}",
                params,
            ).fetchone()
            return int(row["count"] if row else 0)

    def list_categories(self) -> list[str]:
        self._ensure_schema()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT final_category
                FROM reagent_memory
                WHERE final_category IS NOT NULL AND final_category != ''
                ORDER BY final_category
                """
            ).fetchall()
            return [str(row["final_category"]) for row in rows if str(row["final_category"] or "")]

    def _record_filter_sql(
        self,
        *,
        query: str = "",
        category: str = "",
        reusable: str = "",
        conflict: str = "",
    ) -> tuple[str, list[Any]]:
        clauses = ["1 = 1"]
        params: list[Any] = []
        query = str(query or "").strip()
        if query:
            like = f"%{query}%"
            clauses.append(
                """
                (
                    raw_name LIKE ?
                    OR cleaned_name LIKE ?
                    OR standard_name LIKE ?
                    OR cas LIKE ?
                    OR final_category LIKE ?
                    OR reason LIKE ?
                )
                """
            )
            params.extend([like, like, like, like, like, like])
        if category:
            clauses.append("final_category = ?")
            params.append(category)
        if reusable in {"0", "1"}:
            clauses.append("reusable = ?")
            params.append(int(reusable))
        if conflict in {"0", "1"}:
            clauses.append("conflict = ?")
            params.append(int(conflict))
        return " AND ".join(clauses), params

    def update_record(self, record_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        self._ensure_schema()
        allowed = {
            "raw_name",
            "cleaned_name",
            "standard_name",
            "cas",
            "specification",
            "unit",
            "final_category",
            "confidence",
            "reason",
            "source",
            "url",
            "need_manual_review",
            "manual_verified",
            "conflict",
            "reusable",
        }
        cleaned: dict[str, Any] = {key: value for key, value in updates.items() if key in allowed}
        if not cleaned:
            raise ValueError("No editable memory fields were provided.")

        for key in ("need_manual_review", "manual_verified", "conflict", "reusable"):
            if key in cleaned:
                cleaned[key] = int(self._truthy(cleaned[key]))
        if "confidence" in cleaned:
            cleaned["confidence"] = self._float(cleaned["confidence"], 0.0)
        if "final_category" in cleaned:
            cleaned["final_category"] = self._normalize_final_category(cleaned["final_category"])

        for name_field, key_field in (
            ("raw_name", "raw_name_key"),
            ("cleaned_name", "cleaned_name_key"),
            ("standard_name", "standard_name_key"),
            ("cas", "cas_key"),
        ):
            if name_field in cleaned:
                cleaned[key_field] = self._norm(cleaned[name_field])

        current = self.find_by_id(record_id) or {}
        raw_name = cleaned.get("raw_name", current.get("raw_name", ""))
        cleaned_name = cleaned.get("cleaned_name", current.get("cleaned_name", ""))
        standard_name = cleaned.get("standard_name", current.get("standard_name", ""))
        unknown_reason = unknown_reagent_name_reason(raw_name, cleaned_name, standard_name)
        if unknown_reason:
            previous_reason = str(cleaned.get("reason") or current.get("reason") or "").strip()
            cleaned["final_category"] = UNKNOWN_CATEGORY
            cleaned["confidence"] = 1.0
            cleaned["reusable"] = int(self._truthy(cleaned.get("reusable", True)))
            cleaned["conflict"] = 0
            cleaned["need_manual_review"] = 0
            cleaned["manual_verified"] = 1
            if unknown_reason not in previous_reason:
                cleaned["reason"] = f"{previous_reason}\n{unknown_reason}".strip()
            else:
                cleaned["reason"] = previous_reason

        cleaned["updated_at"] = self._now()
        assignments = ", ".join(f"{column} = ?" for column in cleaned)
        params = [*cleaned.values(), int(record_id)]
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(f"UPDATE reagent_memory SET {assignments} WHERE id = ?", params)
                row = conn.execute("SELECT * FROM reagent_memory WHERE id = ?", (int(record_id),)).fetchone()
                if not row:
                    raise ValueError(f"Memory record not found: {record_id}")
                return dict(row)

    def delete_record(self, record_id: int) -> bool:
        self._ensure_schema()
        with closing(self._connect()) as conn:
            with conn:
                cursor = conn.execute("DELETE FROM reagent_memory WHERE id = ?", (int(record_id),))
                return cursor.rowcount > 0

    def delete_conflicting_records(self) -> int:
        self._ensure_schema()
        with closing(self._connect()) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    DELETE FROM reagent_memory
                    WHERE conflict = 1
                    """
                )
                return int(cursor.rowcount or 0)

    def count_conflicting_records(self) -> int:
        self._ensure_schema()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM reagent_memory
                WHERE conflict = 1
                """
            ).fetchone()
            return int(row["count"] if row else 0)

    def add_record(
        self,
        *,
        raw_name: str,
        cleaned_name: str = "",
        standard_name: str = "",
        cas: str = "",
        final_category: str,
        confidence: float = 1.0,
        reason: str = "",
        source: str = "manual",
        url: str = "",
        specification: str = "",
        unit: str = "",
        need_manual_review: bool = False,
        manual_verified: bool = False,
        track_conflicts: bool = True,
    ) -> bool:
        self._ensure_schema()
        final_category = self._normalize_final_category(final_category)
        unknown_reason = unknown_reagent_name_reason(raw_name, cleaned_name, standard_name)
        if unknown_reason:
            final_category = UNKNOWN_CATEGORY
            confidence = max(confidence, 1.0)
            reason = f"{reason}\n{unknown_reason}".strip()
            need_manual_review = False
            manual_verified = True
        if unknown_reason:
            conflict = False
        else:
            conflict = track_conflicts and self._has_conflict(
                cas=cas,
                standard_name=standard_name,
                cleaned_name=cleaned_name,
                raw_name=raw_name,
                final_category=final_category,
            )
        reusable = bool(
            final_category
            and not need_manual_review
            and not conflict
            and confidence >= self.min_confidence
        )
        now = self._now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO reagent_memory (
                        created_at, updated_at, last_used_at, use_count,
                        raw_name, raw_name_key, cleaned_name, cleaned_name_key,
                        standard_name, standard_name_key, cas, cas_key,
                        specification, unit, final_category, confidence,
                        reason, source, url, need_manual_review, manual_verified,
                        conflict, reusable
                    )
                    VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        now,
                        "",
                        raw_name,
                        self._norm(raw_name),
                        cleaned_name,
                        self._norm(cleaned_name),
                        standard_name,
                        self._norm(standard_name),
                        cas,
                        self._norm(cas),
                        specification,
                        unit,
                        final_category,
                        float(confidence),
                        reason,
                        source,
                        url,
                        int(bool(need_manual_review)),
                        int(bool(manual_verified)),
                        int(bool(conflict)),
                        int(bool(reusable)),
                    ),
                )
                if conflict:
                    self._mark_conflicts(conn, cas, standard_name, cleaned_name, raw_name)
        return reusable

    @property
    def min_confidence(self) -> float:
        memory_settings = ((self.settings or {}).get("memory", {}) or {})
        return self._float(memory_settings.get("min_confidence"), 0.8)

    def _has_conflict(
        self,
        *,
        cas: str,
        standard_name: str,
        cleaned_name: str,
        raw_name: str,
        final_category: str,
    ) -> bool:
        existing = self.lookup(
            cas=cas,
            standard_name=standard_name,
            cleaned_name=cleaned_name,
            raw_name=raw_name,
        )
        return bool(existing and str(existing.get("final_category") or "").strip() != final_category)

    def _mark_conflicts(
        self,
        conn: sqlite3.Connection,
        cas: str,
        standard_name: str,
        cleaned_name: str,
        raw_name: str,
    ) -> None:
        keys = [
            ("cas_key", self._norm(cas)),
            ("standard_name_key", self._norm(standard_name)),
            ("cleaned_name_key", self._norm(cleaned_name)),
            ("raw_name_key", self._norm(raw_name)),
        ]
        clauses = []
        params: list[str] = []
        for column, value in keys:
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if not clauses:
            return
        conn.execute(
            f"UPDATE reagent_memory SET conflict = 1, reusable = 0, updated_at = ? WHERE {' OR '.join(clauses)}",
            [self._now(), *params],
        )

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reagent_memory (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_used_at TEXT NOT NULL DEFAULT '',
                        use_count INTEGER NOT NULL DEFAULT 0,
                        raw_name TEXT NOT NULL DEFAULT '',
                        raw_name_key TEXT NOT NULL DEFAULT '',
                        cleaned_name TEXT NOT NULL DEFAULT '',
                        cleaned_name_key TEXT NOT NULL DEFAULT '',
                        standard_name TEXT NOT NULL DEFAULT '',
                        standard_name_key TEXT NOT NULL DEFAULT '',
                        cas TEXT NOT NULL DEFAULT '',
                        cas_key TEXT NOT NULL DEFAULT '',
                        specification TEXT NOT NULL DEFAULT '',
                        unit TEXT NOT NULL DEFAULT '',
                        final_category TEXT NOT NULL DEFAULT '',
                        confidence REAL NOT NULL DEFAULT 0,
                        reason TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        url TEXT NOT NULL DEFAULT '',
                        need_manual_review INTEGER NOT NULL DEFAULT 0,
                        manual_verified INTEGER NOT NULL DEFAULT 0,
                        conflict INTEGER NOT NULL DEFAULT 0,
                        reusable INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                for column in ("cas_key", "standard_name_key", "cleaned_name_key", "raw_name_key"):
                    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_reagent_memory_{column} ON reagent_memory({column})")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _norm(value: Any) -> str:
        return " ".join(str(value or "").strip().lower().split())

    @staticmethod
    def _normalize_final_category(value: Any) -> str:
        category = str(value or "").strip()
        if category == "不建议接收类":
            return "拒收类"
        return category

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _truthy(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
