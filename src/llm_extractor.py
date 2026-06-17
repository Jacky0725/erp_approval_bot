from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from openai import OpenAI


DEFAULT_RESULT = {
    "name": "",
    "cas": "",
    "flash_point": "",
    "boiling_point": "",
    "toxicity": "",
    "corrosive": None,
    "oxidizing": None,
    "flammable": None,
    "water_reactive": None,
    "explosive_risk": None,
    "heavy_metal": None,
    "suggested_categories": [],
    "evidence": [],
    "confidence": 0.0,
}


SYSTEM_PROMPT = """
You are a chemical data extraction assistant. Extract physical properties and
hazard signals from the provided raw web text and return strict JSON only.

You only organize source material and evidence. You must not decide whether an
approval should pass or fail. Final categorization is handled separately by
rule_engine.py.

Rules:
- Use only the provided raw_text. Do not invent facts.
- Use empty strings, null, or empty arrays for uncertain fields.
- Boolean fields must be true, false, or null.
- suggested_categories are tentative risk labels based on source material, not
  an approval decision.
- evidence should contain short source-backed snippets.
- Do not label a chemical as Toxic/High toxic only because LD50/LDLo data or
  mg/kg units appear. Treat LD50 values above 50 mg/kg as ordinary toxicity
  data unless source text explicitly says highly toxic, poison, danger, or
  the value crosses a strict toxicity threshold.
- For toxicity, summarize the actual dose and route. If the text only shows
  high LD50 values such as thousands of mg/kg, say "low acute toxicity data"
  instead of a high-risk toxic label.
- Do not treat "incompatible with strong oxidizing agents" or "keep away from
  oxidizing agents" as evidence that this chemical itself is an oxidizer.
  Only mark oxidizing=true when the source says the substance is an oxidizer
  or shows oxidizer-specific evidence such as H272, Hazard Class 5.1,
  oxidizing solid/liquid/gas, or strong oxidizer.
- A hydrochloride salt is not hydrochloric acid. Do not infer strong acid,
  special acid, or corrosive acid behavior only from "hydrochloride",
  "\u76d0\u9178\u76d0", or an organic compound name ending with hydrochloride salt.
""".strip()


