"""Reconcile review items with explicit rules or verified reusable memory."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from category_mapper import to_erp_property  # noqa: E402
from reagent_memory import ReagentMemory  # noqa: E402
from rule_engine import RuleEngine  # noqa: E402


def rule_decision(row: dict[str, Any], engine: RuleEngine, settings: dict[str, Any]) -> dict[str, Any] | None:
    name = str(row.get("试剂名称") or row.get("chemical_name") or "").strip()
    if not name or str(row.get("cas_name_conflict") or "").lower() in {"1", "true", "yes"}:
        return None
    standard = str(row.get("standard_name") or "").strip()
    cleaned = str(row.get("cleaned_name") or "").strip()
    result = engine.classify({
        "reagent_name": name, "name": standard or name,
        "standard_name": standard, "cleaned_name": cleaned,
        "cas": str(row.get("cas") or "").strip(),
        "allow_default_normal": False,
    })
    category = to_erp_property(result.get("final_category"), settings)
    # Name-only ordinary and legacy keyword hits are not enough to close a
    # manual-review item without positive, traceable rule evidence.
    if (not category or category == "普通类" or result.get("need_manual_review")
            or not result.get("matched_rule_ids")
            or float(result.get("confidence") or 0) < float(settings.get("memory", {}).get("min_confidence", 0.8))):
        return None
    return {"category": category, "name": name, "standard": standard, "cleaned": cleaned,
            "confidence": float(result["confidence"]), "reason": str(result.get("reason") or ""),
            "rule_ids": result["matched_rule_ids"], "rule_version": str(result.get("rule_version") or "")}


def reusable_memory_decision(
    conn: sqlite3.Connection, row: dict[str, Any], engine: RuleEngine, settings: dict[str, Any],
) -> dict[str, Any] | None:
    if str(row.get("review_kind") or "") not in {"", "classification_review"}:
        return None
    if str(row.get("cas_name_conflict") or "").strip().lower() in {"1", "true", "yes"}:
        return None
    name = str(row.get("试剂名称") or row.get("chemical_name") or "").strip()
    if not name:
        return None
    conn.row_factory = sqlite3.Row
    records = [dict(item) for item in conn.execute(
        "SELECT * FROM reagent_memory WHERE raw_name_key=?", (ReagentMemory._norm(name),)
    )]
    if not records or any(int(item.get("conflict") or 0) for item in records):
        return None
    eligible = [
        item for item in records
        if int(item.get("reusable") or 0) and not int(item.get("need_manual_review") or 0)
        and not ReagentMemory.is_unsafe_reusable_evidence(item)
    ]
    categories = {to_erp_property(item.get("final_category"), settings) for item in eligible}
    categories.discard("")
    if len(categories) != 1:
        return None
    category = categories.pop()
    if category == "未知类":
        return None
    matching = [item for item in eligible if to_erp_property(item.get("final_category"), settings) == category]
    if category == "易燃类" and not any(
        item.get("manual_verified") or item.get("source") == "verified_erp_write" for item in matching
    ):
        return None
    rule = engine.classify({
        "reagent_name": name, "name": str(row.get("standard_name") or name),
        "standard_name": str(row.get("standard_name") or ""),
        "cleaned_name": str(row.get("cleaned_name") or ""),
        "cas": str(row.get("cas") or ""), "allow_default_normal": False,
    })
    rule_category = to_erp_property(rule.get("final_category"), settings)
    if rule_category and rule_category != category:
        return None
    return {"category": category, "record_id": int(matching[0]["id"])}


def has_memory_conflict(conn: sqlite3.Connection, decision: dict[str, Any], settings: dict[str, Any]) -> bool:
    for column, value in (
        ("raw_name_key", decision["name"]),
        ("cleaned_name_key", decision["cleaned"]),
        ("standard_name_key", decision["standard"]),
    ):
        key = ReagentMemory._norm(value)
        if not key:
            continue
        for category, conflict in conn.execute(
            f"SELECT final_category, conflict FROM reagent_memory WHERE {column}=?", (key,)
        ):
            if int(conflict or 0) or to_erp_property(category, settings) != decision["category"]:
                return True
    return False


def run(root: Path, settings: dict[str, Any], *, apply: bool = False) -> dict[str, Any]:
    queue_path = root / "data" / "review_queue.xlsx"
    memory = ReagentMemory.from_settings(settings, root)
    if not queue_path.is_file() or not memory.path.is_file():
        raise FileNotFoundError("review_queue.xlsx and reagent_memory.sqlite are required")
    original_stat = queue_path.stat()
    frame = pd.read_excel(queue_path, dtype=str).fillna("")
    engine = RuleEngine.from_settings(settings, root)
    candidates: list[tuple[int, dict[str, Any]]] = []
    memory_candidates: list[tuple[int, dict[str, Any]]] = []
    conflicted = 0
    with sqlite3.connect(f"{memory.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
        for index, row in frame.iterrows():
            if str(row.get("status") or "").strip().lower() not in {"pending", "manual_review", "open"}:
                continue
            memory_decision = reusable_memory_decision(conn, row.to_dict(), engine, settings)
            if memory_decision:
                memory_candidates.append((index, memory_decision))
                continue
            decision = rule_decision(row.to_dict(), engine, settings)
            if not decision:
                continue
            if has_memory_conflict(conn, decision, settings):
                conflicted += 1
                continue
            candidates.append((index, decision))
    categories_by_name: dict[str, set[str]] = {}
    for _, decision in candidates:
        categories_by_name.setdefault(ReagentMemory._norm(decision["name"]), set()).add(decision["category"])
    conflicted += sum(len(categories_by_name[ReagentMemory._norm(decision["name"])]) > 1 for _, decision in candidates)
    candidates = [
        (index, decision) for index, decision in candidates
        if len(categories_by_name[ReagentMemory._norm(decision["name"])]) == 1
    ]
    result = {"pending": int(frame["status"].astype(str).str.strip().str.lower().isin({"pending", "manual_review", "open"}).sum()),
              "rule_candidates": len(candidates), "reusable_memory_matches": len(memory_candidates),
              "memory_conflicts": conflicted, "applied": 0, "applied_memory": 0, "applied_rules": 0}
    if not apply or not (candidates or memory_candidates):
        return result

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    queue_backup = queue_path.with_name(f"review_queue.before_rule_reaudit_{stamp}.xlsx")
    memory_backup = memory.path.with_name(f"{memory.path.stem}.before_rule_reaudit_{stamp}{memory.path.suffix}")
    shutil.copy2(queue_path, queue_backup)
    with sqlite3.connect(memory.path) as source, sqlite3.connect(memory_backup) as backup:
        source.backup(backup)
    result["backups"] = {"review_queue": str(queue_backup), "memory": str(memory_backup)}
    now = datetime.now().isoformat(timespec="seconds")
    for index, decision in memory_candidates:
        stored = memory.find_by_id(decision["record_id"])
        if not stored or not stored["reusable"] or stored["conflict"]:
            continue
        frame.at[index, "status"] = "resolved_by_reusable_memory"
        frame.at[index, "manual_result"] = decision["category"]
        frame.at[index, "confirmed_at"] = now
        frame.at[index, "confirmed_by"] = "reusable_memory_reaudit"
        frame.at[index, "memory_added"] = "True"
        frame.at[index, "erp_write_status"] = "not_requested"
        frame.at[index, "erp_write_note"] = "历史复核项已与可复用记忆核对；本次未请求 ERP 写入。"
        result["applied_memory"] += 1
        result["applied"] += 1
    for index, decision in candidates:
        memory.add_record(
            raw_name=decision["name"], cleaned_name=decision["cleaned"],
            standard_name=decision["standard"], cas="",
            final_category=decision["category"], confidence=decision["confidence"],
            reason=f"规则复核：{decision['reason']}\n规则 ID: {', '.join(decision['rule_ids'])}",
            source="rule_reaudit", need_manual_review=False, manual_verified=False,
        )
        stored = memory.find_any(raw_name=decision["name"], final_category=decision["category"])
        if stored and stored["reusable"] and not stored["conflict"]:
            frame.at[index, "status"] = "resolved_by_rule"
            frame.at[index, "manual_result"] = decision["category"]
            frame.at[index, "confirmed_at"] = now
            frame.at[index, "confirmed_by"] = "rule_reaudit"
            frame.at[index, "memory_added"] = "True"
            frame.at[index, "rule_version"] = decision["rule_version"]
            frame.at[index, "matched_rule_ids"] = ",".join(decision["rule_ids"])
            result["applied_rules"] += 1
            result["applied"] += 1
    if result["applied"]:
        current_stat = queue_path.stat()
        if (current_stat.st_mtime_ns, current_stat.st_size) != (original_stat.st_mtime_ns, original_stat.st_size):
            raise RuntimeError("Review queue changed during rule audit; memory backup retained for recovery")
        temporary = queue_path.with_name(f".{queue_path.stem}.{stamp}.tmp.xlsx")
        try:
            frame.to_excel(temporary, index=False)
            os.replace(temporary, queue_path)
        finally:
            temporary.unlink(missing_ok=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    print(json.dumps(run(ROOT, settings, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
