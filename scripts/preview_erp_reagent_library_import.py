from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path.home() / "Desktop" / "试剂库.xlsx"
CAS_PATTERN = re.compile(r"\d{2,7}-\d{2}-\d")
CONCENTRATION_PATTERNS = [
    re.compile(r"\d+(?:\.\d+)?\s*%"),
    re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:mol\s*/\s*l|mol/l|ppm|ppb)\b"),
]
PACKAGING_PATTERN = re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:mg|g|kg|ml|l)\b")
GRADE_PATTERN = re.compile(r"(?i)\b(?:AR|GR|CP|HPLC|ACS|GC|LCMS|UPLC)\b")
BRACKET_PATTERN = re.compile(r"[\[\]【】{}（）()]")
NOISE_TOKENS = [
    "易制爆",
    "易制毒",
    "危化品",
    "进口",
    "国产",
    "现货",
    "原装",
    "大瓶",
    "小瓶",
    "分析纯",
    "优级纯",
    "化学纯",
    "色谱纯",
    "基准试剂",
    "实验试剂",
    "工业级",
    "试剂级",
    "食品级",
    "电子级",
    "无水",
]
NON_WRITABLE_CATEGORIES = {"拒收类", "未知类", "不建议接收类"}


@dataclass(frozen=True)
class SourceRow:
    source_row: int
    erp_id: str
    raw_name: str
    cleaned_name: str
    name_key: str
    cas: str
    category: str

    @property
    def identity_type(self) -> str:
        return "cas" if self.cas else "name"

    @property
    def identity_key(self) -> str:
        return self.cas or self.name_key


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview importing existing ERP reagent physicochemical categories into reagent_memory.sqlite."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Path to ERP reagent-library workbook.")
    parser.add_argument("--sheet", default="预处理", help="Sheet to import. Defaults to the enabled/preprocessed sheet.")
    parser.add_argument("--out-dir", type=Path, default=ROOT_DIR / "data" / "erp_library_import_preview")
    args = parser.parse_args()

    source_path = resolve_source(args.source)
    settings = load_settings()
    writable_categories = set(settings.get("reagent", {}).get("physicochemical_property_options") or [])
    alias_data = load_aliases(settings)
    rows = load_source_rows(source_path, args.sheet)

    memory_path = ROOT_DIR / (settings.get("paths", {}).get("reagent_memory_sqlite") or "data/reagent_memory.sqlite")
    existing_records = load_existing_memory(memory_path)

    grouped: dict[str, list[SourceRow]] = {}
    skipped: list[dict[str, Any]] = []
    for row in rows:
        if not row.category:
            skipped.append(row_to_dict(row, status="skipped_missing_category"))
            continue
        if not row.identity_key:
            skipped.append(row_to_dict(row, status="skipped_missing_identity"))
            continue
        grouped.setdefault(f"{row.identity_type}:{row.identity_key}", []).append(row)

    safe_import: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []

    for group_key, group_rows in sorted(grouped.items()):
        categories = sorted({row.category for row in group_rows if row.category})
        base = summarize_group(group_key, group_rows, alias_data)

        if len(categories) != 1:
            conflicts.append(
                {
                    **base,
                    "status": "source_conflict",
                    "conflict_categories": " | ".join(categories),
                    "reason": "同一 CAS 或清洗名称下存在多个物化类别，需人工确认。",
                }
            )
            continue

        category = categories[0]
        base["final_category"] = category
        matches = memory_matches(existing_records, base)
        same_category = [record for record in matches if record["final_category"] == category]
        different_category = [record for record in matches if record["final_category"] != category]

        if same_category:
            existing.append(
                {
                    **base,
                    "status": "already_existing",
                    "existing_ids": join_values(record["id"] for record in same_category),
                    "reason": "数据库中已有同类别记忆记录。",
                }
            )
            continue

        if different_category:
            conflicts.append(
                {
                    **base,
                    "status": "database_conflict",
                    "conflict_categories": join_values(record["final_category"] for record in different_category),
                    "existing_ids": join_values(record["id"] for record in different_category),
                    "reason": "数据库中存在同一身份但不同类别的记录，需人工确认。",
                }
            )
            continue

        if category in NON_WRITABLE_CATEGORIES or category not in writable_categories:
            manual_review.append(
                {
                    **base,
                    "status": "manual_review_non_writable_category",
                    "reason": "类别不是可直接写入 ERP 的下拉项，先作为人工复核候选。",
                }
            )
            continue

        safe_import.append(
            {
                **base,
                "status": "safe_to_import",
                "confidence": 1.0,
                "manual_verified": 1,
                "source": "erp_existing_reagent_library",
                "reason": "ERP 已启用试剂库中的既有物化类别；同身份合并后类别一致。",
            }
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "safe_import_preview.csv", safe_import)
    write_csv(args.out_dir / "conflict_review.csv", conflicts)
    write_csv(args.out_dir / "manual_review_preview.csv", manual_review)
    write_csv(args.out_dir / "already_existing.csv", existing)
    write_csv(args.out_dir / "skipped_rows.csv", skipped)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(source_path),
        "sheet": args.sheet,
        "source_rows": len(rows),
        "identity_groups": len(grouped),
        "safe_to_import": len(safe_import),
        "source_or_database_conflicts": len(conflicts),
        "manual_review": len(manual_review),
        "already_existing": len(existing),
        "skipped_rows": len(skipped),
        "category_counts_source_rows": dict(sorted(Counter(row.category for row in rows).items())),
        "category_counts_safe_import": dict(sorted(Counter(row["final_category"] for row in safe_import).items())),
    }
    (args.out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def resolve_source(path: Path) -> Path:
    if path.exists():
        return path
    candidates = [item for item in (Path.home() / "Desktop").glob("*.xlsx") if "rules" not in item.name.lower()]
    if candidates:
        return max(candidates, key=lambda item: item.stat().st_mtime)
    raise FileNotFoundError(f"Workbook not found: {path}")


def load_settings() -> dict[str, Any]:
    with (ROOT_DIR / "config" / "settings.yaml").open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def load_aliases(settings: dict[str, Any]) -> dict[str, Any]:
    aliases_path = ROOT_DIR / (settings.get("paths", {}).get("name_aliases_yaml") or "config/name_aliases.yaml")
    if not aliases_path.exists():
        return {"cas": {}, "aliases": {}}
    with aliases_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    return {"cas": data.get("cas") or {}, "aliases": data.get("aliases") or {}}


def load_source_rows(path: Path, sheet_name: str) -> list[SourceRow]:
    excel = pd.ExcelFile(path, engine="openpyxl")
    actual_sheet = sheet_name if sheet_name in excel.sheet_names else excel.sheet_names[0]
    frame = pd.read_excel(path, sheet_name=actual_sheet, dtype=str, engine="openpyxl").fillna("")
    columns = list(frame.columns)
    if len(columns) < 4:
        raise ValueError(f"Sheet {actual_sheet!r} must have at least 4 columns.")
    id_col, name_col, cas_col, category_col = columns[:4]
    rows: list[SourceRow] = []
    for index, row in frame.iterrows():
        raw_name = str(row.get(name_col) or "").strip()
        cleaned_name = clean_name(raw_name)
        rows.append(
            SourceRow(
                source_row=int(index) + 2,
                erp_id=str(row.get(id_col) or "").strip(),
                raw_name=raw_name,
                cleaned_name=cleaned_name,
                name_key=name_key(cleaned_name),
                cas=clean_cas(row.get(cas_col)),
                category=str(row.get(category_col) or "").strip(),
            )
        )
    return rows


def clean_cas(value: Any) -> str:
    match = CAS_PATTERN.search(str(value or ""))
    return match.group(0) if match else ""


def clean_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = CAS_PATTERN.sub(" ", text)
    for pattern in CONCENTRATION_PATTERNS:
        text = pattern.sub(" ", text)
    text = PACKAGING_PATTERN.sub(" ", text)
    text = GRADE_PATTERN.sub(" ", text)
    for token in NOISE_TOKENS:
        text = text.replace(token, " ")
    text = BRACKET_PATTERN.sub(" ", text)
    text = re.sub(r"[，、]", ",", text)
    text = re.sub(r"[;。；：:]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_/")
    return text


def name_key(value: Any) -> str:
    return re.sub(r"[\s\-_·.]+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()


def summarize_group(group_key: str, rows: list[SourceRow], aliases: dict[str, Any]) -> dict[str, Any]:
    identity_type, identity_key = group_key.split(":", 1)
    raw_names = [row.raw_name for row in rows if row.raw_name]
    cleaned_names = [row.cleaned_name for row in rows if row.cleaned_name]
    cas_values = [row.cas for row in rows if row.cas]
    cas = most_common(cas_values)
    cleaned_name = most_common(cleaned_names)
    raw_name = most_common(raw_names)
    standard_name = standard_name_for(cas=cas, cleaned_name=cleaned_name, raw_name=raw_name, aliases=aliases)
    return {
        "identity_type": identity_type,
        "identity_key": identity_key,
        "row_count": len(rows),
        "erp_ids": join_values(row.erp_id for row in rows),
        "source_rows": join_values(row.source_row for row in rows),
        "raw_name": raw_name,
        "cleaned_name": cleaned_name,
        "standard_name": standard_name,
        "cas": cas,
        "sample_names": join_values(raw_names[:6], separator=" || "),
    }


def standard_name_for(cas: str, cleaned_name: str, raw_name: str, aliases: dict[str, Any]) -> str:
    cas_data = aliases.get("cas") or {}
    cas_entry = cas_data.get(cas)
    if isinstance(cas_entry, dict) and cas_entry.get("standard_name"):
        return str(cas_entry["standard_name"]).strip()
    if isinstance(cas_entry, str) and cas_entry.strip():
        return cas_entry.strip()

    alias_data = aliases.get("aliases") or {}
    lookup_keys = {cleaned_name, raw_name, name_key(cleaned_name), name_key(raw_name)}
    for key, value in alias_data.items():
        if key in lookup_keys or name_key(key) in lookup_keys:
            if isinstance(value, dict):
                return str(value.get("standard_name") or cleaned_name or raw_name).strip()
            return str(value or cleaned_name or raw_name).strip()
    return cleaned_name or raw_name


def most_common(values: list[str]) -> str:
    if not values:
        return ""
    counts = Counter(values)
    return sorted(counts, key=lambda value: (-counts[value], -len(value), value))[0]


def load_existing_memory(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'reagent_memory'"
        ).fetchone()
        if not table:
            return []
        return [dict(row) for row in conn.execute("SELECT * FROM reagent_memory")]
    finally:
        conn.close()


def memory_matches(records: list[dict[str, Any]], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    keys = {
        "cas_key": norm(candidate.get("cas")),
        "standard_name_key": norm(candidate.get("standard_name")),
        "cleaned_name_key": norm(candidate.get("cleaned_name")),
        "raw_name_key": norm(candidate.get("raw_name")),
    }
    usable = {key: value for key, value in keys.items() if value}
    if not usable:
        return []
    matches = []
    for record in records:
        if any(str(record.get(key) or "") == value for key, value in usable.items()):
            matches.append(record)
    return matches


def norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def row_to_dict(row: SourceRow, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "source_row": row.source_row,
        "erp_id": row.erp_id,
        "raw_name": row.raw_name,
        "cleaned_name": row.cleaned_name,
        "cas": row.cas,
        "final_category": row.category,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def join_values(values: Any, separator: str = "; ") -> str:
    unique = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in unique:
            unique.append(text)
    return separator.join(unique)


if __name__ == "__main__":
    sys.exit(main())
