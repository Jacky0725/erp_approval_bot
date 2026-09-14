from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from name_normalizer import NameNormalizer


CAS_PATTERN = re.compile(r"^\d{2,7}-\d{2}-\d$")
MIXTURE_MARKERS = (
    "混合",
    "溶液",
    "试剂盒",
    "kit",
    "buffer",
    "培养基",
    "清洗剂",
    "标准溶液",
)
IDENTITY_STATUSES = {"verified", "name_only", "cas_missing", "ambiguous", "conflict", "unresolved"}


@dataclass(frozen=True)
class IdentityRecord:
    """A deterministic identity result before any external chemical lookup."""

    raw_name: str
    cleaned_base_name: str
    standard_name_cn: str
    standard_name_en: str
    cas: str
    concentration: str
    aliases: tuple[str, ...]
    mixture_state: str
    status: str
    confidence: float
    reason: str
    evidence_refs: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    provider_ids: tuple[tuple[str, str], ...] = ()

    def provider_id(self, provider: str) -> str:
        """Return a stable external identifier without changing the ERP CAS."""
        wanted = str(provider or "").strip().casefold()
        return next((value for key, value in self.provider_ids if key.casefold() == wanted), "")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["aliases"] = list(self.aliases)
        result["evidence_refs"] = list(self.evidence_refs)
        result["conflicts"] = list(self.conflicts)
        result["provider_ids"] = {key: value for key, value in self.provider_ids}
        return result


@dataclass
class ReagentIdentityResolver:
    """Resolve local reagent identity without LLM or network access.

    The resolver deliberately does not treat a syntactically valid ERP CAS as
    externally verified.  It is `verified` only when the local reviewed alias
    store confirms that CAS or alias.  External source adapters promote the
    remaining `name_only` records later in the V2 chain.
    """

    settings: dict[str, Any] | None = None
    root_dir: Path | None = None

    def __post_init__(self) -> None:
        self.root_dir = self.root_dir or Path(__file__).resolve().parents[1]
        self.normalizer = NameNormalizer(
            settings=self.settings,
            root_dir=self.root_dir,
            enable_llm=False,
        )

    def resolve(
        self,
        raw_name: str,
        *,
        cas: str = "",
        specification: str = "",
        unit: str = "",
    ) -> IdentityRecord:
        raw_name = str(raw_name or "").strip()
        supplied_cas = self._extract_cas(cas) or self._extract_cas(raw_name)
        normalized = self.normalizer.normalize(
            raw_name,
            cas=supplied_cas,
            specification=specification,
            unit=unit,
        )
        cleaned = str(normalized.get("cleaned_name") or "").strip()
        standard_name = str(normalized.get("standard_name") or cleaned).strip()
        english_name = str(normalized.get("english_name") or "").strip()
        aliases = tuple(dict.fromkeys(str(value).strip() for value in normalized.get("aliases", []) if str(value).strip()))
        normalized_cas = self._extract_cas(str(normalized.get("cas") or ""))
        valid_supplied_cas = bool(supplied_cas and self.is_valid_cas(supplied_cas))
        valid_normalized_cas = bool(normalized_cas and self.is_valid_cas(normalized_cas))
        mixture_state = self._mixture_state(raw_name, cleaned, specification)
        conflicts: list[str] = []

        if supplied_cas and not valid_supplied_cas:
            conflicts.append("invalid_erp_cas_checksum")
        if supplied_cas and normalized_cas and supplied_cas != normalized_cas:
            conflicts.append("erp_cas_conflicts_with_local_alias")
        if mixture_state != "single_substance":
            conflicts.append("mixture_or_product_requires_sds")

        local_cas_match = self.normalizer._lookup_by_cas(supplied_cas) if valid_supplied_cas else None
        local_alias_match = self.normalizer._lookup_alias(cleaned, raw_name) if cleaned else None
        if conflicts:
            status = "conflict" if any(item.startswith("invalid_") or "conflicts" in item for item in conflicts) else "ambiguous"
            confidence = 0.0 if status == "conflict" else min(float(normalized.get("confidence") or 0.0), 0.65)
        elif local_cas_match or local_alias_match:
            status = "verified"
            confidence = 0.98 if local_cas_match else 0.92
        elif cleaned or valid_normalized_cas:
            status = "name_only"
            confidence = min(float(normalized.get("confidence") or 0.0), 0.65)
        else:
            status = "unresolved"
            confidence = 0.0

        evidence_refs: list[str] = []
        if local_cas_match:
            evidence_refs.append("local_alias:cas")
        if local_alias_match:
            evidence_refs.append("local_alias:name")
        return IdentityRecord(
            raw_name=raw_name,
            cleaned_base_name=cleaned,
            standard_name_cn=standard_name,
            standard_name_en=english_name,
            cas=normalized_cas if valid_normalized_cas else "",
            concentration=str(normalized.get("concentration") or "").strip(),
            aliases=aliases,
            mixture_state=mixture_state,
            status=status,
            confidence=max(0.0, min(1.0, confidence)),
            reason=str(normalized.get("reason") or "").strip(),
            evidence_refs=tuple(evidence_refs),
            conflicts=tuple(conflicts),
        )

    @staticmethod
    def is_valid_cas(value: str) -> bool:
        value = str(value or "").strip()
        if not CAS_PATTERN.fullmatch(value):
            return False
        digits = value.replace("-", "")
        checksum = sum(int(digit) * index for index, digit in enumerate(reversed(digits[:-1]), start=1)) % 10
        return checksum == int(digits[-1])

    @staticmethod
    def _extract_cas(value: str) -> str:
        match = re.search(r"\b\d{2,7}-\d{2}-\d\b", str(value or ""))
        return match.group(0) if match else ""

    @staticmethod
    def _mixture_state(raw_name: str, cleaned_name: str, specification: str) -> str:
        text = " ".join([raw_name, cleaned_name, str(specification or "")]).casefold()
        return "mixture_or_product" if any(marker.casefold() in text for marker in MIXTURE_MARKERS) else "single_substance"
