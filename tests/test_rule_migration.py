from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from rule_migration import migrate_rules, read_legacy_rules  # noqa: E402


class RuleMigrationTest(unittest.TestCase):
    def test_migrates_all_legacy_categories_with_rules_and_examples(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            output_path = Path(tempdir) / "rules_structured.xlsx"

            migrate_rules(ROOT_DIR / "config" / "rules.xlsx", output_path)

            legacy = read_legacy_rules(ROOT_DIR / "config" / "rules.xlsx")
            categories = pd.read_excel(output_path, sheet_name="categories", engine="openpyxl").fillna("")
            rules = pd.read_excel(output_path, sheet_name="rules", engine="openpyxl").fillna("")
            examples = pd.read_excel(output_path, sheet_name="examples", engine="openpyxl").fillna("")
            notes = pd.read_excel(output_path, sheet_name="notes", engine="openpyxl").fillna("")

            self.assertEqual(set(legacy.categories), set(categories["category"].astype(str)))
            self.assertGreaterEqual(len(rules), 70)
            self.assertGreaterEqual(len(examples), 300)
            self.assertGreaterEqual(len(notes), 1)

            for category in legacy.categories:
                with self.subTest(category=category):
                    self.assertFalse(rules[rules["category"].astype(str) == category].empty)
                    self.assertFalse(examples[examples["category"].astype(str) == category].empty)

    def test_checked_in_structured_rules_match_migration_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            generated = Path(tempdir) / "rules_structured.xlsx"
            migrate_rules(ROOT_DIR / "config" / "rules.xlsx", generated)
            checked_in = ROOT_DIR / "config" / "rules_structured.xlsx"

            for sheet_name in ("categories", "rules", "examples", "thresholds", "notes"):
                with self.subTest(sheet_name=sheet_name):
                    generated_df = pd.read_excel(generated, sheet_name=sheet_name, engine="openpyxl").fillna("")
                    checked_in_df = pd.read_excel(checked_in, sheet_name=sheet_name, engine="openpyxl").fillna("")
                    self.assertEqual(list(generated_df.columns), list(checked_in_df.columns))
                    self.assertEqual(len(generated_df), len(checked_in_df))


if __name__ == "__main__":
    unittest.main()
