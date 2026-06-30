from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from excel_exports import ExcelExportsMixin  # noqa: E402
from review_queue import ReviewQueueMixin  # noqa: E402


class ReviewQueueBot(ReviewQueueMixin, ExcelExportsMixin):
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.settings = {"paths": {"review_queue_excel": "review_queue.xlsx"}}

    def _log_dir(self) -> Path:
        log_dir = self.root_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir


class ReviewQueueTest(unittest.TestCase):
    def test_missing_review_queue_does_not_block_auto_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocked, reason = ReviewQueueBot(Path(tmp)).current_list_has_manual_review_item("SJ1")

        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_resolved_rows_do_not_block_auto_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pd.DataFrame(
                [
                    {
                        "list_number": "SJ1",
                        "status": "resolved",
                        "decision": "manual_review",
                    }
                ]
            ).to_excel(root / "review_queue.xlsx", index=False)

            blocked, reason = ReviewQueueBot(root).current_list_has_manual_review_item("SJ1")

        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_pending_rows_block_auto_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pd.DataFrame(
                [
                    {
                        "list_number": "SJ1",
                        "status": "pending",
                        "decision": "manual_review",
                    }
                ]
            ).to_excel(root / "review_queue.xlsx", index=False)

            blocked, reason = ReviewQueueBot(root).current_list_has_manual_review_item("SJ1")

        self.assertTrue(blocked)
        self.assertIn("pending manual review", reason)

    def test_clear_manual_review_items_for_list_removes_only_target_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pd.DataFrame(
                [
                    {"试剂清单号": "SJ1", "试剂名称": "A", "status": "pending"},
                    {"试剂清单号": "SJ1", "试剂名称": "B", "status": "pending"},
                    {"试剂清单号": "SJ2", "试剂名称": "C", "status": "pending"},
                ]
            ).to_excel(root / "review_queue.xlsx", index=False)

            ReviewQueueBot(root).clear_manual_review_items_for_list("SJ1")

            remaining = pd.read_excel(root / "review_queue.xlsx", dtype=str).fillna("")
            self.assertEqual(remaining["试剂清单号"].tolist(), ["SJ2"])
            self.assertEqual(remaining["试剂名称"].tolist(), ["C"])

    def test_manual_review_dedup_keeps_same_name_with_different_cas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bot = ReviewQueueBot(root)
            bot._current_detail_info = {"当前清单号": "SJ1", "申请人": "tester"}

            base_reagent = {
                "序号": "1",
                "试剂名称": "同名试剂",
                "CAS号": "111-11-1",
                "规格": "10",
                "规格单位": "g",
                "试剂数量": "1",
            }
            bot.add_manual_review_item_from_search_failure(
                base_reagent,
                {"standard_name": "同名试剂"},
                {"raw_text": "lookup failed"},
            )
            bot.add_manual_review_item_from_search_failure(
                {**base_reagent, "序号": "2", "CAS号": "222-22-2"},
                {"standard_name": "同名试剂"},
                {"raw_text": "lookup failed"},
            )
            bot.add_manual_review_item_from_search_failure(
                base_reagent,
                {"standard_name": "同名试剂"},
                {"raw_text": "lookup failed"},
            )

            queue = pd.read_excel(root / "review_queue.xlsx", dtype=str).fillna("")

        self.assertEqual(len(queue), 2)
        self.assertEqual(queue["cas"].tolist(), ["111-11-1", "222-22-2"])


if __name__ == "__main__":
    unittest.main()
