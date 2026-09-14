from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from reagent_page import ReagentPageMixin  # noqa: E402


class FakePage:
    def __init__(self) -> None:
        self.waits: list[int] = []

    def wait_for_timeout(self, timeout: int) -> None:
        self.waits.append(timeout)


class ReagentPageAutoMatchTest(unittest.TestCase):
    def test_detail_identity_waits_until_expected_list_is_visible(self) -> None:
        class Bot(ReagentPageMixin):
            detail_rows = iter([
                {"当前清单号": "SJ0001"},
                {"当前清单号": "SJ0002", "客户名称": "测试客户"},
            ])

            def read_detail_info(self, page: FakePage) -> dict[str, str]:
                return next(self.detail_rows)

        page = FakePage()
        page.wait_for_load_state = lambda *args, **kwargs: None
        result = Bot().wait_for_detail_ready(page, {"试剂清单号": "SJ0002 加急"}, timeout_ms=1000)
        self.assertEqual(result["当前清单号"], "SJ0002")
        self.assertEqual(page.waits, [200])

    def test_detail_identity_mismatch_fails_closed(self) -> None:
        class Bot(ReagentPageMixin):
            def read_detail_info(self, page: FakePage) -> dict[str, str]:
                return {"当前清单号": "SJ0001"}

        page = FakePage()
        page.wait_for_load_state = lambda *args, **kwargs: None
        with self.assertRaisesRegex(RuntimeError, "expected SJ0002, observed SJ0001"):
            Bot().wait_for_detail_ready(page, {"试剂清单号": "SJ0002"}, timeout_ms=1)

    def test_detail_identity_missing_fails_closed(self) -> None:
        class Bot(ReagentPageMixin):
            def read_detail_info(self, page: FakePage) -> dict[str, str]:
                return {}

        page = FakePage()
        page.wait_for_load_state = lambda *args, **kwargs: None
        with self.assertRaisesRegex(RuntimeError, "observed <missing>"):
            Bot().wait_for_detail_ready(page, {"试剂清单号": "SJ0002"}, timeout_ms=1)

    def test_detail_identity_requires_list_number_on_selected_row(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no reagent list number"):
            ReagentPageMixin().wait_for_detail_ready(FakePage(), {}, timeout_ms=1)

    def test_todo_page_change_requires_positive_page_or_table_evidence(self) -> None:
        class Bot(ReagentPageMixin):
            def wait_for_table_ready(self, page: FakePage) -> None:
                return None

            def current_todo_page_number(self, page: FakePage) -> str:
                return ""

            def todo_table_signature(self, page: FakePage) -> str:
                return "unchanged"

        self.assertFalse(Bot().wait_for_todo_page_change(FakePage(), "", "unchanged"))

    def test_todo_page_change_accepts_changed_table_when_page_number_is_unavailable(self) -> None:
        class Bot(ReagentPageMixin):
            signatures = iter(["new page"])

            def wait_for_table_ready(self, page: FakePage) -> None:
                return None

            def current_todo_page_number(self, page: FakePage) -> str:
                return ""

            def todo_table_signature(self, page: FakePage) -> str:
                return next(self.signatures)

        self.assertTrue(Bot().wait_for_todo_page_change(FakePage(), "", "old page"))

    def test_todo_page_change_accepts_changed_page_number(self) -> None:
        class Bot(ReagentPageMixin):
            def wait_for_table_ready(self, page: FakePage) -> None:
                return None

            def current_todo_page_number(self, page: FakePage) -> str:
                return "2"

            def todo_table_signature(self, page: FakePage) -> str:
                return ""

        self.assertTrue(Bot().wait_for_todo_page_change(FakePage(), "1", ""))

    def test_auto_match_no_table_change_continues(self) -> None:
        class Bot(ReagentPageMixin):
            def capture_prompt_if_present(self, page: FakePage, screenshot_name: str) -> str:
                return ""

            def _auto_match_snapshot(self, page: FakePage) -> dict[str, object]:
                return {"rows": 4, "unmatched": 4, "signature": "-|-|-|-"}

        bot = Bot()
        with patch("reagent_page.time.time", side_effect=[0, 21]):
            result = bot._confirm_auto_match_result(
                FakePage(),
                {"rows": 4, "unmatched": 4, "signature": "-|-|-|-"},
            )

        self.assertTrue(result)

    def test_auto_match_error_prompt_blocks(self) -> None:
        class Bot(ReagentPageMixin):
            def capture_prompt_if_present(self, page: FakePage, screenshot_name: str) -> str:
                return "操作失败"

        bot = Bot()
        result = bot._confirm_auto_match_result(FakePage(), {"rows": 4, "unmatched": 4, "signature": "-|-|-|-"})

        self.assertFalse(result)

    def test_success_prompt_does_not_block(self) -> None:
        self.assertFalse(ReagentPageMixin._is_error_prompt("一键匹配成功"))
        self.assertTrue(ReagentPageMixin._is_error_prompt("一键匹配失败"))

    def test_no_dash_after_sort_is_treated_as_complete(self) -> None:
        class Bot(ReagentPageMixin):
            pagination_check_succeeded = False

            def wait_for_reagent_table_ready(self, page: FakePage) -> None:
                return None

            def goto_first_reagent_page(self, page: FakePage) -> bool:
                return True

            def sort_property_column_until_unmatched_visible(self, page: FakePage) -> bool:
                return False

            def current_page_unmatched_reagents(self, page: FakePage) -> list[dict[str, str]]:
                return []

        bot = Bot()
        result = bot.find_unmatched_reagents_across_all_pages(FakePage())

        self.assertEqual(result, [])
        self.assertTrue(bot.pagination_check_succeeded)

    def test_dash_after_failed_sort_blocks_auto_pass(self) -> None:
        class Bot(ReagentPageMixin):
            pagination_check_succeeded = False
            saved_unmatched: list[dict[str, str]] = []

            def wait_for_reagent_table_ready(self, page: FakePage) -> None:
                return None

            def goto_first_reagent_page(self, page: FakePage) -> bool:
                return True

            def sort_property_column_until_unmatched_visible(self, page: FakePage) -> bool:
                return False

            def current_page_unmatched_reagents(self, page: FakePage) -> list[dict[str, str]]:
                return [{"\u8bd5\u5242\u540d\u79f0": "\u5f85\u5904\u7406\u8bd5\u5242", "\u7269\u5316\u7279\u6027": "-"}]

            def save_auto_pass_blocking_unmatched(self, unmatched: list[dict[str, str]]) -> None:
                self.saved_unmatched = unmatched

        bot = Bot()
        result = bot.find_unmatched_reagents_across_all_pages(FakePage())

        self.assertEqual(len(result), 1)
        self.assertTrue(bot.pagination_check_succeeded)
        self.assertEqual(bot.saved_unmatched, result)

    def test_target_detail_not_found_does_not_open_first_task(self) -> None:
        class Bot(ReagentPageMixin):
            def enter_reagent_judgement_page(self, page: FakePage) -> None:
                return None

            def goto_first_todo_page(self, page: FakePage) -> bool:
                return True

            def current_todo_page_number(self, page: FakePage) -> str:
                return "1"

            def read_todo_tasks(self, page: FakePage) -> list[dict[str, str]]:
                return [{"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ0001"}]

            def click_next_todo_page(self, page: FakePage) -> tuple[bool, bool]:
                return False, True

        bot = Bot()
        self.assertFalse(bot.open_task_detail_by_list_number(FakePage(), "SJ9999"))

    def test_extract_list_number_ignores_urgent_suffix(self) -> None:
        self.assertEqual(ReagentPageMixin.extract_list_number("SJ202606170003 \u52a0\u6025"), "SJ202606170003")

    def test_preferred_reagent_page_size_defaults_to_20(self) -> None:
        class Bot(ReagentPageMixin):
            settings = {}

        self.assertEqual(Bot().preferred_reagent_page_size(), 20)

    def test_preferred_reagent_page_size_reads_settings(self) -> None:
        class Bot(ReagentPageMixin):
            settings = {"approval": {"reagent_page_size": 50}}

        self.assertEqual(Bot().preferred_reagent_page_size(), 50)


if __name__ == "__main__":
    unittest.main()
