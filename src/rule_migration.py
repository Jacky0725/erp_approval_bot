from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


LEGACY_COLUMNS = ["category", "explanation", "examples"]
STRUCTURED_SHEETS = ["categories", "rules", "examples", "aliases", "thresholds", "notes"]

DEFAULT_PRIORITY = [
    "\u4e0d\u5efa\u8bae\u63a5\u6536\u7c7b",
    "\u5267\u6bd2\u54c1",
    "\u6613\u7206\u7c7b",
    "\u5f3a\u53cd\u5e94\u6027",
    "\u6c27\u5316\u5242",
    "\u9ad8\u6bd2\u7c7b",
    "\u53d1\u70df\u7c7b",
    "\u5f02\u5473",
    "\u7279\u6b8a\u9178",
    "\u6eb4\u7898\u7c7b",
    "\u523a\u6fc0\u6027",
    "\u91cd\u91d1\u5c5e\u7c7b",
    "\u6613\u71c3\u6db2\u4f53",
    "\u5e38\u89c4\u9178",
    "\u5e38\u89c4\u78b1",
    "\u666e\u901a\u7c7b",
    "\u672a\u77e5\u7c7b",
]

MANUAL_REVIEW_CATEGORIES = {"\u4e0d\u5efa\u8bae\u63a5\u6536\u7c7b", "\u5267\u6bd2\u54c1", "\u672a\u77e5\u7c7b"}

SPECIAL_RULES = [
    {
        "rule_id": "SPECIAL-EXP-001",
        "category": "\u6613\u7206\u7c7b",
        "match_type": "keyword",
        "field_scope": "name,text,evidence",
        "pattern": "\u53e0\u6c2e",
        "condition": "any",
        "confidence": 0.92,
        "description": "confirmed azide explosive class",
        "enabled": True,
    },
    {
        "rule_id": "SPECIAL-EXP-002",
        "category": "\u6613\u7206\u7c7b",
        "match_type": "keyword",
        "field_scope": "name,text,evidence",
        "pattern": "\u53e0\u5316",
        "condition": "any",
        "confidence": 0.92,
        "description": "confirmed azide alias explosive class",
        "enabled": True,
    },
    {
        "rule_id": "SPECIAL-EXP-003",
        "category": "\u6613\u7206\u7c7b",
        "match_type": "keyword",
        "field_scope": "name,text,evidence",
        "pattern": "azide",
        "condition": "any",
        "confidence": 0.92,
        "description": "confirmed azide explosive class",
        "enabled": True,
    },
    {
        "rule_id": "SPECIAL-EXP-004",
        "category": "\u6613\u7206\u7c7b",
        "match_type": "keyword",
        "field_scope": "name,text,evidence",
        "pattern": "\u9ad8\u6c2f\u9178",
        "condition": "concentration > 72% or missing",
        "confidence": 0.90,
        "description": "perchloric acid is explosive when concentration is >72% or missing",
        "enabled": True,
    },
    {
        "rule_id": "SPECIAL-SPA-001",
        "category": "\u7279\u6b8a\u9178",
        "match_type": "keyword",
        "field_scope": "name,text,evidence",
        "pattern": "\u9ad8\u6c2f\u9178",
        "condition": "concentration < 72%",
        "confidence": 0.88,
        "description": "perchloric acid below 72%",
        "enabled": True,
    },
]

THRESHOLD_ROWS = [
    {
        "threshold_id": "T-FLA-001",
        "category": "\u6613\u71c3\u6db2\u4f53",
        "field": "flash_point",
        "operator": "<",
        "value": 60,
        "unit": "\u2103",
        "description": "flash point below 60 C",
        "enabled": True,
    },
    {
        "threshold_id": "T-TOX-001",
        "category": "\u5267\u6bd2\u54c1",
        "field": "oral_ld50",
        "operator": "<=",
        "value": 5,
        "unit": "mg/kg",
        "description": "oral LD50 acute poison threshold",
        "enabled": True,
    },
    {
        "threshold_id": "T-TOX-002",
        "category": "\u9ad8\u6bd2\u7c7b",
        "field": "oral_ld50",
        "operator": "between",
        "value": "5<x<50",
        "unit": "mg/kg",
        "description": "oral LD50 high toxicity threshold",
        "enabled": True,
    },
]


