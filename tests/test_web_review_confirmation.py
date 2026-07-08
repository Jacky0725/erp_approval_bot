from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from reagent_memory import ReagentMemory  # noqa: E402
from web_runner import (  # noqa: E402
    confirm_review_item,
    delete_conflicting_memory,
    delete_memory_record,
    delete_review_item,
    import_approval_suggestions_to_memory,
    memory_summary,
    review_queue_summary,
    update_memory_record,
)


class WebReviewConfirmationTest(unittest.TestCase):
    def test_confirm_review_item_marks_resolved_and_adds_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir(parents=True)
            queue_path = data_dir / "review_queue.xlsx"
            pd.DataFrame(
                [
                    {
                        "timestamp": "2026-06-25T10:00:00",
                        "试剂清单号": "SJ1",
                        "序号": "1",
                        "试剂名称": "测试试剂",
                        "cas": "123-45-6",
                        "standard_name": "测试试剂",
                        "cleaned_name": "测试试剂",
                        "reason": "测试原因",
                        "status": "pending",
                    }
                ]
            ).to_excel(queue_path, index=False)

            summary = review_queue_summary(root)
            review_key = summary["preview"][0]["review_key"]
            result = confirm_review_item(
                {
                    "review_key": review_key,
                    "final_category": "普通类",
                    "reason": "人工确认普通类",
                },
                root,
            )

            self.assertTrue(result["confirmed"])
            self.assertTrue(result["memory_added"])
            self.assertEqual(review_queue_summary(root)["pending"], 0)

            memory = ReagentMemory.from_settings({}, root)
            match = memory.lookup(cas="123-45-6")
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match["final_category"], "普通类")

    def test_delete_review_item_removes_pending_row_without_memory_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir(parents=True)
            queue_path = data_dir / "review_queue.xlsx"
            pd.DataFrame(
                [
                    {
                        "timestamp": "2026-06-25T10:00:00",
                        "试剂清单号": "SJ1",
                        "序号": "1",
                        "试剂名称": "待删除试剂",
                        "cas": "123-45-6",
                        "standard_name": "待删除试剂",
                        "cleaned_name": "待删除试剂",
                        "reason": "不需要继续处理",
                        "status": "pending",
                    }
                ]
            ).to_excel(queue_path, index=False)

            summary = review_queue_summary(root)
            result = delete_review_item({"review_key": summary["preview"][0]["review_key"]}, root)

            self.assertTrue(result["deleted"])
            self.assertEqual(review_queue_summary(root)["pending"], 0)
            memory = ReagentMemory.from_settings({}, root)
            self.assertIsNone(memory.lookup(cas="123-45-6"))

    def test_confirm_review_item_accepts_reject_alias_as_writable_erp_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir(parents=True)
            queue_path = data_dir / "review_queue.xlsx"
            pd.DataFrame(
                [
                    {
                        "timestamp": "2026-06-25T10:00:00",
                        "试剂清单号": "SJ1",
                        "序号": "1",
                        "试剂名称": "TNT",
                        "cas": "",
                        "standard_name": "TNT",
                        "cleaned_name": "TNT",
                        "reason": "命中拒收类规则",
                        "status": "pending",
                    }
                ]
            ).to_excel(queue_path, index=False)

            summary = review_queue_summary(root)
            review_key = summary["preview"][0]["review_key"]
            result = confirm_review_item(
                {
                    "review_key": review_key,
                    "final_category": "拒收类",
                    "reason": "人工确认拒收类",
                },
                root,
            )

            self.assertTrue(result["confirmed"])
            self.assertTrue(result["memory_added"])
            self.assertEqual(review_queue_summary(root)["pending"], 0)

            memory = ReagentMemory.from_settings({}, root)
            rows = memory.list_records(query="TNT")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["final_category"], "拒收类")
            self.assertEqual(rows[0]["reusable"], 1)

    def test_memory_summary_and_update_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings({}, root)
            memory.add_record(
                raw_name="old",
                standard_name="old",
                cleaned_name="old",
                cas="111-11-1",
                final_category="普通类",
                confidence=0.9,
            )

            summary = memory_summary(query="old", root_dir=root)
            self.assertEqual(summary["rows"], 1)
            record_id = summary["preview"][0]["id"]

            result = update_memory_record(
                record_id,
                {"standard_name": "new", "final_category": "易燃类", "reusable": True},
                root_dir=root,
            )

            self.assertTrue(result["updated"])
            self.assertEqual(memory_summary(query="new", root_dir=root)["preview"][0]["final_category"], "易燃类")

    def test_memory_summary_paginates_preview_but_keeps_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings({}, root)
            for index in range(25):
                memory.add_record(
                    raw_name=f"page-item-{index:02d}",
                    final_category="普通类",
                    confidence=0.9,
                    source="unit_test",
                )

            first = memory_summary(root_dir=root, page=1, per_page=20)
            second = memory_summary(root_dir=root, page=2, per_page=20)

            self.assertEqual(first["rows"], 25)
            self.assertEqual(first["page_rows"], 20)
            self.assertEqual(first["pages"], 2)
            self.assertEqual(second["rows"], 25)
            self.assertEqual(second["page_rows"], 5)
            self.assertEqual(second["page"], 2)

    def test_delete_conflicting_memory_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings({}, root)
            memory.add_record(raw_name="delete-me", final_category="普通类", confidence=0.9)
            delete_row = memory_summary(query="delete-me", root_dir=root)["preview"][0]
            update_memory_record(delete_row["id"], {"conflict": True, "manual_verified": False}, root_dir=root)

            memory.add_record(raw_name="keep-confirmed", final_category="普通类", confidence=0.9)
            keep_row = memory_summary(query="keep-confirmed", root_dir=root)["preview"][0]
            update_memory_record(keep_row["id"], {"conflict": True, "manual_verified": True}, root_dir=root)

            result = delete_conflicting_memory(root_dir=root)

            self.assertEqual(result["deleted"], 2)
            self.assertTrue(Path(result["backup"]).exists())
            self.assertEqual(memory_summary(query="delete-me", root_dir=root)["rows"], 0)
            self.assertEqual(memory_summary(query="keep-confirmed", root_dir=root)["rows"], 0)

    def test_update_memory_record_maps_rule_category_to_erp_property(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings({}, root)
            memory.add_record(
                raw_name="old",
                standard_name="old",
                cleaned_name="old",
                cas="111-11-1",
                final_category="普通类",
                confidence=0.9,
            )
            record_id = memory_summary(query="old", root_dir=root)["preview"][0]["id"]

            result = update_memory_record(
                record_id,
                {"final_category": "易燃液体", "reusable": True},
                root_dir=root,
            )

            self.assertTrue(result["updated"])
            self.assertEqual(result["record"]["final_category"], "易燃类")

    def test_update_memory_record_reusable_clears_blocking_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings({}, root)
            memory.add_record(
                raw_name="blocked reusable",
                standard_name="blocked reusable",
                cleaned_name="blocked reusable",
                final_category="普通类",
                confidence=1.0,
                need_manual_review=True,
            )
            record_id = memory.find_any(raw_name="blocked reusable")["id"]
            memory.update_record(record_id, {"conflict": True, "reusable": False})

            result = update_memory_record(
                record_id,
                {
                    "raw_name": "blocked reusable",
                    "standard_name": "blocked reusable",
                    "cleaned_name": "blocked reusable",
                    "final_category": "普通类",
                    "confidence": 1,
                    "reusable": True,
                    "conflict": True,
                    "manual_verified": False,
                    "need_manual_review": True,
                },
                root_dir=root,
            )

            self.assertTrue(result["updated"])
            self.assertEqual(result["record"]["reusable"], 1)
            self.assertEqual(result["record"]["conflict"], 0)
            self.assertEqual(result["record"]["manual_verified"], 1)
            self.assertEqual(result["record"]["need_manual_review"], 0)

    def test_update_memory_record_rejects_unmapped_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings({}, root)
            memory.add_record(
                raw_name="old",
                standard_name="old",
                cleaned_name="old",
                cas="111-11-1",
                final_category="普通类",
                confidence=0.9,
            )
            record_id = memory_summary(query="old", root_dir=root)["preview"][0]["id"]

            with self.assertRaises(ValueError):
                update_memory_record(record_id, {"final_category": "不存在类别"}, root_dir=root)

    def test_import_approval_suggestions_to_memory_skips_unsafe_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "data" / "logs"
            log_dir.mkdir(parents=True)
            pd.DataFrame(
                [
                    {
                        "试剂名称": "可信试剂",
                        "清洗后名称": "可信试剂",
                        "标准化名称": "可信试剂",
                        "CAS号": "111-11-1",
                        "最终建议类别": "普通类",
                        "置信度": "0.92",
                        "需人工复核": "False",
                        "规则原因": "test",
                    },
                    {
                        "试剂名称": "人工复核试剂",
                        "CAS号": "222-22-2",
                        "最终建议类别": "易燃类",
                        "置信度": "0.95",
                        "需人工复核": "True",
                    },
                    {
                        "试剂名称": "低置信试剂",
                        "CAS号": "333-33-3",
                        "最终建议类别": "普通类",
                        "置信度": "0.3",
                        "需人工复核": "False",
                    },
                ]
            ).to_excel(log_dir / "approval_suggestions.xlsx", index=False)
            pd.DataFrame(
                [
                    {
                        "试剂名称": "历史时间戳试剂",
                        "清洗后名称": "历史时间戳试剂",
                        "标准化名称": "历史时间戳试剂",
                        "CAS号": "444-44-4",
                        "最终建议类别": "普通类",
                        "置信度": "0.91",
                        "需人工复核": "False",
                    }
                ]
            ).to_excel(log_dir / "approval_suggestions_20260616_110830.xlsx", index=False)

            stats = import_approval_suggestions_to_memory(root)

            self.assertEqual(stats["scanned"], 4)
            self.assertEqual(stats["imported"], 2)
            self.assertEqual(stats["candidate_manual_review"], 1)
            self.assertEqual(stats["candidate_low_confidence"], 1)
            self.assertEqual(memory_summary(query="可信试剂", root_dir=root)["rows"], 1)
            self.assertEqual(memory_summary(query="历史时间戳试剂", root_dir=root)["rows"], 1)
            manual_candidate = memory_summary(query="人工复核试剂", root_dir=root)["preview"][0]
            low_candidate = memory_summary(query="低置信试剂", root_dir=root)["preview"][0]
            self.assertEqual(manual_candidate["reusable"], 0)
            self.assertEqual(manual_candidate["need_manual_review"], 1)
            self.assertEqual(low_candidate["reusable"], 0)
            self.assertEqual(low_candidate["need_manual_review"], 1)

            delete_result = delete_memory_record(manual_candidate["id"], root_dir=root)
            self.assertTrue(delete_result["deleted"])
            self.assertEqual(memory_summary(query="人工复核试剂", root_dir=root)["rows"], 0)


if __name__ == "__main__":
    unittest.main()
