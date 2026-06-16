from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


RULE_COLUMNS = ["category", "explanation", "examples"]
CRITICAL_PRIORITY = ["不建议接收类", "剧毒品"]
UNKNOWN_CATEGORY = "未知类"
NORMAL_CATEGORY = "普通类"


@dataclass(frozen=True)
class Rule:
    category: str
    explanation: str
    examples: str
    explanation_keywords: tuple[str, ...]
    example_keywords: tuple[str, ...]


@dataclass
class RuleMatch:
    category: str
    explanation_hits: list[str]
    example_hits: list[str]
    score: float


@dataclass
class RuleEngine:
    rules: list[Rule]
    priority: list[str]

    @classmethod
    def from_settings(cls, settings: dict[str, Any], root_dir: Path) -> "RuleEngine":
        paths = settings.get("paths", {})
        rules_path = root_dir / paths.get("rules_excel", "config/rules.xlsx")
        return cls.from_excel(rules_path)

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
        return cls(rules=rules, priority=priority)

    def classify(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
        text = self._reagent_text(reagent_info)
        if not text:
            return self._manual_result("无法判断：试剂信息为空。")

        matches: dict[str, RuleMatch] = {}
        for rule in self.rules:
            explanation_hits = self._hits(rule.explanation_keywords, text)
            example_hits = self._specific_example_hits(rule.example_keywords, reagent_info)
            category_hits = self._category_suggestion_hits(rule.category, reagent_info)
            halogen_hits = self._bromine_iodine_hits(rule.category, reagent_info)
            explanation_hits = list(dict.fromkeys([*explanation_hits, *category_hits, *halogen_hits]))

            toxic_hits = self._toxic_threshold_hits(rule.category, text)
            if toxic_hits is not None:
                explanation_hits = toxic_hits
                if not toxic_hits:
                    example_hits = self._specific_example_hits(rule.example_keywords, reagent_info)

            if not explanation_hits:
                example_hits = self._exact_example_hits(rule.example_keywords, reagent_info)

            score = len(explanation_hits) * 2.0 + len(example_hits) * 0.8
            if score > 0:
                matches[rule.category] = RuleMatch(
                    category=rule.category,
                    explanation_hits=explanation_hits,
                    example_hits=example_hits,
                    score=score,
                )

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

        if not matches:
            if reagent_info.get("allow_default_normal"):
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
        need_manual_review = final_category in {UNKNOWN_CATEGORY, "不建议接收类"} or confidence < 0.55

        return {
            "final_category": final_category,
            "matched_categories": matched_categories,
            "reason": self._reason(final_category, matched_categories, matches),
            "confidence": confidence,
            "need_manual_review": need_manual_review,
        }

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
        return sorted(
            matches,
            key=lambda category: (
                rank.get(category, len(rank) + 100),
                -matches[category].score,
            ),
        )

    def _confidence(self, match: RuleMatch) -> float:
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
            parts.append(f"{category}({'; '.join(hit_parts)})")
        return f"以 rules.xlsx 的解释列为主要依据判定为 {final_category}。命中依据：{' | '.join(parts)}"

    @staticmethod
    def _manual_result(reason: str) -> dict[str, Any]:
        return {
            "final_category": "",
            "matched_categories": [],
            "reason": reason,
            "confidence": 0.0,
            "need_manual_review": True,
        }

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
            "suggested_categories",
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
            "flammable": ["易燃", "闪点低"],
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
            if normalized and normalized in text and keyword not in hits:
                hits.append(keyword)
        return hits

    @staticmethod
    def _category_suggestion_hits(category: str, reagent_info: dict[str, Any]) -> list[str]:
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
    def _bromine_iodine_hits(category: str, reagent_info: dict[str, Any]) -> list[str]:
        if category != "\u6eb4\u7898\u7c7b":
            return []

        parts = []
        for key in ("name", "reagent_name", "chemical_name", "text"):
            value = reagent_info.get(key)
            if value:
                parts.append(str(value))
        for value in reagent_info.get("evidence", []) or []:
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
                if (is_dermal and mg_per_kg <= 200) or (is_oral and 5 < mg_per_kg < 50):
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
        return any(keyword in text for keyword in ("未知", "无标签", "无msds", "无法辨识", "标签腐烂"))

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None or pd.isna(value):
            return ""
        return str(value).strip()

    @staticmethod
    def _normalize_text(text: str) -> str:
        return re.sub(r"\s+", "", text).lower()
