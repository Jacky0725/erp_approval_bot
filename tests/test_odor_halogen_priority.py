from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from rule_engine import RuleEngine  # noqa: E402


class OdorHalogenPriorityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

    def test_odor_takes_priority_when_name_also_matches_bromine_iodine(self) -> None:
        for name in (
            "4-\u6eb4\u5421\u5576",
            "5-\u7898-1H-\u5432\u54da",
            "bromo pyridine",
            "iodomercaptopyridine",
        ):
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "english_name": name,
                        "text": name,
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], "\u5f02\u5473")
                self.assertIn("\u5f02\u5473", result["matched_categories"])
                self.assertIn("\u6eb4\u7898\u7c7b", result["matched_categories"])


if __name__ == "__main__":
    unittest.main()
