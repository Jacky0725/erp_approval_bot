from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from data_health import (  # noqa: E402
    apply_data_health_repairs,
    audit_data_health,
    data_health_summary,
    run_data_health_audit,
)
from reagent_memory import ReagentMemory  # noqa: E402


class DataHealthTest(unittest.TestCase):
    def make_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path, dict]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        settings = {
            "paths": {
                "reagent_memory_sqlite": "data/reagent_memory.sqlite",
                "review_queue_excel": "data/review_queue.xlsx",
                "approval_suggestions_excel": "data/logs/approval_suggestions.xlsx",
            },
            "memory": {"min_confidence": 0.8},
        }
        return tmp, root, settings

    def test_audit_reports_reusable_mojibake_and_confirmed_missing_memory(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            memory = ReagentMemory.from_settings(settings, root)
            memory.add_record(
                raw_name="鐕冩枡鍙婃补鍝�",
                final_category="普通类",
                confidence=1.0,
                reason="legacy",
                source="unit_test",
            )
            pd.DataFrame(
                [
                    {
                        "试剂清单号": "SJ1",
                        "序号": "1",
                        "试剂名称": "燃料及油品",
                        "standard_name": "燃料及油品",
                        "cas": "-",
                        "status": "confirmed",
                        "manual_result": "易燃类",
                    },
                    {
                        "试剂清单号": "SJ1",
                        "序号": "1",
                        "试剂名称": "燃料及油品",
                        "status": "pending",
                    },
                ]
            ).to_excel(root / "data" / "review_queue.xlsx", index=False)

            report = audit_data_health(root, settings=settings)

        self.assertEqual(report["summary"]["memory_mojibake_records"], 1)
        self.assertEqual(report["summary"]["reusable_mojibake_records"], 1)
        self.assertEqual(report["summary"]["confirmed_review_missing_memory"], 1)
        self.assertEqual(report["summary"]["duplicate_review_items"], 2)

    def test_dry_run_export_does_not_modify_memory(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            memory = ReagentMemory.from_settings(settings, root)
            memory.add_record(
                raw_name="鐕冩枡鍙婃补鍝�",
                final_category="普通类",
                confidence=1.0,
            )
            before = memory.find_any(raw_name="鐕冩枡鍙婃补鍝�")
            output_path = root / "data" / "logs" / "audit.xlsx"

            result = run_data_health_audit(root, settings=settings, output_path=output_path)
            after = memory.find_any(raw_name="鐕冩枡鍙婃补鍝�")
            output_exists = output_path.exists()

        self.assertTrue(output_exists)
        self.assertEqual(result["output_path"], str(output_path))
        self.assertEqual(before["reusable"], after["reusable"])
        self.assertEqual(before["conflict"], after["conflict"])

    def test_apply_backs_up_disables_unrecoverable_and_rebuilds_confirmed_memory(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            memory = ReagentMemory.from_settings(settings, root)
            memory.add_record(
                raw_name="鐕冩枡鍙婃补鍝�",
                final_category="普通类",
                confidence=1.0,
            )
            pd.DataFrame(
                [
                    {
                        "试剂清单号": "SJ1",
                        "序号": "27",
                        "试剂名称": "燃料及油品",
                        "cleaned_name": "燃料及油品",
                        "standard_name": "燃料及油品",
                        "cas": "-",
                        "status": "confirmed",
                        "manual_result": "易燃类",
                        "specification": "1",
                        "unit": "kg",
                    }
                ]
            ).to_excel(root / "data" / "review_queue.xlsx", index=False)

            result = apply_data_health_repairs(root, settings=settings)
            bad = memory.find_any(raw_name="鐕冩枡鍙婃补鍝�")
            rebuilt = memory.lookup(raw_name="燃料及油品")

        self.assertIn("memory", result["backups"])
        self.assertIn("review_queue", result["backups"])
        self.assertEqual(bad["reusable"], 0)
        self.assertEqual(bad["conflict"], 1)
        self.assertEqual(rebuilt["final_category"], "易燃类")
        self.assertEqual(rebuilt["manual_verified"], 1)

    def test_data_health_summary_is_lightweight(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            summary = data_health_summary(root, settings=settings)

        self.assertFalse(summary["memory_exists"])
        self.assertFalse(summary["review_queue_exists"])
        self.assertEqual(summary["memory_mojibake_records"], 0)


if __name__ == "__main__":
    unittest.main()
