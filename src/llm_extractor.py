from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from openai import OpenAI

from llm_providers import (
    get_llm_provider,
    provider_base_url,
    provider_default_model,
    resolve_llm_api_key,
)


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
- Nitrate and sulfate/sulphate salts are not nitric acid or sulfuric acid.
  Do not infer regular acid behavior only from "nitrate", "sulfate",
  "sulphate", "\u785d\u9178\u76d0", "\u786b\u9178\u76d0", "\u785d\u9178X", or "\u786b\u9178X" salt names.
- Ordinary hydrochloric acid / HCl / 盐酸, nitric acid / HNO3 / 硝酸,
  and sulfuric acid / H2SO4 / 硫酸 should be treated as regular acids
  unless the source material clearly gives another higher-priority risk.
""".strip()


@dataclass
class LlmExtractor:
    settings: dict[str, Any] | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        llm_settings = (self.settings or {}).get("llm", {})
        self.provider = os.getenv("LLM_PROVIDER") or llm_settings.get("provider") or "siliconflow"
        provider = get_llm_provider(self.provider)
        self.model = (
            self.model
            or os.getenv("LLM_MODEL")
            or (os.getenv("SILICONFLOW_MODEL") if provider.id == "siliconflow" else "")
            or os.getenv("OPENAI_MODEL")
            or llm_settings.get("model")
            or provider_default_model(provider.id)
            or "deepseek-ai/DeepSeek-V3.2"
        )
        self.base_url = (
            os.getenv("LLM_BASE_URL")
            or (os.getenv("SILICONFLOW_BASE_URL") if provider.id == "siliconflow" else "")
            or llm_settings.get("base_url")
        )
        self.base_url = provider_base_url(provider.id, self.base_url)
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
        self.api_key_env = provider.api_key_env
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
            api_key = resolve_llm_api_key(self.provider)
            self.client = OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                max_retries=self.max_retries,
            )
        return self.client

    def extract_reagent_fields(self, text: str) -> dict[str, Any]:
        return self.extract_properties(raw_text=text)

    def generate_manual_review_advice(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
        """Generate a Chinese, advisory-only second opinion for manual review."""
        rules = reagent_info.get("rule_summary") or []
        allowed_categories = {
            str(item).strip()
            for item in (reagent_info.get("allowed_categories") or [])
            if str(item).strip()
        }
        allowed_categories.update(
            str(item.get("category") or "").strip()
            for item in rules
            if isinstance(item, dict) and str(item.get("category") or "").strip()
        )
        base = {
            "candidate_category": "",
            "physicochemical_summary_cn": "",
            "reason_cn": "",
            "matched_rule_summary_cn": "",
            "uncertainties_cn": [],
            "identity_confidence": 0.0,
            "advisory_confidence": 0.0,
            "evidence_basis": "证据不足",
            "must_manual_review": True,
            "advisory_only": True,
            "used_llm": False,
            "high_risk": False,
            "model": str(self.model or ""),
            "provider": str(self.provider or ""),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "rules_fingerprint": str(reagent_info.get("rules_fingerprint") or ""),
            "raw_diagnostic": "",
        }
        if not self._has_api_key():
            base.update(
                {
                    "reason_cn": "未配置 LLM API Key，无法生成大模型辅助意见，请人工核对物化特性。",
                    "uncertainties_cn": ["缺少大模型辅助意见"],
                    "raw_diagnostic": "LLM API key is not configured.",
                }
            )
            return base

        prompt = f"""
请针对需要人工复核的试剂，结合当前判定规则、已有网页资料和你的通用化学知识，
给出一份仅供人工复核参考的物化特性第二意见。不得决定审批结果，不得声称模型知识是网页证据。

只返回严格 JSON：
{{
  "candidate_category": "候选类别或空字符串",
  "physicochemical_summary_cn": "中文物化特性摘要",
  "reason_cn": "中文判定理由",
  "matched_rule_summary_cn": "中文规则依据",
  "uncertainties_cn": ["中文不确定项"],
  "identity_confidence": 0.0,
  "advisory_confidence": 0.0,
  "evidence_basis": "网页资料|模型知识|混合依据|证据不足"
}}

