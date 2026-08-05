from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


NORMAL_CATEGORY = "\u666e\u901a\u7c7b"

DEFAULT_ORDINARY_ITEMS = (
    "\u8bd5\u7ba1",
    "\u79bb\u5fc3\u7ba1",
    "pcr\u7ba1",
    "ep\u7ba1",
    "\u5438\u7ba1",
    "\u6ef4\u7ba1",
    "\u5df4\u6c0f\u5438\u7ba1",
    "\u79fb\u6db2\u7ba1",
    "\u79fb\u6db2\u67aa\u5934",
    "\u67aa\u5934",
    "\u5438\u5934",
    "\u57f9\u517b\u76bf",
    "\u6bd4\u8272\u76bf",
    "\u70e7\u676f",
    "\u91cf\u7b52",
    "\u5bb9\u91cf\u74f6",
    "\u73bb\u7483\u68d2",
    "\u8f7d\u73bb\u7247",
    "\u76d6\u73bb\u7247",
    "\u6ee4\u7eb8",
    "\u6ee4\u819c",
    "\u6807\u7b7e\u7eb8",
    "\u624b\u5957",
    "\u53e3\u7f69",
    "\u68c9\u7b7e",
    "\u91c7\u6837\u888b",
    "\u6837\u54c1\u74f6",
    "\u87ba\u53e3\u74f6",
    "\u8fdb\u6837\u74f6",
    "\u79bb\u5fc3\u67f1",
)

DEFAULT_RISK_EXCLUSIONS = (
    "\u53e0\u6c2e",
    "\u53e0\u5316",
    "\u9ad8\u6c2f\u9178",
    "\u785d\u9178",
    "\u786b\u9178",
    "\u76d0\u9178",
    "\u6c22\u6c1f\u9178",
    "azide",
    "perchloricacid",
    "nitricacid",
    "sulfuricacid",
    "hydrochloricacid",
    "hydrofluoricacid",
)


@dataclass
class NonReagentClassifier:
    settings: dict[str, Any] | None = None
    root_dir: Path | None = None

    def __post_init__(self) -> None:
        root_dir = self.root_dir or Path(__file__).resolve().parents[1]
        config_path = root_dir / "config" / "non_reagent_items.yaml"
        self.ordinary_items = list(DEFAULT_ORDINARY_ITEMS)
        self.risk_exclusions = list(DEFAULT_RISK_EXCLUSIONS)
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as file:
                data = yaml.safe_load(file) or {}
            self.ordinary_items = self._string_list(data.get("ordinary_items")) or self.ordinary_items
            self.risk_exclusions = self._string_list(data.get("risk_exclusions")) or self.risk_exclusions

    def classify(self, raw_name: str, *extra_values: Any) -> dict[str, Any] | None:
        text = " ".join(str(value or "") for value in (raw_name, *extra_values))
        normalized = self._normalize(text)
        if not normalized:
            return None
        exclusion = self._first_match(self.risk_exclusions, normalized)
        if exclusion:
            return None
        matched = self._first_match(self.ordinary_items, normalized)
        if not matched:
            return None
        return {
            "final_category": NORMAL_CATEGORY,
            "matched_categories": [NORMAL_CATEGORY],
            "reason": f"\u8bd5\u5242\u540d\u79f0\u5305\u542b\u5b9e\u9a8c\u8017\u6750/\u5668\u5177\u5173\u952e\u8bcd\u201c{matched}\u201d\uff0c\u4e0d\u5c5e\u4e8e\u9700\u67e5\u8be2\u7269\u6027\u7684\u5316\u5b66\u8bd5\u5242\uff0c\u6309\u666e\u901a\u7c7b\u5904\u7406\u3002",
            "confidence": 0.95,
            "need_manual_review": False,
        }

    @staticmethod
    def _first_match(patterns: list[str], normalized: str) -> str:
        for pattern in patterns:
            pattern_text = str(pattern or "").strip()
            if pattern_text and NonReagentClassifier._normalize(pattern_text) in normalized:
                return pattern_text
        return ""

    @staticmethod
    def _normalize(value: str) -> str:
        return re.sub(r"[\s\-_()/\\\[\]{}]+", "", str(value or "")).lower()

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list) else [value]
        return [str(item).strip() for item in values if str(item).strip()]
