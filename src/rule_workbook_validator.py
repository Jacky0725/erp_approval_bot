from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


REQUIRED_COLUMNS = {
    "categories": {"category", "priority", "default_manual_review", "enabled"},
    "rules": {"rule_id", "category", "match_type", "field_scope", "pattern", "condition", "enabled"},
    "examples": {"category", "example_name", "match_mode", "enabled"},
    "thresholds": {"threshold_id", "category", "field", "operator", "value", "unit", "enabled"},
}
VALID_OPERATORS = {"<", "<=", "=", "==", ">=", ">", "between", "contains", "any"}


@dataclass(frozen=True)
class RuleWorkbookValidation:
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    summary: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "errors": list(self.errors), "warnings": list(self.warnings), "summary": self.summary}


def validate_rule_workbook(path: str | Path) -> RuleWorkbookValidation:
    path = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        book = pd.read_excel(path, sheet_name=None, dtype=str, engine="openpyxl")
    except Exception as error:
        return RuleWorkbookValidation(False, (f"无法读取规则文件：{type(error).__name__}",), (), {})
    for sheet, required in REQUIRED_COLUMNS.items():
        columns = set(book.get(sheet, pd.DataFrame()).columns)
        missing = sorted(required - columns)
        if missing:
            errors.append(f"{sheet} 缺少列：{', '.join(missing)}")
    if errors:
        return RuleWorkbookValidation(False, tuple(errors), tuple(warnings), {})

    enabled = lambda frame: frame[frame["enabled"].astype(str).str.strip().str.lower().isin({"true", "1", "yes", "y", "on"})].fillna("")
    categories = enabled(book["categories"])
    rules = enabled(book["rules"])
    examples = enabled(book["examples"])
    thresholds = enabled(book["thresholds"])
    category_names = {str(value).strip() for value in categories["category"] if str(value).strip()}
    if not category_names:
        errors.append("categories 没有启用类别")
    for sheet, frame in (("rules", rules), ("examples", examples), ("thresholds", thresholds)):
        unknown = sorted({str(value).strip() for value in frame["category"] if str(value).strip()} - category_names)
        if unknown:
            errors.append(f"{sheet} 引用了未知类别：{', '.join(unknown)}")
    for field, frame in (("rule_id", rules), ("threshold_id", thresholds)):
        values = frame[field].astype(str).str.strip()
        duplicates = sorted(set(values[values.ne("") & values.duplicated()].tolist()))
        if duplicates:
            errors.append(f"{field} 重复：{', '.join(duplicates)}")
    priorities = categories["priority"].astype(str).str.strip()
    duplicated_priorities = sorted(set(priorities[priorities.ne("") & priorities.duplicated()].tolist()))
    if duplicated_priorities:
        warnings.append(f"类别 priority 重复，依赖 Excel 行顺序：{', '.join(duplicated_priorities)}")
    invalid_ops = sorted({str(value).strip() for value in thresholds["operator"] if str(value).strip() and str(value).strip() not in VALID_OPERATORS})
    if invalid_ops:
        errors.append(f"thresholds 存在未知 operator：{', '.join(invalid_ops)}")
    empty_rule_ids = int(rules["rule_id"].astype(str).str.strip().eq("").sum())
    if empty_rule_ids:
        errors.append(f"rules 存在 {empty_rule_ids} 条启用规则缺少 rule_id")
    return RuleWorkbookValidation(
        not errors,
        tuple(errors),
        tuple(warnings),
        {"enabled_categories": len(categories), "enabled_rules": len(rules), "enabled_examples": len(examples), "enabled_thresholds": len(thresholds)},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a returned structured rule workbook before promotion.")
    parser.add_argument("workbook", type=Path)
    args = parser.parse_args()
    result = validate_rule_workbook(args.workbook)
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
