from __future__ import annotations

import re
from typing import Any


UNKNOWN_CATEGORY = "未知类"

UNKNOWN_REAGENT_NAME_PATTERNS = (
    "\u672a\u77e5",
    "未知药品",
    "未知成分",
    "自制化学品",
    "白瓶",
    "红盖",
    "白盖",
    "Lot#",
    "LOT#",
)

BUSINESS_NORMAL_NAME_PATTERNS = (
    "\u6e05\u6d17\u6db2",
    "\u6807\u51c6\u6db2",
    "\u6807\u51c6\u6eb6\u6db2",
    "ICP",
    "\u8bd5\u5242",
    "\u7f13\u51b2\u6db2",
    "\u86cb\u767d",
    "\u514d\u75ab",
    "\u6297\u4f53",
    "\u6807\u6db2",
    "\u6821\u51c6",
    "\u836f\u7269",
)


def business_normal_name_reason(raw_name: str, *extra_values: Any) -> str:
    text = " ".join(str(value or "") for value in (raw_name, *extra_values))
    normalized = re.sub(r"\s+", "", text).lower()
    if any(token in normalized for token in ("\u65e0\u6807\u7b7e", "\u6a21\u62df\u8bd5\u5242", "\u5b8c\u5168\u4e0d\u5b58\u5728")):
        return ""
    for pattern in BUSINESS_NORMAL_NAME_PATTERNS:
        if pattern.lower() in normalized:
            return (
                "\u8bd5\u5242\u540d\u79f0\u547d\u4e2d\u666e\u901a\u7c7b\u4e1a\u52a1\u5173\u952e\u8bcd"
                "\uff08\u6e05\u6d17\u6db2/\u6807\u51c6\u6db2/\u6807\u51c6\u6eb6\u6db2/ICP/"
                "\u8bd5\u5242/\u7f13\u51b2\u6db2/\u86cb\u767d/\u514d\u75ab/\u6297\u4f53/"
                "\u6807\u6db2/\u6821\u51c6/\u836f\u7269\uff09\uff0c\u6309\u666e\u901a\u7c7b\u5904\u7406\u3002"
            )
    return ""


def unknown_reagent_name_reason(raw_name: str, *extra_values: Any) -> str:
    if business_normal_name_reason(raw_name, *extra_values):
        return ""
    text = " ".join(str(value or "") for value in (raw_name, *extra_values))
    normalized = re.sub(r"\s+", "", text).lower()
    for pattern in UNKNOWN_REAGENT_NAME_PATTERNS:
        if pattern.lower() in normalized:
            return f"试剂名称包含“{pattern}”，属于未知/非标准试剂描述，按业务规则判定为未知类。"
    return ""


def looks_like_unknown_reagent_name(raw_name: str, *extra_values: Any) -> bool:
    return bool(unknown_reagent_name_reason(raw_name, *extra_values))


def looks_like_business_normal_name(raw_name: str, *extra_values: Any) -> bool:
    return bool(business_normal_name_reason(raw_name, *extra_values))
