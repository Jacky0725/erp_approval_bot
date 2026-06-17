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


if __name__ == "__main__":
    unittest.main()
