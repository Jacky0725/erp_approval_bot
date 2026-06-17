from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from rule_engine import RuleEngine  # noqa: E402


class StructuredRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = RuleEngine.from_settings(
            {
                "paths": {
                    "structured_rules_excel": "config/rules_structured.xlsx",
                    "rules_excel": "config/rules.xlsx",
                }
            },
            ROOT_DIR,
        )

    def test_azide_uses_explosive_class(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "\u53e0\u5316\u94a0",
                "text": "sodium azide",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "\u6613\u7206\u7c7b")
        self.assertFalse(result["need_manual_review"])

    def test_perchloric_acid_concentration_rules(self) -> None:
        low = self.engine.classify({"reagent_name": "70%\u9ad8\u6c2f\u9178", "allow_default_normal": True})
        high = self.engine.classify({"reagent_name": "75%\u9ad8\u6c2f\u9178", "allow_default_normal": True})
        missing = self.engine.classify({"reagent_name": "\u9ad8\u6c2f\u9178", "allow_default_normal": True})

        self.assertEqual(low["final_category"], "\u7279\u6b8a\u9178")
        self.assertEqual(high["final_category"], "\u6613\u7206\u7c7b")
        self.assertEqual(missing["final_category"], "\u6613\u7206\u7c7b")

    def test_bromine_iodine_keeps_priority_over_flammable(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "4-\u6eb4-2-\u7f9f\u57fa-6-\u7532\u57fa\u82ef\u7532\u9178\u7532\u916f",
                "text": "Flash point 50 C. Source says flammable liquid. bromo compound.",
                "suggested_categories": ["\u6eb4\u7898\u7c7b", "\u6613\u71c3\u6db2\u4f53"],
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "\u6eb4\u7898\u7c7b")


if __name__ == "__main__":
    unittest.main()
