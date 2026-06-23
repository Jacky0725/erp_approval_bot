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
from rule_engine import RuleEngine  # noqa: E402
from stage_logger import StageLogger  # noqa: E402


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

    def test_parallel_worker_count_defaults_to_three_and_clamps(self) -> None:
        bot = Bot()
        bot.settings = {}

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bot.parallel_worker_count(), 3)

        with patch.dict(os.environ, {"APPROVAL_PARALLEL_WORKERS": "0"}, clear=True):
            self.assertEqual(bot.parallel_worker_count(), 1)

        with patch.dict(os.environ, {"APPROVAL_PARALLEL_WORKERS": "99"}, clear=True):
            self.assertEqual(bot.parallel_worker_count(), 8)

        with patch.dict(os.environ, {"APPROVAL_PARALLEL_WORKERS": "abc"}, clear=True):
            self.assertEqual(bot.parallel_worker_count(), 3)

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

    def test_direct_business_rule_suggestion_allows_color_standard_without_search(self) -> None:
        bot = Bot()
        engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

        suggestion = bot.direct_business_rule_suggestion(
            {
                "\u5e8f\u53f7": "7",
                "\u8bd5\u5242\u540d\u79f0": "\u836f\u5178\u8272\u5ea6\u6807\u51c6\u54c1GY\u7cfb\u7528\u4e8e\u68c0\u6d4b\u989c\u8272\u5bc6\u5ea6",
                "CAS\u53f7": "-",
            },
            engine,
        )

        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertEqual(suggestion["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"], "\u666e\u901a\u7c7b")
        self.assertFalse(suggestion["\u9700\u4eba\u5de5\u590d\u6838"])
        self.assertEqual(suggestion["\u67e5\u8be2\u6765\u6e90"], "business_rule")

    def test_direct_business_rule_suggestion_allows_product_kit_without_search(self) -> None:
        bot = Bot()
        engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

        suggestion = bot.direct_business_rule_suggestion(
            {
                "\u5e8f\u53f7": "13",
                "\u8bd5\u5242\u540d\u79f0": "L47 Geneclean SPIN DNA \u7eaf\u5316\u8bd5\u5242\u76d2(\u79bb\u5fc3\u67f1\u5f0f)",
                "CAS\u53f7": "-",
            },
            engine,
        )

        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertEqual(suggestion["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"], "\u666e\u901a\u7c7b")
        self.assertFalse(suggestion["\u9700\u4eba\u5de5\u590d\u6838"])
        self.assertEqual(suggestion["\u67e5\u8be2\u6765\u6e90"], "business_rule")

    def test_direct_business_rule_suggestion_allows_buffer_strip_product_without_search(self) -> None:
        bot = Bot()
        engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

        suggestion = bot.direct_business_rule_suggestion(
            {
                "\u5e8f\u53f7": "17",
                "\u8bd5\u5242\u540d\u79f0": "BUFFER STRIPS SDS EXCELGEL",
                "CAS\u53f7": "-",
            },
            engine,
        )

        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertEqual(suggestion["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"], "\u666e\u901a\u7c7b")
        self.assertFalse(suggestion["\u9700\u4eba\u5de5\u590d\u6838"])
        self.assertEqual(suggestion["\u67e5\u8be2\u6765\u6e90"], "business_rule")

    def test_direct_business_rule_suggestion_allows_standard_solution_products_without_search(self) -> None:
        bot = Bot()
        engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

        for name in ("HYDRANAL WATER-STD 0.100mL", "TMB\u5e95\u7269", "\u7f8e\u56fd\u836f\u5178\u6bd4\u8272\u6db2\u5355\u8272-H", "PDMINITRAPG-10"):
            with self.subTest(name=name):
                suggestion = bot.direct_business_rule_suggestion(
                    {
                        "\u5e8f\u53f7": "17",
                        "\u8bd5\u5242\u540d\u79f0": name,
                        "CAS\u53f7": "-",
                    },
                    engine,
                )

                self.assertIsNotNone(suggestion)
                assert suggestion is not None
                self.assertEqual(suggestion["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"], "\u666e\u901a\u7c7b")
                self.assertFalse(suggestion["\u9700\u4eba\u5de5\u590d\u6838"])
                self.assertEqual(suggestion["\u67e5\u8be2\u6765\u6e90"], "business_rule")

    def test_parallel_processing_keeps_table_order_with_direct_rules(self) -> None:
        class OrderedBot(Bot):
            def __init__(self) -> None:
                self.settings = {"approval": {"parallel_workers": 3}}
                self.root_dir = ROOT_DIR
                self.stage_logger = StageLogger()

            def read_current_page_reagents(self, page: object) -> list[dict[str, str]]:
                return [
                    {"\u5e8f\u53f7": "1", "\u8bd5\u5242\u540d\u79f0": "needs-search-a", "CAS\u53f7": "-", "\u7269\u5316\u7279\u6027": "-"},
                    {"\u5e8f\u53f7": "2", "\u8bd5\u5242\u540d\u79f0": "\u836f\u5178\u8272\u5ea6\u6807\u51c6\u54c1", "CAS\u53f7": "-", "\u7269\u5316\u7279\u6027": "-"},
                    {"\u5e8f\u53f7": "3", "\u8bd5\u5242\u540d\u79f0": "needs-search-b", "CAS\u53f7": "-", "\u7269\u5316\u7279\u6027": "-"},
                ]

            def search_reagents_parallel(self, items: list[dict[str, object]]) -> dict[int, dict[str, object]]:
                return {
                    int(item["index"]): self.search_failure_result(item["reagent"], "test")
                    for item in items
                }

            def extract_and_classify_parallel(self, items: list[dict[str, object]], rule_engine: object) -> dict[int, tuple[dict[str, object], dict[str, object]]]:
                results = {}
                for item in items:
                    results[int(item["index"])] = (
                        {"evidence": ["test"], "confidence": 0.8},
                        {
                            "final_category": "\u666e\u901a\u7c7b",
                            "matched_categories": ["\u666e\u901a\u7c7b"],
                            "reason": "test",
                            "confidence": 0.8,
                            "need_manual_review": False,
                        },
                    )
                return results

            def add_manual_review_item_from_search_failure(self, *args: object, **kwargs: object) -> None:
                return None

        bot = OrderedBot()
        engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

        suggestions = bot.process_current_unmatched_reagent_page(
            object(),
            engine,
            None,
            {},
        )

        self.assertEqual([row["\u5e8f\u53f7"] for row in suggestions], ["1", "2", "3"])

    def test_multi_page_mode_processes_until_next_page_has_no_unmatched_rows(self) -> None:
        class MultiPageBot(Bot):
            def __init__(self) -> None:
                self.page_number = 1
                self.applied_pages: list[list[dict[str, object]]] = []
                self.partial_lengths: list[int] = []
                self.stage_logger = StageLogger()

            def goto_first_reagent_page(self, page: object) -> bool:
                self.page_number = 1
                return True

            def sort_property_column_until_unmatched_visible(self, page: object) -> bool:
                return True

            def current_reagent_page_number(self, page: object) -> str:
                return str(self.page_number)

            def process_current_unmatched_reagent_page(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
                return [{"\u5e8f\u53f7": str(self.page_number), "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": "\u666e\u901a\u7c7b"}]

            def apply_approval_write_mode(self, page: object, suggestions: list[dict[str, object]]) -> None:
                self.applied_pages.append(suggestions)

            def write_partial_approval_suggestions(self, suggestions: list[dict[str, object]]) -> None:
                self.partial_lengths.append(len(suggestions))

            def click_next_reagent_page(self, page: object) -> tuple[bool, bool]:
                self.page_number += 1
                return True, True

            def current_page_unmatched_reagents(self, page: object) -> list[dict[str, str]]:
                if self.page_number in {1, 2}:
                    return [{"\u5e8f\u53f7": str(self.page_number), "\u7269\u5316\u7279\u6027": "-"}]
                return []

        bot = MultiPageBot()

        result = bot.process_unmatched_reagent_pages(object(), None, None, {})

        self.assertEqual([row["\u5e8f\u53f7"] for row in result], ["1", "2"])
        self.assertEqual(len(bot.applied_pages), 2)
        self.assertEqual(bot.partial_lengths, [1, 2])

    def test_multi_page_mode_retries_failed_web_write_before_marking_handled(self) -> None:
        class RetryBot(Bot):
            def __init__(self) -> None:
                self.settings = {"approval": {"write_mode": "multi_page", "write_max_attempts": 2}}
                self.stage_logger = StageLogger()
                self.apply_calls = 0

            def goto_first_reagent_page(self, page: object) -> bool:
                return True

            def sort_property_column_until_unmatched_visible(self, page: object) -> bool:
                return True

            def current_reagent_page_number(self, page: object) -> str:
                return "1"

            def current_page_unmatched_reagents(self, page: object) -> list[dict[str, str]]:
                if self.apply_calls >= 2:
                    return []
                return [
                    {
                        "\u5e8f\u53f7": "1",
                        "\u8bd5\u5242\u540d\u79f0": "retry-me",
                        "CAS\u53f7": "-",
                        "\u89c4\u683c": "",
                        "\u89c4\u683c\u5355\u4f4d": "",
                        "\u7269\u5316\u7279\u6027": "-",
                    }
                ]

            def process_current_unmatched_reagent_page(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
                return [
                    {
                        "\u5e8f\u53f7": "1",
                        "\u8bd5\u5242\u540d\u79f0": "retry-me",
                        "CAS\u53f7": "-",
                        "\u89c4\u683c": "",
                        "\u89c4\u683c\u5355\u4f4d": "",
                        "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": "\u666e\u901a\u7c7b",
                    }
                ]

            def apply_approval_write_mode(self, page: object, suggestions: list[dict[str, object]]) -> dict[str, set[str]]:
                self.apply_calls += 1
                key = self.suggestion_work_key(suggestions[0])
                if self.apply_calls == 1:
                    return {"attempted": {key}, "handled": set(), "failed": {key}}
                return {"attempted": {key}, "handled": {key}, "failed": set()}

            def write_partial_approval_suggestions(self, suggestions: list[dict[str, object]]) -> None:
                return None

        bot = RetryBot()

        result = bot.process_unmatched_reagent_pages(object(), None, None, {})

        self.assertEqual(bot.apply_calls, 2)
        self.assertEqual([row["\u5e8f\u53f7"] for row in result], ["1", "1"])


if __name__ == "__main__":
    unittest.main()
