"""Keep packaging metadata out of chemical-classification workflows."""
from __future__ import annotations

from typing import Any


IGNORED_REAGENT_METADATA_KEYS = frozenset({
    "规格", "规格单位", "包装方式", "包装", "包装单位", "包装规格", "包装数量", "包装量", "箱数", "单位",
    "spec", "specification", "specunit", "spec_unit", "spec_unit_id", "unit", "uom", "uom_id",
    "package", "packaging", "package_type", "package_unit", "package_size", "package_quantity",
    "box_num", "boxnum", "product_uom_qty",
})


def is_ignored_reagent_metadata_key(key: object) -> bool:
    return str(key or "").strip().casefold().replace("-", "_") in IGNORED_REAGENT_METADATA_KEYS


def minimize_reagent_record(record: dict[str, Any] | None) -> dict[str, Any]:
    """Copy a record without packaging/size fields before classification work."""
    return {str(key): value for key, value in (record or {}).items() if not is_ignored_reagent_metadata_key(key)}
