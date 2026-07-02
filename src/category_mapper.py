from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_ERP_PROPERTY_OPTIONS = [
    "普通类",
    "溴碘类",
    "常规酸",
    "异味",
    "发烟类",
    "高毒类",
    "强反应",
    "刺激性",
    "易燃类",
]

DEFAULT_RULE_TO_ERP_ALIASES = {
    "强反应性": ["强反应"],
    "强反应": ["强反应性"],
    "易燃液体": ["易燃类"],
    "易燃类": ["易燃液体"],
    "拒收类": ["不建议接收类"],
    "不建议接收类": ["拒收类"],
}

NON_WRITABLE_RULE_CATEGORIES = {"不建议接收类", "拒收类", "未知类", "剧毒品"}


def erp_property_options(settings: dict[str, Any] | None = None) -> list[str]:
    settings = settings or {}
    configured = (
        settings.get("reagent", {})
        .get("physicochemical_property_options", [])
    )
    if not isinstance(configured, list):
        configured = []
    options = [str(option or "").strip() for option in configured if str(option or "").strip()]
    return list(dict.fromkeys(options or DEFAULT_ERP_PROPERTY_OPTIONS))


def property_aliases(settings: dict[str, Any] | None = None) -> dict[str, list[str]]:
    settings = settings or {}
    aliases: dict[str, list[str]] = {
        key: list(values) for key, values in DEFAULT_RULE_TO_ERP_ALIASES.items()
    }
    configured = (
        settings.get("reagent", {})
        .get("physicochemical_property_aliases", {})
    )
    if isinstance(configured, dict):
        for key, values in configured.items():
            key_text = str(key or "").strip()
            if not key_text:
                continue
            if isinstance(values, str):
                value_list = [values]
            elif isinstance(values, list):
                value_list = values
            else:
                value_list = []
            aliases.setdefault(key_text, [])
            aliases[key_text].extend(str(value or "").strip() for value in value_list if str(value or "").strip())

    return {
        key: list(dict.fromkeys(value for value in values if value))
        for key, values in aliases.items()
        if key
    }


def rule_categories(settings: dict[str, Any] | None = None, root_dir: Path | None = None) -> list[str]:
    settings = settings or {}
    root_dir = root_dir or Path(__file__).resolve().parents[1]
    rules_path = str(
        settings.get("paths", {}).get("structured_rules_excel")
        or "config/rules_structured.xlsx"
    )
    path = Path(rules_path)
    if not path.is_absolute():
        path = root_dir / path
    if not path.exists():
        return []

    frame = pd.read_excel(path, sheet_name="categories", dtype=str, engine="openpyxl").fillna("")
    if "category" not in frame.columns:
        return []
    if "enabled" in frame.columns:
        enabled = frame["enabled"].astype(str).str.strip().str.lower()
        frame = frame[~enabled.isin({"false", "0", "no", "n"})]
    if "priority" in frame.columns:
        frame["_priority_num"] = pd.to_numeric(frame["priority"], errors="coerce")
        frame = frame.sort_values("_priority_num", kind="stable").drop(columns=["_priority_num"])
    return [str(value or "").strip() for value in frame["category"].tolist() if str(value or "").strip()]


def erp_candidates_for_rule_category(category: str, settings: dict[str, Any] | None = None) -> list[str]:
    category = str(category or "").strip()
    if not category:
        return []
    aliases = property_aliases(settings)
    candidates = [category, *aliases.get(category, [])]
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def to_erp_property(category: str, settings: dict[str, Any] | None = None) -> str:
    options = erp_property_options(settings)
    for candidate in erp_candidates_for_rule_category(category, settings):
        if candidate in options:
            return candidate
    return ""


def to_rule_category(category: str, settings: dict[str, Any] | None = None, root_dir: Path | None = None) -> str:
    category = str(category or "").strip()
    if not category:
        return ""
    categories = rule_categories(settings, root_dir)
    if category in categories:
        return category

    aliases = property_aliases(settings)
    for rule_category in categories:
        if category in erp_candidates_for_rule_category(rule_category, settings):
            return rule_category
    for alias, values in aliases.items():
        if category == alias and values:
            for value in values:
                if value in categories:
                    return value
            for value in values:
                if value in NON_WRITABLE_RULE_CATEGORIES:
                    return value
    return category


def is_non_writable_rule_category(
    category: str,
    settings: dict[str, Any] | None = None,
    root_dir: Path | None = None,
) -> bool:
    category = str(category or "").strip()
    if not category:
        return False
    canonical = to_rule_category(category, settings, root_dir)
    return category in NON_WRITABLE_RULE_CATEGORIES or canonical in NON_WRITABLE_RULE_CATEGORIES


def review_decision_options(settings: dict[str, Any] | None = None, root_dir: Path | None = None) -> list[str]:
    settings = settings or {}
    writable = erp_property_options(settings)
    non_writable = [
        category
        for category in rule_categories(settings, root_dir)
        if is_non_writable_rule_category(category, settings, root_dir)
    ]
    if "不建议接收类" in non_writable and "拒收类" not in non_writable:
        non_writable.append("拒收类")
    return list(dict.fromkeys([*writable, *non_writable]))


def category_mapping_summary(settings: dict[str, Any] | None = None, root_dir: Path | None = None) -> dict[str, Any]:
    settings = settings or {}
    options = erp_property_options(settings)
    mappings: list[dict[str, str]] = []
    unmapped: list[str] = []
    non_writable: list[str] = []

    for category in rule_categories(settings, root_dir):
        erp_value = to_erp_property(category, settings)
        if erp_value:
            mappings.append({"rule_category": category, "erp_property": erp_value})
        elif category in NON_WRITABLE_RULE_CATEGORIES:
            non_writable.append(category)
        else:
            unmapped.append(category)

    return {
        "erp_property_options": options,
        "mappings": mappings,
        "unmapped_rule_categories": unmapped,
        "non_writable_rule_categories": non_writable,
        "review_decision_options": review_decision_options(settings, root_dir),
    }
