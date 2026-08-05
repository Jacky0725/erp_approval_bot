from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass
class ChemicalSearchCache:
    root_dir: Path
    settings: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        cache_settings = self._cache_settings()
        if "enabled" not in cache_settings:
            return False
        return self._truthy(cache_settings.get("enabled"))

    @property
    def path(self) -> Path:
        paths = (self.settings or {}).get("paths", {}) or {}
        cache_settings = self._cache_settings()
        configured = (
            cache_settings.get("path")
            or paths.get("chemical_search_cache_sqlite")
            or "data/chemical_search_cache.sqlite"
        )
        return self.root_dir / str(configured)

    def get(self, cache_key: dict[str, str]) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        key = self.cache_key_hash(cache_key)
        now = self._now_iso()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT payload_json
                FROM chemical_search_cache
                WHERE cache_key = ? AND expires_at > ?
                """,
                (key, now),
            ).fetchone()
        if row is None:
            return None
        try:
            return copy.deepcopy(json.loads(row["payload_json"]))
        except (TypeError, json.JSONDecodeError):
            return None

    def put(self, cache_key: dict[str, str], result: dict[str, Any]) -> None:
        if not self.enabled:
            return
        result_copy = copy.deepcopy(result)
        ttl_days = self._ttl_days(result_copy)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=ttl_days)
        key = self.cache_key_hash(cache_key)
        payload_json = json.dumps(result_copy, ensure_ascii=False, sort_keys=True)
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO chemical_search_cache (
                        cache_key, source, normalized_name, cas, search_mode,
                        settings_version, url, matched_name, source_confidence,
                        failure_reason, payload_json, created_at, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        source = excluded.source,
                        normalized_name = excluded.normalized_name,
                        cas = excluded.cas,
                        search_mode = excluded.search_mode,
                        settings_version = excluded.settings_version,
                        url = excluded.url,
                        matched_name = excluded.matched_name,
                        source_confidence = excluded.source_confidence,
                        failure_reason = excluded.failure_reason,
                        payload_json = excluded.payload_json,
                        created_at = excluded.created_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        key,
                        cache_key.get("source", ""),
                        cache_key.get("normalized_name", ""),
                        cache_key.get("cas", ""),
                        cache_key.get("search_mode", ""),
                        cache_key.get("settings_version", ""),
                        str(result_copy.get("url") or ""),
                        str(result_copy.get("matched_site_name") or result_copy.get("name") or ""),
                        self._float_value(result_copy.get("source_confidence")),
                        str(result_copy.get("failure_reason") or ""),
                        payload_json,
                        now.isoformat(timespec="seconds"),
                        expires_at.isoformat(timespec="seconds"),
                    ),
                )

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chemical_search_cache (
                cache_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                cas TEXT NOT NULL,
                search_mode TEXT NOT NULL,
                settings_version TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                matched_name TEXT NOT NULL DEFAULT '',
                source_confidence REAL NOT NULL DEFAULT 0,
                failure_reason TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )
        return conn

    def _ttl_days(self, result: dict[str, Any]) -> int:
        cache_settings = self._cache_settings()
        failure_reason = str(result.get("failure_reason") or "").strip()
        if result.get("need_manual_review") or failure_reason:
            return max(1, int(cache_settings.get("failure_ttl_days", 1) or 1))
        return max(1, int(cache_settings.get("success_ttl_days", 30) or 30))

    def _cache_settings(self) -> dict[str, Any]:
        settings = self.settings or {}
        return (
            settings.get("chemical_search", {}).get("cache", {})
            or settings.get("chemical_search_cache", {})
            or {}
        )

    @staticmethod
    def cache_key_hash(cache_key: dict[str, str]) -> str:
        payload = json.dumps(cache_key, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def settings_version(settings: dict[str, Any] | None) -> str:
        relevant = {
            "chemical_search": (settings or {}).get("chemical_search", {}),
            "web_research": (settings or {}).get("web_research", {}),
            "name_aliases": (settings or {}).get("paths", {}).get("name_aliases_yaml", ""),
        }
        payload = json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _float_value(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}
