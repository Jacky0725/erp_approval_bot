from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


REQUIRED_COLUMNS = {
    "categories": {"category", "priority", "default_manual_review", "enabled"},
    "rules": {"rule_id", "category", "match_type", "field_scope", "pattern", "condition", "confidence", "enabled"},
    "examples": {"category", "example_name", "match_mode", "expected_final_category", "enabled"},
    "aliases": {"alias", "standard_name", "cas", "confidence", "enabled"},
    "thresholds": {"threshold_id", "category", "field", "operator", "value", "unit", "enabled"},
}
VALID_OPERATORS = {"<", "<=", "=", "==", ">=", ">", "between", "contains", "any"}
VALID_MATCH_TYPES = {"keyword", "exact", "equals", "regex"}
VALID_FIELD_SCOPES = {
    "name", "text", "evidence", "reagent_name", "chemical_name", "standard_name",
    "cleaned_name", "english_name", "cas", "cas_no", "spec", "remark",
    "flash_point", "boiling_point", "toxicity", "oxidizing", "flammable",
    "water_reactive", "explosive_risk", "heavy_metal",
}
EXECUTABLE_THRESHOLD_FIELDS = {
    "flash_point", "oral_ld50", "dermal_ld50",
    "inhalation_lc50_gas", "inhalation_lc50_vapor", "inhalation_lc50_dust_mist",
}


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
    invalid_match_types = sorted({
        str(value).strip().lower() for value in rules["match_type"]
        if str(value).strip() and str(value).strip().lower() not in VALID_MATCH_TYPES
    })
    if invalid_match_types:
        errors.append(f"rules 存在执行器不支持的 match_type：{', '.join(invalid_match_types)}")
    invalid_example_modes = sorted({
        str(value).strip().lower() for value in examples["match_mode"]
        if str(value).strip() and str(value).strip().lower() not in VALID_MATCH_TYPES | {"contains"}
    })
    if invalid_example_modes:
        errors.append(f"examples 存在执行器不支持的 match_mode：{', '.join(invalid_example_modes)}")
    invalid_scopes: set[str] = set()
    for value in rules["field_scope"]:
        invalid_scopes.update(
            item.strip().lower() for item in str(value).replace("，", ",").split(",")
            if item.strip() and item.strip().lower() not in VALID_FIELD_SCOPES
        )
    if invalid_scopes:
        errors.append(f"rules 存在执行器不支持的 field_scope：{', '.join(sorted(invalid_scopes))}")
    expected_categories = {str(value).strip() for value in examples["expected_final_category"] if str(value).strip()}
    unknown_expected = sorted(expected_categories - category_names)
    if unknown_expected:
        errors.append(f"examples 的 expected_final_category 引用了未知类别：{', '.join(unknown_expected)}")
    unsupported_threshold_fields = sorted({
        str(value).strip().lower() for value in thresholds["field"]
        if str(value).strip() and str(value).strip().lower() not in EXECUTABLE_THRESHOLD_FIELDS
    })
    if unsupported_threshold_fields:
        errors.append(f"thresholds 存在执行器不支持的 field：{', '.join(unsupported_threshold_fields)}")
    if "confidence" in rules.columns:
        for index, value in rules["confidence"].items():
            try:
                confidence = float(str(value).strip())
            except ValueError:
                errors.append(f"rules 第 {index + 2} 行 confidence 不是数字")
                continue
            if not 0.0 <= confidence <= 1.0:
                errors.append(f"rules 第 {index + 2} 行 confidence 超出 0-1")
    concentration_conditions = [
        str(value).strip().lower() for value in rules["condition"] if "concentration" in str(value).lower()
    ]
    if any("> 72" in value or ">72" in value for value in concentration_conditions) and not any(
        "<= 72" in value or "<=72" in value for value in concentration_conditions
    ):
        warnings.append("浓度规则包含 >72 但没有 <=72，可能存在 72% 边界空洞")
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
