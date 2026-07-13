from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from openai import OpenAI

from llm_providers import get_llm_provider, provider_base_url, resolve_llm_api_key


CAS_PATTERN = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
CONCENTRATION_PATTERNS = [
    re.compile(r"(?i)\b\d+(?:\.\d+)?\s*%"),
    re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:mol\s*/\s*l|mol/l|m)\b"),
    re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:ppm|ppb)\b"),
]
PACKAGING_PATTERN = re.compile(
    r"(?i)\b\d+(?:\.\d+)?\s*(?:mg|g|kg|ml|mL|l|L)\s*(?:/|每)?\s*(?:瓶|桶|袋|支|盒|包|bottle|drum|bag)?\b"
)
PURITY_PATTERN = re.compile(
    r"(?i)\b(?:AR|GR|CP|HPLC|ACS|GC|LCMS|UPLC|reagent\s*grade|analytical\s*grade)\b|"
    r"(?:分析纯|优级纯|化学纯|色谱纯|基准试剂|实验试剂|工业级|试剂级|食品级|电子级|无水|水合物)"
)
NOISE_PATTERN = re.compile(
    r"(?:≥|>=|≤|<=)?\s*\d+(?:\.\d+)?\s*%?\s*(?:纯度|含量|浓度)?|"
    r"(?:纯度|含量|浓度|规格|包装|原装|进口|国产|现货|危化品|易制毒|易制爆)"
)
BRACKET_PATTERN = re.compile(r"[\[\]【】()（）{}]")
INTERNAL_CODE_SUFFIX_PATTERN = re.compile(
    r"^(.+?)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9_.\-/\\ ]{1,})$"
)


DEFAULT_RESULT = {
    "raw_name": "",
    "cleaned_name": "",
    "standard_name": "",
    "english_name": "",
    "source_url": "",
    "cas": "",
    "concentration": "",
    "aliases": [],
    "candidate_names": [],
    "suspected_invalid_name": False,
    "suspected_invalid_reason": "",
    "confidence": 0.0,
    "need_manual_review": True,
    "reason": "",
}


