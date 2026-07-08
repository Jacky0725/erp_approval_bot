from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from category_mapper import (  # noqa: E402
    category_mapping_summary,
    erp_property_options,
    is_non_writable_rule_category,
    review_decision_options,
    to_erp_property,
    to_rule_category,
)


class CategoryMapperTest(unittest.TestCase):
    def test_erp_options_come_from_settings(self) -> None:
        settings = {
            "reagent": {
                "physicochemical_property_options": ["普通类", "强反应"],
            }
        }

        self.assertEqual(erp_property_options(settings), ["普通类", "强反应"])

    def test_rule_category_maps_to_erp_property_option(self) -> None:
        settings = {
            "reagent": {
                "physicochemical_property_options": ["易燃类", "强反应"],
            }
        }

        self.assertEqual(to_erp_property("易燃液体", settings), "易燃类")
        self.assertEqual(to_erp_property("强反应性", settings), "强反应")

    def test_erp_property_can_map_back_to_rule_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules_path = root / "config" / "rules_structured.xlsx"
            rules_path.parent.mkdir(parents=True)
            with pd.ExcelWriter(rules_path, engine="openpyxl") as writer:
                pd.DataFrame(
                    [
                        {"category": "强反应性", "priority": 1, "enabled": True},
                        {"category": "易燃液体", "priority": 2, "enabled": True},
                    ]
                ).to_excel(writer, sheet_name="categories", index=False)

            settings = {
                "paths": {"structured_rules_excel": "config/rules_structured.xlsx"},
                "reagent": {
                    "physicochemical_property_options": ["强反应", "易燃类"],
                },
            }

            self.assertEqual(to_rule_category("强反应", settings, root), "强反应性")
            self.assertEqual(to_rule_category("易燃类", settings, root), "易燃液体")

    def test_reject_alias_maps_to_erp_property(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules_path = root / "config" / "rules_structured.xlsx"
            rules_path.parent.mkdir(parents=True)
            with pd.ExcelWriter(rules_path, engine="openpyxl") as writer:
                pd.DataFrame(
                    [
                        {"category": "不建议接收类", "priority": 1, "enabled": True},
                        {"category": "普通类", "priority": 2, "enabled": True},
                    ]
                ).to_excel(writer, sheet_name="categories", index=False)

            settings = {
                "paths": {"structured_rules_excel": "config/rules_structured.xlsx"},
                "reagent": {"physicochemical_property_options": ["普通类", "拒收类"]},
            }

            self.assertEqual(to_rule_category("拒收类", settings, root), "不建议接收类")
            self.assertEqual(to_erp_property("不建议接收类", settings), "拒收类")
            self.assertEqual(to_erp_property("拒收类", settings), "拒收类")
            self.assertFalse(is_non_writable_rule_category("拒收类", settings, root))
            self.assertFalse(is_non_writable_rule_category("不建议接收类", settings, root))
            self.assertIn("拒收类", review_decision_options(settings, root))

    def test_mapping_summary_reports_unmapped_rule_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rules_path = root / "config" / "rules_structured.xlsx"
            rules_path.parent.mkdir(parents=True)
            with pd.ExcelWriter(rules_path, engine="openpyxl") as writer:
                pd.DataFrame(
                    [
                        {"category": "普通类", "priority": 1, "enabled": True},
                        {"category": "常规碱", "priority": 2, "enabled": True},
                        {"category": "未知类", "priority": 3, "enabled": True},
                    ]
                ).to_excel(writer, sheet_name="categories", index=False)

            settings = {
                "paths": {"structured_rules_excel": "config/rules_structured.xlsx"},
                "reagent": {"physicochemical_property_options": ["普通类", "未知类"]},
            }

            summary = category_mapping_summary(settings, root)

            self.assertIn("常规碱", summary["unmapped_rule_categories"])
            self.assertNotIn("未知类", summary["non_writable_rule_categories"])
            self.assertEqual(to_erp_property("未知类", settings), "未知类")
            self.assertFalse(is_non_writable_rule_category("未知类", settings, root))


if __name__ == "__main__":
    unittest.main()
