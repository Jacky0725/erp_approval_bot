from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml


DEFAULT_ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = DEFAULT_ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from reagent_memory import ReagentMemory  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete all conflict-marked records from reagent_memory.sqlite after creating a backup."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the number of conflict records; do not modify the database.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Run without an interactive confirmation prompt.",
    )
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT_DIR),
        help="Project root containing config/ and data/. Defaults to this repository.",
    )
    return parser.parse_args()


def integrity_check(path: Path) -> str:
    if not path.exists():
        return "missing"
    with sqlite3.connect(path) as conn:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0] if row else "unknown")


def load_settings(root_dir: Path) -> dict:
    settings_path = root_dir / "config" / "settings.yaml"
    if not settings_path.exists():
        return {}
    with settings_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def main() -> int:
    args = parse_args()
    root_dir = Path(args.root).resolve()
    memory = ReagentMemory.from_settings(load_settings(root_dir), root_dir)
    conflict_count = memory.count_conflicting_records()
    total_count = memory.count_records()

    print(f"Database: {memory.path}")
    print(f"Total records: {total_count}")
    print(f"Conflict records: {conflict_count}")

    if args.dry_run or conflict_count == 0:
        return 0

    if not args.yes:
        answer = input("Delete all conflict records after backup? Type YES to continue: ").strip()
        if answer != "YES":
            print("Cancelled.")
            return 1

    backup_dir = root_dir / "data" / "logs"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"reagent_memory_backup_before_delete_conflicting_{datetime.now():%Y%m%d_%H%M%S}.sqlite"
    shutil.copy2(memory.path, backup_path)
    print(f"Backup: {backup_path}")

    deleted = memory.delete_conflicting_records()
    print(f"Deleted conflict records: {deleted}")
    print(f"Remaining conflict records: {memory.count_conflicting_records()}")
    print(f"Integrity check: {integrity_check(memory.path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
