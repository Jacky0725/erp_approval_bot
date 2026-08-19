from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from chemical_searcher import ChemicalSearcher  # noqa: E402
from llm_extractor import LlmExtractor  # noqa: E402
from reagent_memory import ReagentMemory  # noqa: E402
from rule_engine import RuleEngine  # noqa: E402


FLAMMABLE_CATEGORIES = {"易燃类", "易燃液体"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-query and reclassify flammable reagent-memory records."
    )
    parser.add_argument("--apply", action="store_true", help="Apply changes to reagent_memory.sqlite.")
    parser.add_argument("--limit", type=int, default=0, help="Limit records processed; 0 means all.")
    parser.add_argument(
        "--only-reusable",
        action="store_true",
        help="Only reclassify currently reusable records.",
    )
    args = parser.parse_args()

    settings = load_settings()
    settings.setdefault("chemical_search", {})["enable_llm_knowledge_fallback"] = False
    settings.setdefault("chemical_search", {})["enable_fallback_web_research"] = False
    memory = ReagentMemory.from_settings(settings, ROOT_DIR)
    memory_path = ROOT_DIR / (
        (settings.get("paths", {}) or {}).get("reagent_memory_sqlite") or "data/reagent_memory.sqlite"
    )
    log_dir = ROOT_DIR / ((settings.get("paths", {}) or {}).get("audit_log_dir") or "data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    rows = load_flammable_rows(memory_path, only_reusable=args.only_reusable)
    if args.limit > 0:
        rows = rows[: args.limit]

    backup_path = ""
    if args.apply and rows:
        backup_path = str(backup_memory(memory_path, log_dir))

    searcher = ChemicalSearcher(settings=settings, root_dir=ROOT_DIR)
    extractor = LlmExtractor(settings=settings)
    rule_engine = RuleEngine.from_settings(settings, ROOT_DIR)

    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        print(f"[{index}/{len(rows)}] reclassifying memory id={row['id']} {row['raw_name']}", flush=True)
        try:
            result = reclassify_row(row, searcher, extractor, rule_engine)
        except Exception as error:  # noqa: BLE001 - batch audit must continue.
            result = audit_row(row)
            result.update(
                {
                    "action": "manual_review",
                    "new_category": "",
                    "new_confidence": 0.0,
                    "new_reason": f"重判异常：{error}",
                }
            )
        results.append(result)
        if args.apply:
            apply_result(memory, result)

    output_path = log_dir / f"reclassify_flammable_memory_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    dataframe = pd.DataFrame(results)
    dataframe.to_excel(output_path, index=False)

    summary = dataframe["action"].value_counts(dropna=False).to_dict() if not dataframe.empty else {}
    print(f"Processed {len(results)} records. Summary: {summary}", flush=True)
    print(f"Audit file: {output_path}", flush=True)
    if backup_path:
        print(f"Backup: {backup_path}", flush=True)
    if not args.apply:
        print("Dry run only. Re-run with --apply to update the SQLite memory database.", flush=True)
    return 0


def load_settings() -> dict[str, Any]:
    path = ROOT_DIR / "config" / "settings.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_flammable_rows(memory_path: Path, *, only_reusable: bool) -> list[dict[str, Any]]:
    where = "final_category IN ('易燃类', '易燃液体')"
    if only_reusable:
        where += " AND reusable = 1"
    with closing(sqlite3.connect(memory_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT *
                FROM reagent_memory
                WHERE {where}
                ORDER BY reusable DESC, manual_verified DESC, updated_at DESC, id DESC
                """
            )
        ]


def backup_memory(memory_path: Path, log_dir: Path) -> Path:
    backup_path = log_dir / f"reagent_memory_backup_before_reclassify_flammable_{datetime.now():%Y%m%d_%H%M%S}.sqlite"
    shutil.copy2(memory_path, backup_path)
    return backup_path


def reclassify_row(
    row: dict[str, Any],
    searcher: ChemicalSearcher,
    extractor: LlmExtractor,
    rule_engine: RuleEngine,
) -> dict[str, Any]:
    raw_name = str(row.get("raw_name") or row.get("standard_name") or row.get("cleaned_name") or "").strip()
    cas = str(row.get("cas") or "").strip()
    search_result = searcher.search(
        reagent_name=raw_name,
        cas=cas,
        specification=str(row.get("specification") or ""),
        unit=str(row.get("unit") or ""),
    )
    raw_text = str(search_result.get("raw_text") or "")
    extracted = extractor.extract_properties(
        raw_text=raw_text,
        name=str(search_result.get("name") or raw_name),
        cas=str(search_result.get("cas") or cas),
    )
    name_result = search_result.get("name_normalization", {}) or {}
    classification = rule_engine.classify(
        {
            "reagent_name": raw_name,
            "name": search_result.get("name") or row.get("standard_name") or raw_name,
            "standard_name": name_result.get("standard_name") or row.get("standard_name") or "",
            "cleaned_name": name_result.get("cleaned_name") or row.get("cleaned_name") or "",
            "english_name": name_result.get("english_name") or "",
            "cas": search_result.get("cas") or cas,
            "text": " ".join(
                str(value)
                for value in (
                    raw_name,
                    raw_text[:2000],
                    extracted.get("toxicity", ""),
                    " ".join(extracted.get("suggested_categories", []) or []),
                    " ".join(extracted.get("evidence", []) or []),
                )
                if value
            ),
            "flash_point": extracted.get("flash_point", ""),
            "boiling_point": extracted.get("boiling_point", ""),
            "toxicity": extracted.get("toxicity", ""),
            "corrosive": extracted.get("corrosive"),
            "oxidizing": extracted.get("oxidizing"),
            "flammable": extracted.get("flammable"),
            "water_reactive": extracted.get("water_reactive"),
            "explosive_risk": extracted.get("explosive_risk"),
            "heavy_metal": extracted.get("heavy_metal"),
            "suggested_categories": extracted.get("suggested_categories", []),
            "evidence": extracted.get("evidence", []),
            "allow_default_normal": bool(
                not search_result.get("need_manual_review", True)
                and search_result.get("relevance_passed", False)
            ),
        }
    )
    result = audit_row(row)
    new_category = str(classification.get("final_category") or "").strip()
    new_confidence = float(classification.get("confidence") or 0.0)
    new_reason = str(classification.get("reason") or "").strip()
    need_manual = bool(classification.get("need_manual_review", True)) or bool(search_result.get("need_manual_review", True))

    if new_category in FLAMMABLE_CATEGORIES:
        reliable_flammable = RuleEngine.is_reusable_flammable_evidence(
            {
                "reagent_name": raw_name,
                "name": search_result.get("name") or raw_name,
                "standard_name": name_result.get("standard_name") or row.get("standard_name") or "",
                "cleaned_name": name_result.get("cleaned_name") or row.get("cleaned_name") or "",
                "flash_point": extracted.get("flash_point", ""),
                "text": raw_text,
                "evidence": extracted.get("evidence", []),
            }
        )
        action = "kept" if reliable_flammable and not need_manual and new_confidence >= 0.8 else "disabled"
    elif new_category and not need_manual and new_confidence >= 0.8:
        action = "updated"
    elif new_category:
        action = "disabled"
    else:
        action = "manual_review"

    result.update(
        {
            "action": action,
            "new_category": new_category,
            "new_confidence": new_confidence,
            "new_reason": new_reason,
            "search_source": search_result.get("source", ""),
            "search_url": search_result.get("url", ""),
            "source_confidence": search_result.get("source_confidence", ""),
            "flash_point": extracted.get("flash_point", ""),
            "suggested_categories": ", ".join(extracted.get("suggested_categories", []) or []),
            "need_manual_review": need_manual,
        }
    )
    return result


def audit_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "cas": row.get("cas", ""),
        "raw_name": row.get("raw_name", ""),
        "cleaned_name": row.get("cleaned_name", ""),
        "standard_name": row.get("standard_name", ""),
        "old_category": row.get("final_category", ""),
        "old_confidence": row.get("confidence", ""),
        "old_reusable": row.get("reusable", ""),
        "old_conflict": row.get("conflict", ""),
        "old_source": row.get("source", ""),
        "old_url": row.get("url", ""),
        "old_reason": row.get("reason", ""),
    }


def apply_result(memory: ReagentMemory, result: dict[str, Any]) -> None:
    record_id = int(result["id"])
    action = str(result.get("action") or "")
    previous_reason = str(result.get("old_reason") or "").strip()
    new_reason = str(result.get("new_reason") or "").strip()
    audit_note = f"易燃类全量重判：{action}。{new_reason}".strip()
    reason = f"{previous_reason}\n{audit_note}".strip() if previous_reason else audit_note

    if action in {"kept", "updated"}:
        memory.update_record(
            record_id,
            {
                "final_category": result.get("new_category") or result.get("old_category"),
                "confidence": result.get("new_confidence") or result.get("old_confidence") or 0.0,
                "reason": reason,
                "source": result.get("search_source") or result.get("old_source") or "reclassify_flammable_memory",
                "url": result.get("search_url") or result.get("old_url") or "",
                "need_manual_review": False,
                "manual_verified": False,
                "conflict": False,
                "reusable": True,
            },
        )
        return

    memory.update_record(
        record_id,
        {
            "reason": reason,
            "need_manual_review": True,
            "manual_verified": False,
            "conflict": True,
            "reusable": False,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
