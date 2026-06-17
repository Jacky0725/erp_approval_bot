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

            def read_todo_tasks(self, page: FakePage) -> list[dict[str, str]]:
                return [{"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ0001"}]

        bot = Bot()
        self.assertFalse(bot.open_task_detail_by_list_number(FakePage(), "SJ9999"))

    def test_extract_list_number_ignores_urgent_suffix(self) -> None:
        self.assertEqual(ReagentPageMixin.extract_list_number("SJ202606170003 \u52a0\u6025"), "SJ202606170003")


if __name__ == "__main__":
    unittest.main()
