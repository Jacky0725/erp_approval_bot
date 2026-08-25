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
        self.assertEqual(report["summary"]["blocking_duplicate_review_items"], 1)

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

    def test_apply_rebuilds_confirmed_memory_over_conflicting_old_record(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            memory = ReagentMemory.from_settings(settings, root)
            memory.add_record(
                raw_name="燃料及油品",
                standard_name="燃料及油品",
                cas="-",
                final_category="普通类",
                confidence=1.0,
                source="old_import",
            )
            (root / "data").mkdir(parents=True, exist_ok=True)
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
                    }
                ]
            ).to_excel(root / "data" / "review_queue.xlsx", index=False)

            result = apply_data_health_repairs(root, settings=settings)
            rebuilt = memory.lookup(raw_name="燃料及油品")

        self.assertEqual(result["review_queue"]["rebuilt_memory"], 1)
        self.assertEqual(rebuilt["final_category"], "易燃类")
        self.assertEqual(rebuilt["manual_verified"], 1)

    def test_apply_promotes_confirmed_memory_with_old_write_failure_reason(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            memory = ReagentMemory.from_settings(settings, root)
            memory.add_record(
                raw_name="燃料及油品",
                standard_name="燃料及油品",
                cas="-",
                final_category="易燃类",
                confidence=1.0,
                source="approval_writer",
                reason="网页写入失败：row not found",
            )
            (root / "data").mkdir(parents=True, exist_ok=True)
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
                    }
                ]
            ).to_excel(root / "data" / "review_queue.xlsx", index=False)

            result = apply_data_health_repairs(root, settings=settings)
            rebuilt = memory.lookup(raw_name="燃料及油品")

        self.assertEqual(result["review_queue"]["rebuilt_memory"], 1)
        self.assertEqual(rebuilt["reusable"], 1)
        self.assertEqual(rebuilt["manual_verified"], 1)
        self.assertNotIn("网页写入失败", rebuilt["reason"])

    def test_confirmed_review_with_mojibake_category_is_not_counted_missing_memory(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            (root / "data").mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {
                        "试剂清单号": "SJ1",
                        "序号": "8",
                        "试剂名称": "燃料及油品",
                        "status": "confirmed",
                        "manual_result": "��ͨ��",
                    }
                ]
            ).to_excel(root / "data" / "review_queue.xlsx", index=False)

            report = audit_data_health(root, settings=settings)

        self.assertEqual(report["summary"]["confirmed_review_missing_memory"], 0)

    def test_confirmed_review_with_reusable_alias_match_is_not_counted_missing_memory(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            memory = ReagentMemory.from_settings(settings, root)
            memory.add_record(
                raw_name="别名A",
                standard_name="标准名A",
                cas="83048-65-1",
                final_category="普通类",
                confidence=1.0,
                manual_verified=True,
            )
            (root / "data").mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {
                        "试剂清单号": "SJ1",
                        "序号": "8",
                        "试剂名称": "别名B",
                        "standard_name": "标准名B",
                        "cas": "83048-65-1",
                        "status": "confirmed",
                        "manual_result": "普通类",
                    }
                ]
            ).to_excel(root / "data" / "review_queue.xlsx", index=False)

            report = audit_data_health(root, settings=settings)

        self.assertEqual(report["summary"]["confirmed_review_missing_memory"], 0)

    def test_apply_marks_blocking_duplicate_as_confirmed_duplicate(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            (root / "data").mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {
                        "试剂清单号": "SJ1",
                        "序号": "8",
                        "试剂名称": "燃料及油品",
                        "standard_name": "燃料及油品",
                        "cas": "-",
                        "status": "confirmed",
                        "manual_result": "易燃类",
                        "confirmed_at": "2026-08-25T12:00:00",
                    },
                    {
                        "试剂清单号": "SJ1",
                        "序号": "8",
                        "试剂名称": "燃料及油品",
                        "standard_name": "燃料及油品",
                        "cas": "-",
                        "status": "pending",
                        "manual_result": "",
                    },
                ]
            ).to_excel(root / "data" / "review_queue.xlsx", index=False)

            result = apply_data_health_repairs(root, settings=settings)
            repaired = pd.read_excel(root / "data" / "review_queue.xlsx", dtype=str).fillna("")

        self.assertEqual(result["review_queue"]["blocking_duplicates_resolved"], 1)
        duplicate = repaired[repaired["status"].eq("confirmed_duplicate")].iloc[0]
        self.assertEqual(duplicate["manual_result"], "易燃类")
        self.assertEqual(duplicate["confirmed_by"], "data_health_repair")

    def test_apply_leaves_duplicates_without_confirmed_unchanged(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            (root / "data").mkdir(parents=True, exist_ok=True)
            pd.DataFrame(
                [
                    {
                        "试剂清单号": "SJ1",
                        "序号": "8",
                        "试剂名称": "燃料及油品",
                        "status": "pending",
                    },
                    {
                        "试剂清单号": "SJ1",
                        "序号": "8",
                        "试剂名称": "燃料及油品",
                        "status": "manual_review",
                    },
                ]
            ).to_excel(root / "data" / "review_queue.xlsx", index=False)

            before = audit_data_health(root, settings=settings)
            result = apply_data_health_repairs(root, settings=settings)
            repaired = pd.read_excel(root / "data" / "review_queue.xlsx", dtype=str).fillna("")

        self.assertEqual(before["summary"]["duplicate_review_items"], 2)
        self.assertEqual(before["summary"]["blocking_duplicate_review_items"], 0)
        self.assertEqual(result["review_queue"]["blocking_duplicates_resolved"], 0)
        self.assertEqual(set(repaired["status"].tolist()), {"pending", "manual_review"})

    def test_data_health_summary_is_lightweight(self) -> None:
        tmp, root, settings = self.make_root()
        with tmp:
            summary = data_health_summary(root, settings=settings)

        self.assertFalse(summary["memory_exists"])
        self.assertFalse(summary["review_queue_exists"])
        self.assertEqual(summary["memory_mojibake_records"], 0)
        self.assertEqual(summary["blocking_duplicate_review_items"], 0)


if __name__ == "__main__":
    unittest.main()
