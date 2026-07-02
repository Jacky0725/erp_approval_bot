from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


PREVIEW_DIR = ROOT_DIR / "data" / "erp_library_import_preview"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import reviewed ERP reagent-library preview rows into reagent_memory.sqlite."
    )
    parser.add_argument("--preview-dir", type=Path, default=PREVIEW_DIR)
    parser.add_argument("--no-backup", action="store_true", help="Do not create a timestamped SQLite backup.")
    args = parser.parse_args()

    memory_path = ROOT_DIR / "data" / "reagent_memory.sqlite"
    ensure_schema(memory_path)

    if not args.no_backup and memory_path.exists():
        backup_path = memory_path.with_name(
            f"{memory_path.stem}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{memory_path.suffix}"
        )
        import shutil

        shutil.copy2(memory_path, backup_path)
        print(f"backup={backup_path}")

    rows = []
    rows.extend(load_rows(args.preview_dir / "safe_import_preview.csv", expected_status="safe_to_import"))
    rows.extend(
        load_rows(
            args.preview_dir / "manual_review_preview.csv",
            expected_status="manual_review_non_writable_category",
            allowed_categories={"拒收类", "未知类"},
        )
    )

    stats = bulk_insert(memory_path, rows)
    stats["database_count"] = scalar(memory_path, "SELECT COUNT(*) FROM reagent_memory")
    stats["erp_source_count"] = scalar(
        memory_path, "SELECT COUNT(*) FROM reagent_memory WHERE source = 'erp_existing_reagent_library'"
    )
    stats["reusable_count"] = scalar(memory_path, "SELECT COUNT(*) FROM reagent_memory WHERE reusable = 1")
    stats["manual_verified_count"] = scalar(memory_path, "SELECT COUNT(*) FROM reagent_memory WHERE manual_verified = 1")
    stats["erp_reject_unknown_count"] = scalar(
        memory_path,
        "SELECT COUNT(*) FROM reagent_memory "
        "WHERE source = 'erp_existing_reagent_library' AND final_category IN ('拒收类', '未知类')",
    )
    print(stats)
    return 0


def bulk_insert(path: Path, rows: list[dict[str, str]]) -> dict[str, Any]:
    stats: dict[str, Any] = {"scanned": len(rows), "imported": 0, "existing": 0, "skipped_missing_identity": 0}
    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        existing_keys = {
            source_key(dict(row))
            for row in conn.execute(
                """
                SELECT raw_name, cleaned_name, standard_name, cas, final_category
                FROM reagent_memory
                WHERE source = 'erp_existing_reagent_library'
                """
            )
        }
        with conn:
            for row in rows:
                raw_name = text(row.get("raw_name"))
                cleaned_name = text(row.get("cleaned_name"))
                standard_name = text(row.get("standard_name"))
                cas = text(row.get("cas"))
                final_category = text(row.get("final_category"))
                if not any((raw_name, cleaned_name, standard_name, cas)):
                    stats["skipped_missing_identity"] += 1
                    continue
                key = source_key(
                    {
                        "raw_name": raw_name,
                        "cleaned_name": cleaned_name,
                        "standard_name": standard_name,
                        "cas": cas,
                        "final_category": final_category,
                    }
                )
                if key in existing_keys:
                    stats["existing"] += 1
                    continue
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
                    VALUES (?, ?, '', 0, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?, 1.0, ?, ?, '', 0, 1, 0, 1)
                    """,
                    (
                        now,
                        now,
                        raw_name,
                        norm(raw_name),
                        cleaned_name,
                        norm(cleaned_name),
                        standard_name,
                        norm(standard_name),
                        cas,
                        norm(cas),
                        final_category,
                        text(row.get("reason")) or "ERP 已启用试剂库中的既有物化类别。",
                        "erp_existing_reagent_library",
                    ),
                )
                existing_keys.add(key)
                stats["imported"] += 1
            conn.execute(
                """
                UPDATE reagent_memory
                SET conflict = 0, reusable = 1, need_manual_review = 0, manual_verified = 1, updated_at = ?
                WHERE source = 'erp_existing_reagent_library'
                """,
                (now,),
            )
    finally:
        conn.close()
    return stats


def load_rows(
    path: Path,
    *,
    expected_status: str,
    allowed_categories: set[str] | None = None,
) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    selected = [row for row in rows if text(row.get("status")) == expected_status]
    if allowed_categories is not None:
        selected = [row for row in selected if text(row.get("final_category")) in allowed_categories]
    return selected


def text(value: Any) -> str:
    return str(value or "").strip()


def source_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        norm(row.get("cas")),
        norm(row.get("standard_name")),
        norm(row.get("cleaned_name")),
        norm(row.get("raw_name")),
        text(row.get("final_category")),
    )


def norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def scalar(path: Path, sql: str) -> Any:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def ensure_schema(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
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
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
