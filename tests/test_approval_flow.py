from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from approval_flow import ApprovalFlowMixin  # noqa: E402
from reagent_page import ReagentPageMixin  # noqa: E402


class Bot(ApprovalFlowMixin, ReagentPageMixin):
    pass


class ApprovalFlowTodoLoopTest(unittest.TestCase):
    def test_todo_list_numbers_strip_urgent_suffix(self) -> None:
        bot = Bot()

        result = bot.todo_list_numbers(
            [
                {"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ202606170001 \u52a0\u6025"},
                {"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ202606170002"},
                {"\u8bd5\u5242\u6e05\u5355\u53f7": ""},
            ]
        )

        self.assertEqual(result, ["SJ202606170001", "SJ202606170002"])

    def test_next_unprocessed_list_number_skips_current_run_processed_items(self) -> None:
        bot = Bot()
        tasks = [
            {"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ202606170001"},
            {"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ202606170002"},
        ]

        result = bot.next_unprocessed_list_number(tasks, {"SJ202606170001"})

        self.assertEqual(result, "SJ202606170002")

    def test_next_unprocessed_list_number_returns_empty_when_all_visible_tasks_processed(self) -> None:
        bot = Bot()
        tasks = [{"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ202606170001"}]

        result = bot.next_unprocessed_list_number(tasks, {"SJ202606170001"})

        self.assertEqual(result, "")

    def test_max_process_all_todos_count_has_safe_default_and_minimum(self) -> None:
        bot = Bot()

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bot.max_process_all_todos_count(), 50)

        with patch.dict(os.environ, {"PROCESS_ALL_TODOS_MAX": "0"}, clear=True):
            self.assertEqual(bot.max_process_all_todos_count(), 1)

        with patch.dict(os.environ, {"PROCESS_ALL_TODOS_MAX": "abc"}, clear=True):
            self.assertEqual(bot.max_process_all_todos_count(), 50)


if __name__ == "__main__":
    unittest.main()
