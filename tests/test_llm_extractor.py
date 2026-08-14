from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from llm_extractor import LlmExtractor  # noqa: E402


class LlmExtractorFallbackTest(unittest.TestCase):
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
