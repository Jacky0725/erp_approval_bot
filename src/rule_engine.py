from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


RULE_COLUMNS = ["category", "explanation", "examples"]
CRITICAL_PRIORITY = ["不建议接收类", "拒收类", "剧毒品"]
UNKNOWN_CATEGORY = "未知类"
NORMAL_CATEGORY = "普通类"
FLAMMABLE_CATEGORY = "易燃液体"
IRRITANT_CATEGORY = "刺激性"


@dataclass(frozen=True)
class Rule:
    category: str
    explanation: str
    examples: str
    explanation_keywords: tuple[str, ...]
    example_keywords: tuple[str, ...]
    rule_id: str = ""
    match_type: str = "keyword"
    field_scope: tuple[str, ...] = ()
    condition: str = "any"
    configured_confidence: float = 0.0
    example_match_modes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThresholdRule:
    threshold_id: str
    category: str
    field: str
    operator: str
    value: str
    unit: str = ""
    description: str = ""


@dataclass
class RuleMatch:
    category: str
    explanation_hits: list[str]
    example_hits: list[str]
    score: float
    rule_ids: list[str] = field(default_factory=list)
    configured_confidences: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionTrace:
    rule_version: str
    final_category: str
    matched_categories: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass
class RuleEngine:
    rules: list[Rule]
    priority: list[str]
    manual_review_categories: set[str] = field(default_factory=set)
    rule_version: str = ""
    thresholds: list[ThresholdRule] = field(default_factory=list)
    aliases: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: dict[str, Any], root_dir: Path) -> "RuleEngine":
        paths = settings.get("paths", {})
        structured_path = root_dir / paths.get("structured_rules_excel", "config/rules_structured.xlsx")
        if structured_path.exists():
            return cls.from_structured_excel(structured_path)
        rules_path = root_dir / paths.get("rules_excel", "config/rules.xlsx")
        return cls.from_excel(rules_path)

    @classmethod
    def from_structured_excel(cls, rules_path: str | Path) -> "RuleEngine":
        categories = pd.read_excel(rules_path, sheet_name="categories", engine="openpyxl").fillna("")
        rules_raw = pd.read_excel(rules_path, sheet_name="rules", engine="openpyxl").fillna("")
        try:
            examples_raw = pd.read_excel(rules_path, sheet_name="examples", engine="openpyxl").fillna("")
        except ValueError:
            examples_raw = pd.DataFrame()
        try:
            thresholds_raw = pd.read_excel(rules_path, sheet_name="thresholds", engine="openpyxl").fillna("")
        except ValueError:
            thresholds_raw = pd.DataFrame()
        try:
            aliases_raw = pd.read_excel(rules_path, sheet_name="aliases", engine="openpyxl").fillna("")
        except ValueError:
            aliases_raw = pd.DataFrame()

        categories = cls._enabled_rows(categories)
        rules_raw = cls._enabled_rows(rules_raw)
        examples_raw = cls._enabled_rows(examples_raw)
        thresholds_raw = cls._enabled_rows(thresholds_raw)
        aliases_raw = cls._enabled_rows(aliases_raw)

        priority = [
            str(row["category"]).strip()
            for _, row in categories.sort_values("priority", kind="stable").iterrows()
            if str(row.get("category", "")).strip()
        ]
        manual_review_categories = {
            str(row.get("category", "")).strip()
            for _, row in categories.iterrows()
            if str(row.get("category", "")).strip()
            and str(row.get("default_manual_review", "")).strip().lower()
            in {"true", "1", "yes", "y", "on"}
        }
        manual_review_categories.difference_update(
            {"不建议接收类", "拒收类", UNKNOWN_CATEGORY}
        )

        rule_entries: list[Rule] = []
        for category in priority:
            category_rules = rules_raw[rules_raw["category"].astype(str).str.strip() == category]
            category_examples = (
                examples_raw[examples_raw["category"].astype(str).str.strip() == category]
                if not examples_raw.empty and "category" in examples_raw.columns
                else pd.DataFrame()
            )
            examples = "\n".join(
                str(value).strip()
                for value in category_examples.get("example_name", pd.Series(dtype=str)).tolist()
                if str(value).strip()
            )
            category_example_keywords = tuple(
                str(value).strip()
                for value in category_examples.get("example_name", pd.Series(dtype=str)).tolist()
                if str(value).strip()
            )
            category_example_modes = tuple(
                str(row.get("match_mode", "exact")).strip().lower() or "exact"
                for _, row in category_examples.iterrows()
                if str(row.get("example_name", "")).strip()
            )
            for row_index, (_, row) in enumerate(category_rules.iterrows()):
                pattern = str(row.get("pattern", "")).strip()
                if not pattern:
                    continue
                rule_entries.append(
                    Rule(
                        category=category,
                        explanation=str(row.get("description", "")).strip(),
                        examples=examples if row_index == 0 else "",
                        explanation_keywords=(pattern,),
                        example_keywords=category_example_keywords if row_index == 0 else (),
                        rule_id=str(row.get("rule_id", "")).strip(),
                        match_type=str(row.get("match_type", "keyword")).strip().lower() or "keyword",
                        field_scope=tuple(
                            item.strip() for item in re.split(r"[,，;；|]+", str(row.get("field_scope", ""))) if item.strip()
                        ),
                        condition=str(row.get("condition", "any")).strip() or "any",
                        configured_confidence=cls._float_or_zero(row.get("confidence")),
                        example_match_modes=category_example_modes if row_index == 0 else (),
                    )
                )

        thresholds = [
            ThresholdRule(
                threshold_id=str(row.get("threshold_id", "")).strip(),
                category=str(row.get("category", "")).strip(),
                field=str(row.get("field", "")).strip(),
                operator=str(row.get("operator", "")).strip(),
                value=str(row.get("value", "")).strip(),
                unit=str(row.get("unit", "")).strip(),
                description=str(row.get("description", "")).strip(),
            )
            for _, row in thresholds_raw.iterrows()
            if str(row.get("category", "")).strip() and str(row.get("field", "")).strip()
        ]
        aliases = {
            cls._normalize_text(str(row.get("alias", ""))): {
                "standard_name": str(row.get("standard_name", "")).strip(),
                "cas": str(row.get("cas", "")).strip(),
                "source": str(row.get("source", "")).strip(),
                "confidence": str(row.get("confidence", "")).strip(),
            }
            for _, row in aliases_raw.iterrows()
            if str(row.get("alias", "")).strip()
        }

        for rule in rule_entries:
            if rule.category not in priority:
                priority.append(rule.category)
        rule_version = ""
        try:
            rule_version = f"sha256:{hashlib.sha256(Path(rules_path).read_bytes()).hexdigest()[:16]}"
        except OSError:
            pass
        return cls(
            rules=rule_entries,
            priority=priority,
            manual_review_categories=manual_review_categories,
            rule_version=rule_version,
            thresholds=thresholds,
            aliases=aliases,
        )

    @classmethod
    def from_excel(cls, rules_path: str | Path) -> "RuleEngine":
        raw = pd.read_excel(rules_path, header=1, engine="openpyxl")
        raw = raw.iloc[:, :3].copy()
        raw.columns = RULE_COLUMNS

        remarks = cls._extract_remarks(raw)
        raw["category"] = raw["category"].ffill()
        raw = raw[~raw["category"].astype(str).str.startswith("备注", na=False)]

        grouped = raw.groupby("category", sort=False, dropna=True).agg(
            {
                "explanation": lambda values: "\n".join(
                    cls._clean_text(value) for value in values if cls._clean_text(value)
                ),
                "examples": lambda values: "\n".join(
                    cls._clean_text(value) for value in values if cls._clean_text(value)
                ),
            }
        )

        rules = [
            Rule(
                category=str(category).strip(),
                explanation=row["explanation"],
                examples=row["examples"],
                explanation_keywords=tuple(cls._keywords_from_text(row["explanation"])),
                example_keywords=tuple(cls._keywords_from_text(row["examples"])),
            )
            for category, row in grouped.iterrows()
        ]
        priority = cls._priority_from_remarks(remarks, [rule.category for rule in rules])
        rule_version = ""
        try:
            rule_version = f"sha256:{hashlib.sha256(Path(rules_path).read_bytes()).hexdigest()[:16]}"
        except OSError:
            pass
        return cls(rules=rules, priority=priority, rule_version=rule_version)

    def classify_resolved_properties(
        self,
        properties: Any,
        *,
        name: str = "",
        cas: str = "",
        identity_status: str = "unresolved",
    ) -> dict[str, Any]:
        """Classify field-level evidence without changing the legacy dict API."""

        reagent_info = properties.to_rule_input(name=name, cas=cas)
        result = dict(self.classify(reagent_info))
        warnings: list[str] = []
        if identity_status != "verified":
            warnings.append(f"identity_status:{identity_status or 'unresolved'}")
        conflicts = tuple(getattr(properties, "conflicts", ()) or ())
        if conflicts:
            warnings.extend(f"evidence_conflict:{field}" for field in conflicts)
        if warnings:
            result["need_manual_review"] = True
        evidence_refs = tuple(
            f"evidence:{index}"
            for field in getattr(properties, "fields", {}).values()
            for index in getattr(field, "evidence_refs", ())
        )
        trace = DecisionTrace(
            rule_version=self.rule_version or "unversioned",
            final_category=str(result.get("final_category") or ""),
            matched_categories=tuple(str(value) for value in result.get("matched_categories", []) or []),
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            warnings=tuple(warnings),
        )
        result["decision_trace"] = {
            "rule_version": trace.rule_version,
            "final_category": trace.final_category,
            "matched_categories": list(trace.matched_categories),
            "evidence_refs": list(trace.evidence_refs),
            "warnings": list(trace.warnings),
        }
        return result

    @staticmethod
    def _enabled_rows(dataframe: pd.DataFrame) -> pd.DataFrame:
        if dataframe.empty or "enabled" not in dataframe.columns:
            return dataframe
        enabled = dataframe["enabled"].astype(str).str.strip().str.lower().isin(
            {"true", "1", "yes", "y", "on"}
        )
        return dataframe[enabled]

    @staticmethod
    def _structured_condition_requires_special_handling(condition: str) -> bool:
        normalized = str(condition).strip().lower()
        return bool(normalized and normalized != "any" and "concentration" in normalized)

    def classify(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
        reagent_info = self._apply_structured_alias(reagent_info)
        text = self._reagent_text(reagent_info)
        raw_text = self._raw_reagent_text(reagent_info)
        if not text:
            return self._manual_result("无法判断：试剂信息为空。")

        if self._contains_mercury_name(reagent_info):
            return {
                "final_category": "不建议接收类",
                "matched_categories": ["不建议接收类"],
                "reason": "试剂名称含“汞”，按业务规则判定为拒收类。",
                "confidence": 0.95,
                "need_manual_review": False,
            }

        matches: dict[str, RuleMatch] = {}
        mixture_categories = self._mixture_categories(reagent_info)
        for category in mixture_categories:
            matches[category] = RuleMatch(
                category=category,
                explanation_hits=[f"混合物/SDS组分最高风险候选：{category}"],
                example_hits=[],
                score=10.0,
            )
        suppress_special_acid_rule = (
            self._is_mineral_acid_salt_like(reagent_info)
            or self._is_ordinary_mineral_acid(reagent_info)
        )
        for rule in self.rules:
            if rule.category == "\u7279\u6b8a\u9178" and suppress_special_acid_rule:
                continue

            structured_rule_matched = False
            if rule.rule_id:
                explanation_hits = self._structured_rule_hits(rule, reagent_info)
                structured_rule_matched = bool(explanation_hits)
            elif rule.category == "\u6eb4\u7898\u7c7b":
                explanation_hits = []
            else:
                explanation_hits = self._hits(rule.explanation_keywords, text)
            if rule.category == NORMAL_CATEGORY:
                explanation_hits = [
                    hit for hit in explanation_hits if not self._is_non_decision_normal_hint(hit)
                ]
            if rule.example_match_modes:
                example_hits = self._structured_example_hits(
                    rule.example_keywords,
                    rule.example_match_modes,
                    reagent_info,
                    flammable=rule.category == FLAMMABLE_CATEGORY,
                )
            elif rule.category == FLAMMABLE_CATEGORY:
                example_hits = self._flammable_example_hits(rule.example_keywords, reagent_info)
            else:
                example_hits = self._specific_example_hits(rule.example_keywords, reagent_info)
            category_hits = self._category_suggestion_hits(rule.category, reagent_info)
            halogen_hits = self._bromine_iodine_hits(rule.category, reagent_info)
            conditional_hits = self._conditional_rule_hits(rule.category, reagent_info)
            explanation_hits = list(
                dict.fromkeys([*explanation_hits, *category_hits, *halogen_hits, *conditional_hits])
            )

            # Keep separators between the endpoint label and its value. The
            # normalized text turns "LD50 4 mg/kg" into "ld504mg/kg".
            toxic_hits = self._toxic_threshold_hits(rule.category, raw_text)
            if toxic_hits is not None:
                protected_toxic_name_hits = (
                    RuleEngine._high_toxic_name_hits(reagent_info) if rule.category == "\u9ad8\u6bd2\u7c7b" else []
                )
                explanation_hits = list(dict.fromkeys([*toxic_hits, *protected_toxic_name_hits]))
                if not explanation_hits:
                    if rule.category == FLAMMABLE_CATEGORY:
                        example_hits = self._flammable_example_hits(rule.example_keywords, reagent_info)
                    else:
                        example_hits = self._specific_example_hits(rule.example_keywords, reagent_info)

            if not explanation_hits:
                example_hits = self._exact_example_hits(rule.example_keywords, reagent_info)

            score = len(explanation_hits) * 2.0 + len(example_hits) * 0.8
            if score > 0:
                existing = matches.get(rule.category)
                if existing is None:
                    matches[rule.category] = RuleMatch(
                        category=rule.category,
                        explanation_hits=list(explanation_hits),
                        example_hits=list(example_hits),
                        score=score,
                        rule_ids=[rule.rule_id] if rule.rule_id and structured_rule_matched else [],
                        configured_confidences=[rule.configured_confidence]
                        if rule.configured_confidence and structured_rule_matched else [],
                    )
                else:
                    existing.explanation_hits = list(dict.fromkeys([*existing.explanation_hits, *explanation_hits]))
                    existing.example_hits = list(dict.fromkeys([*existing.example_hits, *example_hits]))
                    existing.score += score
                    if rule.rule_id and structured_rule_matched and rule.rule_id not in existing.rule_ids:
                        existing.rule_ids.append(rule.rule_id)
                    if rule.configured_confidence and structured_rule_matched:
                        existing.configured_confidences.append(rule.configured_confidence)

        for threshold in self.thresholds:
            hit = self._threshold_rule_hit(threshold, reagent_info)
            if not hit:
                continue
            existing = matches.get(threshold.category)
            if existing is None:
                matches[threshold.category] = RuleMatch(
                    category=threshold.category,
                    explanation_hits=[hit],
                    example_hits=[],
                    score=2.0,
                    rule_ids=[threshold.threshold_id] if threshold.threshold_id else [],
                )
            else:
                if hit not in existing.explanation_hits:
                    existing.explanation_hits.append(hit)
                    existing.score += 2.0
                if threshold.threshold_id and threshold.threshold_id not in existing.rule_ids:
                    existing.rule_ids.append(threshold.threshold_id)

        if self._looks_unknown(text):
            matches.setdefault(
                UNKNOWN_CATEGORY,
                RuleMatch(
                    category=UNKNOWN_CATEGORY,
                    explanation_hits=["未知/无标签/MSDS"],
                    example_hits=[],
                    score=10.0,
                ),
            )

        irritation_exclusion = self._irritation_exclusion_reason(reagent_info)
        if irritation_exclusion:
            return self._manual_result(
                f"无法按刺激性自动判定：{irritation_exclusion}；该危害类别不能等同于皮肤/眼刺激，需人工确认对应业务类别。"
            )

        if not matches and self._is_business_normal_name(reagent_info) and self._ordinary_auto_allowed(reagent_info):
            return {
                "final_category": NORMAL_CATEGORY,
                "matched_categories": [NORMAL_CATEGORY],
                "reason": "未命中危险规则，且试剂名称命中普通类业务关键词或药物/API类名称规则，按普通类处理。",
                "confidence": 0.95,
                "need_manual_review": False,
            }

        if not matches and self._is_low_priority_business_normal_name(reagent_info) and self._ordinary_auto_allowed(reagent_info):
            return {
                "final_category": NORMAL_CATEGORY,
                "matched_categories": [NORMAL_CATEGORY],
                "reason": "试剂名称命中低优先级普通类业务关键词（纳米/单体/担体/助滤/脱色/模拟/催化/人工），且未命中其它风险类别，按普通类处理。",
                "confidence": 0.9,
                "need_manual_review": False,
            }

        flammable_issue = self.flammable_evidence_issue(reagent_info)
        if flammable_issue and not matches:
            return self._manual_result(f"无法自动判定易燃类：{flammable_issue}")

        if not matches:
            if reagent_info.get("allow_default_normal") and self._ordinary_auto_allowed(reagent_info):
                return {
                    "final_category": NORMAL_CATEGORY,
                    "matched_categories": [NORMAL_CATEGORY],
                    "reason": "未命中其它风险类别；按规则将不属于其它类别的试剂判定为普通类。",
                    "confidence": 0.8,
                    "need_manual_review": False,
                }
            return self._manual_result("无法判断：未命中 rules.xlsx 解释列中的物化特性依据。")

        matched_categories = self._sort_matched_categories(matches)
        final_category = matched_categories[0]
        confidence = self._confidence(matches[final_category])
        inhalation_issue = self.inhalation_evidence_issue(reagent_info)
        need_manual_review = final_category in self.manual_review_categories or confidence < 0.55 or bool(inhalation_issue)
        reason = self._reason(final_category, matched_categories, matches)
        if inhalation_issue:
            reason = f"{reason}；吸入毒性证据不完整：{inhalation_issue}"

        return {
            "final_category": final_category,
            "matched_categories": matched_categories,
            "reason": reason,
            "confidence": confidence,
            "need_manual_review": need_manual_review,
            "matched_rule_ids": list(matches[final_category].rule_ids),
            "rule_version": self.rule_version,
        }

    @staticmethod
    def _ordinary_auto_allowed(reagent_info: dict[str, Any]) -> bool:
        if "ordinary_evidence_complete" in reagent_info:
            return bool(reagent_info.get("ordinary_evidence_complete"))
        return True

    def highest_priority_category(self, categories: list[str]) -> str:
        """Return the strictest category using the configured rule priority."""
        normalized = [str(value or "").strip() for value in categories if str(value or "").strip()]
        rank = {category: index for index, category in enumerate(self.priority)}
        known = [category for category in dict.fromkeys(normalized) if category in rank]
        return min(known, key=lambda category: rank[category]) if known else ""

    def _mixture_categories(self, reagent_info: dict[str, Any]) -> list[str]:
        categories: list[str] = []
        for key in ("mixture_risk_categories", "sds_categories", "component_categories"):
            value = reagent_info.get(key) or []
            if isinstance(value, str):
                value = re.split(r"[,，;；|]+", value)
            for category in value if isinstance(value, (list, tuple, set)) else []:
                text = str(category or "").strip()
                if text and text in self.priority and text not in categories:
                    categories.append(text)
        return categories

    def evaluate_text(self, item_text: str) -> dict[str, Any]:
        result = self.classify({"text": item_text})
        return {
            "action": "manual_review" if result["need_manual_review"] else "classify",
            "reason": result["reason"],
            "matched_keyword": ", ".join(result["matched_categories"]),
            "classification": result,
        }

    def _sort_matched_categories(self, matches: dict[str, RuleMatch]) -> list[str]:
        rank = {category: index for index, category in enumerate(self.priority)}
        has_reject_match = any(category in {"不建议接收类", "拒收类"} for category in matches)
        return sorted(
            matches,
            key=lambda category: (
                0
                if not has_reject_match and RuleEngine._is_azide_explosive_match(category, matches[category])
                else 1,
                rank.get(category, len(rank) + 100),
                -matches[category].score,
            ),
        )

    def _confidence(self, match: RuleMatch) -> float:
        if match.configured_confidences:
            return max(0.0, min(1.0, max(match.configured_confidences)))
        if match.category in CRITICAL_PRIORITY:
            return min(0.95, 0.78 + len(match.explanation_hits) * 0.06 + len(match.example_hits) * 0.03)
        if match.explanation_hits:
            return min(0.9, 0.68 + len(match.explanation_hits) * 0.08 + len(match.example_hits) * 0.03)
        return min(0.72, 0.55 + len(match.example_hits) * 0.08)

    @staticmethod
    def _reason(final_category: str, matched_categories: list[str], matches: dict[str, RuleMatch]) -> str:
        parts = []
        for category in matched_categories:
            match = matches[category]
            hit_parts = []
            if match.explanation_hits:
                hit_parts.append(f"解释列命中: {', '.join(match.explanation_hits[:5])}")
            if match.example_hits:
                hit_parts.append(f"举例列辅助命中: {', '.join(match.example_hits[:5])}")
            if match.rule_ids:
                hit_parts.append(f"规则ID: {', '.join(match.rule_ids[:5])}")
            parts.append(f"{category}({'; '.join(hit_parts)})")
        return f"以 rules.xlsx 的解释列为主要依据判定为 {final_category}。命中依据：{' | '.join(parts)}"

    @staticmethod
    def _float_or_zero(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _manual_result(reason: str) -> dict[str, Any]:
        return {
            "final_category": "",
            "matched_categories": [],
            "reason": reason,
            "confidence": 0.0,
            "need_manual_review": True,
        }

    def _apply_structured_alias(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
        if not self.aliases:
            return reagent_info
        enriched = dict(reagent_info)
        for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name"):
            normalized = self._normalize_text(str(reagent_info.get(key) or ""))
            alias = self.aliases.get(normalized)
            if not alias:
                continue
            if alias.get("standard_name"):
                enriched["standard_name"] = alias["standard_name"]
            if alias.get("cas") and not enriched.get("cas"):
                enriched["cas"] = alias["cas"]
            enriched["matched_alias"] = str(reagent_info.get(key) or "")
            break
        return enriched

    @staticmethod
    def _reagent_text(reagent_info: dict[str, Any]) -> str:
        values = []
        keys = (
            "name",
            "reagent_name",
            "chemical_name",
            "cas",
            "cas_no",
            "spec",
            "remark",
            "text",
            "flash_point",
            "boiling_point",
            "toxicity",
            "evidence",
        )
        for key in keys:
            value = reagent_info.get(key)
            if isinstance(value, list):
                values.extend(str(item) for item in value)
            elif value is not None:
                values.append(str(value))

        for field, words in {
            "corrosive": ["腐蚀性", "腐蚀"],
            "oxidizing": ["氧化性", "氧化剂"],
            "flammable": [],
            "water_reactive": ["与水反应", "遇湿易燃"],
            "explosive_risk": ["爆炸", "易爆"],
            "heavy_metal": ["重金属"],
        }.items():
            if reagent_info.get(field) is True:
                values.extend(words)

        if not values:
            values = [str(value) for value in reagent_info.values() if value is not None]
        return RuleEngine._normalize_text(" ".join(values))

    @staticmethod
    def _hits(keywords: tuple[str, ...], text: str) -> list[str]:
        hits = []
        for keyword in keywords:
            normalized = RuleEngine._normalize_text(keyword)
            if (
                RuleEngine._keyword_matches_normalized_text(normalized, text)
                and not RuleEngine._keyword_is_negated(normalized, text)
                and keyword not in hits
            ):
                hits.append(keyword)
        return hits

    @staticmethod
    def _structured_rule_hits(rule: Rule, reagent_info: dict[str, Any]) -> list[str]:
        if (
            rule.category == "\u91cd\u91d1\u5c5e\u7c7b"
            and any(scope.strip().lower() == "heavy_metal" for scope in rule.field_scope)
            and not RuleEngine._configured_heavy_metal_element_hits(reagent_info)
        ):
            return []

        scoped_text = RuleEngine._scoped_text(reagent_info, rule.field_scope)
        hits: list[str] = []
        for pattern in rule.explanation_keywords:
            normalized = RuleEngine._normalize_text(pattern)
            match_type = rule.match_type or "keyword"
            if match_type in {"exact", "equals"}:
                matched = normalized == scoped_text
            elif match_type == "regex":
                try:
                    raw_scoped_text = RuleEngine._raw_scoped_text(reagent_info, rule.field_scope)
                    regex_matches = list(re.finditer(pattern, raw_scoped_text, flags=re.I))
                    if rule.category == IRRITANT_CATEGORY:
                        normalized_raw_scoped_text = RuleEngine._normalize_text(raw_scoped_text)
                        matched = any(
                            not RuleEngine._irritation_statement_is_negated(
                                RuleEngine._normalize_text(match.group(0)),
                                normalized_raw_scoped_text,
                            )
                            for match in regex_matches
                        )
                    else:
                        matched = bool(regex_matches)
                except re.error:
                    matched = False
            else:
                matched = RuleEngine._keyword_matches_normalized_text(normalized, scoped_text)
            if (
                not matched
                or RuleEngine._keyword_is_negated(normalized, scoped_text)
                or (
                    rule.category == IRRITANT_CATEGORY
                    and RuleEngine._irritation_statement_is_negated(normalized, scoped_text)
                )
            ):
                continue
            condition = RuleEngine._normalize_text(rule.condition or "any")
            if condition and condition != "any" and "concentration" in condition:
                concentration = RuleEngine._first_percent_concentration(RuleEngine._raw_reagent_text(reagent_info))
                if "missing" in condition and concentration is None:
                    pass
                elif concentration is None:
                    continue
                elif "<=" in condition:
                    if not concentration <= RuleEngine._condition_number(condition, 72.0):
                        continue
                elif ">=" in condition:
                    if not concentration >= RuleEngine._condition_number(condition, 72.0):
                        continue
                elif "<" in condition:
                    if not concentration < RuleEngine._condition_number(condition, 72.0):
                        continue
                elif ">" in condition:
                    if not concentration > RuleEngine._condition_number(condition, 72.0):
                        continue
            elif condition in {"requiresliquidcontext", "requires_liquid_context"}:
                if not RuleEngine._has_liquid_context(reagent_info) or RuleEngine._has_flammable_blocking_context(reagent_info):
                    continue
            elif condition not in {"", "any"}:
                # Toxicity and flash-point conditions are evaluated from the thresholds sheet
                # and the route-aware helpers below; their descriptive pattern is not evidence.
                continue
            hits.append(f"{rule.rule_id}:{pattern}" if rule.rule_id else pattern)
        return hits

    @staticmethod
    def _scoped_text(reagent_info: dict[str, Any], scopes: tuple[str, ...]) -> str:
        return RuleEngine._normalize_text(RuleEngine._raw_scoped_text(reagent_info, scopes))

    @staticmethod
    def _raw_scoped_text(reagent_info: dict[str, Any], scopes: tuple[str, ...]) -> str:
        if not scopes:
            return RuleEngine._reagent_text(reagent_info)
        values: list[str] = []
        mapping = {
            "name": ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name", "english_name"),
            "text": ("text", "remark", "toxicity", "flash_point", "boiling_point"),
            "evidence": ("evidence",),
        }
        for scope in scopes:
            for key in mapping.get(scope.strip().lower(), (scope.strip(),)):
                value = reagent_info.get(key)
                if isinstance(value, (list, tuple, set)):
                    values.extend(str(item) for item in value)
                elif value is not None:
                    values.append(str(value))
        return " ".join(values)

    @staticmethod
    def _keyword_is_negated(keyword: str, text: str) -> bool:
        if not keyword or not text:
            return False
        escaped = re.escape(keyword)
        chinese = rf"(?:无|不含|未见|没有|非)(?:明显)?{escaped}"
        english = rf"(?:no|not|non|without)(?:\s|-)*{escaped}"
        return bool(re.search(chinese, text, flags=re.I) or re.search(english, text, flags=re.I))

    @staticmethod
    def _condition_number(condition: str, fallback: float) -> float:
        match = re.search(r"-?\d+(?:\.\d+)?", condition)
        return float(match.group(0)) if match else fallback

    @staticmethod
    def _threshold_rule_hit(threshold: ThresholdRule, reagent_info: dict[str, Any]) -> str:
        values: list[float] = []
        field = threshold.field.strip().lower()
        if field == "flash_point":
            values = [value for value, _ in RuleEngine._flash_points_celsius(reagent_info)]
            if values and not RuleEngine._has_auto_flammable_context(reagent_info):
                return ""
        elif field in {"oral_ld50", "dermal_ld50"}:
            text = RuleEngine._raw_reagent_text(reagent_info)
            for value, unit, context in RuleEngine._toxicity_values(text):
                converted = RuleEngine._to_mg_per_kg(value, unit)
                route_words = ("经口", "口服", "oral") if field == "oral_ld50" else ("经皮", "皮肤", "dermal", "skin")
                if converted is not None and any(word in context for word in route_words):
                    values.append(converted)
        elif field.startswith("inhalation_lc50_"):
            text = RuleEngine._raw_reagent_text(reagent_info)
            phase_words = {
                "inhalation_lc50_gas": ("气体", "gas"),
                "inhalation_lc50_vapor": ("蒸气", "蒸汽", "vapor", "vapour"),
                "inhalation_lc50_dust_mist": ("粉尘", "烟雾", "dust", "mist"),
            }[field]
            expected_unit = threshold.unit.strip().lower()
            for value, unit, context in RuleEngine._inhalation_toxicity_values(text):
                if unit == expected_unit and any(word in context for word in phase_words):
                    values.append(value)
        else:
            raw = reagent_info.get(field)
            if isinstance(raw, (int, float)):
                values = [float(raw)]
            elif raw:
                match = re.search(r"-?\d+(?:\.\d+)?", str(raw))
                if match:
                    values = [float(match.group(0))]
        if not values:
            return ""
        target = RuleEngine._condition_number(threshold.value, 0.0)
        operator = threshold.operator.strip().lower()
        def matches(value: float) -> bool:
            if operator == "<": return value < target
            if operator == "<=": return value <= target
            if operator == ">": return value > target
            if operator == ">=": return value >= target
            if operator in {"=", "=="}: return value == target
            if operator == "between":
                numbers = [float(item) for item in re.findall(r"-?\d+(?:\.\d+)?", threshold.value)]
                if len(numbers) < 2:
                    return False
                expression = re.sub(r"\s+", "", threshold.value).lower()
                lower_ok = value >= numbers[0] if re.match(r"^-?\d+(?:\.\d+)?<=", expression) else value > numbers[0]
                upper_ok = value <= numbers[1] if "x<=" in expression else value < numbers[1]
                return lower_ok and upper_ok
            return False
        matched_value = next((value for value in values if matches(value)), None)
        if matched_value is None:
            return ""
        return f"{threshold.threshold_id}:{field}={matched_value:g}{threshold.unit} {threshold.operator} {threshold.value}"

    @staticmethod
    def _inhalation_toxicity_values(text: str) -> list[tuple[float, str, str]]:
        values: list[tuple[float, str, str]] = []
        normalized = str(text or "").replace("μ", "u").replace("µ", "u")
        pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(ppm|mg\s*/\s*l)", flags=re.I)
        for match in pattern.finditer(normalized):
            start = max(0, match.start() - 80)
            end = min(len(normalized), match.end() + 80)
            context = normalized[start:end].lower()
            if not any(marker in context for marker in ("lc50", "吸入", "inhalation")):
                continue
            unit = re.sub(r"\s+", "", match.group(2).lower())
            values.append((float(match.group(1)), unit, context))
        return values

    @staticmethod
    def inhalation_evidence_issue(reagent_info: dict[str, Any]) -> str:
        normalized = RuleEngine._raw_reagent_text(reagent_info).lower().replace("μ", "u").replace("µ", "u")
        if not re.search(r"(?:lc\s*50|吸入).{0,80}\d+(?:\.\d+)?\s*(?:ppm|mg\s*/\s*l)", normalized, flags=re.I):
            return ""
        has_phase = any(word in normalized for word in ("气体", "蒸气", "蒸汽", "粉尘", "烟雾", "gas", "vapor", "vapour", "dust", "mist"))
        has_duration = bool(re.search(r"\b\d+(?:\.\d+)?\s*(?:h|hr|hrs|hour|hours)\b|\d+(?:\.\d+)?\s*小时", normalized, flags=re.I))
        missing = []
        if not has_phase:
            missing.append("缺少气体/蒸气/粉尘雾滴相态")
        if not has_duration:
            missing.append("缺少暴露时长")
        return "、".join(missing)

    @staticmethod
    def _keyword_matches_normalized_text(normalized_keyword: str, normalized_text: str) -> bool:
        if not normalized_keyword:
            return False
        if normalized_keyword in normalized_text:
            return True
        if normalized_keyword.endswith("类") and len(normalized_keyword) >= 3:
            base_keyword = normalized_keyword[:-1]
            return bool(base_keyword and base_keyword in normalized_text)
        return False

    @staticmethod
    def _category_suggestion_hits(category: str, reagent_info: dict[str, Any]) -> list[str]:
        if category == IRRITANT_CATEGORY:
            # An LLM/category hint alone is not regulatory evidence. Irritancy
            # must match an explicit SDS/GHS statement configured in the
            # structured workbook (for example H315 or H319).
            return []
        if category == "\u5e38\u89c4\u9178" and RuleEngine._is_mineral_acid_salt_like(reagent_info):
            return []
        if category == "\u7279\u6b8a\u9178" and (
            RuleEngine._is_mineral_acid_salt_like(reagent_info)
            or RuleEngine._is_ordinary_mineral_acid(reagent_info)
        ):
            return []
        if category == "\u6eb4\u7898\u7c7b":
            return []
        if category == FLAMMABLE_CATEGORY:
            return []

        suggested = reagent_info.get("suggested_categories", [])
        if isinstance(suggested, str):
            suggested_values = re.split(r"[,，;；\n]+", suggested)
        elif isinstance(suggested, list):
            suggested_values = suggested
        else:
            suggested_values = []

        normalized_category = RuleEngine._normalize_text(category)
        hits = []
        for value in suggested_values:
            normalized_value = RuleEngine._normalize_text(str(value))
            if normalized_value and (
                normalized_value == normalized_category
                or normalized_value in normalized_category
                or normalized_category in normalized_value
            ):
                hits.append(f"候选类别:{category}")
        return list(dict.fromkeys(hits))

    @staticmethod
    def _specific_example_hits(keywords: tuple[str, ...], reagent_info: dict[str, Any]) -> list[str]:
        name_values = []
        for key in ("name", "reagent_name", "chemical_name"):
            value = reagent_info.get(key)
            if value:
                name_values.append(str(value))
        name_text = RuleEngine._normalize_text(" ".join(name_values))
        hits = []
        for keyword in keywords:
            normalized = RuleEngine._normalize_text(keyword)
            if not normalized:
                continue
            exact_name_hit = normalized == name_text
            long_name_hit = len(normalized) >= 3 and normalized in name_text
            if (exact_name_hit or long_name_hit) and keyword not in hits:
                hits.append(keyword)
        return hits

    @staticmethod
    def _structured_example_hits(
        keywords: tuple[str, ...],
        modes: tuple[str, ...],
        reagent_info: dict[str, Any],
        *,
        flammable: bool = False,
    ) -> list[str]:
        name_values = [
            str(reagent_info.get(key) or "")
            for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name")
            if str(reagent_info.get(key) or "").strip()
        ]
        normalized_names = [RuleEngine._normalize_text(value) for value in name_values]
        joined_names = " ".join(normalized_names)
        hits: list[str] = []
        for index, keyword in enumerate(keywords):
            mode = modes[index] if index < len(modes) else "exact"
            normalized = RuleEngine._normalize_text(keyword)
            if not normalized or RuleEngine._keyword_is_negated(normalized, joined_names):
                continue
            if flammable:
                matched = RuleEngine._is_flammable_example_name(keyword, name_values)
            elif mode in {"contains", "keyword"}:
                matched = any(normalized in value for value in normalized_names)
            elif mode == "regex":
                try:
                    matched = any(re.search(keyword, value, flags=re.I) for value in name_values)
                except re.error:
                    matched = False
            else:
                matched = any(normalized == value for value in normalized_names)
            if matched and keyword not in hits:
                hits.append(keyword)
        return hits

    @staticmethod
    def _flammable_example_hits(keywords: tuple[str, ...], reagent_info: dict[str, Any]) -> list[str]:
        name_values = []
        for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name"):
            value = reagent_info.get(key)
            if value:
                name_values.append(str(value))
        hits = []
        for keyword in keywords:
            if RuleEngine._is_flammable_example_name(keyword, name_values) and keyword not in hits:
                hits.append(keyword)
        return hits

    @staticmethod
    def _is_non_decision_normal_hint(hit: str) -> bool:
        normalized = RuleEngine._normalize_text(hit)
        non_decision_fragments = (
            "含氟氯溴碘类",
            "卤代烃及衍生物除外",
            "价格翻倍",
        )
        return any(fragment in normalized for fragment in non_decision_fragments)

    @staticmethod
    def _bromine_iodine_hits(category: str, reagent_info: dict[str, Any]) -> list[str]:
        if category != "\u6eb4\u7898\u7c7b":
            return []

        parts = []
        for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name", "english_name"):
            value = reagent_info.get(key)
            if value:
                parts.append(str(value))
        text = " ".join(parts).lower()

        hits = []
        if "\u6eb4" in text:
            hits.append("\u542b\u6eb4")
        if "\u7898" in text:
            hits.append("\u542b\u7898")
        for token, label in (
            ("bromo", "bromo"),
            ("bromide", "bromide"),
            ("bromine", "bromine"),
            ("iodo", "iodo"),
            ("iodide", "iodide"),
            ("iodine", "iodine"),
        ):
            if token in text:
                hits.append(label)
        return list(dict.fromkeys(hits))

    @staticmethod
    def _is_azide_explosive_match(category: str, match: RuleMatch) -> bool:
        if category != "\u6613\u7206\u7c7b":
            return False
        return any(
            token in str(hit).lower()
            for hit in match.explanation_hits
            for token in ("\u53e0\u6c2e", "\u53e0\u5316", "azide")
        )

    @staticmethod
    def _conditional_rule_hits(category: str, reagent_info: dict[str, Any]) -> list[str]:
        text = RuleEngine._raw_reagent_text(reagent_info).lower()
        hits: list[str] = []

        if category == "不建议接收类":
            hits.extend(RuleEngine._reject_metal_name_hits(reagent_info))

        if category == "\u6c27\u5316\u5242":
            hits.extend(RuleEngine._oxidizer_name_hits(reagent_info))

        if category == "\u91cd\u91d1\u5c5e\u7c7b":
            hits.extend(RuleEngine._heavy_metal_name_hits(reagent_info))

        if category == "\u9ad8\u6bd2\u7c7b":
            hits.extend(RuleEngine._high_toxic_name_hits(reagent_info))

        if category == "\u5f02\u5473":
            hits.extend(RuleEngine._odor_name_hits(reagent_info))

        if category == "\u7279\u6b8a\u9178" and (
            RuleEngine._is_mineral_acid_salt_like(reagent_info)
            or RuleEngine._is_ordinary_mineral_acid(reagent_info)
        ):
            return []

        if category == "\u5e38\u89c4\u9178" and RuleEngine._is_mineral_acid_salt_like(reagent_info):
            return []

        if category == "\u5e38\u89c4\u9178" and RuleEngine._is_ordinary_mineral_acid(reagent_info):
            hits.append("\u76d0\u9178/\u785d\u9178/\u786b\u9178/HCl/HNO3/H2SO4 \u6309\u5e38\u89c4\u9178")

        if category == "\u6613\u7206\u7c7b" and RuleEngine._contains_azide(text):
            hits.append("\u53e0\u6c2e/\u53e0\u5316/azide")

        if RuleEngine._contains_perchloric_acid(text):
            concentration = RuleEngine._first_percent_concentration(text)
            if category == "\u6613\u7206\u7c7b" and (concentration is None or concentration > 72.0):
                hits.append("\u9ad8\u6c2f\u9178>72%\u6216\u672a\u6807\u6ce8\u6d53\u5ea6")
            if category == "\u7279\u6b8a\u9178" and concentration is not None and concentration <= 72.0:
                hits.append("\u9ad8\u6c2f\u9178<=72%")

        if category == FLAMMABLE_CATEGORY:
            if RuleEngine._has_common_flammable_liquid_example(reagent_info):
                hits.append("易燃液体常见举例")
            hits.extend(RuleEngine._flash_point_flammable_hits(reagent_info))

        return hits

    @staticmethod
    def _reject_metal_name_hits(reagent_info: dict[str, Any]) -> list[str]:
        parts = []
        for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name"):
            value = reagent_info.get(key)
            if value:
                parts.append(str(value))
        name_text = "".join(parts)
        hits = []
        for token in ("铅", "汞", "铊", "铍"):
            if token in name_text and not RuleEngine._keyword_is_negated(token, name_text):
                hits.append(f"含{token}")
        return list(dict.fromkeys(hits))

    @staticmethod
    def _contains_mercury_name(reagent_info: dict[str, Any]) -> bool:
        parts = []
        for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name", "english_name"):
            value = reagent_info.get(key)
            if value:
                parts.append(str(value))
        name_text = "".join(parts).lower()
        return any(
            token in name_text and not RuleEngine._keyword_is_negated(token, name_text)
            for token in ("\u6c5e", "mercury", "mercuric", "mercurous")
        )

    @staticmethod
    def _heavy_metal_name_hits(reagent_info: dict[str, Any]) -> list[str]:
        if RuleEngine._is_known_arsenic_reagent_alias(reagent_info):
            return []

        return RuleEngine._configured_heavy_metal_element_hits(reagent_info)

    @staticmethod
    def _configured_heavy_metal_element_hits(reagent_info: dict[str, Any]) -> list[str]:
        parts = []
        for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name", "english_name"):
            value = reagent_info.get(key)
            if value:
                parts.append(str(value))
        name_text = "".join(parts)
        if "常规无机盐" in name_text:
            return []
        hits = []
        allowed_chinese_tokens = ("汞", "镉", "铬", "砷", "铅", "镍", "铍", "银", "钒", "硒", "钴", "锡")
        for token in allowed_chinese_tokens:
            if token in name_text and not RuleEngine._keyword_is_negated(token, name_text):
                hits.append(f"含{token}")

        english_text = name_text.lower()
        for token, label in (
            ("mercury", "mercury"),
            ("mercuric", "mercuric"),
            ("mercurous", "mercurous"),
            ("cadmium", "cadmium"),
            ("chromium", "chromium"),
            ("arsenic", "arsenic"),
            ("lead", "lead"),
            ("nickel", "nickel"),
            ("beryllium", "beryllium"),
            ("silver", "silver"),
            ("vanadium", "vanadium"),
            ("selenium", "selenium"),
            ("cobalt", "cobalt"),
            ("tin", "tin"),
            ("stannous", "stannous"),
            ("stannic", "stannic"),
        ):
            if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", english_text):
                hits.append(label)
        return list(dict.fromkeys(hits))

    @staticmethod
    def _irritation_exclusion_reason(reagent_info: dict[str, Any]) -> str:
        """Return hazards that must not be collapsed into the irritant class."""

        raw = RuleEngine._raw_reagent_text(reagent_info).lower()
        exclusions = (
            (
                "H314/皮肤腐蚀",
                (
                    r"\bh\s*314\b",
                    r"causes severe skin burns",
                    r"造成严重皮肤灼伤",
                    r"皮肤腐蚀",
                ),
            ),
            (
                "H318/严重眼损伤",
                (
                    r"\bh\s*318\b",
                    r"causes serious eye damage",
                    r"造成严重眼损伤",
                    r"严重眼损伤",
                ),
            ),
            (
                "H317/皮肤致敏",
                (
                    r"\bh\s*317\b",
                    r"allergic skin reaction",
                    r"皮肤过敏反应",
                    r"皮肤致敏",
                ),
            ),
            (
                "H335/呼吸道刺激",
                (
                    r"\bh\s*335\b",
                    r"respiratory irritation",
                    r"呼吸道刺激",
                ),
            ),
        )
        found = [label for label, patterns in exclusions if any(re.search(pattern, raw, flags=re.I) for pattern in patterns)]
        return "、".join(found)

    @staticmethod
    def _irritation_statement_is_negated(keyword: str, text: str) -> bool:
        if not keyword or not text:
            return False
        escaped = re.escape(keyword)
        chinese = rf"(?:无|不|未|没有|非|不会|未观察到|未发现)(?:明显)?(?:会|可)?{escaped}"
        english = rf"(?:no|not|non|without)(?:\s|-)*{escaped}"
        return bool(re.search(chinese, text, flags=re.I) or re.search(english, text, flags=re.I))

    @staticmethod
    def _high_toxic_name_hits(reagent_info: dict[str, Any]) -> list[str]:
        if RuleEngine._is_known_arsenic_reagent_alias(reagent_info):
            return []

        parts = []
        for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name", "english_name"):
            value = reagent_info.get(key)
            if value:
                parts.append(str(value))
        name_text = "".join(parts).lower()
        hits = []
        for token, label in (("砷", "含砷"), ("arsenic", "arsenic")):
            if token in name_text and not RuleEngine._keyword_is_negated(token, name_text):
                hits.append(label)
        return list(dict.fromkeys(hits))

    @staticmethod
    def _is_known_arsenic_reagent_alias(reagent_info: dict[str, Any]) -> bool:
        name_values = RuleEngine._normalized_name_values(reagent_info)
        if not name_values:
            return False
        aliases = {
            "砷试剂",
            "二乙基二硫代氨基甲酸银",
            "二乙基二硫代氨基甲酸银盐",
            "二乙氨基二硫代甲酸银",
            "二乙基氨荒酸银",
            "ddtc银盐",
            "detc银盐",
            "agddc",
            "ag-ddc",
            "agddtc",
            "ag-ddtc",
            "silverdiethyldithiocarbamate",
        }
        normalized_aliases = {RuleEngine._normalize_text(alias) for alias in aliases}
        has_known_alias = any(value in normalized_aliases for value in name_values)
        has_conflicting_arsenic_name = any(
            ("砷" in value or "arsenic" in value) and value not in normalized_aliases
            for value in name_values
        )
        return has_known_alias and not has_conflicting_arsenic_name

    @staticmethod
    def _oxidizer_name_hits(reagent_info: dict[str, Any]) -> list[str]:
        parts = []
        for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name", "english_name"):
            value = reagent_info.get(key)
            if value:
                parts.append(str(value))
        name_text = "".join(parts).lower()
        hits = []
        for token, label in (
            ("重铬酸", "重铬酸盐"),
            ("高锰酸", "高锰酸盐"),
            ("dichromate", "dichromate"),
            ("permanganate", "permanganate"),
        ):
            if token in name_text and not RuleEngine._keyword_is_negated(token, name_text):
                hits.append(label)
        return list(dict.fromkeys(hits))

    @staticmethod
    def _odor_name_hits(reagent_info: dict[str, Any]) -> list[str]:
        parts = []
        for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name", "english_name"):
            value = reagent_info.get(key)
            if value:
                parts.append(str(value))
        name_text = "".join(parts).lower()
        hits = []
        for token, label in (
            ("\u5432\u54da", "\u542b\u5432\u54da"),
            ("\u5421\u5576", "\u542b\u5421\u5576"),
            ("\u786b\u9187", "\u542b\u786b\u9187"),
            ("\u5def\u57fa", "\u542b\u5def\u57fa"),
            ("indole", "indole"),
            ("isoindole", "isoindole"),
            ("pyridine", "pyridine"),
            ("thiol", "thiol"),
            ("mercapto", "mercapto"),
        ):
            if token in name_text and not RuleEngine._keyword_is_negated(token, name_text):
                hits.append(label)
        return list(dict.fromkeys(hits))

    @staticmethod
    def _raw_reagent_text(reagent_info: dict[str, Any]) -> str:
        values: list[str] = []
        for key in (
            "name",
            "reagent_name",
            "chemical_name",
            "standard_name",
            "cleaned_name",
            "english_name",
            "cas",
            "cas_no",
            "spec",
            "remark",
            "text",
            "flash_point",
            "boiling_point",
            "toxicity",
            "concentration",
            "suggested_categories",
            "evidence",
        ):
            value = reagent_info.get(key)
            if isinstance(value, list):
                values.extend(str(item) for item in value)
            elif value is not None:
                values.append(str(value))
        return " ".join(values)

    @staticmethod
    def _flash_point_flammable_hits(reagent_info: dict[str, Any]) -> list[str]:
        if not RuleEngine._has_auto_flammable_context(reagent_info):
            return []
        hits: list[str] = []
        for celsius, source in RuleEngine._flash_points_celsius(reagent_info):
            if celsius < 60.0:
                hits.append(f"\u95ea\u70b9 {celsius:.1f}\u00b0C < 60\u00b0C\uff08{source}\uff09")
        return list(dict.fromkeys(hits))

    @staticmethod
    def flammable_evidence_issue(reagent_info: dict[str, Any]) -> str:
        low_flash_hits = [
            f"{celsius:.1f}°C（{source}）"
            for celsius, source in RuleEngine._flash_points_celsius(reagent_info)
            if celsius < 60.0
        ]
        if not low_flash_hits:
            return ""
        if RuleEngine._has_auto_flammable_context(reagent_info):
            return ""
        if RuleEngine._has_flammable_blocking_context(reagent_info):
            return (
                "检测到低闪点信息，但试剂名称或证据显示可能为固体、盐酸盐、聚合物、"
                "Boc/氨基酸衍生物、硅胶或叠氮/四氮唑类；不自动判定易燃类。"
            )
        return "检测到低闪点信息，但缺少液体、溶液、溶剂或油状物证据；不自动判定易燃类。"

    @staticmethod
    def is_reusable_flammable_evidence(reagent_info: dict[str, Any]) -> bool:
        text = RuleEngine._raw_reagent_text(reagent_info).lower()
        if RuleEngine._has_write_failure_evidence(text):
            return False
        return bool(
            RuleEngine._has_auto_flammable_context(reagent_info)
            or RuleEngine._has_common_flammable_liquid_example(reagent_info)
        )

    @staticmethod
    def _has_auto_flammable_context(reagent_info: dict[str, Any]) -> bool:
        low_flash = any(celsius < 60.0 for celsius, _ in RuleEngine._flash_points_celsius(reagent_info))
        if not low_flash:
            return False
        if RuleEngine._has_flammable_blocking_context(reagent_info):
            return False
        return RuleEngine._has_liquid_context(reagent_info) or RuleEngine._has_common_flammable_liquid_example(reagent_info)

    @staticmethod
    def _has_liquid_context(reagent_info: dict[str, Any]) -> bool:
        text = RuleEngine._raw_reagent_text(reagent_info).lower()
        tokens = (
            "液体",
            "液态",
            "溶液",
            "水溶液",
            "醇溶液",
            "溶剂",
            "油状",
            "油",
            "liquid",
            "solution",
            "solvent",
            "oil",
            "neat liquid",
        )
        return any(token in text for token in tokens)

    @staticmethod
    def _has_flammable_blocking_context(reagent_info: dict[str, Any]) -> bool:
        text = RuleEngine._raw_reagent_text(reagent_info).lower()
        tokens = (
            "固体",
            "粉末",
            "晶体",
            "结晶",
            "solid",
            "powder",
            "crystal",
            "盐酸盐",
            "hydrochloride",
            "boc",
            "叔丁氧羰基",
            "氨基酸",
            "amino acid",
            "聚",
            "poly",
            "硅胶",
            "silica gel",
            "四氮唑",
            "tetrazole",
            "叠氮",
            "叠化",
            "azide",
        )
        return any(token in text for token in tokens)

    @staticmethod
    def _has_write_failure_evidence(text: str) -> bool:
        return any(
            token in text
            for token in (
                "网页写入失败",
                "could not select",
                "row still shows",
                "selected 易燃类, but",
            )
        )

    @staticmethod
    def _has_common_flammable_liquid_example(reagent_info: dict[str, Any]) -> bool:
        name_text = RuleEngine._normalize_text(
            " ".join(
                str(reagent_info.get(key) or "")
                for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name")
            )
        )
        if not name_text:
            return False
        examples = (
            "正戊烷",
            "异戊烷",
            "车用汽油",
            "汽油",
            "乙醚",
            "乙醛",
            "2-环氧丙烷",
            "环氧丙烷",
            "呋喃",
            "甲酸甲酯",
            "正己烷",
            "环戊烷",
            "丁醛丙醚",
            "石油醚",
            "甲醇",
            "乙醇",
            "无水乙醇",
            "异丙醇",
            "丙酮",
            "乙酸乙酯",
            "甲苯",
            "二甲苯",
            "2-丁酮",
            "甲乙酮",
        )
        for example in examples:
            if RuleEngine._is_flammable_example_name(example, [name_text]):
                return True
        return False

    @staticmethod
    def _is_flammable_example_name(example: str, name_values: list[str]) -> bool:
        normalized_example = RuleEngine._normalize_text(example)
        if not normalized_example:
            return False
        safe_prefixes = ("无水", "无醛", "工业", "分析纯", "色谱", "hplc", "95", "99", "75")
        safe_suffixes = ("溶液", "水溶液", "试剂", "液", "无水")
        for name in name_values:
            normalized_name = RuleEngine._normalize_text(name)
            if not normalized_name:
                continue
            if normalized_name == normalized_example:
                return True
            for prefix in safe_prefixes:
                if normalized_name == RuleEngine._normalize_text(f"{prefix}{example}"):
                    return True
            for suffix in safe_suffixes:
                if normalized_name == RuleEngine._normalize_text(f"{example}{suffix}"):
                    return True
            if normalized_example in {"石油醚", "车用汽油", "甲乙酮"} and normalized_example in normalized_name:
                return True
        return False

    @staticmethod
    def _flash_points_celsius(reagent_info: dict[str, Any]) -> list[tuple[float, str]]:
        values: list[tuple[float, str]] = []
        flash_point = str(reagent_info.get("flash_point") or "").strip()
        if flash_point:
            values.extend(RuleEngine._temperatures_from_text(flash_point, require_flash_context=False))

        text_parts: list[str] = []
        for key in ("text", "evidence"):
            value = reagent_info.get(key)
            if isinstance(value, list):
                text_parts.extend(str(item) for item in value)
            elif value:
                text_parts.append(str(value))
        text = " ".join(text_parts)
        if text:
            values.extend(RuleEngine._temperatures_from_text(text, require_flash_context=True))

        unique: list[tuple[float, str]] = []
        seen: set[tuple[float, str]] = set()
        for celsius, source in values:
            key = (round(celsius, 3), source)
            if key not in seen:
                seen.add(key)
                unique.append((celsius, source))
        return unique

    @staticmethod
    def _temperatures_from_text(text: str, require_flash_context: bool) -> list[tuple[float, str]]:
        normalized = (
            str(text or "")
            .replace("\u2103", "\u00b0C")
            .replace("\u2109", "\u00b0F")
            .replace("\u00ba", "\u00b0")
            .replace("\uff05", "%")
        )
        snippets = [normalized]
        if require_flash_context:
            context_pattern = re.compile(
                r"(?:flash\s*point|\u95ea\u70b9)[^\n;。；]{0,80}",
                flags=re.I,
            )
            snippets = [match.group(0) for match in context_pattern.finditer(normalized)]

        results: list[tuple[float, str]] = []
        temperature_pattern = re.compile(
            r"(?P<prefix><|<=|>|>=|less\s+than|below|under|about|approximately|~)?\s*"
            r"(?P<value>-?\d+(?:\.\d+)?)"
            r"(?:\s*(?:\+/-|\u00b1)\s*\d+(?:\.\d+)?)?\s*"
            r"(?P<unit>\u00b0\s*[CFK]|[CFK]\b|\u534e\u6c0f\u5ea6|\u6444\u6c0f\u5ea6|\u5f00\u5c14\u6587)",
            flags=re.I,
        )
        for snippet in snippets:
            for match in temperature_pattern.finditer(snippet):
                value = float(match.group("value"))
                unit = RuleEngine._normalize_temperature_unit(match.group("unit"))
                celsius = RuleEngine._to_celsius(value, unit)
                if celsius is None:
                    continue
                raw = match.group(0).strip()
                results.append((celsius, raw))
        return results

    @staticmethod
    def _normalize_temperature_unit(unit: str) -> str:
        compact = re.sub(r"\s+", "", str(unit or "")).lower()
        if compact in {"\u00b0c", "c", "\u6444\u6c0f\u5ea6"}:
            return "c"
        if compact in {"\u00b0f", "f", "\u534e\u6c0f\u5ea6"}:
            return "f"
        if compact in {"\u00b0k", "k", "\u5f00\u5c14\u6587"}:
            return "k"
        return ""

    @staticmethod
    def _to_celsius(value: float, unit: str) -> float | None:
        if unit == "c":
            return value
        if unit == "f":
            return (value - 32.0) * 5.0 / 9.0
        if unit == "k":
            return value - 273.15
        return None

    @staticmethod
    def _contains_azide(text: str) -> bool:
        return any(token in text for token in ("\u53e0\u6c2e", "\u53e0\u5316", "azide"))

    @staticmethod
    def _contains_perchloric_acid(text: str) -> bool:
        return "\u9ad8\u6c2f\u9178" in text or "perchloric acid" in text

    @staticmethod
    def _is_pharmacopoeia_color_standard(reagent_info: dict[str, Any]) -> bool:
        name_text = RuleEngine._normalize_text(
            " ".join(
                str(reagent_info.get(key) or "")
                for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name")
            )
        )
        tokens = (
            "药典色度标准品",
            "药典色度标准溶液",
            "欧洲药典色度标准溶液",
            "pharmacopoeiacolorstandard",
            "pharmacopoeialcolorstandard",
            "europeanpharmacopoeiacolorstandardsolution",
        )
        return any(RuleEngine._normalize_text(token) in name_text for token in tokens)

    @staticmethod
    def _is_business_normal_name(reagent_info: dict[str, Any]) -> bool:
        name_text = RuleEngine._normalize_text(
            " ".join(
                str(reagent_info.get(key) or "")
                for key in (
                    "name",
                    "reagent_name",
                    "chemical_name",
                    "standard_name",
                    "cleaned_name",
                    "english_name",
                )
            )
        )
        if any(token in name_text for token in ("\u65e0\u6807\u7b7e", "\u6a21\u62df\u8bd5\u5242", "\u5b8c\u5168\u4e0d\u5b58\u5728")):
            return False
        if RuleEngine._is_known_arsenic_reagent_alias(reagent_info):
            return True
        tokens = (
            "\u86cb\u767d",
            "\u7ec6\u80de",
            "\u75c5\u6bd2",
            "\u514d\u75ab",
            "\u6297\u4f53",
            "\u67d3\u8272",
            "\u836f\u7269",
            "\u4e00\u6b21\u6027",
            "\u5361\u9a6c\u897f\u5e73",
            "\u6587\u62c9\u6cd5\u8f9b",
            "\u76d0\u9178\u6587\u62c9\u6cd5\u8f9b",
            "carbamazepine",
            "venlafaxine",
            "venlafaxinehydrochloride",
        )
        return RuleEngine._is_pharmacopoeia_color_standard(reagent_info) or any(
            RuleEngine._normalize_text(token) in name_text for token in tokens
        )

    @staticmethod
    def _is_low_priority_business_normal_name(reagent_info: dict[str, Any]) -> bool:
        name_text = RuleEngine._normalize_text(
            " ".join(
                str(reagent_info.get(key) or "")
                for key in (
                    "name",
                    "reagent_name",
                    "chemical_name",
                    "standard_name",
                    "cleaned_name",
                    "english_name",
                )
            )
        )
        tokens = (
            "\u6e05\u6d17\u6db2",
            "\u6807\u51c6",
            "\u6807\u51c6\u6db2",
            "\u6807\u51c6\u6eb6\u6db2",
            "icp",
            "\u8bd5\u5242",
            "\u7f13\u51b2\u6db2",
            "\u6807\u6db2",
            "\u6807\u5b9a",
            "\u6821\u51c6",
            "\u7eb3\u7c73",
            "\u5355\u4f53",
            "\u62c5\u4f53",
            "\u52a9\u6ee4",
            "\u8131\u8272",
            "\u6a21\u62df",
            "\u50ac\u5316",
            "\u4eba\u5de5",
        )
        return any(RuleEngine._normalize_text(token) in name_text for token in tokens)

    @staticmethod
    def _normalized_name_values(reagent_info: dict[str, Any]) -> list[str]:
        values = []
        for key in ("name", "reagent_name", "chemical_name", "standard_name", "cleaned_name", "english_name"):
            value = RuleEngine._normalize_text(str(reagent_info.get(key) or ""))
            if value:
                values.append(value)
        return values

    @staticmethod
    def _is_mineral_acid_salt_like(reagent_info: dict[str, Any]) -> bool:
        name_values = RuleEngine._normalized_name_values(reagent_info)
        if not name_values:
            return False

        return any(RuleEngine._looks_like_acid_salt_text(name_text) for name_text in name_values)

    @staticmethod
    def _looks_like_acid_salt_text(name_text: str) -> bool:
        if not name_text:
            return False

        english_salt_tokens = (
            "hydrochloride",
            "nitrate",
            "sulfate",
            "sulphate",
            "sulfonate",
            "sulphonate",
            "carboxylate",
            "phenolate",
            "ammoniumsalt",
            "sodiumsalt",
            "potassiumsalt",
        )
        chinese_salt_tokens = (
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
        if any(token in name_text for token in english_salt_tokens):
            return True
        if any(token in name_text for token in chinese_salt_tokens):
            return True
        if re.search(r"酸(钠|钾|铵|銨|氨)", name_text):
            return True
        if re.search(r"(盐酸|硝酸|硫酸|磷酸|磺酸|羧酸|酚).{0,12}(钠|钾|铵|銨)", name_text):
            return True
        if re.search(r"(钠|钾|铵|銨).{0,12}(盐酸|硝酸|硫酸|磷酸|磺酸|羧酸|酚)", name_text):
            return True

        acid_solution_forms = (
            "盐酸溶液",
            "硝酸溶液",
            "硫酸溶液",
            "浓盐酸",
            "浓硝酸",
            "浓硫酸",
            "稀盐酸",
            "稀硝酸",
            "稀硫酸",
            "发烟盐酸",
            "发烟硝酸",
            "发烟硫酸",
        )
        if any(form in name_text for form in acid_solution_forms):
            return False

        for acid in ("盐酸", "硝酸", "硫酸"):
            index = name_text.find(acid)
            while index != -1:
                after = name_text[index + len(acid) :]
                if after and not after.startswith(("溶液", "水溶液", "或", "或者", "/", "／", "和", "及", "、")):
                    return True
                index = name_text.find(acid, index + 1)
        return False

    @staticmethod
    def _is_ordinary_mineral_acid(reagent_info: dict[str, Any]) -> bool:
        name_values = RuleEngine._normalized_name_values(reagent_info)
        if not name_values or RuleEngine._is_mineral_acid_salt_like(reagent_info):
            return False

        if any(
            marker in name_text
            for name_text in name_values
            for marker in ("浓硫酸", "浓硝酸", "发烟硫酸", "发烟硝酸")
        ):
            return False

        exact_acids = (
            "hcl",
            "hydrochloricacid",
            "\u76d0\u9178",
            "hno3",
            "nitricacid",
            "\u785d\u9178",
            "h2so4",
            "sulfuricacid",
            "sulphuricacid",
            "\u786b\u9178",
        )
        if any(name_text in exact_acids for name_text in name_values):
            return True

        explicit_acid_forms = (
            "hydrochloricacid",
            "nitricacid",
            "sulfuricacid",
            "sulphuricacid",
            "\u76d0\u9178\u6eb6\u6db2",
            "\u785d\u9178\u6eb6\u6db2",
            "\u786b\u9178\u6eb6\u6db2",
            "\u6d53\u76d0\u9178",
            "\u6d53\u785d\u9178",
            "\u6d53\u786b\u9178",
            "\u7a00\u76d0\u9178",
            "\u7a00\u785d\u9178",
            "\u7a00\u786b\u9178",
            "\u53d1\u70df\u76d0\u9178",
            "\u53d1\u70df\u785d\u9178",
            "\u53d1\u70df\u786b\u9178",
        )
        if any(form in name_text for name_text in name_values for form in explicit_acid_forms):
            return True

        formula_context = re.compile(
            r"(?:^|[^a-z0-9])(?:\d+(?:\.\d+)?(?:%|mol/l|mol|m)?)?(?:hcl|hno3|h2so4)(?:solution)?(?:$|[^a-z0-9])"
        )
        return any(formula_context.search(name_text) for name_text in name_values)

    @staticmethod
    def _first_percent_concentration(text: str) -> float | None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _exact_example_hits(keywords: tuple[str, ...], reagent_info: dict[str, Any]) -> list[str]:
        name_values = []
        for key in ("name", "reagent_name", "chemical_name"):
            value = reagent_info.get(key)
            if value:
                name_values.append(str(value))
        normalized_names = {RuleEngine._normalize_text(value) for value in name_values}
        hits = []
        for keyword in keywords:
            normalized = RuleEngine._normalize_text(keyword)
            if normalized and normalized in normalized_names and keyword not in hits:
                hits.append(keyword)
        return hits

    @staticmethod
    def _toxic_threshold_hits(category: str, text: str) -> list[str] | None:
        if "剧毒" not in category and "高毒" not in category:
            return None

        hits: list[str] = []
        for value, unit, context in RuleEngine._toxicity_values(text):
            mg_per_kg = RuleEngine._to_mg_per_kg(value, unit)
            if mg_per_kg is None:
                continue

            is_oral = any(word in context for word in ("经口", "口服", "oral"))
            is_dermal = any(word in context for word in ("经皮", "皮肤", "dermal", "skin"))
            is_inhalation = any(word in context for word in ("吸入", "inhalation", "lc50"))
            is_unsupported_route = any(
                word in context
                for word in ("intravenous", "iv", "腹腔", "intraperitoneal", "subcutaneous", "注射")
            )
            if is_unsupported_route or (not is_oral and not is_dermal and not is_inhalation):
                continue

            if "剧毒" in category:
                if (is_dermal and mg_per_kg <= 50) or (is_oral and mg_per_kg <= 5):
                    hits.append(f"LD50阈值 {value:g}{unit}")
            elif "高毒" in category:
                if (is_dermal and 50 < mg_per_kg <= 200) or (is_oral and 5 < mg_per_kg < 50):
                    hits.append(f"LD50阈值 {value:g}{unit}")

        return list(dict.fromkeys(hits))

    @staticmethod
    def _toxicity_values(text: str) -> list[tuple[float, str, str]]:
        values: list[tuple[float, str, str]] = []
        normalized = text.replace("μ", "u").replace("µ", "u")
        pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(ug/kg|µg/kg|μg/kg|mg/kg|g/kg)", flags=re.I)
        for match in pattern.finditer(normalized):
            start = max(0, match.start() - 80)
            end = min(len(normalized), match.end() + 80)
            context = normalized[start:end].lower()
            if not any(marker in context for marker in ("ld50", "ldlo", "lc50", "半数致死", "致死量")):
                continue
            values.append((float(match.group(1)), match.group(2).lower(), context))
        return values

    @staticmethod
    def _to_mg_per_kg(value: float, unit: str) -> float | None:
        unit = unit.lower()
        if unit == "mg/kg":
            return value
        if unit in {"ug/kg", "µg/kg", "μg/kg"}:
            return value / 1000.0
        if unit == "g/kg":
            return value * 1000.0
        return None

    @staticmethod
    def _extract_remarks(raw: pd.DataFrame) -> str:
        remarks = []
        for _, row in raw.iterrows():
            category = RuleEngine._clean_text(row.get("category"))
            if category.startswith("备注"):
                remarks.append(category)
        return "\n".join(remarks)

    @staticmethod
    def _priority_from_remarks(remarks: str, categories: list[str]) -> list[str]:
        priority: list[str] = []
        match = re.search(r"2[、.]\s*(.+)", remarks, flags=re.S)
        if match:
            for group in re.split(r">", match.group(1)):
                for item in re.split(r"[、,，/]", group):
                    canonical = RuleEngine._canonical_category(item, categories)
                    if canonical and canonical not in priority:
                        priority.append(canonical)

        for category in reversed(CRITICAL_PRIORITY):
            if category in categories and category not in priority:
                priority.insert(0, category)

        for category in categories:
            if category not in priority:
                priority.append(category)

        return priority

    @staticmethod
    def _canonical_category(value: str, categories: list[str]) -> str:
        text = RuleEngine._clean_text(value).replace("类", "").replace("性", "")
        aliases = {
            "易爆": "易爆类",
            "强反应": "强反应性",
            "高毒": "高毒类",
            "发烟": "发烟类",
            "溴碘": "溴碘类",
            "重金属": "重金属类",
            "易燃": "易燃液体",
            "普通": "普通类",
            "拒收": "不建议接收类",
            "拒收类": "不建议接收类",
        }
        if text in aliases:
            return aliases[text]
        for category in categories:
            normalized = category.replace("类", "").replace("性", "")
            if text == normalized or text in normalized or normalized in text:
                return category
        return ""

    @staticmethod
    def _keywords_from_text(text: str) -> list[str]:
        cleaned = RuleEngine._clean_text(text)
        pieces = re.split(r"[\s,，、;；。:：()（）<>《》/]+", cleaned)
        keywords: list[str] = []
        for piece in pieces:
            piece = re.sub(r"^\d+[.、]", "", piece).strip()
            if RuleEngine._is_keyword(piece) and piece not in keywords:
                keywords.append(piece)
        return keywords

    @staticmethod
    def _is_keyword(text: str) -> bool:
        if len(text) < 2:
            return False
        if text.lower() in {"nan", "等", "高", "重", "次", "过", "超", "一般", "常见", "注意"}:
            return False
        if re.fullmatch(r"[\d.%-]+", text):
            return False
        return True

    @staticmethod
    def _looks_unknown(text: str) -> bool:
        return any(keyword in text for keyword in ("未知", "不明", "无标签", "无msds", "无法辨识", "标签腐烂"))

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None or pd.isna(value):
            return ""
        return str(value).strip()

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", "", text).lower()
