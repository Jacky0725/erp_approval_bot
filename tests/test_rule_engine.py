from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from rule_engine import Rule, RuleEngine  # noqa: E402


class RuleEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

    def test_classifies_special_acid(self) -> None:
        result = self.engine.classify({"reagent_name": "氢氟酸"})

        self.assertEqual(result["final_category"], "特殊酸")
        self.assertIn("特殊酸", result["matched_categories"])
        self.assertFalse(result["need_manual_review"])

    def test_common_mineral_acid_suppresses_special_acid_suggestion(self) -> None:
        engine = RuleEngine(
            rules=[
                Rule(
                    category="特殊酸",
                    explanation="",
                    examples="",
                    explanation_keywords=(),
                    example_keywords=(),
                ),
                Rule(
                    category="常规酸",
                    explanation="",
                    examples="",
                    explanation_keywords=(),
                    example_keywords=(),
                ),
            ],
            priority=["特殊酸", "常规酸"],
        )

        for name in ("10%HCL", "2mol HCl", "发烟盐酸", "硝酸", "硫酸"):
            with self.subTest(name=name):
                result = engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "suggested_categories": ["特殊酸"],
                    }
                )

                self.assertEqual(result["final_category"], "常规酸")
                self.assertIn("常规酸", result["matched_categories"])
                self.assertNotIn("特殊酸", result["matched_categories"])
                self.assertFalse(result["need_manual_review"])

    def test_mineral_acid_salts_do_not_match_regular_or_special_acid(self) -> None:
        engine = RuleEngine(
            rules=[
                Rule(
                    category="特殊酸",
                    explanation="",
                    examples="",
                    explanation_keywords=(),
                    example_keywords=(),
                ),
                Rule(
                    category="常规酸",
                    explanation="",
                    examples="",
                    explanation_keywords=(),
                    example_keywords=(),
                ),
            ],
            priority=["特殊酸", "常规酸"],
        )

        for name in (
            "硝酸镁",
            "硫酸铜",
            "苯肼盐酸盐",
            "盐酸苯肼",
            "magnesium nitrate",
            "copper sulfate",
            "phenylhydrazine hydrochloride",
        ):
            with self.subTest(name=name):
                result = engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "suggested_categories": ["特殊酸", "常规酸"],
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], "普通类")
                self.assertNotIn("常规酸", result["matched_categories"])
                self.assertNotIn("特殊酸", result["matched_categories"])
                self.assertFalse(result["need_manual_review"])

    def test_uses_priority_when_multiple_categories_match(self) -> None:
        result = self.engine.classify({"reagent_name": "三溴化硼"})

        self.assertEqual(result["final_category"], "发烟类")
        self.assertIn("发烟类", result["matched_categories"])
        self.assertIn("异味", result["matched_categories"])

    def test_critical_poison_category_is_conservative(self) -> None:
        result = self.engine.classify({"reagent_name": "氰化钠"})

        self.assertEqual(result["final_category"], "剧毒品")
        self.assertIn("剧毒品", result["matched_categories"])

    def test_unknown_reagent_keywords_do_not_need_manual_review(self) -> None:
        for name in (
            "无标签历史遗留试剂",
            "未知试剂",
            "不明成分试剂",
            "无法辨识试剂",
            "无MSDS试剂",
        ):
            with self.subTest(name=name):
                result = self.engine.classify({"reagent_name": name})

                self.assertEqual(result["final_category"], "未知类")
                self.assertFalse(result["need_manual_review"])

    def test_unmatched_reagent_needs_manual_review(self) -> None:
        result = self.engine.classify({"reagent_name": "完全不存在的样品XYZ"})

        self.assertEqual(result["final_category"], "")
        self.assertEqual(result["matched_categories"], [])
        self.assertTrue(result["need_manual_review"])
        self.assertEqual(result["confidence"], 0.0)

    def test_unmatched_reliable_reagent_defaults_to_normal(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "葡萄糖一水",
                "text": "glucose monohydrate no special hazard found",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "普通类")
        self.assertFalse(result["need_manual_review"])

    def test_pharmacopoeia_color_standard_is_normal(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "药典色度标准品GY系用于检测颜色密度",
                "text": "",
            }
        )

        self.assertEqual(result["final_category"], "普通类")
        self.assertFalse(result["need_manual_review"])
        self.assertGreaterEqual(result["confidence"], 0.9)

    def test_example_column_alone_does_not_classify(self) -> None:
        engine = RuleEngine(
            rules=[
                Rule(
                    category="易燃液体",
                    explanation="source must explicitly say flammable liquid",
                    examples="甲酸甲酯",
                    explanation_keywords=("source must explicitly say flammable liquid",),
                    example_keywords=("甲酸甲酯",),
                )
            ],
            priority=["易燃液体"],
        )

        result = engine.classify(
            {
                "reagent_name": "4-溴-2-羟基-6-甲基苯甲酸甲酯",
                "text": "Flash point 132.8 C. No source text says flammable liquid.",
            }
        )

        self.assertTrue(result["need_manual_review"])
        self.assertNotEqual(result["final_category"], "易燃液体")

    def test_bromine_iodine_priority_beats_flammable_liquid(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "4-\u6eb4-2-\u7f9f\u57fa-6-\u7532\u57fa\u82ef\u7532\u9178\u7532\u916f",
                "text": "Flash point 50 C. Source says flammable liquid. bromo substituted compound.",
                "suggested_categories": ["\u6eb4\u7898\u7c7b", "\u6613\u71c3\u6db2\u4f53"],
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "\u6eb4\u7898\u7c7b")
        self.assertIn("\u6613\u71c3\u6db2\u4f53", result["matched_categories"])

    def test_azide_is_classified_as_explosive(self) -> None:
        for name in ("\u53e0\u6c2e\u5316\u94a0", "\u53e0\u5316\u94a0", "sodium azide"):
            with self.subTest(name=name):
                result = self.engine.classify({"reagent_name": name, "text": name, "allow_default_normal": True})

                self.assertEqual(result["final_category"], "\u6613\u7206\u7c7b")

    def test_perchloric_acid_uses_concentration_threshold(self) -> None:
        low = self.engine.classify({"reagent_name": "70%\u9ad8\u6c2f\u9178", "allow_default_normal": True})
        high = self.engine.classify({"reagent_name": "75%\u9ad8\u6c2f\u9178", "allow_default_normal": True})
        unknown = self.engine.classify({"reagent_name": "\u9ad8\u6c2f\u9178", "allow_default_normal": True})

        self.assertEqual(low["final_category"], "\u7279\u6b8a\u9178")
        self.assertEqual(high["final_category"], "\u6613\u7206\u7c7b")
        self.assertEqual(unknown["final_category"], "\u6613\u7206\u7c7b")

    def test_low_flash_point_celsius_is_flammable_liquid(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "(2S)-2-(甲氧基甲基)环氧乙烷",
                "text": "Glycidyl methyl ether is a liquid. Flash Point 8.1±3.4 °C.",
                "flash_point": "8.1±3.4 °C",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "易燃液体")
        self.assertFalse(result["need_manual_review"])

    def test_low_flash_point_without_liquid_context_needs_manual_review(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "Boc-L-2-氯苯丙氨酸",
                "text": "Flash Point 25 °C. Solid amino acid derivative.",
                "flash_point": "25 °C",
                "allow_default_normal": True,
            }
        )

        self.assertTrue(result["need_manual_review"])
        self.assertNotEqual(result["final_category"], "易燃液体")

    def test_flammable_suggestion_alone_does_not_classify(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "普通有机中间体",
                "suggested_categories": ["易燃类"],
                "flammable": True,
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "普通类")
        self.assertNotIn("易燃液体", result["matched_categories"])

    def test_tetrazole_and_silica_are_not_auto_flammable(self) -> None:
        for name in ("5-Aminotetrazole", "硅胶（200-300目）"):
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "text": "Flash Point 25 °C. Solid powder.",
                        "flash_point": "25 °C",
                        "allow_default_normal": True,
                    }
                )

                self.assertTrue(result["need_manual_review"])
                self.assertNotEqual(result["final_category"], "易燃液体")

    def test_common_flammable_examples_are_still_flammable(self) -> None:
        for name in ("乙醚", "丙酮", "甲醇", "乙醇", "石油醚"):
            with self.subTest(name=name):
                result = self.engine.classify({"reagent_name": name, "allow_default_normal": True})

                self.assertEqual(result["final_category"], "易燃液体")
                self.assertFalse(result["need_manual_review"])

    def test_flash_point_fahrenheit_is_converted_before_classifying(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "(2S)-2-(甲氧基甲基)环氧乙烷",
                "text": "Methyl glycidyl ether is a liquid. Flash Point: less than 69°F.",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "易燃液体")
        self.assertIn("闪点", result["reason"])

    def test_flash_point_kelvin_is_converted_before_classifying(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "低闪点模拟试剂",
                "text": "低闪点模拟试剂为液体。",
                "flash_point": "333 K",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "易燃液体")


if __name__ == "__main__":
    unittest.main()
