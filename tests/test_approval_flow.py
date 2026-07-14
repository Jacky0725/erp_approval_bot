from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from approval_flow import ApprovalFlowMixin  # noqa: E402
from approval_writer import ApprovalWriter  # noqa: E402
from excel_exports import ExcelExportsMixin  # noqa: E402
from reagent_memory import ReagentMemory  # noqa: E402
from reagent_page import ReagentPageMixin  # noqa: E402
from review_queue import ReviewQueueMixin  # noqa: E402
from rule_engine import RuleEngine  # noqa: E402
from stage_logger import StageLogger  # noqa: E402


class Bot(ApprovalFlowMixin, ReagentPageMixin):
    pass


class ApprovalFlowPropertyMatchTest(unittest.TestCase):
    def test_property_value_matches_duplicate_fixed_column_text(self) -> None:
        writer = ApprovalWriter()

        self.assertTrue(Bot.property_value_matches("\u666e\u901a\u7c7b \u666e\u901a\u7c7b", "\u666e\u901a\u7c7b", writer))

    def test_property_value_rejects_conflicting_duplicate_text(self) -> None:
        writer = ApprovalWriter()

        self.assertFalse(Bot.property_value_matches("\u666e\u901a\u7c7b \u6613\u71c3\u7c7b", "\u666e\u901a\u7c7b", writer))

    def test_local_only_successful_save_allows_auto_pass_precheck(self) -> None:
        bot = Bot()
        bot.save_results = [{"name": "local_approval_suggestions", "success": True, "detail": "xlsx"}]

        self.assertTrue(bot.all_save_operations_successful())

    def test_no_save_result_still_blocks_auto_pass_precheck(self) -> None:
        bot = Bot()
        bot.save_results = []

        self.assertFalse(bot.all_save_operations_successful())

    def test_auto_pass_disabled_skips_page_precheck(self) -> None:
        class AutoPassBot(Bot):
            def read_detail_info(self, page: object) -> dict[str, str]:
                raise AssertionError("AUTO_PASS=false should not read page detail info")

            def find_unmatched_reagents_across_all_pages(self, page: object) -> list[dict[str, str]]:
                raise AssertionError("AUTO_PASS=false should not inspect reagent pages")

        with patch.dict(os.environ, {"AUTO_PASS": "false"}, clear=False):
            AutoPassBot().try_auto_pass_current_task(object())

    def test_auto_pass_page_precheck_failure_blocks_without_raising(self) -> None:
        class AutoPassBot(Bot):
            def __init__(self) -> None:
                self.auto_match_succeeded = True
                self.save_results = [{"name": "reagent_save_1", "success": True, "detail": ""}]

            def read_detail_info(self, page: object) -> dict[str, str]:
                return {"\u5f53\u524d\u6e05\u5355\u53f7": "SJ202607020001"}

            def find_unmatched_reagents_across_all_pages(self, page: object) -> list[dict[str, str]]:
                raise RuntimeError("header missing")

            def current_list_has_manual_review_item(self, list_number: str) -> tuple[bool, str]:
                return False, ""

            def click_top_approve_button(self, page: object) -> None:
                raise AssertionError("Auto-pass should be blocked when page verification fails")

        with patch.dict(os.environ, {"AUTO_PASS": "true"}, clear=False):
            AutoPassBot().try_auto_pass_current_task(object())