@dataclass(frozen=True)
class LegacyRules:
    categories: list[str]
    grouped: pd.DataFrame
    notes: list[str]


def migrate_rules(legacy_path: str | Path, output_path: str | Path) -> Path:
    legacy = read_legacy_rules(legacy_path)
    sheets = build_structured_sheets(legacy)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for sheet_name in STRUCTURED_SHEETS:
            sheets[sheet_name].to_excel(writer, sheet_name=sheet_name, index=False)
    return output_path


def read_legacy_rules(legacy_path: str | Path) -> LegacyRules:
    raw = pd.read_excel(legacy_path, header=1, engine="openpyxl")
    raw = raw.iloc[:, :3].copy()
    raw.columns = LEGACY_COLUMNS

    notes = [
        _clean_text(value)
        for value in raw["category"].tolist()
        if _clean_text(value).startswith("\u5907\u6ce8")
    ]
    raw = raw[~raw["category"].astype(str).str.startswith("\u5907\u6ce8", na=False)].copy()
    raw["category"] = raw["category"].ffill()
    raw = raw[raw["category"].notna()].copy()

    grouped = raw.groupby("category", sort=False, dropna=True).agg(
        {
            "explanation": lambda values: [_clean_text(value) for value in values if _clean_text(value)],
            "examples": lambda values: [_clean_text(value) for value in values if _clean_text(value)],
        }
    )
    categories = [str(category).strip() for category in grouped.index if str(category).strip()]
    return LegacyRules(categories=categories, grouped=grouped, notes=notes)


def build_structured_sheets(legacy: LegacyRules) -> dict[str, pd.DataFrame]:
    priority = _priority(legacy.categories)
    return {
        "categories": _categories_sheet(priority),
        "rules": _rules_sheet(legacy, priority),
        "examples": _examples_sheet(legacy, priority),
        "aliases": pd.DataFrame(columns=["alias", "standard_name", "cas", "source", "confidence", "enabled"]),
        "thresholds": pd.DataFrame(THRESHOLD_ROWS),
        "notes": _notes_sheet(legacy.notes),
    }


def _categories_sheet(priority: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "category": category,
                "priority": index,
                "default_manual_review": category in MANUAL_REVIEW_CATEGORIES,
                "description": "",
                "enabled": True,
            }
            for index, category in enumerate(priority, start=1)
        ]
    )


def _rules_sheet(legacy: LegacyRules, priority: list[str]) -> pd.DataFrame:
    rows = list(SPECIAL_RULES)
    seen = {(row["category"], row["pattern"], row["condition"]) for row in rows}

    for category in priority:
        if category not in legacy.grouped.index:
            continue
        explanations = legacy.grouped.loc[category, "explanation"]
        for explanation_index, explanation in enumerate(explanations, start=1):
            for keyword in _keywords_from_text(explanation):
                condition = _condition_for(category, keyword, explanation)
                key = (category, keyword, condition)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "rule_id": f"LEGACY-{_category_code(category)}-{len(rows) + 1:04d}",
                        "category": category,
                        "match_type": "keyword",
                        "field_scope": "name,text,evidence",
                        "pattern": keyword,
                        "condition": condition,
                        "confidence": _confidence_for(category),
                        "description": explanation,
                        "enabled": True,
                    }
                )
    return pd.DataFrame(rows)


def _examples_sheet(legacy: LegacyRules, priority: list[str]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for category in priority:
        if category not in legacy.grouped.index:
            continue
        for example_text in legacy.grouped.loc[category, "examples"]:
            for example in _split_examples(example_text):
                key = (category, example)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "category": category,
                        "example_name": example,
                        "match_mode": "contains" if _looks_like_group_example(example) else "exact",
                        "enabled": True,
                        "source": "rules.xlsx",
                        "notes": "",
                    }
                )
    return pd.DataFrame(rows)


def _notes_sheet(notes: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "note_id": f"NOTE-{index:03d}",
                "note": note,
                "source": "rules.xlsx",
                "enabled": True,
            }
            for index, note in enumerate(notes, start=1)
        ]
    )


