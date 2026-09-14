from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from excel_exports import ExcelExportsMixin  # noqa: E402
from review_queue import ReviewQueueMixin, localize_review_detail_text, review_display_summary_from_row  # noqa: E402


class ReviewQueueBot(ReviewQueueMixin, ExcelExportsMixin):
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.settings = {"paths": {"review_queue_excel": "review_queue.xlsx"}}

    def _log_dir(self) -> Path:
        log_dir = self.root_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir


class ReviewQueueTest(unittest.TestCase):
    def test_review_detail_temperature_units_are_displayed_as_celsius(self) -> None:
        summary = localize_review_detail_text(
            "flash_point=55 F | boiling_point=333 K | evidence=Flash point less than 69°F and boiling point 78 °C"
        )

        self.assertIn("闪点：12.8℃", summary)
        self.assertIn("沸点：59.9℃", summary)
        self.assertIn("less than 20.6℃", summary)
        self.assertIn("78℃", summary)
        self.assertNotIn("55 F", summary)
        self.assertNotIn("333 K", summary)
        self.assertNotIn("69°F", summary)

    def test_legacy_write_failure_is_displayed_as_write_verification(self) -> None:
        summary = review_display_summary_from_row(
            {
                "chemical_name": "pH计电极保护液",
                "standard_name": "氯化钾",
                "suggested_category": "普通类",
                "reason": "试剂名称、来源证据或规则判定存在不确定性，需要人工核对物化特性。",
                "reason_raw": "网页写入失败：ERP API verified, but webpage row shows -。",
                "display_reason": "缺少可信网站资料或可用的辅助判断，需人工核对。",
            },
            reason="试剂名称、来源证据或规则判定存在不确定性，需要人工核对物化特性。",
        )

        self.assertEqual(summary["review_kind"], "erp_write_verification")
        self.assertEqual(summary["display_suggestion"], "已判定：普通类")
        self.assertEqual(summary["evidence_status"], "写入待核验")
        self.assertNotIn("物化特性不确定", summary["display_reason"])
        self.assertTrue(summary["allow_suggestion_preselect"])

    def test_manual_review_batch_writes_once_and_keeps_each_sequence(self) -> None:
        class CountingBot(ReviewQueueBot):
            def __init__(self, root_dir: Path) -> None:
                super().__init__(root_dir)
                self.write_count = 0
                self._current_detail_info = {"当前清单号": "SJ1"}

            def write_excel_with_fallback(self, frame, path):  # noqa: ANN001
                self.write_count += 1
                return super().write_excel_with_fallback(frame, path)

        with tempfile.TemporaryDirectory() as tmp:
            bot = CountingBot(Path(tmp))
            bot.begin_manual_review_batch()
            for sequence in ("1", "2"):
                bot.add_manual_review_item(
                    {"序号": sequence, "试剂名称": "同名试剂", "CAS号": "1-11-1"},
                    {"standard_name": "同名试剂", "cleaned_name": "同名试剂"},
                    reason="证据不足",
                )
            bot.flush_manual_review_batch()
            frame = pd.read_excel(Path(tmp) / "review_queue.xlsx", dtype=str).fillna("")

        self.assertEqual(bot.write_count, 1)
        self.assertEqual(set(frame["序号"]), {"1", "2"})

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

    def test_clear_manual_review_items_for_list_keeps_confirmed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pd.DataFrame(
                [
                    {"试剂清单号": "SJ1", "试剂名称": "A", "status": "pending"},
                    {"试剂清单号": "SJ1", "试剂名称": "B", "status": "confirmed", "manual_result": "易燃类"},
                    {"试剂清单号": "SJ2", "试剂名称": "C", "status": "pending"},
                ]
            ).to_excel(root / "review_queue.xlsx", index=False)

            ReviewQueueBot(root).clear_manual_review_items_for_list("SJ1")

            remaining = pd.read_excel(root / "review_queue.xlsx", dtype=str).fillna("")
            self.assertEqual(remaining["试剂清单号"].tolist(), ["SJ1", "SJ2"])
            self.assertEqual(remaining["试剂名称"].tolist(), ["B", "C"])
            self.assertEqual(remaining.loc[0, "status"], "confirmed")

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

    def test_manual_review_dedup_matches_legacy_columns_by_list_and_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pd.DataFrame(
                [
                    {
                        "�Լ��嵥��": "SJ1",
                        "���": "27",
                        "chemical_name": "鐕冩枡鍙婃补鍝�",
                        "status": "confirmed",
                        "manual_result": "易燃类",
                    }
                ]
            ).to_excel(root / "review_queue.xlsx", index=False)

            bot = ReviewQueueBot(root)
            bot._current_detail_info = {"当前清单号": "SJ1", "申请人": "tester"}
            bot.add_manual_review_item(
                {
                    "序号": "27",
                    "试剂名称": "燃料及油品",
                    "CAS号": "-",
                    "规格": "1",
                    "规格单位": "kg",
                    "试剂数量": "1",
                },
                {"standard_name": "燃料及油品", "cleaned_name": "燃料及油品"},
                reason="再次运行仍需要人工复核",
            )

            queue = pd.read_excel(root / "review_queue.xlsx", dtype=str).fillna("")

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue.loc[0, "status"], "confirmed")

    def test_existing_manual_review_reason_is_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bot = ReviewQueueBot(root)
            bot._current_detail_info = {"?????": "SJ1", "???": "tester"}
            reagent = {
                "??": "1",
                "????": "?????",
                "CAS?": "111-11-1",
                "??": "10",
                "????": "g",
                "????": "1",
            }

            bot.add_manual_review_item(reagent, {"standard_name": "?????"}, reason="?????")
            bot.add_manual_review_item(reagent, {"standard_name": "?????"}, reason="??????????")

            queue = pd.read_excel(root / "review_queue.xlsx", dtype=str).fillna("")

        self.assertEqual(len(queue), 1)
        self.assertIn("?????", queue.loc[0, "reason"])
        self.assertIn("??????????", queue.loc[0, "reason"])


if __name__ == "__main__":
    unittest.main()
