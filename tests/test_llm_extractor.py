from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from llm_extractor import LlmExtractor  # noqa: E402


class LlmExtractorFallbackTest(unittest.TestCase):
    def test_classify_by_rules_fallback_skips_without_api_key(self) -> None:
        class OfflineExtractor(LlmExtractor):
            def _has_api_key(self) -> bool:
                return False

        result = OfflineExtractor().classify_by_rules_fallback({"raw_name": "unknown"})

        self.assertFalse(result["used_llm"])
        self.assertEqual(result["candidate_category"], "")
        self.assertTrue(result["must_manual_review"])

    def test_classify_by_rules_fallback_parses_structured_json(self) -> None:
        class FakeMessage:
            content = (
                '{"candidate_category":"强反应性","confidence":0.92,'
                '"reason":"名称提示可能按强反应性规则处理。",'
                '"matched_rule_summary":"硼酸类需按强反应性复核",'
                '"evidence_type":"name_based","must_manual_review":false}'
            )

        class FakeChoice:
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **kwargs: object) -> FakeResponse:
                self.kwargs = kwargs
                return FakeResponse()

        class FakeChat:
            def __init__(self) -> None:
                self.completions = FakeCompletions()

        class FakeClient:
            def __init__(self) -> None:
                self.chat = FakeChat()

        class OnlineExtractor(LlmExtractor):
            def _has_api_key(self) -> bool:
                return True

            def _client(self) -> FakeClient:
                return FakeClient()

        result = OnlineExtractor().classify_by_rules_fallback(
            {
                "raw_name": "4-乙氧基-2-甲基苯基硼酸",
                "rule_summary": [{"category": "强反应性", "rule_keywords": "硼酸", "example_names": ""}],
            }
        )

        self.assertTrue(result["used_llm"])
        self.assertEqual(result["candidate_category"], "强反应性")
        self.assertEqual(result["confidence"], 0.92)
        self.assertEqual(result["evidence_type"], "name_based")
        self.assertFalse(result["must_manual_review"])

    def test_classify_by_rules_fallback_caps_insufficient_confidence(self) -> None:
        class FakeMessage:
            content = (
                '{"candidate_category":"普通类","confidence":0.95,'
                '"reason":"无法识别。","matched_rule_summary":"",'
                '"evidence_type":"insufficient","must_manual_review":true}'
            )

        class FakeChoice:
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **kwargs: object) -> FakeResponse:
                return FakeResponse()

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        class OnlineExtractor(LlmExtractor):
            def _has_api_key(self) -> bool:
                return True

            def _client(self) -> FakeClient:
                return FakeClient()

        result = OnlineExtractor().classify_by_rules_fallback({"raw_name": "unknown"})

        self.assertLess(result["confidence"], 0.7)
        self.assertTrue(result["must_manual_review"])

    def test_common_mineral_acids_fallback_to_regular_acid(self) -> None:
        extractor = LlmExtractor()
        for name, raw_text in (
            ("10% HCl", "hydrochloric acid solution CAS 7647-01-0"),
            ("发烟盐酸", "hydrochloric acid fuming solution"),
            ("硝酸", "nitric acid solution CAS 7697-37-2"),
            ("硫酸", "sulfuric acid solution CAS 7664-93-9"),
        ):
            with self.subTest(name=name):
                result = extractor._merge_local_hazard_fallback(
                    {
                        "name": name,
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
                    },
                    raw_text=raw_text,
                    name=name,
                    cas="",
                )

                self.assertTrue(result["corrosive"])
                self.assertIn("常规酸", result["suggested_categories"])
                self.assertNotIn("特殊酸", result["suggested_categories"])

    def test_mineral_acid_salts_do_not_fallback_to_regular_acid(self) -> None:
        extractor = LlmExtractor()
        for name, raw_text in (
            ("magnesium nitrate", "magnesium nitrate salt SDS; nitrate compound"),
            ("copper sulfate", "copper sulfate salt SDS; source mentions sulfuric acid in unrelated context"),
            (
                "phenylhydrazine hydrochloride",
                "hydrochloride salt. source mentions hydrochloric acid in unrelated context.",
            ),
        ):
            with self.subTest(name=name):
                result = extractor._merge_local_hazard_fallback(
                    {
                        "name": name,
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
                    },
                    raw_text=raw_text,
                    name=name,
                    cas="",
                )

                self.assertNotIn("常规酸", result["suggested_categories"])
                self.assertFalse(
                    any("ordinary mineral acid" in item.lower() for item in result["evidence"])
                )

    def test_fuming_hydrochloric_acid_fallback(self) -> None:
        extractor = LlmExtractor()
        result = extractor._merge_local_hazard_fallback(
            {
                "name": "发烟盐酸 / Hydrochloric acid",
                "cas": "7647-01-0",
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
            },
            raw_text="special hazardous chemicals do not display product information",
            name="发烟盐酸 / Hydrochloric acid",
            cas="7647-01-0",
        )

        self.assertTrue(result["corrosive"])
        self.assertIn("常规酸", result["suggested_categories"])
        self.assertNotIn("特殊酸", result["suggested_categories"])
        self.assertGreaterEqual(result["confidence"], 0.75)
        self.assertTrue(result["evidence"])

    def test_incompatible_with_oxidizing_agents_is_not_oxidizer(self) -> None:
        extractor = LlmExtractor()
        result = extractor._suppress_incompatibility_only_oxidizing(
            {
                "name": "Polyethylene glycol 400",
                "cas": "",
                "flash_point": "171 C",
                "boiling_point": "250 C",
                "toxicity": "",
                "corrosive": None,
                "oxidizing": True,
                "flammable": None,
                "water_reactive": None,
                "explosive_risk": None,
                "heavy_metal": None,
                "suggested_categories": ["Toxic", "氧化剂"],
                "evidence": ["Stable. Incompatible with strong oxidizing agents."],
                "confidence": 0.8,
            },
            "Stable. Incompatible with strong oxidizing agents. Combustible.",
        )

        self.assertFalse(result["oxidizing"])
        self.assertNotIn("氧化剂", result["suggested_categories"])
        self.assertFalse(any("oxidizing agent" in item.lower() for item in result["evidence"]))

    def test_h272_is_positive_oxidizer_signal(self) -> None:
        extractor = LlmExtractor()
        result = extractor._suppress_incompatibility_only_oxidizing(
            {
                "name": "oxidizer",
                "cas": "",
                "flash_point": "",
                "boiling_point": "",
                "toxicity": "",
                "corrosive": None,
                "oxidizing": True,
                "flammable": None,
                "water_reactive": None,
                "explosive_risk": None,
                "heavy_metal": None,
                "suggested_categories": ["氧化剂"],
                "evidence": ["Hazard statement H272"],
                "confidence": 0.8,
            },
            "Hazard statement H272: may intensify fire; oxidizer.",
        )

        self.assertTrue(result["oxidizing"])
        self.assertIn("氧化剂", result["suggested_categories"])

    def test_generate_search_candidates_falls_back_to_local_terms(self) -> None:
        class OfflineExtractor(LlmExtractor):
            def _client(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("offline")

        extractor = OfflineExtractor()
        result = extractor.generate_search_candidates(
            {
                "raw_name": "NaOH 0.1mol/L",
                "cas": "1310-73-2",
                "standard_name": "sodium hydroxide",
            }
        )

        self.assertFalse(result["used_llm"])
        self.assertIn("1310-73-2", result["candidates"])
        self.assertIn("1310-73-2 SDS", result["candidates"])
        self.assertIn("sodium hydroxide SDS", result["candidates"])


if __name__ == "__main__":
    unittest.main()
