from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from rule_engine import Rule, RuleEngine  # noqa: E402


class RuleEngineToxicityTest(unittest.TestCase):
    def test_suggested_category_can_drive_rule_match(self) -> None:
        engine = RuleEngine(
            rules=[
                Rule(
                    category="特殊酸",
                    explanation="腐蚀性较高的酸类",
                    examples="",
                    explanation_keywords=("腐蚀性",),
                    example_keywords=(),
                ),
                Rule(
                    category="发烟类",
                    explanation="常温常压下易产生烟雾",
                    examples="",
                    explanation_keywords=("烟雾",),
                    example_keywords=(),
                ),
            ],
            priority=["发烟类", "特殊酸"],
        )

        result = engine.classify({"suggested_categories": ["特殊酸", "发烟类"]})

        self.assertEqual(result["final_category"], "发烟类")
        self.assertIn("特殊酸", result["matched_categories"])

    def test_toxic_units_do_not_imply_high_toxicity_without_threshold(self) -> None:
        engine = RuleEngine(
            rules=[
                Rule(
                    category="高毒类",
                    explanation="半数致死量（5mg/kg < x < 50mg/kg）",
                    examples="",
                    explanation_keywords=("5mg", "kg"),
                    example_keywords=(),
                )
            ],
            priority=["高毒类", "普通类"],
        )

        result = engine.classify(
            {
                "reagent_name": "甘氨酸",
                "toxicity": "LD50 oral rat 7930 mg/kg",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "普通类")
        self.assertNotIn("高毒类", result["matched_categories"])

    def test_high_toxicity_requires_ld50_threshold(self) -> None:
        engine = RuleEngine(
            rules=[
                Rule(
                    category="高毒类",
                    explanation="半数致死量（5mg/kg < x < 50mg/kg）",
                    examples="",
                    explanation_keywords=("5mg", "kg"),
                    example_keywords=(),
                )
            ],
            priority=["高毒类"],
        )

        result = engine.classify({"toxicity": "Oral LD50 rat 20 mg/kg"})

        self.assertEqual(result["final_category"], "高毒类")
        self.assertFalse(result["need_manual_review"])

    def test_intravenous_ld50_does_not_match_oral_or_dermal_threshold(self) -> None:
        engine = RuleEngine(
            rules=[
                Rule(
                    category="剧毒品",
                    explanation="经口LD50≤5mg/kg、经皮LD50≤50mg/kg",
                    examples="",
                    explanation_keywords=("LD50", "mg/kg"),
                    example_keywords=(),
                ),
                Rule(
                    category="高毒类",
                    explanation="半数致死量（5mg/kg < x < 50mg/kg）",
                    examples="",
                    explanation_keywords=("5mg", "kg"),
                    example_keywords=(),
                ),
            ],
            priority=["剧毒品", "高毒类", "普通类"],
        )

        result = engine.classify(
            {
                "toxicity": "LD50 Intravenous rat 21500 ug/kg",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "普通类")
        self.assertNotIn("剧毒品", result["matched_categories"])
        self.assertNotIn("高毒类", result["matched_categories"])

    def test_acute_toxicity_requires_ld50_threshold(self) -> None:
        engine = RuleEngine(
            rules=[
                Rule(
                    category="剧毒品",
                    explanation="经口LD50≤5mg/kg",
                    examples="",
                    explanation_keywords=("LD50", "mg/kg"),
                    example_keywords=(),
                )
            ],
            priority=["剧毒品"],
        )

        result = engine.classify({"toxicity": "Oral LD50 rat 2 mg/kg"})

        self.assertEqual(result["final_category"], "剧毒品")
        self.assertFalse(result["need_manual_review"])


if __name__ == "__main__":
    unittest.main()
