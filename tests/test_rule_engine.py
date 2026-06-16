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

    def test_uses_priority_when_multiple_categories_match(self) -> None:
        result = self.engine.classify({"reagent_name": "三溴化硼"})

        self.assertEqual(result["final_category"], "发烟类")
        self.assertIn("发烟类", result["matched_categories"])
        self.assertIn("异味", result["matched_categories"])

    def test_critical_poison_category_is_conservative(self) -> None:
        result = self.engine.classify({"reagent_name": "氰化钠"})

        self.assertEqual(result["final_category"], "剧毒品")
        self.assertIn("剧毒品", result["matched_categories"])

    def test_unknown_reagent_needs_manual_review(self) -> None:
        result = self.engine.classify({"reagent_name": "无标签历史遗留试剂"})

        self.assertEqual(result["final_category"], "未知类")
        self.assertTrue(result["need_manual_review"])

    def test_unmatched_reagent_needs_manual_review(self) -> None:
        result = self.engine.classify({"reagent_name": "完全不存在的模拟试剂XYZ"})

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


if __name__ == "__main__":
    unittest.main()