def _priority(categories: list[str]) -> list[str]:
    priority = [category for category in DEFAULT_PRIORITY if category in categories]
    priority.extend(category for category in categories if category not in priority)
    return priority


def _keywords_from_text(text: str) -> list[str]:
    pieces = re.split(r"[\s,，、;；。:：()（）<>《》/]+", text)
    keywords: list[str] = []
    for piece in pieces:
        piece = re.sub(r"^\d+[.、]", "", piece).strip()
        if _is_keyword(piece) and piece not in keywords:
            keywords.append(piece)
    return keywords


def _split_examples(text: str) -> list[str]:
    normalized = text.replace("\n", " ")
    pieces = re.split(r"[、，,；;]+|\s{2,}", normalized)
    examples: list[str] = []
    for piece in pieces:
        piece = re.sub(r"^\s*\d+[.、]\s*", "", piece).strip()
        piece = re.sub(r"\s+", "", piece)
        piece = piece.strip(" 。.")
        if _is_example(piece) and piece not in examples:
            examples.append(piece)
    return examples


def _condition_for(category: str, keyword: str, explanation: str) -> str:
    if category == "\u6613\u71c3\u6db2\u4f53" and ("\u95ea\u70b9" in keyword or "\u6613\u71c3" in keyword):
        return "flash_point < 60C"
    if category in {"\u5267\u6bd2\u54c1", "\u9ad8\u6bd2\u7c7b"} and ("LD50" in explanation or "LC50" in explanation):
        return "toxicity threshold"
    return "any"


def _confidence_for(category: str) -> float:
    if category in MANUAL_REVIEW_CATEGORIES:
        return 0.78
    if category in {"\u6613\u7206\u7c7b", "\u5f3a\u53cd\u5e94\u6027", "\u6c27\u5316\u5242", "\u9ad8\u6bd2\u7c7b"}:
        return 0.84
    return 0.80


def _category_code(category: str) -> str:
    mapping = {
        "\u4e0d\u5efa\u8bae\u63a5\u6536\u7c7b": "REJ",
        "\u5267\u6bd2\u54c1": "ACU",
        "\u6613\u7206\u7c7b": "EXP",
        "\u5f3a\u53cd\u5e94\u6027": "REA",
        "\u6c27\u5316\u5242": "OXI",
        "\u9ad8\u6bd2\u7c7b": "TOX",
        "\u53d1\u70df\u7c7b": "FUM",
        "\u5f02\u5473": "ODO",
        "\u7279\u6b8a\u9178": "SAC",
        "\u6eb4\u7898\u7c7b": "HAL",
        "\u523a\u6fc0\u6027": "IRR",
        "\u91cd\u91d1\u5c5e\u7c7b": "HMT",
        "\u6613\u71c3\u6db2\u4f53": "FLA",
        "\u5e38\u89c4\u9178": "RAC",
        "\u5e38\u89c4\u78b1": "RBA",
        "\u666e\u901a\u7c7b": "NOR",
        "\u672a\u77e5\u7c7b": "UNK",
    }
    return mapping.get(category, "CAT")


def _looks_like_group_example(text: str) -> bool:
    return any(marker in text for marker in ("\u7c7b", "\u7b49", "\uff08", "(", "\u9664\u5916"))


def _is_keyword(text: str) -> bool:
    if len(text) < 2:
        return False
    if text.lower() in {"nan", "\u7b49", "\u9ad8", "\u91cd", "\u6b21", "\u8fc7", "\u8d85", "\u4e00\u822c", "\u5e38\u89c1", "\u6ce8\u610f"}:
        return False
    if re.fullmatch(r"[\d.%-]+", text):
        return False
    return True


def _is_example(text: str) -> bool:
    if not _is_keyword(text):
        return False
    if len(text) > 80:
        return False
    return True


def _clean_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


if __name__ == "__main__":
    project_root = Path(__file__).resolve().parents[1]
    migrate_rules(project_root / "config" / "rules.xlsx", project_root / "config" / "rules_structured.xlsx")
