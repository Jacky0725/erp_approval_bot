from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from rule_engine import RuleEngine  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