class ApprovalSuggestionExportTest(unittest.TestCase):
    def test_save_outputs_keep_latest_list_specific_and_aggregate_files(self) -> None:
        class ExportBot(ApprovalFlowMixin, ExcelExportsMixin):
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            bot = ExportBot()
            bot.root_dir = Path(temp_dir)
            bot.settings = {"paths": {"audit_log_dir": "logs"}}
            bot._current_detail_info = {"当前清单号": "SJ202606300009"}

            saved_paths = bot.save_approval_suggestions_outputs(
                [
                    {
                        "序号": "3",
                        "试剂名称": "测试试剂",
                        "CAS号": "-",
                        "最终建议类别": "普通类",
                    }
                ]
            )

            log_dir = Path(temp_dir) / "logs"
            self.assertEqual(
                {path.name for path in saved_paths},
                {
                    "approval_suggestions.xlsx",
                    "approval_suggestions_SJ202606300009.xlsx",
                    "approval_suggestions_all.xlsx",
                },
            )
            aggregate = pd.read_excel(log_dir / "approval_suggestions_all.xlsx")
            self.assertEqual(aggregate.loc[0, "试剂清单号"], "SJ202606300009")

            bot._current_detail_info = {"当前清单号": "SJ202606300010"}
            bot.save_approval_suggestions_outputs(
                [
                    {
                        "序号": "1",
                        "试剂名称": "第二清单试剂",
                        "CAS号": "-",
                        "最终建议类别": "普通类",
                    }
                ]
            )
            aggregate = pd.read_excel(log_dir / "approval_suggestions_all.xlsx")
            self.assertEqual(set(aggregate["试剂清单号"].astype(str)), {"SJ202606300009", "SJ202606300010"})


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

    def test_selected_todos_are_processed_even_when_not_on_current_page(self) -> None:
        class SelectedBot(Bot):
            def __init__(self) -> None:
                self.target_list_numbers = ["SJ202606170001", "SJ202606170002"]
                self.processed: list[str] = []

            def enter_reagent_judgement_page(self, page: object) -> None:
                return None

            def generate_approval_suggestions(self, page: object) -> None:
                self.processed.append(self.target_list_number)

        bot = SelectedBot()

        bot.generate_selected_todo_approval_suggestions(object())

        self.assertEqual(bot.processed, ["SJ202606170001", "SJ202606170002"])

    def test_all_todos_uses_all_pages_snapshot(self) -> None:
        class AllTodoBot(Bot):
            def __init__(self) -> None:
                self.processed: list[str] = []

            def enter_reagent_judgement_page(self, page: object) -> None:
                return None

            def read_all_todo_tasks(self, page: object) -> list[dict[str, str]]:
                return [
                    {"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ202606170001"},
                    {"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ202606170002"},
                ]

            def generate_approval_suggestions(self, page: object) -> None:
                self.processed.append(self.target_list_number)

        bot = AllTodoBot()

        with patch.dict(os.environ, {"PROCESS_ALL_TODOS_MAX": "50"}, clear=True):
            bot.generate_all_todo_approval_suggestions(object())

        self.assertEqual(bot.processed, ["SJ202606170001", "SJ202606170002"])

    def test_scheduled_all_todos_skips_pending_manual_review_lists(self) -> None:
        class ScheduledTodoBot(Bot):
            def __init__(self) -> None:
                self.processed: list[str] = []

            def enter_reagent_judgement_page(self, page: object) -> None:
                return None

            def read_all_todo_tasks(self, page: object) -> list[dict[str, str]]:
                return [
                    {"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ202606170001"},
                    {"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ202606170002"},
                ]

            def current_list_has_manual_review_item(self, list_number: str) -> tuple[bool, str]:
                return list_number == "SJ202606170001", "pending manual review"

            def generate_approval_suggestions(self, page: object) -> None:
                self.processed.append(self.target_list_number)

        bot = ScheduledTodoBot()

        with patch.dict(
            os.environ,
            {
                "PROCESS_ALL_TODOS_MAX": "50",
                "SCHEDULED_RUN": "true",
                "SCHEDULED_SKIP_MANUAL_REVIEW_LISTS": "true",
            },
            clear=True,
        ):
            bot.generate_all_todo_approval_suggestions(object())

        self.assertEqual(bot.processed, ["SJ202606170002"])

    def test_manual_all_todos_does_not_apply_scheduled_review_filter(self) -> None:
        class ManualTodoBot(Bot):
            def __init__(self) -> None:
                self.processed: list[str] = []

            def enter_reagent_judgement_page(self, page: object) -> None:
                return None

            def read_all_todo_tasks(self, page: object) -> list[dict[str, str]]:
                return [{"\u8bd5\u5242\u6e05\u5355\u53f7": "SJ202606170001"}]

            def current_list_has_manual_review_item(self, list_number: str) -> tuple[bool, str]:
                raise AssertionError("Manual runs should not apply scheduled skip filtering")

            def generate_approval_suggestions(self, page: object) -> None:
                self.processed.append(self.target_list_number)

        bot = ManualTodoBot()

        with patch.dict(os.environ, {"PROCESS_ALL_TODOS_MAX": "50"}, clear=True):
            bot.generate_all_todo_approval_suggestions(object())

        self.assertEqual(bot.processed, ["SJ202606170001"])

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

    def test_high_confidence_candidate_maps_rule_category_to_erp_property(self) -> None:
        bot = Bot()
        bot.settings = {
            "approval": {"write_min_confidence": 0.7},
            "reagent": {
                "physicochemical_property_options": ["\u6613\u71c3\u7c7b"],
                "physicochemical_property_aliases": {"\u6613\u71c3\u6db2\u4f53": ["\u6613\u71c3\u7c7b"]},
            },
        }
        suggestion = {
            "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": "\u6613\u71c3\u6db2\u4f53",
            "\u9700\u4eba\u5de5\u590d\u6838": False,
            "\u7f6e\u4fe1\u5ea6": 0.76,
        }

        with patch.dict(os.environ, {}, clear=True):
            result = bot.high_confidence_write_candidates([suggestion])

        self.assertEqual(result[0]["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"], "\u6613\u71c3\u7c7b")
        self.assertEqual(result[0]["\u89c4\u5219\u5224\u5b9a\u7c7b\u522b"], "\u6613\u71c3\u6db2\u4f53")

    def test_not_recommended_category_maps_to_reject_property_for_write(self) -> None:
        bot = Bot()
        bot.settings = {
            "approval": {"write_min_confidence": 0.7},
            "reagent": {
                "physicochemical_property_options": ["\u62d2\u6536\u7c7b"],
                "physicochemical_property_aliases": {
                    "\u4e0d\u5efa\u8bae\u63a5\u6536\u7c7b": ["\u62d2\u6536\u7c7b"],
                },
            },
        }
        suggestion = {
            "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": "\u4e0d\u5efa\u8bae\u63a5\u6536\u7c7b",
            "\u9700\u4eba\u5de5\u590d\u6838": False,
            "\u7f6e\u4fe1\u5ea6": 0.9,
        }

        with patch.dict(os.environ, {}, clear=True):
            result = bot.high_confidence_write_candidates([suggestion])

        self.assertEqual(result[0]["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"], "\u62d2\u6536\u7c7b")
        self.assertEqual(result[0]["\u89c4\u5219\u5224\u5b9a\u7c7b\u522b"], "\u4e0d\u5efa\u8bae\u63a5\u6536\u7c7b")

    def test_write_failure_recovery_reopens_target_detail_without_old_page_number(self) -> None:
        class RecoveryBot(Bot):
            def __init__(self) -> None:
                self._current_detail_info = {"\u5f53\u524d\u6e05\u5355\u53f7": "SJ202607020001"}
                self.reopened: list[str] = []

            def current_reagent_page_number(self, page: object) -> str:
                return "7"

            def goto_reagent_page_number(self, page: object, target_page: object) -> bool:
                raise AssertionError("write recovery should not restore an old reagent page number")

            def read_detail_info(self, page: object) -> dict[str, str]:
                return {}

            def read_current_page_reagents(self, page: object) -> list[dict[str, str]]:
                return []

            def open_task_detail_by_list_number(self, page: object, target_list_number: str) -> bool:
                self.reopened.append(target_list_number)
                return True

            def wait_for_reagent_table_ready(self, page: object) -> None:
                return None

        class FakePage:
            def reload(self, **kwargs: object) -> None:
                return None

            def wait_for_timeout(self, timeout: int) -> None:
                return None

        bot = RecoveryBot()

        self.assertTrue(bot.recover_reagent_detail_page_after_write_failure(FakePage(), "unit test"))
        self.assertEqual(bot.reopened, ["SJ202607020001"])

    def test_write_failure_recovery_tries_browser_back_after_reopen_failure(self) -> None:
        class RecoveryBot(Bot):
            def __init__(self) -> None:
                self._current_detail_info = {"\u5f53\u524d\u6e05\u5355\u53f7": "SJ202607020002"}
                self.open_attempts = 0

            def force_close_editing_overlays(self, page: object) -> None:
                return None

            def current_page_is_target_detail(self, page: object, target_list_number: str = "") -> bool:
                return False

            def open_task_detail_by_list_number(self, page: object, target_list_number: str) -> bool:
                self.open_attempts += 1
                if self.open_attempts == 1:
                    raise RuntimeError("menu temporarily hidden")
                return True

            def wait_for_reagent_table_ready(self, page: object) -> None:
                return None

            def read_detail_info(self, page: object) -> dict[str, str]:
                return {"\u5f53\u524d\u6e05\u5355\u53f7": "SJ202607020002"}

        class FakePage:
            def __init__(self) -> None:
                self.back_calls = 0

            def reload(self, **kwargs: object) -> None:
                return None

            def wait_for_timeout(self, timeout: int) -> None:
                return None

            def go_back(self, **kwargs: object) -> None:
                self.back_calls += 1

        page = FakePage()
        bot = RecoveryBot()

        self.assertTrue(bot.recover_reagent_detail_page_after_write_failure(page, "unit test"))
        self.assertEqual(bot.open_attempts, 2)
        self.assertEqual(page.back_calls, 1)

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

    def test_direct_business_rule_suggestion_maps_ambiguous_sulfuric_or_phosphoric_acid(self) -> None:
        bot = Bot()
        engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

        suggestion = bot.direct_business_rule_suggestion(
            {
                "\u5e8f\u53f7": "87",
                "\u8bd5\u5242\u540d\u79f0": "\u7591\u4f3c\u786b\u9178\u6216\u78f7\u9178",
                "CAS\u53f7": "7664-93-9/7664-38-2",
            },
            engine,
        )

        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertEqual(suggestion["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"], "\u5e38\u89c4\u9178")
        self.assertEqual(suggestion["\u7f6e\u4fe1\u5ea6"], 1.0)
        self.assertFalse(suggestion["\u9700\u4eba\u5de5\u590d\u6838"])
        self.assertIn("\u786b\u9178\u6216\u78f7\u9178", suggestion["\u89c4\u5219\u539f\u56e0"])

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

    def test_direct_business_rule_suggestion_allows_business_normal_keywords(self) -> None:
        bot = Bot()
        engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

        for name in (
            "\u94c5ICP\u6807\u51c6\u6eb6\u6db2",
            "\u94cdICP\u6807\u51c6\u6db2",
            "\u94c5\u6807\u51c6\u54c1",
            "\u6c2f\u5316\u94cd\u6807\u5b9a\u6eb6\u6db2",
            "\u5432\u54da\u6807\u51c6\u6eb6\u6db2",
            "\u86cb\u767d\u514d\u75ab\u6297\u4f53\u8bd5\u5242",
            "\u672a\u77e5\u7ec6\u80de\u57f9\u517b\u6db2",
            "\u4e00\u6b21\u6027\u75c5\u6bd2\u91c7\u6837\u7ba1",
            "\u75c5\u6bd2\u4fdd\u5b58\u6db2",
            "\u82cf\u6728\u7d20\u67d3\u8272\u6db2",
            "\u8bd5\u5242\uff08\u672a\u77e5\uff09",
            "\u5361\u9a6c\u897f\u5e73\u836f\u7269\u5bf9\u7167\u54c1",
            "\u76d0\u9178\u6587\u62c9\u6cd5\u8f9b",
        ):
            with self.subTest(name=name):
                suggestion = bot.direct_business_rule_suggestion(
                    {
                        "\u5e8f\u53f7": "27",
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
            def __init__(self, root_dir: Path) -> None:
                self.settings = {
                    "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
                    "approval": {"parallel_workers": 3},
                }
                self.root_dir = root_dir
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

            def add_manual_review_item_from_suggestion(self, *args: object, **kwargs: object) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            bot = OrderedBot(Path(tmp))
            engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

            suggestions = bot.process_current_unmatched_reagent_page(
                object(),
                engine,
                None,
                {},
            )

        self.assertEqual([row["\u5e8f\u53f7"] for row in suggestions], ["1", "2", "3"])

    def test_classification_manual_review_is_written_to_review_queue(self) -> None:
        class ManualReviewBot(Bot, ReviewQueueMixin, ExcelExportsMixin):
            def __init__(self, root_dir: Path) -> None:
                self.settings = {
                    "paths": {"review_queue_excel": "data/review_queue.xlsx"},
                    "approval": {"parallel_workers": 1},
                }
                self.root_dir = root_dir
                self.stage_logger = StageLogger()
                self._current_detail_info = {"\u5f53\u524d\u6e05\u5355\u53f7": "SJ1", "\u7533\u8bf7\u4eba": "tester"}

            def read_current_page_reagents(self, page: object) -> list[dict[str, str]]:
                return [
                    {
                        "\u5e8f\u53f7": "1",
                        "\u8bd5\u5242\u540d\u79f0": "needs-manual",
                        "CAS\u53f7": "-",
                        "\u89c4\u683c": "10",
                        "\u89c4\u683c\u5355\u4f4d": "g",
                        "\u7269\u5316\u7279\u6027": "-",
                    }
                ]

            def search_reagents_parallel(self, items: list[dict[str, object]]) -> dict[int, dict[str, object]]:
                item = items[0]
                reagent = item["reagent"]
                return {
                    int(item["index"]): {
                        "name": "needs-manual",
                        "cas": "",
                        "source": "test",
                        "url": "",
                        "raw_text": "source text",
                        "hazard_keywords": [],
                        "need_manual_review": False,
                        "name_normalization": {
                            "raw_name": reagent["\u8bd5\u5242\u540d\u79f0"],
                            "cleaned_name": "needs-manual",
                            "standard_name": "needs-manual",
                            "cas": "",
                            "confidence": 0.9,
                            "need_manual_review": False,
                        },
                        "relevance_passed": True,
                    }
                }

            def extract_and_classify_parallel(self, items: list[dict[str, object]], rule_engine: object) -> dict[int, tuple[dict[str, object], dict[str, object]]]:
                item = items[0]
                return {
                    int(item["index"]): (
                        {"evidence": ["uncertain evidence"], "confidence": 0.2},
                        {
                            "final_category": "",
                            "matched_categories": [],
                            "reason": "cannot classify from rule evidence",
                            "confidence": 0.2,
                            "need_manual_review": True,
                        },
                    )
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bot = ManualReviewBot(root)
            engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

            suggestions = bot.process_current_unmatched_reagent_page(object(), engine, None, {})
            queue = __import__("pandas").read_excel(root / "data" / "review_queue.xlsx", dtype=str).fillna("")

        self.assertTrue(suggestions[0]["\u9700\u4eba\u5de5\u590d\u6838"])
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue["\u8bd5\u5242\u6e05\u5355\u53f7"].tolist(), ["SJ1"])

    def test_reagent_memory_match_skips_chemical_search(self) -> None:
        class MemoryBot(Bot):
            def __init__(self, root_dir: Path) -> None:
                self.settings = {
                    "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
                    "memory": {"min_confidence": 0.8},
                    "approval": {"parallel_workers": 3},
                }
                self.root_dir = root_dir
                self.stage_logger = StageLogger()

            def read_current_page_reagents(self, page: object) -> list[dict[str, str]]:
                return [
                    {
                        "\u5e8f\u53f7": "8",
                        "\u8bd5\u5242\u540d\u79f0": "\u805a\u4e59\u4e8c\u9187",
                        "CAS\u53f7": "25322-68-3",
                        "\u89c4\u683c": "500",
                        "\u89c4\u683c\u5355\u4f4d": "g",
                        "\u7269\u5316\u7279\u6027": "-",
                    }
                ]

            def search_reagents_parallel(self, items: list[dict[str, object]]) -> dict[int, dict[str, object]]:
                raise AssertionError("chemical search should be skipped when reagent memory matches")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings(
                {
                    "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
                    "memory": {"min_confidence": 0.8},
                },
                root,
            )
            memory.add_record(
                raw_name="\u805a\u4e59\u4e8c\u9187",
                standard_name="\u805a\u4e59\u4e8c\u9187",
                cleaned_name="\u805a\u4e59\u4e8c\u9187",
                cas="25322-68-3",
                final_category="\u666e\u901a\u7c7b",
                confidence=0.95,
                reason="unit test memory",
            )
            bot = MemoryBot(root)
            engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

            suggestions = bot.process_current_unmatched_reagent_page(object(), engine, None, {})

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["\u67e5\u8be2\u6765\u6e90"], "reagent_memory")
        self.assertEqual(suggestions[0]["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"], "\u666e\u901a\u7c7b")
        self.assertFalse(suggestions[0]["\u9700\u4eba\u5de5\u590d\u6838"])

    def test_reagent_memory_manual_review_match_is_written_to_review_queue(self) -> None:
        class ManualMemoryBot(Bot, ReviewQueueMixin, ExcelExportsMixin):
            def __init__(self, root_dir: Path) -> None:
                self.settings = {
                    "paths": {
                        "reagent_memory_sqlite": "data/memory.sqlite",
                        "review_queue_excel": "data/review_queue.xlsx",
                    },
                    "memory": {"min_confidence": 0.8},
                    "approval": {"parallel_workers": 3},
                }
                self.root_dir = root_dir
                self.stage_logger = StageLogger()
                self._current_detail_info = {"\u5f53\u524d\u6e05\u5355\u53f7": "SJ-MEM", "\u7533\u8bf7\u4eba": "tester"}

            def read_current_page_reagents(self, page: object) -> list[dict[str, str]]:
                return [
                    {
                        "\u5e8f\u53f7": "3",
                        "\u8bd5\u5242\u540d\u79f0": "MID COPPER T0425",
                        "CAS\u53f7": "-",
                        "\u89c4\u683c": "4",
                        "\u89c4\u683c\u5355\u4f4d": "L",
                        "\u7269\u5316\u7279\u6027": "-",
                    }
                ]

            def search_reagents_parallel(self, items: list[dict[str, object]]) -> dict[int, dict[str, object]]:
                raise AssertionError("chemical search should be skipped when reagent memory matches")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings(
                {
                    "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
                    "memory": {"min_confidence": 0.8},
                },
                root,
            )
            memory.add_record(
                raw_name="MID COPPER T0425",
                standard_name="\u94dc",
                cleaned_name="MID COPPER T",
                cas="-",
                final_category="\u666e\u901a\u7c7b",
                confidence=1.0,
                reason="vendor code needs manual confirmation",
                need_manual_review=True,
                manual_verified=True,
                track_conflicts=False,
            )
            row = memory.find_any(raw_name="MID COPPER T0425")
            assert row is not None
            memory.update_record(row["id"], {"reusable": True, "need_manual_review": False})

            bot = ManualMemoryBot(root)
            original = bot.reagent_memory_suggestion

            def manual_review_suggestion(reagent, memory_row, name_result=None):  # type: ignore[no-untyped-def]
                suggestion = original(reagent, memory_row, name_result)
                suggestion["\u9700\u4eba\u5de5\u590d\u6838"] = True
                suggestion["\u540d\u79f0\u9700\u4eba\u5de5\u590d\u6838"] = True
                suggestion["\u540d\u79f0\u6807\u51c6\u5316\u539f\u56e0"] = "low-confidence vendor code"
                return suggestion

            bot.reagent_memory_suggestion = manual_review_suggestion  # type: ignore[method-assign]
            engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

            suggestions = bot.process_current_unmatched_reagent_page(object(), engine, None, {})
            queue = __import__("pandas").read_excel(root / "data" / "review_queue.xlsx", dtype=str).fillna("")

        self.assertTrue(suggestions[0]["\u9700\u4eba\u5de5\u590d\u6838"])
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue["\u8bd5\u5242\u6e05\u5355\u53f7"].tolist(), ["SJ-MEM"])
        self.assertEqual(queue["\u5e8f\u53f7"].tolist(), ["3"])
        self.assertEqual(queue["chemical_name"].tolist(), ["MID COPPER T0425"])

    def test_unknown_packaging_name_forces_unknown_class_before_memory(self) -> None:
        class UnknownNameBot(Bot):
            def __init__(self, root_dir: Path) -> None:
                self.settings = {
                    "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
                    "memory": {"min_confidence": 0.8},
                    "approval": {"parallel_workers": 3},
                }
                self.root_dir = root_dir
                self.stage_logger = StageLogger()
                self.review_items: list[tuple[dict[str, str], str]] = []

            def read_current_page_reagents(self, page: object) -> list[dict[str, str]]:
                return [
                    {
                        "\u5e8f\u53f7": "6",
                        "\u8bd5\u5242\u540d\u79f0": "\u672a\u77e5\u836f\u54c1\uff08\u767d\u74f6\u7ea2\u76d6\uff09",
                        "CAS\u53f7": "-",
                        "\u89c4\u683c": "10",
                        "\u89c4\u683c\u5355\u4f4d": "mL",
                        "\u7269\u5316\u7279\u6027": "-",
                    }
                ]

            def add_manual_review_item(self, reagent, name_result, *, reason: str) -> None:  # type: ignore[no-untyped-def]
                self.review_items.append((reagent, reason))

            def search_reagents_parallel(self, items: list[dict[str, object]]) -> dict[int, dict[str, object]]:
                raise AssertionError("unknown packaging names should not be searched")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings(
                {
                    "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
                    "memory": {"min_confidence": 0.8},
                },
                root,
            )
            memory.add_record(
                raw_name="\u672a\u77e5\u836f\u54c1\uff08\u767d\u74f6\u7ea2\u76d6\uff09",
                standard_name="\u672a\u77e5\u836f\u54c1\uff08\u767d\u74f6\u7ea2\u76d6\uff09",
                cleaned_name="\u672a\u77e5\u836f\u54c1\uff08\u767d\u74f6\u7ea2\u76d6\uff09",
                cas="-",
                final_category="\u5f3a\u53cd\u5e94",
                confidence=0.95,
                reason="old bad memory",
            )
            bot = UnknownNameBot(root)
            engine = RuleEngine.from_excel(ROOT_DIR / "config" / "rules.xlsx")

            suggestions = bot.process_current_unmatched_reagent_page(object(), engine, None, {})
            stored = memory.find_any(raw_name="\u672a\u77e5\u836f\u54c1\uff08\u767d\u74f6\u7ea2\u76d6\uff09")

        self.assertEqual(suggestions[0]["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"], "\u672a\u77e5\u7c7b")
        self.assertFalse(suggestions[0]["\u9700\u4eba\u5de5\u590d\u6838"])
        self.assertEqual(len(bot.review_items), 0)
        self.assertEqual(stored["final_category"], "\u672a\u77e5\u7c7b")
        self.assertEqual(stored["reusable"], 1)
        self.assertEqual(stored["conflict"], 0)

    def test_memory_url_cas_overrides_and_drops_conflicting_erp_cas(self) -> None:
        bot = Bot()
        suggestion = bot.reagent_memory_suggestion(
            {
                "\u5e8f\u53f7": "226",
                "\u8bd5\u5242\u540d\u79f0": "\u5421\u5511-5-\u787c\u9178",
                "CAS\u53f7": "724710-02-5",
                "\u89c4\u683c": "1",
                "\u89c4\u683c\u5355\u4f4d": "g",
                "\u8bd5\u5242\u6570\u91cf": "1",
            },
            {
                "raw_name": "1H-\u5421\u5511-5-\u787c\u9178",
                "cleaned_name": "1H-\u5421\u5511-5-\u787c\u9178",
                "standard_name": "1H-\u5421\u5511-5-\u787c\u9178",
                "cas": "376584-63-3",
                "final_category": "\u666e\u901a\u7c7b",
                "confidence": 0.9,
                "reason": "unit memory",
                "url": "https://www.chemsrc.com/en/cas/376584-63-3_727612.html",
            },
        )

        self.assertEqual(suggestion["CAS\u53f7"], "")
        self.assertEqual(suggestion["\u67e5\u8be2\u6765\u6e90"], "reagent_memory")
        self.assertEqual(suggestion["\u67e5\u8be2URL"], "https://www.chemsrc.com/en/cas/376584-63-3_727612.html")
        self.assertIn("724710-02-5", suggestion["\u89c4\u5219\u539f\u56e0"])
        self.assertIn("376584-63-3", suggestion["\u89c4\u5219\u539f\u56e0"])

    def test_unsafe_pharmacopoeia_memory_match_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings(
                {
                    "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
                    "memory": {"min_confidence": 0.8},
                },
                root,
            )
            memory.add_record(
                raw_name="3,4-\u4e8c\u6eb4\u9a6c\u6765\u9170\u4e9a\u80fa",
                standard_name="\u836f\u5178\u8272\u5ea6\u6807\u51c6\u54c1",
                cleaned_name="\u836f\u5178\u8272\u5ea6\u6807\u51c6\u54c1",
                cas="-",
                final_category="\u666e\u901a\u7c7b",
                confidence=0.95,
                reason="\u836f\u5178\u8272\u5ea6\u6807\u51c6\u54c1\u4e1a\u52a1\u89c4\u5219",
            )
            row = memory.lookup(raw_name="3,4-\u4e8c\u6eb4\u9a6c\u6765\u9170\u4e9a\u80fa")
            bot = Bot()

            self.assertFalse(
                bot.memory_match_is_safe(
                    {"\u8bd5\u5242\u540d\u79f0": "3,4-\u4e8c\u6eb4\u9a6c\u6765\u9170\u4e9a\u80fa"},
                    row,
                )
            )
            bot.disable_unsafe_memory_match(
                memory,
                row,
                {"\u8bd5\u5242\u540d\u79f0": "3,4-\u4e8c\u6eb4\u9a6c\u6765\u9170\u4e9a\u80fa"},
            )

            self.assertIsNone(memory.lookup(raw_name="3,4-\u4e8c\u6eb4\u9a6c\u6765\u9170\u4e9a\u80fa"))
            disabled = memory.find_any(raw_name="3,4-\u4e8c\u6eb4\u9a6c\u6765\u9170\u4e9a\u80fa")
            self.assertEqual(disabled["reusable"], 0)
            self.assertEqual(disabled["conflict"], 1)

    def test_halogen_memory_match_cannot_force_ordinary_class(self) -> None:
        bot = Bot()
        row = {
            "raw_name": "3,4-\u4e8c\u6eb4\u9a6c\u6765\u9170\u4e9a\u80fa",
            "cleaned_name": "3,4-\u4e8c\u6eb4\u9a6c\u6765\u9170\u4e9a\u80fa",
            "standard_name": "3,4-\u4e8c\u6eb4\u9a6c\u6765\u9170\u4e9a\u80fa",
            "final_category": "\u666e\u901a\u7c7b",
        }

        self.assertFalse(
            bot.memory_match_is_safe(
                {"\u8bd5\u5242\u540d\u79f0": "3,4-\u4e8c\u6eb4\u9a6c\u6765\u9170\u4e9a\u80fa"},
                row,
            )
        )

    def test_ordinary_memory_match_is_rechecked_against_current_rules(self) -> None:
        bot = Bot()
        row = {
            "raw_name": "BIOTIN-PEG5-AZIDE",
            "cleaned_name": "BIOTIN-PEG5-AZIDE",
            "standard_name": "BIOTIN-PEG5-AZIDE",
            "final_category": "\u666e\u901a\u7c7b",
        }
        engine = RuleEngine.from_structured_excel(ROOT_DIR / "config" / "rules_structured.xlsx")

        self.assertFalse(
            bot.memory_match_is_safe(
                {"\u8bd5\u5242\u540d\u79f0": "BIOTIN-PEG5-AZIDE"},
                row,
                rule_engine=engine,
            )
        )

    def test_reagent_memory_source_is_not_remembered_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = ReagentMemory.from_settings(
                {"paths": {"reagent_memory_sqlite": "data/memory.sqlite"}},
                root,
            )
            bot = Bot()
            bot.settings = {}

            saved = bot.remember_erp_suggestion(
                memory,
                {
                    "\u8bd5\u5242\u540d\u79f0": "\u6d4b\u8bd5\u8bd5\u5242",
                    "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": "\u666e\u901a\u7c7b",
                    "\u7f6e\u4fe1\u5ea6": 0.95,
                    "\u9700\u4eba\u5de5\u590d\u6838": False,
                    "\u67e5\u8be2\u6765\u6e90": "reagent_memory",
                },
            )

            self.assertFalse(saved)
            self.assertIsNone(memory.lookup(raw_name="\u6d4b\u8bd5\u8bd5\u5242"))

    def test_write_failure_for_reusable_suggestion_skips_manual_review_queue(self) -> None:
        class WriteFailureBot(Bot):
            def __init__(self, root_dir: Path) -> None:
                self.root_dir = root_dir
                self.settings = {
                    "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
                    "approval": {"write_min_confidence": 0.8},
                    "reagent": {"physicochemical_property_options": ["普通类"]},
                }
                self.review_items: list[tuple[dict[str, str], str]] = []

            def add_manual_review_item(self, reagent, name_result, *, reason: str) -> None:  # type: ignore[no-untyped-def]
                self.review_items.append((reagent, reason))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bot = WriteFailureBot(root)
            bot.add_manual_review_item_from_write_failure(
                {
                    "\u5e8f\u53f7": "1",
                    "\u8bd5\u5242\u540d\u79f0": "\u53ef\u4fe1\u8bd5\u5242",
                    "CAS\u53f7": "123-45-6",
                    "\u6807\u51c6\u5316\u540d\u79f0": "\u53ef\u4fe1\u8bd5\u5242",
                    "\u6e05\u6d17\u540e\u540d\u79f0": "\u53ef\u4fe1\u8bd5\u5242",
                    "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": "\u666e\u901a\u7c7b",
                    "\u7f6e\u4fe1\u5ea6": 0.95,
                    "\u9700\u4eba\u5de5\u590d\u6838": False,
                },
                "saved=False, row shows <empty>",
            )

            memory = ReagentMemory.from_settings(bot.settings, root)
            match = memory.lookup(cas="123-45-6")

        self.assertEqual(bot.review_items, [])
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["final_category"], "\u666e\u901a\u7c7b")
        self.assertEqual(match["reusable"], 1)

    def test_write_failure_for_manual_review_suggestion_still_queues_review(self) -> None:
        class WriteFailureBot(Bot):
            def __init__(self, root_dir: Path) -> None:
                self.root_dir = root_dir
                self.settings = {
                    "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
                    "approval": {"write_min_confidence": 0.8},
                    "reagent": {"physicochemical_property_options": ["普通类"]},
                }
                self.review_items: list[tuple[dict[str, str], str]] = []

            def add_manual_review_item(self, reagent, name_result, *, reason: str) -> None:  # type: ignore[no-untyped-def]
                self.review_items.append((reagent, reason))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bot = WriteFailureBot(root)
            bot.add_manual_review_item_from_write_failure(
                {
                    "\u5e8f\u53f7": "1",
                    "\u8bd5\u5242\u540d\u79f0": "\u4f4e\u7f6e\u4fe1\u8bd5\u5242",
                    "CAS\u53f7": "123-45-6",
                    "\u6807\u51c6\u5316\u540d\u79f0": "\u4f4e\u7f6e\u4fe1\u8bd5\u5242",
                    "\u6e05\u6d17\u540e\u540d\u79f0": "\u4f4e\u7f6e\u4fe1\u8bd5\u5242",
                    "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": "\u666e\u901a\u7c7b",
                    "\u7f6e\u4fe1\u5ea6": 0.5,
                    "\u9700\u4eba\u5de5\u590d\u6838": True,
                },
                "saved=False, row shows <empty>",
            )

        self.assertEqual(len(bot.review_items), 1)
        self.assertIn("\u7f51\u9875\u5199\u5165\u5931\u8d25", bot.review_items[0][1])

    def test_multi_page_mode_stops_when_sorted_current_page_has_no_unmatched_rows(self) -> None:
        class MultiPageBot(Bot):
            def __init__(self) -> None:
                self.page_number = 1
                self.applied_pages: list[list[dict[str, object]]] = []
                self.partial_lengths: list[int] = []
                self.next_clicks = 0
                self.stage_logger = StageLogger()

            def goto_first_reagent_page(self, page: object) -> bool:
                self.page_number = 1
                return True

            def sort_property_column_until_unmatched_visible(self, page: object) -> bool:
                return True

            def stabilize_reagent_detail_after_write_failure(self, page: object) -> bool:
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
                self.next_clicks += 1
                if self.page_number >= 3:
                    return False, True
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
        self.assertEqual(bot.next_clicks, 2)

    def test_multi_page_mode_advances_after_current_page_write_attempt(self) -> None:
        class RetryBot(Bot):
            def __init__(self) -> None:
                self.settings = {"approval": {"write_mode": "multi_page", "write_max_attempts": 2}}
                self.stage_logger = StageLogger()
                self.apply_calls = 0

            def goto_first_reagent_page(self, page: object) -> bool:
                return True

            def sort_property_column_until_unmatched_visible(self, page: object) -> bool:
                return True

            def stabilize_reagent_detail_after_write_failure(self, page: object) -> bool:
                return True

            def current_reagent_page_number(self, page: object) -> str:
                return "1"

            def current_page_unmatched_reagents(self, page: object) -> list[dict[str, str]]:
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

            def click_next_reagent_page(self, page: object) -> tuple[bool, bool]:
                return False, True

        bot = RetryBot()

        result = bot.process_unmatched_reagent_pages(object(), None, None, {})

        self.assertEqual(bot.apply_calls, 2)
        self.assertEqual([row["\u5e8f\u53f7"] for row in result], ["1"])

    def test_multi_page_mode_retries_failed_item_even_if_returned_as_handled(self) -> None:
        class RetryBot(Bot):
            def __init__(self) -> None:
                self.settings = {"approval": {"write_mode": "multi_page", "write_max_attempts": 2}}
                self.stage_logger = StageLogger()
                self.apply_calls = 0

            def goto_first_reagent_page(self, page: object) -> bool:
                return True

            def sort_property_column_until_unmatched_visible(self, page: object) -> bool:
                return True

            def stabilize_reagent_detail_after_write_failure(self, page: object) -> bool:
                return True

            def current_reagent_page_number(self, page: object) -> str:
                return "1"

            def current_page_unmatched_reagents(self, page: object) -> list[dict[str, str]]:
                return [
                    {
                        "\u5e8f\u53f7": "1",
                        "\u8bd5\u5242\u540d\u79f0": "retry-me",
                        "CAS\u53f7": "-",
                        "\u7269\u5316\u7279\u6027": "-",
                    }
                ]

            def process_current_unmatched_reagent_page(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
                return [
                    {
                        "\u5e8f\u53f7": "1",
                        "\u8bd5\u5242\u540d\u79f0": "retry-me",
                        "CAS\u53f7": "-",
                        "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": "\u666e\u901a\u7c7b",
                    }
                ]

            def apply_approval_write_mode(self, page: object, suggestions: list[dict[str, object]]) -> dict[str, set[str]]:
                self.apply_calls += 1
                key = self.suggestion_work_key(suggestions[0])
                if self.apply_calls == 1:
                    return {"attempted": {key}, "handled": {key}, "failed": {key}}
                return {"attempted": {key}, "handled": {key}, "failed": set()}

            def write_partial_approval_suggestions(self, suggestions: list[dict[str, object]]) -> None:
                return None

            def click_next_reagent_page(self, page: object) -> tuple[bool, bool]:
                return False, True

        bot = RetryBot()

        bot.process_unmatched_reagent_pages(object(), None, None, {})

        self.assertEqual(bot.apply_calls, 2)


if __name__ == "__main__":
    unittest.main()
