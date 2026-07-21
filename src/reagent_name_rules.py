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
    "\u6807\u51c6",
    "\u6807\u51c6\u6db2",
    "\u6807\u51c6\u6eb6\u6db2",
    "ICP",
    "\u8bd5\u5242",
    "\u7f13\u51b2\u6db2",
    "\u86cb\u767d",
    "\u7ec6\u80de",
    "\u75c5\u6bd2",
    "\u514d\u75ab",
    "\u6297\u4f53",
    "\u67d3\u8272",
    "\u6807\u6db2",
    "\u6807\u5b9a",
    "\u6821\u51c6",
    "\u836f\u7269",
    "\u4e00\u6b21\u6027",
)

LOW_PRIORITY_BUSINESS_NORMAL_NAME_PATTERNS = (
    "\u7eb3\u7c73",
    "\u5355\u4f53",
    "\u62c5\u4f53",
    "\u52a9\u6ee4",
    "\u8131\u8272",
    "\u6a21\u62df",
    "\u50ac\u5316",
    "\u4eba\u5de5",
)

PHARMACEUTICAL_NORMAL_NAME_PATTERNS = (
    "\u5361\u9a6c\u897f\u5e73",
    "\u6587\u62c9\u6cd5\u8f9b",
    "\u76d0\u9178\u6587\u62c9\u6cd5\u8f9b",
    "carbamazepine",
    "venlafaxine",
    "venlafaxinehydrochloride",
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
                "\uff08\u6e05\u6d17\u6db2/\u6807\u51c6/\u6807\u51c6\u6db2/\u6807\u51c6\u6eb6\u6db2/ICP/"
                "\u8bd5\u5242/\u7f13\u51b2\u6db2/\u86cb\u767d/\u7ec6\u80de/\u75c5\u6bd2/\u514d\u75ab/\u6297\u4f53/"
                "\u67d3\u8272/\u6807\u6db2/\u6807\u5b9a/\u6821\u51c6/\u836f\u7269/\u4e00\u6b21\u6027\uff09\uff0c\u6309\u666e\u901a\u7c7b\u5904\u7406\u3002"
            )
    for pattern in PHARMACEUTICAL_NORMAL_NAME_PATTERNS:
        if pattern.lower() in normalized:
            return "\u8bd5\u5242\u540d\u79f0\u547d\u4e2d\u836f\u7269/API\u7c7b\u666e\u901a\u7c7b\u540d\u79f0\u89c4\u5219\uff0c\u6309\u666e\u901a\u7c7b\u5904\u7406\u3002"
    return ""


def low_priority_business_normal_name_reason(raw_name: str, *extra_values: Any) -> str:
    text = " ".join(str(value or "") for value in (raw_name, *extra_values))
    normalized = re.sub(r"\s+", "", text).lower()
    for pattern in LOW_PRIORITY_BUSINESS_NORMAL_NAME_PATTERNS:
        if pattern.lower() in normalized:
            return (
                "\u8bd5\u5242\u540d\u79f0\u547d\u4e2d\u4f4e\u4f18\u5148\u7ea7\u666e\u901a\u7c7b\u4e1a\u52a1\u5173\u952e\u8bcd"
                "\uff08\u7eb3\u7c73/\u5355\u4f53/\u62c5\u4f53/\u52a9\u6ee4/\u8131\u8272/"
                "\u6a21\u62df/\u50ac\u5316/\u4eba\u5de5\uff09\uff1b\u82e5\u540c\u65f6\u547d\u4e2d"
                "\u5176\u5b83\u7c7b\u522b\u89c4\u5219\uff0c\u4f18\u5148\u6309\u5176\u5b83\u7c7b\u522b\u5904\u7406\u3002"
            )
    return ""


def unknown_reagent_name_reason(raw_name: str, *extra_values: Any) -> str:
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


def looks_like_low_priority_business_normal_name(raw_name: str, *extra_values: Any) -> bool:
    return bool(low_priority_business_normal_name_reason(raw_name, *extra_values))
