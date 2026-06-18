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

    def test_empty_extracted_evidence_does_not_force_manual_review(self) -> None:
        bot = Bot()

        result = bot._suggestion_needs_manual_review(
            search_result={"need_manual_review": False, "relevance_passed": True},
            name_result={"need_manual_review": False},
            extracted={"confidence": 0.0, "evidence": []},
            classification={"need_manual_review": False, "final_category": "\u666e\u901a\u7c7b"},
        )

        self.assertFalse(result)

    def test_high_confidence_candidate_allows_empty_evidence_when_not_manual_review(self) -> None:
        bot = Bot()
        bot.settings = {"approval": {"write_min_confidence": 0.8}}
        suggestion = {
            "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": "\u666e\u901a\u7c7b",
            "\u9700\u4eba\u5de5\u590d\u6838": False,
            "\u7f6e\u4fe1\u5ea6": 0.8,
            "\u8bc1\u636e": "",
        }

        with patch.dict(os.environ, {}, clear=True):
            result = bot.high_confidence_write_candidates([suggestion])

        self.assertEqual(result, [suggestion])


if __name__ == "__main__":
    unittest.main()
