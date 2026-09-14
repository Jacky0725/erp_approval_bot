from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


DECISION_FIELDS = (
    "flash_point",
    "toxicity",
    "corrosive",
    "oxidizing",
    "flammable",
    "water_reactive",
    "explosive_risk",
    "heavy_metal",
)
SOURCE_TIERS = {"Supplier SDS": 1, "PubChem": 2, "EPA CompTox": 3, "NIST": 3, "LLM": 9}
TEMPERATURE_PATTERN = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(°\s*[CF]|[CFK])\b", re.I)


@dataclass(frozen=True)
class EvidenceItem:
    field: str
    raw_value: str
    normalized_value: str | float | bool | None
    unit: str
    status: str
    source: str
    source_url: str
    retrieved_at: str
    confidence: float
    evidence_span: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedField:
    value: str | float | bool | None
    unit: str
    status: str
    evidence_refs: tuple[int, ...]


@dataclass(frozen=True)
class ResolvedProperties:
    fields: dict[str, ResolvedField]
    evidence: tuple[EvidenceItem, ...]
    conflicts: tuple[str, ...] = ()

    def missing_decision_fields(self) -> list[str]:
        return [field for field in DECISION_FIELDS if self.fields.get(field, ResolvedField(None, "", "unknown", ())).status == "unknown"]

    def to_rule_input(self, *, name: str = "", cas: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {"name": name, "cas": cas, "evidence": []}
        for field, resolved in self.fields.items():
            result[field] = resolved.value
            result["evidence"].extend(
                self.evidence[index].raw_value for index in resolved.evidence_refs if index < len(self.evidence)
            )
        return result


class EvidenceResolver:
    """Normalize provider facts while preserving uncertainty and disagreements."""

    def normalize_legacy_items(self, items: Iterable[dict[str, Any]]) -> list[EvidenceItem]:
        normalized: list[EvidenceItem] = []
        for item in items:
            field = str(item.get("field") or "").strip()
            raw = str(item.get("value") or "").strip()
            if not field or not raw:
                continue
            source = str(item.get("source") or "").strip()
            value, unit, status = self._normalize_value(field, raw)
            normalized.append(
                EvidenceItem(
                    field=field,
                    raw_value=raw,
                    normalized_value=value,
                    unit=unit,
                    status=status,
                    source=source,
                    source_url=str(item.get("source_url") or ""),
                    retrieved_at=str(item.get("retrieved_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    confidence=self._confidence(source, status),
                    evidence_span=str(item.get("evidence_span") or ""),
                )
            )
        return normalized

    def resolve(self, evidence: Iterable[EvidenceItem]) -> ResolvedProperties:
        values = tuple(evidence)
        grouped: dict[str, list[tuple[int, EvidenceItem]]] = {}
        for index, item in enumerate(values):
            grouped.setdefault(item.field, []).append((index, item))

        fields: dict[str, ResolvedField] = {}
        conflicts: list[str] = []
        for field, candidates in grouped.items():
            viable = [entry for entry in candidates if entry[1].status in {"observed", "reported"}]
            if not viable:
                fields[field] = ResolvedField(None, "", "unknown", tuple(index for index, _ in candidates))
                continue
            viable.sort(key=lambda entry: (SOURCE_TIERS.get(entry[1].source, 8), -entry[1].confidence))
            selected_index, selected = viable[0]
            if self._has_conflict([item for _, item in viable]):
                fields[field] = ResolvedField(None, selected.unit, "conflict", tuple(index for index, _ in viable))
                conflicts.append(field)
            else:
                fields[field] = ResolvedField(
                    selected.normalized_value,
                    selected.unit,
                    selected.status,
                    tuple(index for index, _ in viable),
                )
        for field in DECISION_FIELDS:
            fields.setdefault(field, ResolvedField(None, "", "unknown", ()))
        return ResolvedProperties(fields=fields, evidence=values, conflicts=tuple(conflicts))

    def _normalize_value(self, field: str, raw: str) -> tuple[str | float | bool | None, str, str]:
        lowered = raw.casefold()
        if field in {"flash_point", "boiling_point"}:
            match = TEMPERATURE_PATTERN.search(raw)
            if not match:
                return None, "", "unknown"
            value = float(match.group(1))
            unit = re.sub(r"\s+", "", match.group(2)).upper()
            if unit == "K":
                value -= 273.15
            elif unit in {"F", "°F"}:
                value = (value - 32) * 5 / 9
            return round(value, 3), "degC", "observed"
        if field in {"corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal"}:
            negative_tokens = {
                "corrosive": ("not corrosive", "non-corrosive", "noncorrosive", "无腐蚀", "不腐蚀"),
                "oxidizing": ("not oxidizing", "non-oxidizing", "nonoxidizing", "无氧化性", "非氧化性"),
                "flammable": ("not flammable", "non-flammable", "nonflammable", "不易燃", "不可燃"),
                "water_reactive": ("not water-reactive", "non-water-reactive", "不与水反应", "遇水不反应"),
                "explosive_risk": ("not explosive", "non-explosive", "无爆炸危险", "不易爆"),
                "heavy_metal": ("no heavy metal", "heavy-metal-free", "不含重金属", "无重金属"),
            }
            if any(token in lowered for token in negative_tokens[field]):
                return False, "", "reported"
            tokens = {
                "corrosive": ("corros", "腐蚀"),
                "oxidizing": ("oxidiz", "氧化"),
                "flammable": ("flamm", "易燃", "可燃"),
                "water_reactive": ("water-react", "与水反应", "遇水"),
                "explosive_risk": ("explos", "爆炸", "易爆"),
                "heavy_metal": ("heavy metal", "重金属"),
            }
            if any(token in lowered for token in tokens[field]):
                return True, "", "reported"
            return None, "", "unknown"
        return raw, "", "reported"

    @staticmethod
    def _confidence(source: str, status: str) -> float:
        if status == "unknown":
            return 0.0
        return {1: 0.95, 2: 0.9, 3: 0.8}.get(SOURCE_TIERS.get(source, 8), 0.5)

    @staticmethod
    def _has_conflict(items: list[EvidenceItem]) -> bool:
        values = {item.normalized_value for item in items}
        if len(values) <= 1:
            return False
        numeric = [value for value in values if isinstance(value, float)]
        return len(numeric) != len(values) or max(numeric) - min(numeric) > 5.0
