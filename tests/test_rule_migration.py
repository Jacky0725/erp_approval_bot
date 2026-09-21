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
    PHOSPHORUS_PENTOXIDE = "\u4e94\u6c27\u5316\u4e8c\u78f7"
    OXIDIZER_EXAMPLES = {
        "高锰酸钾",
        "高锰酸钠",
        "高锰酸盐",
        "重铬酸钾",
        "重铬酸钠",
        "重铬酸盐",
    }

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
                    if sheet_name != "rules":
                        self.assertEqual(len(generated_df), len(checked_in_df))
                        continue

                    # The structured workbook is the approved production rule
                    # source and may contain reviewed additions beyond the
                    # legacy workbook. Migration must not silently drop its
                    # baseline rows, while checked-in additions remain valid
                    # structured rules rather than being treated as drift.
                    generated_rows = set(generated_df.astype(str).agg("\x1f".join, axis=1))
                    checked_in_rows = set(checked_in_df.astype(str).agg("\x1f".join, axis=1))
                    self.assertTrue(generated_rows.issubset(checked_in_rows))
                    extra_rows = checked_in_df[
                        ~checked_in_df.astype(str).agg("\x1f".join, axis=1).isin(generated_rows)
                    ]
                    self.assertTrue(extra_rows["rule_id"].astype(str).str.strip().ne("").all())
                    self.assertTrue(extra_rows["category"].astype(str).str.strip().ne("").all())

    def test_oxidizer_examples_are_standardized_in_migration_and_checked_in_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            generated = Path(tempdir) / "rules_structured.xlsx"
            migrate_rules(ROOT_DIR / "config" / "rules.xlsx", generated)
            checked_in = ROOT_DIR / "config" / "rules_structured.xlsx"

            for rules_path in (generated, checked_in):
                with self.subTest(rules_path=rules_path.name):
                    examples = pd.read_excel(rules_path, sheet_name="examples", engine="openpyxl").fillna("")
                    oxidizers = examples[examples["category"].astype(str).str.strip() == "氧化剂"]
                    example_names = set(oxidizers["example_name"].astype(str).str.strip())
                    match_modes = {
                        row["example_name"]: row["match_mode"]
                        for _, row in oxidizers.iterrows()
                        if row["example_name"] in self.OXIDIZER_EXAMPLES
                    }

                    self.assertTrue(self.OXIDIZER_EXAMPLES.issubset(example_names))
                    self.assertEqual(set(match_modes), self.OXIDIZER_EXAMPLES)
                    self.assertTrue(all(mode == "contains" for mode in match_modes.values()))


    def test_phosphorus_pentoxide_reactivity_example_is_migrated_and_checked_in(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            generated = Path(tempdir) / "rules_structured.xlsx"
            migrate_rules(ROOT_DIR / "config" / "rules.xlsx", generated)
            checked_in = ROOT_DIR / "config" / "rules_structured.xlsx"

            for rules_path in (generated, checked_in):
                with self.subTest(rules_path=rules_path.name):
                    examples = pd.read_excel(rules_path, sheet_name="examples", engine="openpyxl").fillna("")
                    rows = examples[
                        (examples["category"].astype(str).str.strip() == "\u5f3a\u53cd\u5e94\u6027")
                        & (examples["example_name"].astype(str).str.strip() == self.PHOSPHORUS_PENTOXIDE)
                    ]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(str(rows.iloc[0]["match_mode"]).strip(), "contains")


if __name__ == "__main__":
    unittest.main()
