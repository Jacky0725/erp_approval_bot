"""Exact list-scoped chemical identities; never infer chemical aliases."""
from __future__ import annotations

from typing import Any


def reagent_group_key(record: dict[str, Any]) -> tuple[str, ...] | None:
    def value(*keys: str) -> str:
        for key in keys:
            raw = record.get(key)
            if raw is not None and raw is not False and str(raw).strip():
                return str(raw).strip()
        return ""

    number, name = value("试剂清单号", "清单号"), value("试剂名称", "name")
    if not number or not name:
        return None
    # Packaging is excluded by the existing classification input contract.
    # Concentration/formulation remain in name/type/desc.
    return number, name, value("CAS号", "cas_code"), value("type"), value("desc")


def conflicting_groups(records: list[tuple[dict[str, Any], str]]) -> set[tuple[str, ...]]:
    groups: dict[tuple[str, ...], set[str]] = {}
    for record, category in records:
        key = reagent_group_key(record)
        if key is not None:
            groups.setdefault(key, set()).add(category)
    return {key for key, categories in groups.items() if len(categories) > 1}
