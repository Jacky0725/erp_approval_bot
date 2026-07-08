from __future__ import annotations

import re
from typing import Any


UNKNOWN_CATEGORY = "未知类"

UNKNOWN_REAGENT_NAME_PATTERNS = (
    "未知药品",
    "未知成分",
    "自制化学品",
    "白瓶",
    "红盖",
    "白盖",
    "Lot#",
    "LOT#",
)


def unknown_reagent_name_reason(raw_name: str, *extra_values: Any) -> str:
    text = " ".join(str(value or "") for value in (raw_name, *extra_values))
    normalized = re.sub(r"\s+", "", text).lower()
    for pattern in UNKNOWN_REAGENT_NAME_PATTERNS:
        if pattern.lower() in normalized:
            return f"试剂名称包含“{pattern}”，属于未知/非标准试剂描述，按业务规则判定为未知类。"
    return ""


def looks_like_unknown_reagent_name(raw_name: str, *extra_values: Any) -> bool:
    return bool(unknown_reagent_name_reason(raw_name, *extra_values))
