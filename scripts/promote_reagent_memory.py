"""Audit and promote nonconflicting memory; --apply backs up SQLite first."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from category_mapper import to_erp_property  # noqa: E402
from reagent_memory import ReagentMemory  # noqa: E402


def plan_promotion(rows: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    categories: dict[int, str] = {}
    for row in rows:
        record_id = int(row["id"])
        category = to_erp_property(str(row.get("final_category") or ""), settings)
        categories[record_id] = category
        if not category:
            continue
        for field in ("raw_name_key", "cleaned_name_key", "standard_name_key", "cas_key"):
            value = str(row.get(field) or "").strip()
            if value:
                groups[(field, value)][category].add(record_id)

    conflict_ids = {
        record_id
        for grouped in groups.values() if len(grouped) > 1
        for ids in grouped.values() for record_id in ids
    }
    promote_ids: set[int] = set()
    deactivate_ids: set[int] = set()
    held = defaultdict(int)
    for row in rows:
        record_id = int(row["id"])
        reason = ""
        if record_id in conflict_ids or int(row.get("conflict") or 0):
            reason = "conflict"
        elif not categories[record_id] or int(row.get("need_manual_review") or 0):
            reason = "unresolved"
        elif str(row.get("source") or "").startswith("workbuddy:") and "ERP同步=成功" not in str(row.get("reason") or ""):
            reason = "awaiting_erp_sync"
        elif ReagentMemory.is_unsafe_reusable_evidence(row):
            reason = "unsafe_evidence"
        elif not int(row.get("reusable") or 0):
            promote_ids.add(record_id)
        if reason:
            held[reason] += 1
            if int(row.get("reusable") or 0):
                deactivate_ids.add(record_id)
    return {"conflict_ids": conflict_ids, "promote_ids": promote_ids,
            "deactivate_ids": deactivate_ids, "held": dict(held)}


def run(memory_path: Path, settings: dict[str, Any], *, apply: bool = False) -> dict[str, Any]:
    if not memory_path.is_file():
        raise FileNotFoundError(memory_path)
    uri = f"{memory_path.resolve().as_uri()}?mode=ro" if not apply else str(memory_path)
    conn = sqlite3.connect(uri, uri=not apply, timeout=15)
    conn.row_factory = sqlite3.Row
    backup_path: Path | None = None
    try:
        if apply:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup_path = memory_path.with_name(f"{memory_path.stem}.before_reuse_{stamp}{memory_path.suffix}")
            with sqlite3.connect(backup_path) as backup:
                conn.backup(backup)
            conn.execute("BEGIN IMMEDIATE")
        rows = [dict(row) for row in conn.execute("SELECT * FROM reagent_memory")]
        plan = plan_promotion(rows, settings)
        conflict_ids = plan["conflict_ids"]
        promote_ids = plan["promote_ids"]
        deactivate_ids = plan["deactivate_ids"]
        newly_conflicting = sum(int(row["id"]) in conflict_ids and not int(row["conflict"] or 0) for row in rows)
        result = {
            "records": len(rows),
            "newly_conflicting": newly_conflicting,
            "to_promote": len(promote_ids),
            "to_deactivate": len(deactivate_ids),
            "held": plan["held"],
            "applied": apply,
        }
        if apply:
            now = datetime.now().isoformat(timespec="seconds")
            conn.executemany(
                "UPDATE reagent_memory SET conflict=1, reusable=0, updated_at=? WHERE id=?",
                [(now, record_id) for record_id in sorted(conflict_ids)],
            )
            conn.executemany(
                "UPDATE reagent_memory SET reusable=0, updated_at=? WHERE id=?",
                [(now, record_id) for record_id in sorted(deactivate_ids - conflict_ids)],
            )
            conn.executemany(
                "UPDATE reagent_memory SET reusable=1, updated_at=? WHERE id=? AND conflict=0",
                [(now, record_id) for record_id in sorted(promote_ids)],
            )
            conn.commit()
            result["backup"] = str(backup_path)
        return result
    except Exception:
        if apply:
            conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Back up and update the memory database")
    parser.add_argument("--memory-path", type=Path, default=ROOT / "data" / "reagent_memory.sqlite")
    args = parser.parse_args()
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    print(json.dumps(run(args.memory_path, settings, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
