from __future__ import annotations

import re
import time
from typing import Any

from chemical_sources.base import ProviderDiagnostic, ProviderEvidence
from reagent_identity import IdentityRecord


class SupplierSdsAdapter:
    """Read only locally supplied SDS text into field-level evidence.

    No supplier website is contacted here.  The adapter is deliberately small:
    it preserves the source text in the audit payload and only exposes values
    that can be found by an explicit SDS label or hazard wording.
    """

    name = "Supplier SDS"
    _SECTION = re.compile(r"(?im)^\s*(?:SECTION\s*)?(?:第\s*)?(?P<number>0?[1-9]|1[0-6])\s*(?:部分|节|SECTION)?\s*[:：.、-]?\s*")

    def extract(self, identity: IdentityRecord, reagent: dict[str, Any]) -> ProviderEvidence | None:
        raw_text = str(reagent.get("sds_text") or reagent.get("SDS文本") or "").strip()
        if not raw_text:
            return None
        started = time.monotonic()
        sections = self._sections(raw_text)
        fields: dict[str, str] = {}
        section_9 = sections.get("9", raw_text)
        for field, labels in {
            "flash_point": ("flash point", "闪点"),
            "boiling_point": ("initial boiling point", "boiling point", "沸点"),
        }.items():
            value = self._labeled_value(section_9, labels)
            if value:
                fields[field] = value

        hazard_text = " ".join(sections.get(number, "") for number in ("2", "10")) or raw_text
        for field, tokens in {
            "flammable": ("flammable", "易燃", "可燃"),
            "corrosive": ("corrosive", "腐蚀"),
            "oxidizing": ("oxidizing", "氧化性"),
            "water_reactive": ("water-react", "reacts with water", "遇水", "与水反应"),
            "explosive_risk": ("explosive", "爆炸", "易爆"),
        }.items():
            if any(token.casefold() in hazard_text.casefold() for token in tokens):
                fields[field] = hazard_text[:1200]

        return ProviderEvidence(
            identity=identity,
            source=self.name,
            source_url="",
            fields=fields,
            raw={
                "manufacturer": str(reagent.get("manufacturer") or reagent.get("供应商") or reagent.get("生产厂家") or "").strip(),
                "catalog_number": str(reagent.get("catalog_number") or reagent.get("货号") or "").strip(),
                "sections_found": sorted(sections),
            },
            diagnostic=ProviderDiagnostic(self.name, "success", int((time.monotonic() - started) * 1000)),
        )

    @classmethod
    def _sections(cls, text: str) -> dict[str, str]:
        normalized = re.sub(r"\r\n?", "\n", text)
        matches = list(cls._SECTION.finditer(normalized))
        sections: dict[str, str] = {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
            sections[str(int(match.group("number")))] = normalized[match.end() : end].strip()
        return sections

    @staticmethod
    def _labeled_value(text: str, labels: tuple[str, ...]) -> str:
        for label in labels:
            match = re.search(rf"(?im){re.escape(label)}\s*[:：]?\s*([^\n;；]{{1,80}})", text)
            if match:
                return match.group(1).strip()
        return ""
