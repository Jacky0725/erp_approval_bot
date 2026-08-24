from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from rule_engine import RuleEngine  # noqa: E402


class OxidizerStructuredRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = RuleEngine.from_structured_excel(ROOT_DIR / "config" / "rules_structured.xlsx")

    def test_permanganates_are_oxidizers(self) -> None:
        for name in ("高锰酸钾", "高锰酸钠", "高锰酸盐"):
            with self.subTest(name=name):
                result = self.engine.classify({"reagent_name": name, "text": name, "allow_default_normal": True})

                self.assertEqual(result["final_category"], "氧化剂")
                self.assertIn("氧化剂", result["matched_categories"])
                self.assertFalse(result["need_manual_review"])

    def test_dichromates_prefer_oxidizer_over_heavy_metal(self) -> None:
        for name in ("重铬酸钾", "重铬酸钠", "重铬酸盐"):
            with self.subTest(name=name):
                result = self.engine.classify({"reagent_name": name, "text": name, "allow_default_normal": True})

                self.assertEqual(result["final_category"], "氧化剂")
                self.assertIn("氧化剂", result["matched_categories"])
                self.assertIn("重金属类", result["matched_categories"])
                self.assertFalse(result["need_manual_review"])


if __name__ == "__main__":
    unittest.main()