必须遵守：
- 所有说明、理由和不确定项尽量使用中文，必要的 CAS、化学式、SDS/MSDS 和化学名称可保留原文。
- candidate_category 只能来自 allowed_categories；无法可靠判断时返回空字符串。
- 无可信网页证据时 advisory_confidence 不得超过 0.65。
- 身份不明确、混合物组成不明，或规则依赖但缺少浓度/剂型时，不得给出候选类别。
- 盐酸盐、硝酸盐、硫酸盐、磷酸盐、磺酸盐、羧酸盐等不得仅凭名称判为对应酸类。
- 明确列出不确定项，不得省略人工复核要求。

allowed_categories:
{json.dumps(sorted(allowed_categories), ensure_ascii=False)}

reagent_info:
{json.dumps(reagent_info, ensure_ascii=False)}
""".strip()
        try:
            response = self._client().chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是保守的化学品人工复核助手。你只输出中文 JSON 第二意见，"
                            "不执行 ERP 写入，不替代确定性规则引擎或人工确认。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw_content = response.choices[0].message.content or "{}"
            parsed = json.loads(raw_content)
        except Exception as error:
            base.update(self._manual_advice_failure(error))
            return base

        category = str(parsed.get("candidate_category") or "").strip()
        if category not in allowed_categories:
            category = ""
        evidence_basis = str(parsed.get("evidence_basis") or "证据不足").strip()
        evidence_basis = {
            "web": "网页资料",
            "llm_chemical_knowledge": "模型知识",
            "model_knowledge": "模型知识",
            "mixed": "混合依据",
            "insufficient": "证据不足",
        }.get(evidence_basis.lower(), evidence_basis)
        if evidence_basis not in {"网页资料", "模型知识", "混合依据", "证据不足"}:
            evidence_basis = "证据不足"

        has_trusted_web = bool(reagent_info.get("has_trusted_web_evidence"))
        confidence = self._normalize_confidence(parsed.get("advisory_confidence"))
        if not has_trusted_web:
            confidence = min(confidence, 0.65)
        reason_cn = self._prefer_chinese_text(
            parsed.get("reason_cn"),
            "大模型未能形成可靠的中文判定理由，请人工核对物化特性。",
        )
        summary_cn = self._prefer_chinese_text(
            parsed.get("physicochemical_summary_cn"),
            "现有信息不足，无法形成可靠的物化特性摘要。",
        )
        rule_cn = self._prefer_chinese_text(parsed.get("matched_rule_summary_cn"), "")
        uncertainties = [
            self._prefer_chinese_text(item, "存在未明确的不确定项")
            for item in self._normalize_string_list(parsed.get("uncertainties_cn"))
        ]
        if not uncertainties:
            uncertainties = ["该意见来自大模型辅助判断，仍需人工核验"]
        base.update(
            {
                "candidate_category": category,
                "physicochemical_summary_cn": summary_cn,
                "reason_cn": reason_cn,
                "matched_rule_summary_cn": rule_cn,
                "uncertainties_cn": uncertainties,
                "identity_confidence": self._normalize_confidence(parsed.get("identity_confidence")),
                "advisory_confidence": confidence,
                "evidence_basis": evidence_basis,
                "used_llm": True,
                "high_risk": category in {"高毒类", "易爆类", "发烟类", "特殊酸"},
            }
        )
        return base

    @staticmethod
    def _prefer_chinese_text(value: Any, fallback: str) -> str:
        text = str(value or "").strip()
        if not text:
            return fallback
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
        latin_words = len(re.findall(r"[A-Za-z]{3,}", text))
        if not cjk_count and latin_words >= 3:
            return fallback
        return text

    @staticmethod
    def _manual_advice_failure(error: Exception) -> dict[str, Any]:
        raw = str(error or "").strip()
        lowered = raw.lower()
        if "402" in lowered or "balance" in lowered or "insufficient" in lowered:
            reason = "LLM 账户余额不足，无法生成辅助意见，请人工核对物化特性。"
        elif "api key" in lowered or "unauthorized" in lowered or "401" in lowered:
            reason = "LLM API Key 无效或未配置，无法生成辅助意见，请人工核对物化特性。"
        elif "timeout" in lowered or "timed out" in lowered:
            reason = "LLM 调用超时，未生成辅助意见，请人工核对物化特性。"
        elif isinstance(error, (json.JSONDecodeError, TypeError, ValueError)):
            reason = "LLM 返回格式不符合要求，未采用该辅助意见，请人工核对物化特性。"
        else:
            reason = "LLM 辅助调用失败，未生成可靠意见，请人工核对物化特性。"
        return {
            "reason_cn": reason,
            "uncertainties_cn": ["缺少可靠的大模型辅助意见"],
            "raw_diagnostic": raw[:1000],
        }

    def generate_knowledge_fallback(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper for callers that still expect a raw-text fallback."""
        info = dict(reagent_info)
        info.setdefault("has_trusted_web_evidence", False)
        advice = self.generate_manual_review_advice(info)
        summary = str(advice.get("physicochemical_summary_cn") or "").strip()
        raw_text = f"LLM 知识辅助（无可信网页证据）：{summary}" if advice.get("used_llm") and summary else ""
        return {
            "raw_text": raw_text,
            "reason": advice.get("reason_cn", ""),
            "confidence": min(self._normalize_confidence(advice.get("advisory_confidence")), 0.65),
            "used_llm": bool(advice.get("used_llm") and raw_text),
        }

    def classify_by_rules_fallback(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper for the unified manual-review advice API."""
        advice = self.generate_manual_review_advice(dict(reagent_info))
        basis = str(advice.get("evidence_basis") or "证据不足")
        evidence_type = {
            "网页资料": "name_based",
            "模型知识": "llm_chemical_knowledge",
            "混合依据": "llm_chemical_knowledge",
        }.get(basis, "insufficient")
        return {
            "candidate_category": advice.get("candidate_category", ""),
            "confidence": advice.get("advisory_confidence", 0.0),
            "reason": advice.get("reason_cn", ""),
            "matched_rule_summary": advice.get("matched_rule_summary_cn", ""),
            "evidence_type": evidence_type,
            "must_manual_review": True,
            "used_llm": advice.get("used_llm", False),
        }

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

        mineral_acid = LlmExtractor._ordinary_mineral_acid_label(combined)

        if mineral_acid:
            result["corrosive"] = True
            categories.extend(["常规酸", "腐蚀性"])
            evidence.append(f"Known ordinary mineral acid ({mineral_acid}); classify as regular acid by business rule.")

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
    def _ordinary_mineral_acid_label(text: str) -> str:
        normalized = text.lower()
        compact = re.sub(r"\s+", "", normalized)
        if LlmExtractor._looks_like_acid_salt_text(compact):
            return ""

        acid_terms = (
            ("盐酸", ("hcl", "hydrochloric acid", "7647-01-0", "盐酸")),
            ("硝酸", ("hno3", "nitric acid", "7697-37-2", "硝酸")),
            ("硫酸", ("h2so4", "sulfuric acid", "sulphuric acid", "7664-93-9", "硫酸")),
        )
        for label, terms in acid_terms:
            if any(term in normalized for term in terms):
                return label
        return ""

    @staticmethod
    def _looks_like_acid_salt_text(compact_text: str) -> bool:
        salt_terms = (
            "hydrochloride",
            "nitrate",
            "sulfate",
            "sulphate",
            "sulfonate",
            "sulphonate",
            "carboxylate",
            "phenolate",
            "urate",
            "acidsalt",
            "ammoniumsalt",
            "sodiumsalt",
            "potassiumsalt",
            "盐酸盐",
            "硝酸盐",
            "硫酸盐",
            "磷酸盐",
            "磺酸盐",
            "羧酸盐",
            "钠盐",
            "钾盐",
            "铵盐",
            "酚钠盐",
            "酚钠",
        )
        if any(term in compact_text for term in salt_terms):
            return True
        return bool(
            re.search(r"酸(钠|钾|铵|銨|氨)", compact_text)
            or re.search(r"(盐酸|硝酸|硫酸|磷酸|磺酸|羧酸|酚).{0,12}(钠|钾|铵|銨|氨)", compact_text)
            or re.search(r"(钠|钾|铵|銨|氨).{0,12}(盐酸|硝酸|硫酸|磷酸|磺酸|羧酸|酚)", compact_text)
        )

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
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "是"}

    @staticmethod
    def _extract_cas(text: str) -> str:
        match = re.search(r"\b\d{2,7}-\d{2}-\d\b", text)
        return match.group(0) if match else ""

    def _has_api_key(self) -> bool:
        provider = get_llm_provider(self.provider)
        if not provider.requires_api_key:
            return True
        return bool(resolve_llm_api_key(self.provider))