@dataclass
class NameNormalizer:
    settings: dict[str, Any] | None = None
    root_dir: Path | None = None
    aliases_path: Path | None = None
    enable_llm: bool = True

    def __post_init__(self) -> None:
        root_dir = self.root_dir or Path(__file__).resolve().parents[1]
        if self.aliases_path is None:
            paths = (self.settings or {}).get("paths", {})
            self.aliases_path = root_dir / paths.get("name_aliases_yaml", "config/name_aliases.yaml")
        self.alias_data = self._load_aliases(self.aliases_path)
        self.client: OpenAI | None = None

    def normalize(
        self,
        raw_name: str,
        cas: str = "",
        specification: str = "",
        unit: str = "",
        **extra_fields: Any,
    ) -> dict[str, Any]:
        raw_name = str(raw_name or "").strip()
        cas_no = self._extract_cas(cas) or self._extract_cas(raw_name)
        concentration = self._extract_concentration(" ".join([raw_name, str(specification or ""), str(unit or "")]))
        cleaned_name = self._clean_name(raw_name)
        aliases: list[str] = []

        if cas_no:
            cas_match = self._lookup_by_cas(cas_no)
            if cas_match:
                aliases = cas_match.get("aliases", [])
                return self._result(
                    raw_name=raw_name,
                    cleaned_name=cleaned_name,
                    standard_name=cas_match["standard_name"],
                    english_name=cas_match.get("english_name", ""),
                    source_url=cas_match.get("source_url", ""),
                    cas=cas_no,
                    concentration=concentration,
                    aliases=aliases,
                    confidence=0.98,
                    reason="Matched standard name by CAS number.",
                )

        alias_match = self._lookup_alias(cleaned_name, raw_name)
        if alias_match:
            standard_name = alias_match["standard_name"]
            aliases = alias_match.get("aliases", [])
            alias_cas = self._extract_cas(str(alias_match.get("cas", "")))
            matched_cas = cas_no or alias_cas
            source_url = ""
            if not cas_no or not alias_cas or alias_cas == cas_no:
                source_url = alias_match.get("source_url", "") or self._source_url_for_standard_name(standard_name)
            reason = "Matched standard name by configured alias."
            if cas_no and alias_cas and alias_cas != cas_no:
                reason = (
                    "ERP provided CAS number has priority; configured alias matched the reagent name "
                    "but its CAS/source URL was ignored because it conflicts with the ERP CAS."
                )
            return self._result(
                raw_name=raw_name,
                cleaned_name=cleaned_name,
                standard_name=standard_name,
                english_name=alias_match.get("english_name", "") or self._english_name_for_standard_name(standard_name),
                source_url=source_url,
                cas=matched_cas,
                concentration=concentration,
                aliases=aliases,
                confidence=0.92,
                reason=reason,
            )

        abbreviation_match = self._lookup_abbreviation(cleaned_name, raw_name)
        if abbreviation_match:
            standard_name, aliases = abbreviation_match
            return self._result(
                raw_name=raw_name,
                cleaned_name=cleaned_name,
                standard_name=standard_name,
                english_name=self._english_name_for_standard_name(standard_name),
                source_url=self._source_url_for_standard_name(standard_name),
                cas=cas_no,
                concentration=concentration,
                aliases=aliases,
                confidence=0.90,
                reason="Matched standard name by common abbreviation or formula.",
            )

        if cleaned_name:
            llm_result = self._llm_candidate(
                raw_name=raw_name,
                cleaned_name=cleaned_name,
                cas=cas_no,
                concentration=concentration,
                specification=specification,
                unit=unit,
                extra_fields=extra_fields,
            )
            if llm_result:
                return llm_result

            return self._result(
                raw_name=raw_name,
                cleaned_name=cleaned_name,
                standard_name=cleaned_name,
                english_name="",
                source_url="",
                cas=cas_no,
                concentration=concentration,
                aliases=[],
                confidence=0.65,
                reason="Cleaned name by rules, but no alias/CAS/abbreviation matched.",
            )

        return self._result(
            raw_name=raw_name,
            cleaned_name=cleaned_name,
            standard_name="",
            english_name="",
            source_url="",
            cas=cas_no,
            concentration=concentration,
            aliases=[],
            confidence=0.0,
            reason="No usable reagent name was found after cleaning.",
        )

    def _lookup_by_cas(self, cas: str) -> dict[str, Any] | None:
        value = (self.alias_data.get("cas") or {}).get(cas)
        if isinstance(value, str):
            return {"standard_name": value, "aliases": []}
        if isinstance(value, dict) and value.get("standard_name"):
            return {
                "standard_name": str(value.get("standard_name", "")).strip(),
                "english_name": str(value.get("english_name", "")).strip(),
                "source_url": str(value.get("source_url") or value.get("chemsrc_url") or "").strip(),
                "aliases": self._string_list(value.get("aliases")),
            }
        return None

    def _lookup_alias(self, cleaned_name: str, raw_name: str) -> dict[str, Any] | None:
        aliases = self.alias_data.get("aliases") or {}
        candidates = self._lookup_keys(cleaned_name, raw_name)
        for key, value in aliases.items():
            if self._alias_key(key) in candidates:
                if isinstance(value, dict):
                    standard_name = str(value.get("standard_name") or "").strip()
                    if not standard_name:
                        continue
                    cas = self._extract_cas(str(value.get("cas") or ""))
                    by_cas = self._lookup_by_cas(cas) if cas else None
                    return {
                        "standard_name": standard_name,
                        "english_name": str(value.get("english_name") or (by_cas or {}).get("english_name") or "").strip(),
                        "source_url": str(
                            value.get("source_url")
                            or value.get("chemsrc_url")
                            or (by_cas or {}).get("source_url")
                            or ""
                        ).strip(),
                        "cas": cas,
                        "aliases": self._aliases_for_standard_name(standard_name, extra_alias=str(key)),
                    }
                standard_name = str(value).strip()
                return {
                    "standard_name": standard_name,
                    "english_name": self._english_name_for_standard_name(standard_name),
                    "source_url": self._source_url_for_standard_name(standard_name),
                    "cas": self._cas_for_standard_name(standard_name),
                    "aliases": self._aliases_for_standard_name(standard_name, extra_alias=str(key)),
                }
        return None

    def _lookup_abbreviation(self, cleaned_name: str, raw_name: str) -> tuple[str, list[str]] | None:
        abbreviations = self.alias_data.get("abbreviations") or {}
        candidates = self._lookup_keys(cleaned_name, raw_name)
        for key, value in abbreviations.items():
            if self._alias_key(key) in candidates:
                standard_name = str(value).strip()
                return standard_name, self._aliases_for_standard_name(standard_name, extra_alias=str(key))
        return None

    def _lookup_keys(self, *values: str) -> set[str]:
        keys = set()
        for value in values:
            if not value:
                continue
            keys.add(self._alias_key(value))
            for token in re.split(r"[\s,，;；/]+", value):
                if token:
                    keys.add(self._alias_key(token))
        return keys

    def _aliases_for_standard_name(self, standard_name: str, extra_alias: str = "") -> list[str]:
        aliases = []
        for cas_info in (self.alias_data.get("cas") or {}).values():
            if isinstance(cas_info, dict) and str(cas_info.get("standard_name", "")).strip() == standard_name:
                aliases.extend(self._string_list(cas_info.get("aliases")))
        if extra_alias:
            aliases.append(extra_alias)
        return list(dict.fromkeys(alias for alias in aliases if alias))

    def _english_name_for_standard_name(self, standard_name: str) -> str:
        for cas_info in (self.alias_data.get("cas") or {}).values():
            if isinstance(cas_info, dict) and str(cas_info.get("standard_name", "")).strip() == standard_name:
                return str(cas_info.get("english_name", "")).strip()
        return ""

    def _source_url_for_standard_name(self, standard_name: str) -> str:
        for cas_info in (self.alias_data.get("cas") or {}).values():
            if isinstance(cas_info, dict) and str(cas_info.get("standard_name", "")).strip() == standard_name:
                return str(cas_info.get("source_url") or cas_info.get("chemsrc_url") or "").strip()
        return ""

    def _cas_for_standard_name(self, standard_name: str) -> str:
        for cas, cas_info in (self.alias_data.get("cas") or {}).items():
            if isinstance(cas_info, dict) and str(cas_info.get("standard_name", "")).strip() == standard_name:
                return self._extract_cas(str(cas))
        return ""

    def _llm_candidate(
        self,
        raw_name: str,
        cleaned_name: str,
        cas: str,
        concentration: str,
        specification: str,
        unit: str,
        extra_fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.enable_llm or not self._has_api_key():
            return None

        llm_settings = (self.settings or {}).get("llm", {})
        model = (
            os.getenv("SILICONFLOW_MODEL")
            or os.getenv("LLM_MODEL")
            or os.getenv("OPENAI_MODEL")
            or llm_settings.get("model")
            or "deepseek-ai/DeepSeek-V3.2"
        )
        try:
            response = self._client().chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Return strict JSON only. Normalize an ERP reagent name into "
                            "a likely standard chemical name for chemical website search. "
                            "standard_name must be a Chinese standard chemical name. "
                            "english_name must be a widely searchable English chemical name. "
                            "Remove concentration, purity, grade, package size, and vendor text. "
                            "Prefer a widely searchable English chemical name when possible. "
                            "If the ERP name is a mixture, solution, kit, or unclear trade name, "
                            "set confidence below 0.8 and explain why. Do not decide approval."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "raw_name": raw_name,
                                "cleaned_name": cleaned_name,
                                "cas": cas,
                                "concentration": concentration,
                                "specification": specification,
                                "unit": unit,
                                "extra_fields": extra_fields,
                                "required_json_fields": [
                                    "standard_name",
                                    "english_name",
                                    "chinese_name",
                                    "main_component",
                                    "is_mixture_or_solution",
                                    "aliases",
                                    "confidence",
                                    "reason",
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            parsed = json.loads(response.choices[0].message.content or "{}")
        except Exception:
            return None

        standard_name = str(
            parsed.get("standard_name")
            or parsed.get("chinese_name")
            or parsed.get("main_component")
            or parsed.get("name")
            or ""
        ).strip()
        if not standard_name:
            return None

        aliases = self._string_list(parsed.get("aliases"))
        english_name = str(parsed.get("english_name") or "").strip()
        confidence = self._normalize_confidence(parsed.get("confidence"), default=0.70)
        reason = str(parsed.get("reason") or "Generated candidate standard name by LLM.").strip()
        return self._result(
            raw_name=raw_name,
            cleaned_name=cleaned_name,
            standard_name=standard_name,
            english_name=english_name,
            source_url="",
            cas=cas,
            concentration=concentration,
            aliases=aliases,
            confidence=confidence,
            reason=reason,
        )

    def update_aliases_after_approval(
        self,
        raw_name: str,
        standard_name: str,
        cas: str = "",
        english_name: str = "",
        cleaned_name: str = "",
        aliases: list[str] | None = None,
        approved: bool = False,
    ) -> bool:
        if not approved:
            return False

        standard_name = str(standard_name or "").strip()
        if not standard_name:
            return False

        raw_name = str(raw_name or "").strip()
        cleaned_name = str(cleaned_name or self._clean_name(raw_name)).strip()
        cas_no = self._extract_cas(cas)
        alias_values = self._string_list([raw_name, cleaned_name, *(aliases or [])])
        alias_values = list(dict.fromkeys(value for value in alias_values if value and value != standard_name))

        if not alias_values and not cas_no:
            return False

        data = self._load_aliases(self.aliases_path)
        data.setdefault("cas", {})
        data.setdefault("aliases", {})
        data.setdefault("abbreviations", {})

        for alias in alias_values:
            data["aliases"][alias] = standard_name

        if cas_no:
            cas_entry = data["cas"].get(cas_no)
            if not isinstance(cas_entry, dict):
                cas_entry = {"standard_name": standard_name, "aliases": []}
            cas_entry["standard_name"] = standard_name
            if english_name:
                cas_entry["english_name"] = english_name
            existing_aliases = self._string_list(cas_entry.get("aliases"))
            cas_entry["aliases"] = list(dict.fromkeys([*existing_aliases, *alias_values, standard_name]))
            data["cas"][cas_no] = cas_entry

        self.aliases_path.parent.mkdir(parents=True, exist_ok=True)
        with self.aliases_path.open("w", encoding="utf-8") as file:
            yaml.safe_dump(data, file, allow_unicode=True, sort_keys=False)

        self.alias_data = data
        return True

    def _client(self) -> OpenAI:
        if self.client is None:
            llm_settings = (self.settings or {}).get("llm", {})
            provider_id = os.getenv("LLM_PROVIDER") or llm_settings.get("provider") or "siliconflow"
            provider = get_llm_provider(provider_id)
            base_url = (
                os.getenv("LLM_BASE_URL")
                or (os.getenv("SILICONFLOW_BASE_URL") if provider.id == "siliconflow" else "")
                or llm_settings.get("base_url")
            )
            self.client = OpenAI(api_key=self._api_key(provider.id), base_url=provider_base_url(provider.id, base_url))
        return self.client

    def _has_api_key(self) -> bool:
        return bool(self._api_key())

    @staticmethod
    def _api_key(provider_id: str | None = None) -> str:
        return resolve_llm_api_key(provider_id or os.getenv("LLM_PROVIDER") or "siliconflow")

    def _clean_name(self, raw_name: str) -> str:
        text = unicodedata.normalize("NFKC", raw_name or "")
        text = self._strip_internal_code_suffix(text)
        text = CAS_PATTERN.sub(" ", text)
        for pattern in CONCENTRATION_PATTERNS:
            text = pattern.sub(" ", text)
        text = PACKAGING_PATTERN.sub(" ", text)
        text = PURITY_PATTERN.sub(" ", text)
        text = NOISE_PATTERN.sub(" ", text)
        text = BRACKET_PATTERN.sub(" ", text)
        text = re.sub(r"[，,;；:：|]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" -_/")
        return text

    @staticmethod
    def _strip_internal_code_suffix(text: str) -> str:
        match = INTERNAL_CODE_SUFFIX_PATTERN.match(text.strip())
        if not match:
            return text
        suffix = match.group(2).strip()
        is_code_like = bool(re.search(r"\d", suffix)) and bool(
            re.search(r"[-_/\\]|[A-Za-z].*\d|\d.*[A-Za-z]", suffix)
        )
        if not is_code_like:
            return text
        return match.group(1).strip()

    @staticmethod
    def _extract_concentration(text: str) -> str:
        values = []
        for pattern in CONCENTRATION_PATTERNS:
            for match in pattern.findall(unicodedata.normalize("NFKC", text or "")):
                value = match if isinstance(match, str) else match[0]
                normalized = re.sub(r"\s+", "", value)
                if normalized and normalized not in values:
                    values.append(normalized)
        return "; ".join(values)

    @staticmethod
    def _extract_cas(text: str) -> str:
        match = CAS_PATTERN.search(text or "")
        return match.group(0) if match else ""

    @staticmethod
    def _alias_key(value: str) -> str:
        return re.sub(r"[\s\-_]+", "", unicodedata.normalize("NFKC", value or "")).lower()

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            values = value
        else:
            values = [value]
        return [str(item).strip() for item in values if str(item).strip()]

    @staticmethod
    def _normalize_confidence(value: Any, default: float = 0.0) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = default
        return max(0.0, min(1.0, confidence))

    @classmethod
    def _load_aliases(cls, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"cas": {}, "aliases": {}, "abbreviations": {}}
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}
        return {
            "cas": data.get("cas") or {},
            "aliases": data.get("aliases") or {},
            "abbreviations": data.get("abbreviations") or {},
        }

    @staticmethod
    def _result(
        raw_name: str,
        cleaned_name: str,
        standard_name: str,
        english_name: str,
        source_url: str,
        cas: str,
        concentration: str,
        aliases: list[str],
        confidence: float,
        reason: str,
    ) -> dict[str, Any]:
        result = dict(DEFAULT_RESULT)
        result.update(
            {
                "raw_name": raw_name,
                "cleaned_name": cleaned_name,
                "standard_name": standard_name,
                "english_name": english_name,
                "source_url": source_url,
                "cas": cas,
                "concentration": concentration,
                "aliases": aliases,
                "confidence": confidence,
                "need_manual_review": confidence < 0.8,
                "reason": reason,
            }
        )
        return result


def normalize_name(
    raw_name: str,
    cas: str = "",
    specification: str = "",
    unit: str = "",
    settings: dict[str, Any] | None = None,
    root_dir: Path | None = None,
) -> dict[str, Any]:
    return NameNormalizer(settings=settings, root_dir=root_dir).normalize(
        raw_name=raw_name,
        cas=cas,
        specification=specification,
        unit=unit,
    )


def update_aliases_after_approval(
    raw_name: str,
    standard_name: str,
    cas: str = "",
    english_name: str = "",
    cleaned_name: str = "",
    aliases: list[str] | None = None,
    approved: bool = False,
    settings: dict[str, Any] | None = None,
    root_dir: Path | None = None,
) -> bool:
    return NameNormalizer(settings=settings, root_dir=root_dir).update_aliases_after_approval(
        raw_name=raw_name,
        standard_name=standard_name,
        cas=cas,
        english_name=english_name,
        cleaned_name=cleaned_name,
        aliases=aliases,
        approved=approved,
    )