@dataclass
class LlmExtractor:
    settings: dict[str, Any] | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        llm_settings = (self.settings or {}).get("llm", {})
        self.provider = os.getenv("LLM_PROVIDER") or llm_settings.get("provider") or "siliconflow"
        self.model = (
            self.model
            or os.getenv("SILICONFLOW_MODEL")
            or os.getenv("LLM_MODEL")
            or os.getenv("OPENAI_MODEL")
            or llm_settings.get("model")
            or "deepseek-ai/DeepSeek-V3.2"
        )
        self.base_url = (
            os.getenv("SILICONFLOW_BASE_URL")
            or os.getenv("LLM_BASE_URL")
            or llm_settings.get("base_url")
            or "https://api.siliconflow.cn/v1"
        )
        self.timeout_seconds = float(
            os.getenv("LLM_TIMEOUT_SECONDS")
            or llm_settings.get("timeout_seconds")
            or 45
        )
        self.max_retries = int(
            os.getenv("LLM_MAX_RETRIES")
            or llm_settings.get("max_retries")
            or 1
        )
        self.api_key_env = "SILICONFLOW_API_KEY" if self.provider == "siliconflow" else "OPENAI_API_KEY"
        self.client: OpenAI | None = None

    def extract_properties(self, raw_text: str, name: str = "", cas: str = "") -> dict[str, Any]:
        if not raw_text.strip():
            result = dict(DEFAULT_RESULT)
            result.update({"name": name, "cas": cas, "evidence": ["raw_text is empty; no source material to extract."]})
            return result

        user_prompt = self._build_user_prompt(raw_text=raw_text, name=name, cas=cas)
        try:
            client = self._client()
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            content = response.choices[0].message.content or "{}"
            parsed = json.loads(content)
        except Exception as error:
            result = dict(DEFAULT_RESULT)
            result.update(
                {
                    "name": name,
                    "cas": cas,
                    "evidence": [f"LLM extraction failed: {error}"],
                    "confidence": 0.0,
                }
            )
            return self._merge_local_hazard_fallback(result, raw_text=raw_text, name=name, cas=cas)

        result = self._normalize_result(parsed, fallback_name=name, fallback_cas=cas)
        result = self._suppress_incompatibility_only_oxidizing(result, raw_text)
        return self._merge_local_hazard_fallback(result, raw_text=raw_text, name=name, cas=cas)

    def _client(self) -> OpenAI:
        if self.client is None:
            api_key = (
                os.getenv("SILICONFLOW_API_KEY")
                or os.getenv("LLM_API_KEY")
                or os.getenv("OPENAI_API_KEY")
            )
            self.client = OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                max_retries=self.max_retries,
            )
        return self.client

    def extract_reagent_fields(self, text: str) -> dict[str, Any]:
        return self.extract_properties(raw_text=text)

    def generate_search_candidates(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
        local_candidates = self._local_search_candidates(reagent_info)
        prompt = f"""
Generate search keywords for finding reliable chemical physical-property,
hazard, SDS, or MSDS information. Return strict JSON only:

{{
  "candidates": string[],
  "reason": string,
  "confidence": number
}}

Use Chinese standard names, English names, CAS, aliases, and SDS/MSDS variants.
Do not decide an approval category.

reagent_info:
{json.dumps(reagent_info, ensure_ascii=False)}
""".strip()
        try:
            client = self._client()
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "You generate chemical lookup keywords as strict JSON only."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            parsed = json.loads(response.choices[0].message.content or "{}")
            llm_candidates = self._normalize_string_list(parsed.get("candidates"))
            candidates = self._dedupe_strings([*local_candidates, *llm_candidates])
            return {
                "candidates": candidates,
                "reason": str(parsed.get("reason") or "Generated lookup keywords from reagent fields.").strip(),
                "confidence": self._normalize_confidence(parsed.get("confidence") or 0.7),
                "used_llm": True,
            }
        except Exception as error:
            return {
                "candidates": local_candidates,
                "reason": f"LLM search-candidate generation failed; used local candidates: {error}",
                "confidence": 0.45 if local_candidates else 0.0,
                "used_llm": False,
            }

    def _build_user_prompt(self, raw_text: str, name: str, cas: str) -> str:
        clipped_text = raw_text[:12000]
        return f"""
Extract chemical physical-property and hazard information from the text below.
Return JSON with all fields present:

{{
  "name": string,
  "cas": string,
  "flash_point": string,
  "boiling_point": string,
  "toxicity": string,
  "corrosive": boolean|null,
  "oxidizing": boolean|null,
  "flammable": boolean|null,
  "water_reactive": boolean|null,
  "explosive_risk": boolean|null,
  "heavy_metal": boolean|null,
  "suggested_categories": string[],
  "evidence": string[],
  "confidence": number
}}

Known reagent name: {name or "unknown"}
Known CAS: {cas or "unknown"}

raw_text:
{clipped_text}
""".strip()

    @staticmethod
    def _local_search_candidates(reagent_info: dict[str, Any]) -> list[str]:
        values: list[str] = []
        for key in ("cas", "standard_name", "cleaned_name", "english_name", "raw_name", "name"):
            value = str(reagent_info.get(key) or "").strip()
            if value:
                values.append(value)
                if key == "cas":
                    values.extend([f"{value} SDS", f"{value} MSDS"])
                else:
                    values.extend([f"{value} SDS", f"{value} MSDS", f"{value} flash point toxicity"])
        for alias in reagent_info.get("aliases") or []:
            value = str(alias or "").strip()
            if value:
                values.append(value)
        return LlmExtractor._dedupe_strings(values)

    def _normalize_result(self, parsed: dict[str, Any], fallback_name: str, fallback_cas: str) -> dict[str, Any]:
        result = dict(DEFAULT_RESULT)
        result.update({key: parsed.get(key, default) for key, default in DEFAULT_RESULT.items()})

        result["name"] = str(result.get("name") or fallback_name or "").strip()
        result["cas"] = str(result.get("cas") or fallback_cas or self._extract_cas(" ".join(map(str, parsed.values()))) or "").strip()
        result["flash_point"] = str(result.get("flash_point") or "").strip()
        result["boiling_point"] = str(result.get("boiling_point") or "").strip()
        result["toxicity"] = str(result.get("toxicity") or "").strip()

        for field in ("corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal"):
            result[field] = self._normalize_bool(result.get(field))

        result["suggested_categories"] = self._normalize_string_list(result.get("suggested_categories"))
        result["evidence"] = self._normalize_string_list(result.get("evidence"))
        result["confidence"] = self._normalize_confidence(result.get("confidence"))
        return result

    def _merge_local_hazard_fallback(
        self,
        result: dict[str, Any],
        raw_text: str,
        name: str,
        cas: str,
    ) -> dict[str, Any]:
        fallback = self._local_hazard_fallback(raw_text=raw_text, name=name, cas=cas)
        if not fallback:
            return result

        merged = dict(result)
        for field in ("corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal"):
            if fallback.get(field) is True:
                merged[field] = True
            elif merged.get(field) is None and fallback.get(field) is not None:
                merged[field] = fallback[field]

        for field in ("flash_point", "boiling_point", "toxicity"):
            if not str(merged.get(field) or "").strip() and fallback.get(field):
                merged[field] = fallback[field]

        merged["suggested_categories"] = self._dedupe_strings(
            [*self._normalize_string_list(merged.get("suggested_categories")), *fallback.get("suggested_categories", [])]
        )
        merged["evidence"] = self._dedupe_strings(
            [*self._normalize_string_list(merged.get("evidence")), *fallback.get("evidence", [])]
        )
        merged["confidence"] = max(float(merged.get("confidence") or 0.0), float(fallback.get("confidence") or 0.0))
        merged["name"] = str(merged.get("name") or name or fallback.get("name") or "").strip()
        merged["cas"] = str(merged.get("cas") or cas or fallback.get("cas") or "").strip()
        return merged

    @staticmethod
    def _local_hazard_fallback(raw_text: str, name: str, cas: str) -> dict[str, Any] | None:
        combined = f"{name} {cas} {raw_text}".lower()
        evidence: list[str] = []
        categories: list[str] = []
        result = dict(DEFAULT_RESULT)
        result.update({"name": name, "cas": cas})

        special_hidden = "special hazardous chemicals do not display product information" in combined
        hydrochloric = any(token in combined for token in ("发烟盐酸", "盐酸", "hydrochloric acid", "7647-01-0"))
        fuming = "发烟" in combined or "fuming" in combined

        if hydrochloric:
            result["corrosive"] = True
            categories.extend(["特殊酸", "腐蚀性"])
            evidence.append("Known hydrochloric acid / 盐酸: corrosive strong acid.")
            if fuming:
                categories.insert(0, "发烟类")
                evidence.append("ERP name contains 发烟/fuming, indicating fuming acid.")
            if special_hidden:
                evidence.append("Chemsrc states special hazardous chemicals do not display product information.")

        if any(token in combined for token in ("h314", "causes severe skin burns", "corrosive", "r34", "腐蚀")):
            result["corrosive"] = True
            categories.append("腐蚀性")
            evidence.append("Source text contains corrosive hazard signal.")

        if LlmExtractor._has_positive_oxidizing_signal(combined):
            result["oxidizing"] = True
            categories.append("氧化剂")
            evidence.append("Source text contains oxidizing hazard signal.")

        if not evidence:
            return None

        result["suggested_categories"] = LlmExtractor._dedupe_strings(categories)
        result["evidence"] = LlmExtractor._dedupe_strings(evidence)
        result["confidence"] = 0.75
        return result

    @staticmethod
    def _suppress_incompatibility_only_oxidizing(result: dict[str, Any], raw_text: str) -> dict[str, Any]:
        text = (raw_text or "").lower()
        if LlmExtractor._has_positive_oxidizing_signal(text):
            return result

        incompatibility_only = any(
            phrase in text
            for phrase in (
                "incompatible with strong oxidizing agents",
                "incompatible with oxidizing agents",
                "keep away from oxidizing agents",
                "avoid oxidizing agents",
                "strong oxidizing agents",
                "强氧化剂不相容",
                "与氧化剂不相容",
                "避免接触氧化剂",
            )
        )
        has_oxidizing_result = result.get("oxidizing") is True or any(
            LlmExtractor._normalize_category(value) in {"氧化剂", "oxidizer", "oxidizing", "oxidizingagent"}
            for value in LlmExtractor._normalize_string_list(result.get("suggested_categories"))
        )
        if not incompatibility_only and not has_oxidizing_result:
            return result

        cleaned = dict(result)
        cleaned["oxidizing"] = False if result.get("oxidizing") is True else result.get("oxidizing")
        cleaned["suggested_categories"] = [
            value
            for value in LlmExtractor._normalize_string_list(cleaned.get("suggested_categories"))
            if LlmExtractor._normalize_category(value) not in {"氧化剂", "oxidizer", "oxidizing", "oxidizingagent"}
        ]
        cleaned["evidence"] = [
            value
            for value in LlmExtractor._normalize_string_list(cleaned.get("evidence"))
            if "oxidizing agent" not in value.lower()
            and "oxidizing hazard signal" not in value.lower()
            and "氧化剂" not in value
        ]
        return cleaned

    @staticmethod
    def _has_positive_oxidizing_signal(text: str) -> bool:
        normalized = text.lower()
        positive_patterns = [
            r"\bh272\b",
            r"hazard\s+class\s+5\.1",
            r"\bclass\s+5\.1\b",
            r"\boxidizing\s+(solid|liquid|gas|substance)\b",
            r"\bstrong\s+oxidizer\b",
            r"\boxidizer\b",
            r"\boxidising\s+(solid|liquid|gas|substance)\b",
            r"氧化剂",
            r"氧化性物质",
        ]
        return any(re.search(pattern, normalized, flags=re.I) for pattern in positive_patterns)

    @staticmethod
    def _normalize_category(value: Any) -> str:
        return re.sub(r"[\s,，;；:：()（）\-_]+", "", str(value or "").strip().lower())

    @staticmethod
    def _dedupe_strings(values: list[Any]) -> list[str]:
        output: list[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in output:
                output.append(text)
        return output

    @staticmethod
    def _normalize_bool(value: Any) -> bool | None:
        if isinstance(value, bool) or value is None:
            return value
        text = str(value).strip().lower()
        if text in {"true", "yes", "y", "1", "是", "有"}:
            return True
        if text in {"false", "no", "n", "0", "否", "无"}:
            return False
        return None

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            values = re.split(r"[,，;；\n]+", value)
        elif isinstance(value, list):
            values = value
        else:
            values = [value]
        return [str(item).strip() for item in values if str(item).strip()]

    @staticmethod
    def _normalize_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, confidence))

    @staticmethod
    def _extract_cas(text: str) -> str:
        match = re.search(r"\b\d{2,7}-\d{2}-\d\b", text)
        return match.group(0) if match else ""
