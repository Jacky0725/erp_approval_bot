from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
from typing import Any

from playwright.sync_api import Error, Locator, Page

import pandas as pd

from approval_writer import ApprovalWriter
from category_mapper import to_erp_property
from chemical_searcher import ChemicalSearcher
from llm_extractor import LlmExtractor
from name_normalizer import NameNormalizer
from reagent_name_rules import UNKNOWN_CATEGORY, unknown_reagent_name_reason
from reagent_memory import ReagentMemory
from rule_engine import RuleEngine
from rule_maintainer import RuleMaintainer
from stage_logger import StageLogger


class ApprovalFlowMixin:

    def run_debug_capture(self) -> None:
        self.run_after_login_capture(
            screenshot_name="home.png",
            html_name="home.html",
            after_login=None,
        )

    def run_reagent_judgement_capture(self) -> None:
        self.run_after_login_capture(
            screenshot_name="reagent_judgement.png",
            html_name="reagent_judgement.html",
            after_login=self.enter_reagent_judgement_page,
        )

    def run_todo_tasks_export(self) -> None:
        self.run_after_login_capture(
            screenshot_name="reagent_judgement.png",
            html_name="reagent_judgement.html",
            after_login=self.export_todo_tasks,
        )

    def run_first_task_detail_capture(self) -> None:
        self.run_after_login_capture(
            screenshot_name="task_detail.png",
            html_name="task_detail.html",
            after_login=self.open_first_task_detail,
        )

    def run_auto_match_capture(self) -> None:
        self.run_after_login_capture(
            screenshot_name="after_auto_match.png",
            html_name="after_auto_match.html",
            after_login=self.perform_auto_match,
        )

    def run_current_page_reagents_export(self) -> None:
        self.run_after_login_capture(
            screenshot_name="task_detail.png",
            html_name="task_detail.html",
            after_login=self.export_current_page_reagents,
        )

    def run_unmatched_reagents_export(self) -> None:
        self.run_after_login_capture(
            screenshot_name="task_detail.png",
            html_name="task_detail.html",
            after_login=self.sort_and_export_unmatched_reagents,
        )

    def run_single_fill_test(self) -> None:
        self.run_after_login_capture(
            screenshot_name="dropdown_options.png",
            html_name="dropdown_options.html",
            after_login=self.inspect_first_unmatched_property_options,
        )

    def run_semi_auto_approval_suggestions(self) -> None:
        if self.selected_todo_list_numbers():
            after_login = self.generate_selected_todo_approval_suggestions
        elif self.process_all_todos_enabled():
            after_login = self.generate_all_todo_approval_suggestions
        else:
            after_login = self.generate_approval_suggestions
        self.run_after_login_capture(
            screenshot_name="after_auto_match.png",
            html_name="after_auto_match.html",
            after_login=after_login,
        )

    def selected_todo_list_numbers(self) -> list[str]:
        configured = getattr(self, "target_list_numbers", None)
        if configured:
            return [str(item).strip() for item in configured if str(item).strip()]
        value = os.getenv("TARGET_LIST_NUMBERS", "")
        result = []
        for part in str(value or "").replace("\n", ",").replace(";", ",").split(","):
            item = part.strip()
            if item and item not in result:
                result.append(item)
        return result

    def process_all_todos_enabled(self) -> bool:
        value = os.getenv("PROCESS_ALL_TODOS", "")
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    def generate_approval_suggestions(self, page: Page) -> None:
        stage_logger = getattr(self, "stage_logger", None) or StageLogger()
        self.stage_logger = stage_logger
        self.save_results = []
        self.auto_match_succeeded = False
        self.pagination_check_succeeded = False
        with stage_logger.stage("perform_auto_match"):
            if not self.perform_auto_match(page):
                print("Semi-auto approval suggestions stopped because no detail page or auto-match result is available.")
                return
        with stage_logger.stage("wait_reagent_table_ready"):
            self.wait_for_reagent_table_ready(page)
        with stage_logger.stage("read_detail_info"):
            self._current_detail_info = self.read_detail_info(page)
        self.clear_manual_review_items_for_list(self._current_detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7", ""))

        with stage_logger.stage("sort_property_column"):
            sort_succeeded = self.sort_property_column_until_unmatched_visible(page)
        if not sort_succeeded:
            print("Sorting did not bring '-' into the first rows within 4 clicks; reading current page '-' rows anyway.")

        rule_engine = RuleEngine.from_settings(self.settings, self.root_dir)
        rule_maintainer = RuleMaintainer.from_settings(self.settings, self.root_dir)
        seen_search_urls: dict[str, str] = {}

        if self.approval_write_mode() == "multi_page":
            suggestions = self.process_unmatched_reagent_pages(
                page,
                rule_engine,
                rule_maintainer,
                seen_search_urls,
            )
        else:
            suggestions = self.process_current_unmatched_reagent_page(
                page,
                rule_engine,
                rule_maintainer,
                seen_search_urls,
            )
            with stage_logger.stage("apply_approval_write_mode"):
                self.apply_approval_write_mode(page, suggestions)

        with stage_logger.stage("write_approval_suggestions"):
            saved_paths = self.save_approval_suggestions_outputs(suggestions)
        print(f"Saved approval suggestions: {saved_paths[0] if saved_paths else '-'}")
        self.record_save_result(
            "local_approval_suggestions",
            True,
            ", ".join(str(path) for path in saved_paths) if saved_paths else "-",
        )

        with stage_logger.stage("try_auto_pass_current_task"):
            self.try_auto_pass_current_task(page)

    def generate_all_todo_approval_suggestions(self, page: Page) -> None:
        original_target = getattr(self, "target_list_number", "")
        processed_list_numbers: set[str] = set()
        max_todos = self.max_process_all_todos_count()

        try:
            self.enter_reagent_judgement_page(page)
            tasks = self.read_all_todo_tasks(page)
            all_list_numbers = self.todo_list_numbers(tasks)
            list_numbers = self.filter_scheduled_todo_list_numbers(all_list_numbers)
            print(f"Todo list refresh: {len(all_list_numbers)} total task(s) across all visible todo pages.")
            if len(list_numbers) != len(all_list_numbers):
                print(f"Scheduled review filter kept {len(list_numbers)} task(s) for automatic approval.")

            for list_number in list_numbers:
                if len(processed_list_numbers) >= max_todos:
                    break
                if list_number in processed_list_numbers:
                    continue

                print(f"Processing todo detail {len(processed_list_numbers) + 1}: {list_number}")
                self.target_list_number = list_number
                try:
                    self.generate_approval_suggestions(page)
                finally:
                    processed_list_numbers.add(list_number)

            if len(processed_list_numbers) >= max_todos:
                print(f"Stopped all-todo processing after PROCESS_ALL_TODOS_MAX={max_todos}.")
            elif not processed_list_numbers:
                print("No unprocessed todo task remains across todo pages.")
        finally:
            self.target_list_number = original_target

    def generate_selected_todo_approval_suggestions(self, page: Page) -> None:
        original_target = getattr(self, "target_list_number", "")
        selected = self.selected_todo_list_numbers()
        max_todos = self.max_process_all_todos_count()
        processed_count = 0

        try:
            self.enter_reagent_judgement_page(page)
            print(f"Selected todo list number(s): {', '.join(selected)}")

            for list_number in selected:
                if processed_count >= max_todos:
                    print(f"Stopped selected-todo processing after PROCESS_ALL_TODOS_MAX={max_todos}.")
                    break

                processed_count += 1
                print(f"Processing selected todo detail {processed_count}: {list_number}")
                self.target_list_number = list_number
                self.generate_approval_suggestions(page)
                if processed_count < min(len(selected), max_todos):
                    try:
                        self.enter_reagent_judgement_page(page)
                    except Exception as error:
                        print(f"Could not return to reagent judgement list after {list_number}: {error}")
                        break
        finally:
            self.target_list_number = original_target

    def todo_list_numbers(self, tasks: list[dict[str, str]]) -> list[str]:
        list_key = "\u8bd5\u5242\u6e05\u5355\u53f7"
        return [
            list_number
            for list_number in (self.extract_list_number(task.get(list_key, "")) for task in tasks)
            if list_number
        ]

    def next_unprocessed_list_number(self, tasks: list[dict[str, str]], processed: set[str]) -> str:
        for list_number in self.todo_list_numbers(tasks):
            if list_number not in processed:
                return list_number
        return ""

    def max_process_all_todos_count(self) -> int:
        value = os.getenv("PROCESS_ALL_TODOS_MAX", "50")
        try:
            max_count = int(value)
        except ValueError:
            print(f"Invalid PROCESS_ALL_TODOS_MAX={value}; using 50.")
            return 50
        return max(1, max_count)

    def filter_scheduled_todo_list_numbers(self, list_numbers: list[str]) -> list[str]:
        if not self.scheduled_manual_review_skip_enabled():
            return list_numbers

        filtered = []
        skipped = []
        for list_number in list_numbers:
            has_manual_review, reason = self.current_list_has_manual_review_item(list_number)
            if has_manual_review:
                skipped.append(list_number)
                print(
                    "Scheduled approval skipped list with pending manual review: "
                    f"{list_number}. {reason}"
                )
                continue
            filtered.append(list_number)

        if skipped:
            print(f"Scheduled approval skipped {len(skipped)} manual-review blocked list(s): {', '.join(skipped)}")
        return filtered

    def scheduled_manual_review_skip_enabled(self) -> bool:
        scheduled = os.getenv("SCHEDULED_RUN", "").strip().lower() in {"1", "true", "yes", "y", "on"}
        skip = os.getenv("SCHEDULED_SKIP_MANUAL_REVIEW_LISTS", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }
        return scheduled and skip

    def process_unmatched_reagent_pages(
        self,
        page: Page,
        rule_engine: RuleEngine,
        rule_maintainer: RuleMaintainer,
        seen_search_urls: dict[str, str],
    ) -> list[dict[str, Any]]:
        if not self.goto_first_reagent_page(page):
            print("Could not move to first reagent page; multi-page mode will continue from current page.")

        all_suggestions: list[dict[str, Any]] = []
        all_suggestion_keys: set[str] = set()
        handled_reagent_keys: set[str] = set()
        write_attempts_by_key: dict[str, int] = {}
        visited_steps = 0
        self.sort_property_column_until_unmatched_visible(page)

        while True:
            visited_steps += 1

            current_page = self.current_reagent_page_number(page) or str(visited_steps)
            current_unmatched = self.current_page_unmatched_reagents(page)
            unhandled_unmatched = [
                reagent
                for reagent in current_unmatched
                if self.reagent_work_key(reagent) not in handled_reagent_keys
                and write_attempts_by_key.get(self.reagent_work_key(reagent), 0) < self.max_reagent_write_attempts()
            ]
            if current_unmatched and not unhandled_unmatched:
                print(
                    f"Reagent page {current_page} still has {len(current_unmatched)} '-' row(s), "
                    "but all were already processed, queued, or reached the write retry limit; moving to the next page."
                )
                moved_next, terminal_or_error = self.click_next_reagent_page(page)
                if not moved_next:
                    if terminal_or_error:
                        print("Multi-page mode reached the last reagent page.")
                    else:
                        print("Multi-page mode stopped because next-page navigation could not be verified.")
                    break
                self.sort_property_column_until_unmatched_visible(page)
                continue

            if not current_unmatched:
                print(
                    "Current sorted reagent page has no '-' rows; "
                    "multi-page mode considers this reagent list complete."
                )
                break

            page_suggestions = self.process_current_unmatched_reagent_page(
                page,
                rule_engine,
                rule_maintainer,
                seen_search_urls,
                page_label=current_page,
                skip_reagent_keys=handled_reagent_keys,
            )
            for suggestion in page_suggestions:
                key = self.suggestion_work_key(suggestion)
                if key not in all_suggestion_keys:
                    all_suggestions.append(suggestion)
                    all_suggestion_keys.add(key)
            if page_suggestions:
                self.write_partial_approval_suggestions(all_suggestions)

            if page_suggestions:
                with self.stage_logger.stage("apply_approval_write_mode", f"page {current_page}"):
                    raw_write_result = self.apply_approval_write_mode(page, page_suggestions)
                    write_result = raw_write_result or {
                        "attempted": set(),
                        "handled": {self.suggestion_work_key(suggestion) for suggestion in page_suggestions},
                        "failed": set(),
                    }
                for key in write_result.get("attempted", set()):
                    write_attempts_by_key[key] = write_attempts_by_key.get(key, 0) + 1
                failed_keys = set(write_result.get("failed", set()))
                handled_reagent_keys.update(set(write_result.get("handled", set())) - failed_keys)
                for key in failed_keys:
                    if write_attempts_by_key.get(key, 0) >= self.max_reagent_write_attempts():
                        handled_reagent_keys.add(key)
                        print(f"Write retry limit reached for reagent key: {key}")

                if failed_keys:
                    print(
                        "Multi-page mode will re-read the current reagent page after write failure "
                        "before moving to the next page."
                    )
                    if not self.stabilize_reagent_detail_after_write_failure(page):
                        print(
                            "Multi-page mode stopped because the reagent detail page could not be "
                            "stabilized after a write failure."
                        )
                        break
                    try:
                        self.sort_property_column_until_unmatched_visible(page)
                    except Exception as error:
                        print(
                            "Multi-page mode stopped because the physicochemical property header "
                            f"was not available after write recovery: {error}"
                        )
                        break
                    continue
                if self.approval_write_mode() == "multi_page" and write_result.get("attempted"):
                    print("Multi-page mode saved one reagent; re-sorting and re-reading the current page.")
                    try:
                        self.sort_property_column_until_unmatched_visible(page)
                    except Exception as error:
                        print(
                            "Multi-page mode stopped because sorting after a successful save failed: "
                            f"{error}"
                        )
                        break
                    continue
            else:
                write_result = {"attempted": set(), "handled": set(), "failed": set()}

            if self.approval_write_mode() in {"disabled", "test_one"}:
                handled_reagent_keys.update(self.reagent_work_key(reagent) for reagent in current_unmatched)

            moved_next, terminal_or_error = self.click_next_reagent_page(page)
            if not moved_next:
                if terminal_or_error:
                    print("Multi-page mode reached the last reagent page.")
                else:
                    print("Multi-page mode stopped because next-page navigation could not be verified.")
                break
            try:
                self.sort_property_column_until_unmatched_visible(page)
            except Exception as error:
                print(f"Multi-page mode stopped because sorting after next-page navigation failed: {error}")
                break

            if visited_steps >= 200:
                raise RuntimeError("Stopped multi-page approval after 200 pages; page navigation may be stuck.")

        return all_suggestions

    def write_partial_approval_suggestions(self, suggestions: list[dict[str, Any]]) -> None:
        if not suggestions:
            return
        export_rows = self.suggestions_with_current_list_number(suggestions)
        columns = self.approval_suggestion_export_columns()
        output_path = self._log_dir() / "approval_suggestions_partial.xlsx"
        output_path = self.write_excel_with_fallback(
            pd.DataFrame(export_rows, columns=columns),
            output_path,
        )
        print(f"Saved partial approval suggestions: {output_path}")

    def save_approval_suggestions_outputs(self, suggestions: list[dict[str, Any]]) -> list[Any]:
        export_rows = self.suggestions_with_current_list_number(suggestions)
        columns = self.approval_suggestion_export_columns()
        dataframe = pd.DataFrame(export_rows, columns=columns)
        log_dir = self._log_dir()
        saved_paths: list[Any] = []

        latest_path = self.write_excel_with_fallback(dataframe, log_dir / "approval_suggestions.xlsx")
        saved_paths.append(latest_path)

        list_number = self.current_detail_list_number()
        if list_number:
            list_path = self.write_excel_with_fallback(
                dataframe,
                log_dir / f"approval_suggestions_{self.safe_filename_part(list_number)}.xlsx",
            )
            saved_paths.append(list_path)

        aggregate_path = self.write_aggregate_approval_suggestions(dataframe, list_number)
        if aggregate_path is not None:
            saved_paths.append(aggregate_path)
        return saved_paths

    def suggestions_with_current_list_number(self, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        list_number = self.current_detail_list_number()
        return [{"试剂清单号": list_number, **suggestion} for suggestion in suggestions]

    def approval_suggestion_export_columns(self) -> list[str]:
        return ["试剂清单号", *self.approval_suggestion_columns()]

    def current_detail_list_number(self) -> str:
        detail_info = getattr(self, "_current_detail_info", {}) or {}
        return str(detail_info.get("当前清单号") or detail_info.get("试剂清单号") or "").strip()

    def write_aggregate_approval_suggestions(self, dataframe: pd.DataFrame, list_number: str) -> Any:
        output_path = self._log_dir() / "approval_suggestions_all.xlsx"
        try:
            existing = pd.read_excel(output_path) if output_path.exists() else pd.DataFrame(columns=dataframe.columns)
            if list_number and "试剂清单号" in existing.columns:
                existing = existing[existing["试剂清单号"].astype(str) != str(list_number)]
            combined = pd.concat([existing, dataframe], ignore_index=True)
            return self.write_excel_with_fallback(
                combined.reindex(columns=self.approval_suggestion_export_columns()),
                output_path,
            )
        except Exception as error:
            print(f"Could not update aggregate approval suggestions: {error}")
            return None

    @staticmethod
    def safe_filename_part(value: str) -> str:
        safe = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in {"-", "_"})
        return safe or "unknown"

    def max_reagent_write_attempts(self) -> int:
        approval_settings = getattr(self, "settings", {}).get("approval", {}) or {}
        value = os.getenv("APPROVAL_WRITE_MAX_ATTEMPTS") or approval_settings.get("write_max_attempts", 2)
        try:
            attempts = int(value)
        except (TypeError, ValueError):
            return 2
        return max(1, min(5, attempts))

    def process_current_unmatched_reagent_page(
        self,
        page: Page,
        rule_engine: RuleEngine,
        rule_maintainer: RuleMaintainer,
        seen_search_urls: dict[str, str],
        page_label: str = "",
        skip_reagent_keys: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        stage_logger = getattr(self, "stage_logger", None) or StageLogger()
        property_key = "\u7269\u5316\u7279\u6027"
        name_key = "\u8bd5\u5242\u540d\u79f0"
        cas_key = "CAS\u53f7"
        skip_reagent_keys = skip_reagent_keys or set()

        with stage_logger.stage("read_current_page_unmatched", f"page {page_label}".strip()):
            unmatched_reagents = [
                record
                for record in self.read_current_page_reagents(page)
                if record.get(property_key, "").strip() == "-"
            ]
            skipped_count = sum(1 for record in unmatched_reagents if self.reagent_work_key(record) in skip_reagent_keys)
            unmatched_reagents = [
                record
                for record in unmatched_reagents
                if self.reagent_work_key(record) not in skip_reagent_keys
            ]

        page_text = f" page {page_label}" if page_label else ""
        if skipped_count:
            print(f"Skipped {skipped_count} already processed current-page '-' reagent row(s).")
        print(f"Found {len(unmatched_reagents)} current-page{page_text} reagent row(s) with physicochemical property '-'.")

        suggestions_by_index: dict[int, dict[str, Any]] = {}
        pending_reagents: list[dict[str, Any]] = []
        memory = ReagentMemory.from_settings(self.settings, self.root_dir)
        normalizer = NameNormalizer(settings=self.settings, root_dir=self.root_dir)
        for index, reagent in enumerate(unmatched_reagents, start=1):
            reagent_name = reagent.get(name_key, "").strip()
            cas = reagent.get(cas_key, "").strip()
            progress = f"{index}/{len(unmatched_reagents)}"
            if page_label:
                progress = f"page {page_label} {progress}"
            stage_logger.event(f"Processing reagent {progress}: {reagent_name} / {cas}")

            unknown_reason = unknown_reagent_name_reason(
                reagent_name,
                reagent.get("\u89c4\u683c", ""),
                reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
            )
            if unknown_reason:
                suggestion = self.unknown_reagent_suggestion(reagent, unknown_reason)
                suggestions_by_index[index] = suggestion
                self.remember_erp_suggestion(memory, suggestion)
                print(
                    "Direct unknown reagent rule suggestion: "
                    f"{reagent.get('\u5e8f\u53f7', '')} {reagent_name} -> {UNKNOWN_CATEGORY}"
                )
                continue

            memory_match = memory.lookup(cas=cas, raw_name=reagent_name)
            if memory_match:
                if not self.memory_match_is_safe(reagent, memory_match, rule_engine=rule_engine):
                    self.disable_unsafe_memory_match(memory, memory_match, reagent)
                else:
                    suggestion = self.reagent_memory_suggestion(reagent, memory_match)
                    suggestions_by_index[index] = suggestion
                    self.queue_manual_review_if_suggestion_requires_it(
                        reagent,
                        suggestion,
                        name_result={},
                    )
                    print(
                        "Reagent memory suggestion: "
                        f"{reagent.get('\u5e8f\u53f7', '')} {reagent_name} -> {memory_match.get('final_category', '')}"
                    )
                    continue

            direct_suggestion = self.direct_business_rule_suggestion(reagent, rule_engine)
            if direct_suggestion:
                suggestions_by_index[index] = direct_suggestion
                self.remember_erp_suggestion(memory, direct_suggestion)
                sequence = reagent.get("\u5e8f\u53f7", "")
                category = direct_suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b", "")
                print(
                    "Direct business rule suggestion: "
                    f"{sequence} {reagent_name} -> {category}"
                )
                continue

            try:
                name_result = normalizer.normalize(
                    reagent_name,
                    cas=cas,
                    specification=reagent.get("\u89c4\u683c", ""),
                    unit=reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
                )
            except Exception as error:
                name_result = {
                    "raw_name": reagent_name,
                    "cleaned_name": reagent_name,
                    "standard_name": reagent_name,
                    "cas": cas,
                    "confidence": 0.0,
                    "need_manual_review": True,
                    "reason": f"name normalization failed before memory lookup: {error}",
                }

            memory_match = memory.lookup(
                cas=name_result.get("cas") or cas,
                standard_name=name_result.get("standard_name", ""),
                cleaned_name=name_result.get("cleaned_name", ""),
                raw_name=reagent_name,
            )
            if memory_match:
                if not self.memory_match_is_safe(reagent, memory_match, name_result, rule_engine=rule_engine):
                    self.disable_unsafe_memory_match(memory, memory_match, reagent)
                else:
                    suggestion = self.reagent_memory_suggestion(reagent, memory_match, name_result)
                    suggestions_by_index[index] = suggestion
                    self.queue_manual_review_if_suggestion_requires_it(
                        reagent,
                        suggestion,
                        name_result=name_result,
                    )
                    print(
                        "Reagent memory suggestion after normalization: "
                        f"{reagent.get('\u5e8f\u53f7', '')} {reagent_name} -> {memory_match.get('final_category', '')}"
                    )
                    continue

            pending_reagents.append({"index": index, "progress": progress, "reagent": reagent})

        if not pending_reagents:
            return [suggestions_by_index[index] for index in sorted(suggestions_by_index)]

        with stage_logger.stage("chemical_search", f"parallel {len(pending_reagents)} reagent(s)"):
            search_results = self.search_reagents_parallel(pending_reagents)
        prepared_items: list[dict[str, Any]] = []
        for item in pending_reagents:
            reagent = item["reagent"]
            reagent_name = reagent.get(name_key, "").strip()
            cas = reagent.get(cas_key, "").strip()
            search_result = search_results.get(item["index"]) or self.search_failure_result(reagent, "parallel search did not return a result")
            name_result = search_result.get("name_normalization", {})
            self.mark_duplicate_search_url_if_needed(reagent, search_result, seen_search_urls)
            search_name = search_result.get("name") or name_result.get("standard_name") or name_result.get("cleaned_name") or reagent_name
            search_cas = search_result.get("cas") or name_result.get("cas") or cas
            if search_result.get("need_manual_review"):
                with stage_logger.stage("add_manual_review_item", reagent_name):
                    self.add_manual_review_item_from_search_failure(reagent, name_result, search_result)
            prepared_items.append(
                {
                    **item,
                    "search_result": search_result,
                    "name_result": name_result,
                    "search_name": search_name,
                    "search_cas": search_cas,
                }
            )

        with stage_logger.stage("llm_extract", f"parallel {len(prepared_items)} reagent(s)"):
            extracted_results = self.extract_and_classify_parallel(prepared_items, rule_engine)
        with stage_logger.stage("rule_classify", f"parallel {len(prepared_items)} reagent(s)"):
            print(f"Rule classification completed for {len(prepared_items)} reagent(s).")
        for item in prepared_items:
            reagent = item["reagent"]
            reagent_name = reagent.get(name_key, "").strip()
            name_result = item["name_result"]
            search_result = item["search_result"]
            extracted, classification = extracted_results.get(item["index"]) or self.empty_extraction_and_classification(
                reagent,
                search_result,
                rule_engine,
                "parallel LLM/classification did not return a result",
            )
            try:
                with stage_logger.stage("record_rule_candidate", reagent_name):
                    if rule_maintainer.record_candidate(reagent, name_result, search_result, extracted, classification):
                        print(f"Recorded pending rule candidate: {reagent_name}")
            except Exception as error:
                print(f"Could not record rule candidate for {reagent_name}: {error}")
            suggestions_by_index[item["index"]] = self._approval_suggestion_row(
                reagent,
                name_result,
                search_result,
                extracted,
                classification,
            )
            if suggestions_by_index[item["index"]].get("需人工复核"):
                with stage_logger.stage("add_manual_review_item", reagent_name):
                    self.add_manual_review_item_from_suggestion(
                        reagent,
                        name_result,
                        search_result,
                        extracted,
                        classification,
                        suggestions_by_index[item["index"]],
                    )
            self.remember_erp_suggestion(memory, suggestions_by_index[item["index"]])

        return [suggestions_by_index[index] for index in sorted(suggestions_by_index)]

    def queue_manual_review_if_suggestion_requires_it(
        self,
        reagent: dict[str, str],
        suggestion: dict[str, Any],
        name_result: dict[str, Any] | None = None,
    ) -> None:
        if not suggestion.get("\u9700\u4eba\u5de5\u590d\u6838"):
            return
        self.add_manual_review_item_from_suggestion(
            reagent,
            name_result or {
                "raw_name": reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
                "cleaned_name": suggestion.get("\u6e05\u6d17\u540e\u540d\u79f0", ""),
                "standard_name": suggestion.get("\u6807\u51c6\u5316\u540d\u79f0", ""),
                "reason": suggestion.get("\u540d\u79f0\u6807\u51c6\u5316\u539f\u56e0", ""),
            },
            {
                "source": suggestion.get("\u67e5\u8be2\u6765\u6e90", ""),
                "url": suggestion.get("\u67e5\u8be2URL", ""),
                "failure_reason": suggestion.get("\u67e5\u8be2\u5931\u8d25\u539f\u56e0", ""),
                "raw_text": suggestion.get("\u8bc1\u636e", ""),
            },
            {
                "evidence": [suggestion.get("\u8bc1\u636e", "")]
                if suggestion.get("\u8bc1\u636e")
                else [],
            },
            {
                "reason": suggestion.get("\u89c4\u5219\u539f\u56e0", ""),
                "need_manual_review": True,
            },
            suggestion,
        )

    def unknown_reagent_suggestion(self, reagent: dict[str, Any], reason: str) -> dict[str, Any]:
        name = str(reagent.get("\u8bd5\u5242\u540d\u79f0", "") or "").strip()
        cas = str(reagent.get("CAS\u53f7", "") or "").strip()
        return {
            "\u8bd5\u5242\u6e05\u5355\u53f7": self.current_detail_list_number(),
            "\u5e8f\u53f7": reagent.get("\u5e8f\u53f7", ""),
            "\u8bd5\u5242\u540d\u79f0": name,
            "CAS\u53f7": cas,
            "\u89c4\u683c": reagent.get("\u89c4\u683c", ""),
            "\u89c4\u683c\u5355\u4f4d": reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
            "\u8bd5\u5242\u6570\u91cf": reagent.get("\u8bd5\u5242\u6570\u91cf", ""),
            "\u6807\u51c6\u5316\u540d\u79f0": name,
            "\u82f1\u6587\u540d\u79f0": "",
            "\u6e05\u6d17\u540e\u540d\u79f0": name,
            "\u6d53\u5ea6": "",
            "\u540d\u79f0\u6807\u51c6\u5316\u7f6e\u4fe1\u5ea6": 1.0,
            "\u540d\u79f0\u9700\u4eba\u5de5\u590d\u6838": False,
            "\u540d\u79f0\u6807\u51c6\u5316\u539f\u56e0": reason,
            "\u67e5\u8be2\u6765\u6e90": "business_rule_unknown_name",
            "\u67e5\u8be2URL": "",
            "\u67e5\u8be2\u9700\u4eba\u5de5": False,
            "\u7f51\u7ad9\u5339\u914d\u540d\u79f0": "",
            "\u540d\u79f0\u76f8\u4f3c\u5ea6": 1.0,
            "\u67e5\u8be2\u76f8\u5173\u6027\u901a\u8fc7": True,
            "\u8d44\u6599\u53ef\u4fe1\u5ea6": 1.0,
            "\u8bc1\u636e\u8d28\u91cf": "unknown_name",
            "\u67e5\u8be2\u5931\u8d25\u539f\u56e0": "",
            "\u5927\u6a21\u578b\u5019\u9009\u7c7b\u522b": [UNKNOWN_CATEGORY],
            "\u8bc1\u636e": reason,
            "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": UNKNOWN_CATEGORY,
            "\u547d\u4e2d\u7c7b\u522b": [UNKNOWN_CATEGORY],
            "\u89c4\u5219\u539f\u56e0": reason,
            "\u7f6e\u4fe1\u5ea6": 1.0,
            "\u9700\u4eba\u5de5\u590d\u6838": False,
        }

    def remember_erp_suggestion(self, memory: ReagentMemory, suggestion: dict[str, Any]) -> bool:
        if str(suggestion.get("查询来源") or "").strip() == "reagent_memory":
            return False
        final_category = str(suggestion.get("最终建议类别") or "").strip()
        erp_category = to_erp_property(final_category, self.settings)
        if not erp_category:
            return False
        normalized = dict(suggestion)
        normalized["最终建议类别"] = erp_category
        return memory.remember_suggestion(normalized)

    def memory_match_is_safe(
        self,
        reagent: dict[str, str],
        memory_row: dict[str, Any],
        name_result: dict[str, Any] | None = None,
        rule_engine: RuleEngine | None = None,
    ) -> bool:
        final_category = str(memory_row.get("final_category") or "").strip()
        if final_category != "普通类":
            return True

        raw_name = str(reagent.get("试剂名称", "") or "").strip()
        memory_names = " ".join(
            str(memory_row.get(key) or "")
            for key in ("raw_name", "cleaned_name", "standard_name")
        )
        normalized_names = " ".join(
            str((name_result or {}).get(key) or "")
            for key in ("raw_name", "cleaned_name", "standard_name", "english_name")
        )
        actual_names = " ".join([raw_name, normalized_names])

        if self._has_bromine_or_iodine(actual_names):
            return False
        if self._looks_like_pharmacopoeia_color(memory_names) and not self._looks_like_pharmacopoeia_color(actual_names):
            return False
        if rule_engine and self._ordinary_memory_conflicts_with_rules(
            reagent,
            memory_row,
            name_result,
            rule_engine,
        ):
            return False
        return True

    @staticmethod
    def _ordinary_memory_conflicts_with_rules(
        reagent: dict[str, str],
        memory_row: dict[str, Any],
        name_result: dict[str, Any] | None,
        rule_engine: RuleEngine,
    ) -> bool:
        raw_name = str(reagent.get("试剂名称", "") or memory_row.get("raw_name") or "").strip()
        cleaned_name = str(
            (name_result or {}).get("cleaned_name")
            or memory_row.get("cleaned_name")
            or raw_name
        ).strip()
        standard_name = str(
            (name_result or {}).get("standard_name")
            or memory_row.get("standard_name")
            or raw_name
        ).strip()
        classification = rule_engine.classify(
            {
                "reagent_name": raw_name,
                "name": standard_name or raw_name,
                "standard_name": standard_name,
                "cleaned_name": cleaned_name,
                "cas": reagent.get("CAS号", "") or memory_row.get("cas") or "",
                "text": " ".join(value for value in (raw_name, cleaned_name, standard_name) if value),
                "allow_default_normal": False,
            }
        )
        category = str(classification.get("final_category") or "").strip()
        return bool(category and category != "普通类")

    def disable_unsafe_memory_match(
        self,
        memory: ReagentMemory,
        memory_row: dict[str, Any],
        reagent: dict[str, str],
    ) -> None:
        record_id = memory_row.get("id")
        reagent_name = str(reagent.get("试剂名称", "") or "").strip()
        message = (
            "Unsafe reagent memory ignored: "
            f"{reagent_name} matched memory id {record_id} -> {memory_row.get('final_category', '')}."
        )
        print(message)
        if not record_id:
            return
        previous_reason = str(memory_row.get("reason") or "").strip()
        reason = (
            f"{previous_reason}\n"
            f"自动停用：{reagent_name} 的本地记忆命中未通过安全校验，需人工确认后再复用。"
        ).strip()
        try:
            memory.update_record(
                int(record_id),
                {
                    "reusable": False,
                    "conflict": True,
                    "reason": reason,
                },
            )
        except Exception as error:
            print(f"Could not disable unsafe reagent memory id {record_id}: {error}")

    @staticmethod
    def _has_bromine_or_iodine(text: str) -> bool:
        return bool(
            re.search(
                r"溴|碘|bromo|bromide|bromine|iodo|iodide|iodine",
                str(text or ""),
                flags=re.I,
            )
        )

    @staticmethod
    def _looks_like_pharmacopoeia_color(text: str) -> bool:
        normalized = re.sub(r"\s+", "", str(text or "").lower())
        tokens = (
            "药典色度标准品",
            "药典色度标准溶液",
            "欧洲药典色度标准溶液",
            "色度标准品",
            "色度标准溶液",
            "pharmacopoeiacolor",
            "pharmacopoeialcolor",
            "colourstandard",
            "colorstandard",
        )
        return any(token in normalized for token in tokens)

    def add_manual_review_item_from_suggestion(
        self,
        reagent: dict[str, str],
        name_result: dict[str, Any],
        search_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
        suggestion: dict[str, Any],
    ) -> None:
        reason_parts = [
            str(suggestion.get("规则原因") or "").strip(),
            str(search_result.get("failure_reason") or "").strip(),
            str(name_result.get("reason") or "").strip(),
            " | ".join(str(item) for item in extracted.get("evidence", []) or [] if str(item).strip()),
            str(search_result.get("raw_text") or "").strip()[:800],
        ]
        reason = next((part for part in reason_parts if part), "")
        if not reason:
            reason = "Rule classification, name normalization, or source evidence requires manual review."
        self.add_manual_review_item(
            reagent,
            name_result,
            reason=reason,
        )

    def search_reagents_parallel(self, items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        worker_count = self.parallel_worker_count()
        if worker_count <= 1 or len(items) <= 1:
            return {item["index"]: self.search_reagent_worker(item) for item in items}

        print(f"Searching chemical websites with {worker_count} worker(s) for {len(items)} reagent(s).")
        results: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="chemical-search") as executor:
            futures = {executor.submit(self.search_reagent_worker, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    results[item["index"]] = future.result()
                except Exception as error:  # noqa: BLE001 - keep one failed lookup from stopping the page
                    results[item["index"]] = self.search_failure_result(item["reagent"], str(error))
        return results

    def search_reagent_worker(self, item: dict[str, Any]) -> dict[str, Any]:
        reagent = item["reagent"]
        reagent_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "").strip()
        print(f"[parallel search] START {item['progress']} {reagent_name}")
        searcher = ChemicalSearcher(settings=self.settings, root_dir=self.root_dir)
        result = searcher.search(
            reagent_name,
            cas=reagent.get("CAS\u53f7", "").strip(),
            specification=reagent.get("\u89c4\u683c", ""),
            unit=reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
        )
        print(f"[parallel search] END {item['progress']} {reagent_name} -> {result.get('source') or 'manual_review'}")
        return result

    def extract_and_classify_parallel(
        self,
        items: list[dict[str, Any]],
        rule_engine: RuleEngine,
    ) -> dict[int, tuple[dict[str, Any], dict[str, Any]]]:
        worker_count = self.parallel_worker_count()
        if worker_count <= 1 or len(items) <= 1:
            return {item["index"]: self.extract_and_classify_worker(item, rule_engine) for item in items}

        print(f"Extracting LLM properties with {worker_count} worker(s) for {len(items)} reagent(s).")
        results: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="llm-extract") as executor:
            futures = {executor.submit(self.extract_and_classify_worker, item, rule_engine): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    results[item["index"]] = future.result()
                except Exception as error:  # noqa: BLE001
                    results[item["index"]] = self.empty_extraction_and_classification(
                        item["reagent"],
                        item["search_result"],
                        rule_engine,
                        str(error),
                    )
        return results

    def extract_and_classify_worker(
        self,
        item: dict[str, Any],
        rule_engine: RuleEngine,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        reagent = item["reagent"]
        reagent_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "").strip()
        search_result = item["search_result"]
        print(f"[parallel llm] START {item['progress']} {reagent_name}")
        extractor = LlmExtractor(settings=self.settings)
        extracted = extractor.extract_properties(
            raw_text=search_result.get("raw_text", ""),
            name=f"{reagent_name} / {search_result.get('name') or str(item.get('search_name') or reagent_name)}",
            cas=search_result.get("cas") or str(item.get("search_cas") or reagent.get("CAS\u53f7", "")),
        )
        classification = rule_engine.classify(self._classification_input(reagent, search_result, extracted))
        print(
            f"[parallel llm] END {item['progress']} {reagent_name} "
            f"-> {classification.get('final_category') or '<manual_review>'}"
        )
        return extracted, classification

    def parallel_worker_count(self) -> int:
        approval_settings = self.settings.get("approval", {}) or {}
        value = os.getenv("APPROVAL_PARALLEL_WORKERS") or approval_settings.get("parallel_workers", 3)
        try:
            workers = int(value)
        except (TypeError, ValueError):
            workers = 3
        return max(1, min(8, workers))

    @staticmethod
    def search_failure_result(reagent: dict[str, Any], reason: str) -> dict[str, Any]:
        name = str(reagent.get("\u8bd5\u5242\u540d\u79f0", "") or "").strip()
        cas = str(reagent.get("CAS\u53f7", "") or "").strip()
        return {
            "name": name,
            "cas": cas,
            "source": "",
            "url": "",
            "raw_text": reason,
            "hazard_keywords": [],
            "need_manual_review": True,
            "name_normalization": {
                "raw_name": name,
                "cleaned_name": name,
                "standard_name": name,
                "cas": cas,
                "confidence": 0.0,
                "need_manual_review": True,
                "reason": reason,
            },
            "query": "",
            "matched_site_name": "",
            "name_similarity": 0.0,
            "relevance_passed": False,
            "source_confidence": 0.0,
            "evidence_quality": "none",
            "failure_reason": reason,
            "fallback_source": "",
            "fallback_url": "",
            "used_llm_search_candidates": False,
            "llm_search_candidates": [],
        }

    def empty_extraction_and_classification(
        self,
        reagent: dict[str, Any],
        search_result: dict[str, Any],
        rule_engine: RuleEngine,
        reason: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extracted = {
            "name": reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
            "cas": reagent.get("CAS\u53f7", ""),
            "flash_point": "",
            "boiling_point": "",
            "toxicity": "",
            "corrosive": None,
            "oxidizing": None,
            "flammable": None,
            "water_reactive": None,
            "explosive_risk": None,
            "heavy_metal": None,
            "suggested_categories": [],
            "evidence": [f"LLM/classification failed: {reason}"],
            "confidence": 0.0,
        }
        classification = rule_engine.classify(self._classification_input(reagent, search_result, extracted))
        return extracted, classification

    @staticmethod
    def reagent_work_key(reagent: dict[str, Any]) -> str:
        parts = [
            str(reagent.get("\u5e8f\u53f7", "")).strip(),
            str(reagent.get("\u8bd5\u5242\u540d\u79f0", "")).strip(),
            str(reagent.get("CAS\u53f7", "")).strip(),
            str(reagent.get("\u89c4\u683c", "")).strip(),
            str(reagent.get("\u89c4\u683c\u5355\u4f4d", "")).strip(),
        ]
        return "|".join(parts)

    def direct_business_rule_suggestion(
        self,
        reagent: dict[str, str],
        rule_engine: RuleEngine,
    ) -> dict[str, Any] | None:
        reagent_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "")
        ambiguous_acid_reason = self.ambiguous_acid_reason(reagent_name)
        if ambiguous_acid_reason:
            classification = {
                "final_category": "\u5e38\u89c4\u9178",
                "matched_categories": ["\u5e38\u89c4\u9178"],
                "reason": ambiguous_acid_reason,
                "confidence": 1.0,
                "need_manual_review": False,
            }
            return self._direct_business_suggestion(
                reagent,
                reagent_name,
                classification,
                ambiguous_acid_reason,
            )

        kit_reason = self.product_kit_normal_reason(reagent_name)
        if kit_reason:
            classification = {
                "final_category": "\u666e\u901a\u7c7b",
                "matched_categories": ["\u666e\u901a\u7c7b"],
                "reason": kit_reason,
                "confidence": 0.9,
                "need_manual_review": False,
            }
            return self._direct_business_suggestion(reagent, reagent_name, classification, kit_reason)

        classification = rule_engine.classify(
            {
                "reagent_name": reagent_name,
                "name": reagent_name,
                "standard_name": reagent_name,
                "cleaned_name": reagent_name,
                "text": reagent_name,
            }
        )
        if classification.get("final_category") != "\u666e\u901a\u7c7b":
            return None
        if float(classification.get("confidence") or 0.0) < 0.9:
            return None
        return self._direct_business_suggestion(
            reagent,
            reagent_name,
            classification,
            str(classification.get("reason") or "\u547d\u4e2d\u666e\u901a\u7c7b\u4e1a\u52a1\u89c4\u5219"),
        )

    @staticmethod
    def ambiguous_acid_reason(reagent_name: str) -> str:
        normalized = str(reagent_name or "").replace(" ", "").lower()
        if not normalized:
            return ""
        has_uncertain_prefix = any(token in normalized for token in ("\u7591\u4f3c", "\u53ef\u80fd", "\u6216", "/"))
        has_common_acid = "\u786b\u9178" in normalized or "\u78f7\u9178" in normalized
        if has_uncertain_prefix and has_common_acid:
            return "\u8bd5\u5242\u540d\u79f0\u5305\u542b\u201c\u7591\u4f3c/\u53ef\u80fd/\u6216\u201d\u4e14\u6307\u5411\u786b\u9178\u6216\u78f7\u9178\uff0c\u6309\u4e1a\u52a1\u89c4\u5219\u76f4\u63a5\u5224\u5b9a\u4e3a\u5e38\u89c4\u9178\u3002"
        return ""

    @staticmethod
    def product_kit_normal_reason(reagent_name: str) -> str:
        normalized = str(reagent_name or "").replace(" ", "").lower()
        tokens = (
            "\u8bd5\u5242\u76d2",
            "\u7eaf\u5316\u8bd5\u5242",
            "\u7f13\u51b2\u6761",
            "\u6bd4\u8272\u6db2",
            "\u5e95\u7269",
            "geneclean",
            "spin",
            "dna\u7eaf\u5316",
            "rna\u7eaf\u5316",
            "hydranal",
            "water-std",
            "waterstd",
            "tmb",
            "substrate",
            "minitrap",
            "pdmnitrap",
            "pdminitrap",
            "bufferstrips",
            "bufferstrip",
            "excelgel",
            "gelbuffer",
            "kit",
        )
        if any(token in normalized for token in tokens) and not any(
            risk_token in normalized for risk_token in ("\u53e0\u6c2e", "\u53e0\u5316", "\u9ad8\u6c2f\u9178", "azide")
        ):
            return "\u8bd5\u5242\u540d\u79f0\u547d\u4e2d\u8bd5\u5242\u76d2/\u6807\u51c6\u6db2/\u5546\u54c1\u8bd5\u5242\u4e1a\u52a1\u89c4\u5219\uff0c\u6309\u666e\u901a\u7c7b\u5904\u7406\u3002"
        return ""

    def _direct_business_suggestion(
        self,
        reagent: dict[str, str],
        reagent_name: str,
        classification: dict[str, Any],
        name_reason: str,
    ) -> dict[str, Any]:

        name_result = {
            "standard_name": reagent_name,
            "cleaned_name": reagent_name,
            "confidence": classification.get("confidence", 0.9),
            "need_manual_review": False,
            "reason": name_reason,
        }
        search_result = {
            "name": reagent_name,
            "cas": reagent.get("CAS\u53f7", ""),
            "source": "business_rule",
            "url": "",
            "raw_text": "",
            "need_manual_review": False,
            "relevance_passed": True,
            "source_confidence": classification.get("confidence", 0.9),
            "evidence_quality": "business_rule",
            "name_normalization": name_result,
        }
        extracted = {
            "suggested_categories": [classification.get("final_category", "")],
            "evidence": [classification.get("reason", "")],
            "confidence": 0.95,
        }
        return self._approval_suggestion_row(reagent, name_result, search_result, extracted, classification)

    def reagent_memory_suggestion(
        self,
        reagent: dict[str, str],
        memory_row: dict[str, Any],
        name_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        confidence = float(memory_row.get("confidence") or 0.0)
        final_category = str(memory_row.get("final_category") or "").strip()
        memory_url = str(memory_row.get("url") or "").strip()
        erp_cas = str(reagent.get("CAS\u53f7", "") or "").strip()
        memory_cas = str(memory_row.get("cas") or "").strip()
        url_cas = self.extract_cas_from_url(memory_url)
        authoritative_cas = url_cas or memory_cas or erp_cas
        cas_conflict = bool(erp_cas and authoritative_cas and self.normalize_cas(erp_cas) != self.normalize_cas(authoritative_cas))
        suggestion_reagent = dict(reagent)
        if cas_conflict:
            suggestion_reagent["CAS\u53f7"] = ""
            print(
                "Reagent memory CAS conflict; using memory URL/CAS and dropping ERP CAS from suggestion: "
                f"{reagent.get('\u5e8f\u53f7', '')} {reagent.get('\u8bd5\u5242\u540d\u79f0', '')} "
                f"ERP CAS={erp_cas}, memory CAS={authoritative_cas}, url={memory_url}"
            )
        name_result = dict(name_result or {})
        name_result.setdefault("raw_name", reagent.get("\u8bd5\u5242\u540d\u79f0", ""))
        name_result.setdefault("cleaned_name", memory_row.get("cleaned_name") or reagent.get("\u8bd5\u5242\u540d\u79f0", ""))
        name_result.setdefault("standard_name", memory_row.get("standard_name") or reagent.get("\u8bd5\u5242\u540d\u79f0", ""))
        name_result["cas"] = authoritative_cas
        name_result.setdefault("confidence", confidence)
        name_result.setdefault("need_manual_review", False)
        name_result.setdefault(
            "reason",
            "Matched a reusable local reagent memory record before chemical website lookup.",
        )

        reason = str(memory_row.get("reason") or "Matched reusable local reagent memory.").strip()
        if cas_conflict:
            reason = (
                f"{reason}\n"
                f"ERP CAS {erp_cas} conflicts with local memory URL/CAS {authoritative_cas}; "
                "the ERP CAS was removed from this suggestion and the memory URL/CAS was used as evidence."
            ).strip()
        search_result = {
            "name": memory_row.get("standard_name") or reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
            "cas": authoritative_cas,
            "source": "reagent_memory",
            "url": memory_url,
            "raw_text": reason,
            "hazard_keywords": [],
            "need_manual_review": False,
            "relevance_passed": True,
            "source_confidence": confidence,
            "evidence_quality": "local_memory",
            "name_normalization": name_result,
            "matched_site_name": memory_row.get("standard_name") or memory_row.get("raw_name") or "",
            "name_similarity": 1.0,
        }
        extracted = {
            "suggested_categories": [final_category] if final_category else [],
            "evidence": [reason],
            "confidence": confidence,
        }
        classification = {
            "final_category": final_category,
            "matched_categories": [final_category] if final_category else [],
            "reason": f"本地高可信试剂记忆库命中：{reason}",
            "confidence": confidence,
            "need_manual_review": False,
        }
        return self._approval_suggestion_row(suggestion_reagent, name_result, search_result, extracted, classification)

    @staticmethod
    def extract_cas_from_url(url: str) -> str:
        match = re.search(r"(?<!\d)(\d{2,7}-\d{2}-\d)(?!\d)", str(url or ""))
        return match.group(1) if match else ""

    @staticmethod
    def normalize_cas(cas: str) -> str:
        return re.sub(r"\s+", "", str(cas or "").strip())

    def apply_approval_write_mode(self, page: Page, suggestions: list[dict[str, Any]]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {"attempted": set(), "handled": set(), "failed": set()}
        mode = self.approval_write_mode()
        if mode == "disabled":
            print("Approval write mode is disabled; no webpage fields will be changed.")
            result["handled"].update(self.suggestion_work_key(suggestion) for suggestion in suggestions)
            return result

        candidates = self.high_confidence_write_candidates(suggestions)
        candidate_keys = {self.suggestion_work_key(suggestion) for suggestion in candidates}
        result["handled"].update(
            self.suggestion_work_key(suggestion)
            for suggestion in suggestions
            if self.suggestion_work_key(suggestion) not in candidate_keys
        )
        if not candidates:
            print("No high-confidence approval suggestion is eligible for webpage writing.")
            return result

        if mode in {"test_one", "save_one"}:
            candidates = candidates[:1]
        elif mode in {"single_page", "generate_library"}:
            pass
        elif mode == "multi_page":
            print(
                "Multi-page write mode uses single-row transactions; "
                f"1 of {len(candidates)} writable candidate(s) will be saved before re-reading the page."
            )
            candidates = candidates[:1]
        else:
            print(f"Unknown APPROVAL_WRITE_MODE={mode}; no webpage fields will be changed.")
            return result

        writer = ApprovalWriter(settings=self.settings)
        consecutive_failures = 0
        for row_index, suggestion in enumerate(candidates, start=1):
            sequence = str(suggestion.get("\u5e8f\u53f7") or "").strip()
            rule_category = str(
                suggestion.get("\u89c4\u5219\u5224\u5b9a\u7c7b\u522b")
                or suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b")
                or ""
            ).strip()
            category = to_erp_property(rule_category, self.settings) or str(
                suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b") or ""
            ).strip()
            reagent_name = str(suggestion.get("\u8bd5\u5242\u540d\u79f0") or "").strip()
            cas = str(suggestion.get("CAS\u53f7") or "").strip()
            work_key = self.suggestion_work_key(suggestion)
            result["attempted"].add(work_key)
            if category != rule_category:
                print(
                    f"Approval write candidate {row_index}/{len(candidates)}: "
                    f"{sequence} {reagent_name} -> {rule_category} / ERP {category}"
                )
            else:
                print(f"Approval write candidate {row_index}/{len(candidates)}: {sequence} {reagent_name} -> {category}")

            # In multi-page mode, ERP saves can re-render or re-sort the table.
            # Keep retries at the page-loop level so each retry starts from a
            # freshly read current page instead of stale row locators.
            write_attempt_limit = 1
            last_failure_detail = ""
            verified = False
            saved = False
            row = None

            for write_attempt in range(1, write_attempt_limit + 1):
                if write_attempt > 1:
                    print(f"Retrying approval write for sequence {sequence}, attempt {write_attempt}/{write_attempt_limit}.")
                if not self.clear_existing_edit_state(page, writer, sequence):
                    last_failure_detail = "could not clear existing edit row"
                    print(f"Could not clear an existing edit row before writing sequence: {sequence}")
                    self.capture_write_failure(page, sequence, write_attempt, last_failure_detail)
                    break
                writer.dismiss_open_dropdown(page)
                row = self.find_reagent_row_by_sequence(page, sequence, reagent_name, cas)
                if row is None:
                    last_failure_detail = "row not found"
                    print(f"Could not find current-page row for sequence: {sequence}")
                    break

                self.prepare_reagent_row_for_write(page, row)
                already_editing = writer.row_is_editing(page, row)
                if already_editing:
                    print(f"Sequence {sequence} is already in edit mode; continuing property selection.")
                    opened = True
                else:
                    opened = writer.open_technical_judgement(row, page)
                if not opened:
                    last_failure_detail = "technical judgement button not found"
                    print(f"Could not open technical judgement for sequence: {sequence}")
                    self.capture_write_failure(page, sequence, write_attempt, last_failure_detail)
                    self.cleanup_failed_write(page, writer, row, sequence, last_failure_detail)
                    continue

                page.wait_for_timeout(500)
                selected_value = ""
                selected = False
                for selection_attempt in range(1, 3):
                    if selection_attempt > 1:
                        print(
                            f"Retrying property dropdown selection for sequence {sequence}, "
                            f"attempt {selection_attempt}/2."
                        )
                    selected = writer.choose_property(page, category, row)
                    page.wait_for_timeout(500)
                    if not selected:
                        continue
                    selected_value = self.read_reagent_property_by_sequence(page, sequence)
                    if self.property_value_matches(selected_value, category, writer):
                        break
                    if selected_value in {"", "-", "\u9009\u62e9\u641c\u7d22"}:
                        writer.dismiss_open_dropdown(page)
                        page.wait_for_timeout(250)
                        continue
                    break
                if not selected:
                    last_failure_detail = f"could not select {category}"
                    print(f"Could not select physicochemical property {category} for sequence: {sequence}")
                    self.capture_write_failure(page, sequence, write_attempt, last_failure_detail)
                    self.cleanup_failed_write(page, writer, row, sequence, last_failure_detail)
                    continue

                if not self.property_value_matches(selected_value, category, writer):
                    last_failure_detail = f"selected {category}, but row still shows {selected_value or '<empty>'}"
                    print(
                        f"Property selection verification failed for sequence {sequence}: "
                        f"expected {category}, got {selected_value or '<empty>'}."
                    )
                    self.capture_write_failure(page, sequence, write_attempt, last_failure_detail)
                    self.cleanup_failed_write(page, writer, row, sequence, last_failure_detail)
                    continue

                screenshot_path = self._log_dir() / f"write_mode_{mode}_{sequence}.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"Saved approval write screenshot before save: {screenshot_path}")

                if mode == "test_one":
                    print("Test write mode: selected value for inspection only; not saving.")
                    result["handled"].add(work_key)
                    return result

                saved = writer.save(page, row)
                page.wait_for_timeout(800)
                saved_value = self.read_reagent_property_by_sequence(page, sequence)
                verified = saved and self.property_value_matches(saved_value, category, writer)
                print(f"Save verified for sequence {sequence}: {verified} (clicked_save={saved})")
                if verified:
                    break
                last_failure_detail = f"saved={saved}, row shows {saved_value or '<empty>'}"
                print(
                    f"Save verification failed for sequence {sequence}: "
                    f"expected {category}, got {saved_value or '<empty>'}."
                )
                self.capture_write_failure(page, sequence, write_attempt, last_failure_detail)
                self.cleanup_failed_write(page, writer, row, sequence, last_failure_detail)

            self.record_save_result(
                f"reagent_save_{sequence}",
                verified,
                category if verified else last_failure_detail or f"could not select {category}",
            )
            if verified:
                result["handled"].add(work_key)
                consecutive_failures = 0
                if not self.settle_after_successful_write(page, writer, sequence):
                    result["failed"].add(work_key)
                    print(
                        f"Page edit state did not settle after saving sequence {sequence}; "
                        "the current page will be stabilized before continuing."
                    )
                    if mode == "multi_page":
                        break
            else:
                result["failed"].add(work_key)
                self.add_manual_review_item_from_write_failure(
                    suggestion,
                    last_failure_detail or f"could not write {category}",
                )
                consecutive_failures += 1
                if mode == "multi_page":
                    print(
                        "Stopping this multi-page write round after a failed webpage write; "
                        "the failed reagent was recorded according to its suggestion confidence and the page will be re-sorted/re-read."
                    )
                    if not self.stabilize_reagent_detail_after_write_failure(page):
                        print("Stopping this multi-page write round because the page could not be stabilized.")
                        break
                    break
                if consecutive_failures >= self.approval_write_failure_break_limit():
                    print("Stopping this write round after repeated save failures; the page will be re-read.")
                    break

            if mode == "generate_library" and verified and row is not None:
                generated = writer.generate_reagent_library(page, row)
                self.record_save_result(f"reagent_library_{sequence}", generated, category)
                print(f"Generate reagent library result for sequence {sequence}: {generated}")

            if mode == "save_one":
                return result

        return result

    def add_manual_review_item_from_write_failure(self, suggestion: dict[str, Any], reason: str) -> None:
        if self.write_failure_can_skip_manual_review(suggestion):
            memory = ReagentMemory.from_settings(self.settings, self.root_dir)
            saved = self.remember_erp_suggestion(memory, suggestion)
            category = suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b", "")
            sequence = suggestion.get("\u5e8f\u53f7", "")
            reagent_name = suggestion.get("\u8bd5\u5242\u540d\u79f0", "")
            print(
                "Web write failed, but classification is reusable; skipped manual review queue "
                f"and recorded memory={saved}: {sequence} {reagent_name} -> {category}; reason={reason}"
            )
            return

        reagent = {
            "\u5e8f\u53f7": suggestion.get("\u5e8f\u53f7", ""),
            "\u8bd5\u5242\u540d\u79f0": suggestion.get("\u8bd5\u5242\u540d\u79f0", ""),
            "CAS\u53f7": suggestion.get("CAS\u53f7", ""),
            "\u89c4\u683c": suggestion.get("\u89c4\u683c", ""),
            "\u89c4\u683c\u5355\u4f4d": suggestion.get("\u89c4\u683c\u5355\u4f4d", ""),
            "\u8bd5\u5242\u6570\u91cf": suggestion.get("\u8bd5\u5242\u6570\u91cf", ""),
        }
        name_result = {
            "standard_name": suggestion.get("\u6807\u51c6\u5316\u540d\u79f0", ""),
            "cleaned_name": suggestion.get("\u6e05\u6d17\u540e\u540d\u79f0", ""),
        }
        manual_reason = (
            f"网页写入失败：{reason}。程序没有确认物化特性已成功写入 ERP 行内，"
            "需要人工在网页端核对并处理该试剂。"
        )
        self.add_manual_review_item(reagent, name_result, reason=manual_reason)

    def write_failure_can_skip_manual_review(self, suggestion: dict[str, Any]) -> bool:
        rule_category = str(suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b") or "").strip()
        if not rule_category or not to_erp_property(rule_category, self.settings):
            return False
        if str(suggestion.get("\u9700\u4eba\u5de5\u590d\u6838")).strip().lower() in {"true", "1", "yes"}:
            return False
        try:
            confidence = float(suggestion.get("\u7f6e\u4fe1\u5ea6") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        threshold = float(
            os.getenv("APPROVAL_WRITE_MIN_CONFIDENCE")
            or getattr(self, "settings", {}).get("approval", {}).get("write_min_confidence", 0.8)
        )
        return confidence >= threshold

    def stabilize_reagent_detail_after_write_failure(self, page: Page) -> bool:
        try:
            self.wait_for_reagent_table_ready(page)
            page.wait_for_timeout(800)
        except Exception as error:
            print(f"Reagent table was not ready while stabilizing after write failure: {error}")
            return self.recover_reagent_detail_page_after_write_failure(page, "stabilize after write failure")

        if self.reagent_property_header_available(page):
            return True

        print("Physicochemical property header is not visible after write failure; recovering detail page.")
        return self.recover_reagent_detail_page_after_write_failure(page, "missing property header after write failure")

    def reagent_property_header_available(self, page: Page) -> bool:
        try:
            header = page.locator("thead th").filter(has_text="\u7269\u5316\u7279\u6027").first
            if header.count() and header.is_visible():
                return True
        except Exception:
            pass
        try:
            return bool(
                page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('thead th')).some((th) => {
                      const rect = th.getBoundingClientRect();
                      const style = window.getComputedStyle(th);
                      const text = (th.innerText || th.textContent || '').replace(/\\s+/g, '');
                      return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && text.includes('\u7269\u5316\u7279\u6027');
                    })
                    """
                )
            )
        except Exception:
            return False

    def clear_existing_edit_state(self, page: Page, writer: ApprovalWriter, sequence: str) -> bool:
        if not writer.any_row_is_editing(page):
            return True
        print(f"An existing edit row is open before sequence {sequence}; cancelling it first.")
        if writer.cancel_any_edit(page):
            return True
        return self.recover_reagent_detail_page_after_write_failure(page, "existing edit row before write")

    def settle_after_successful_write(self, page: Page, writer: ApprovalWriter, sequence: str) -> bool:
        writer.dismiss_open_dropdown(page)
        try:
            self.wait_for_reagent_table_ready(page)
        except Exception as error:
            print(f"Reagent table was not ready immediately after saving sequence {sequence}: {error}")
        page.wait_for_timeout(500)
        for _ in range(10):
            if not writer.any_row_is_editing(page):
                return True
            page.wait_for_timeout(250)
        print(f"Edit controls are still visible after saving sequence {sequence}; cancelling before next row.")
        writer.cancel_any_edit(page)
        page.wait_for_timeout(300)
        return not writer.any_row_is_editing(page)

    def cleanup_failed_write(
        self,
        page: Page,
        writer: ApprovalWriter,
        row: Any,
        sequence: str,
        reason: str,
    ) -> bool:
        cleaned = writer.cancel_edit(page, row)
        if cleaned:
            print(f"Cancelled edit state after failed write for sequence {sequence}: {reason}")
        else:
            print(f"Could not fully cancel edit state after failed write for sequence {sequence}: {reason}")
            cleaned = self.recover_reagent_detail_page_after_write_failure(page, f"failed write for sequence {sequence}")
        return cleaned

    def recover_reagent_detail_page_after_write_failure(self, page: Page, reason: str) -> bool:
        target_list_number = self.current_detail_list_number() or str(getattr(self, "target_list_number", "") or "").strip()
        print(
            "Recovering reagent detail page after write-state failure"
            f" ({reason}); target list: {target_list_number or '<current>'}."
        )
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        except Exception:
            pass
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as error:
            print(f"Normal detail page reload failed; continuing with current page: {error}")

        if self.current_page_is_target_detail(page, target_list_number):
            try:
                self.wait_for_reagent_table_ready(page)
                page.wait_for_timeout(1000)
                return True
            except Exception as error:
                print(f"Target detail page is visible, but reagent table was not ready after reload: {error}")

        if target_list_number:
            print(
                "Detail page recovery will reopen the target list from the todo page "
                "and let the next loop re-sort/re-read current '-' rows."
            )
            try:
                opened = self.open_task_detail_by_list_number(page, target_list_number)
            except Exception as error:
                print(f"Could not reopen target detail {target_list_number}: {error}")
                return False
            if not opened:
                print(f"Could not reopen target detail after write failure: {target_list_number}")
                return False
            try:
                self.wait_for_reagent_table_ready(page)
                page.wait_for_timeout(1000)
                self._current_detail_info = self.read_detail_info(page)
                return True
            except Exception as error:
                print(f"Reopened target detail, but reagent table was not ready: {error}")
                return False

        try:
            self.wait_for_reagent_table_ready(page)
            page.wait_for_timeout(1000)
            return True
        except Exception as error:
            print(f"Reagent table was not ready after reload and no target list was known: {error}")
            return False

    def current_page_is_target_detail(self, page: Page, target_list_number: str = "") -> bool:
        target_list_number = str(target_list_number or "").strip()
        try:
            detail_info = self.read_detail_info(page)
        except Exception:
            detail_info = {}
        current_list_number = str(detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7") or "").strip()
        if target_list_number and current_list_number and current_list_number != target_list_number:
            return False
        if target_list_number and not current_list_number:
            try:
                body_text = str(page.locator("body").inner_text(timeout=3000) or "")
            except Exception:
                body_text = ""
            if target_list_number not in body_text:
                return False
        if not target_list_number and not current_list_number:
            try:
                if not self.reagent_property_header_available(page):
                    return False
            except Exception:
                return False
        try:
            records = self.read_current_page_reagents(page)
        except Exception:
            records = []
        if any(str(record.get("\u5e8f\u53f7") or "").strip() for record in records):
            return True
        try:
            row_count = page.locator("tbody tr.ant-table-row").count()
            return bool(row_count and self.reagent_property_header_available(page))
        except Exception:
            return False

    def capture_write_failure(self, page: Page, sequence: str, attempt: int, reason: str) -> None:
        safe_sequence = "".join(ch for ch in str(sequence or "unknown") if ch.isalnum() or ch in {"-", "_"})
        safe_reason = "".join(ch if ch.isalnum() else "_" for ch in str(reason or "failure"))[:60].strip("_")
        prefix = f"write_fail_{safe_sequence}_attempt{attempt}_{safe_reason or 'failure'}"
        try:
            screenshot_path = self._log_dir() / f"{prefix}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"Saved write failure screenshot: {screenshot_path}")
        except Exception as error:
            print(f"Could not save write failure screenshot for sequence {sequence}: {error}")
        try:
            html_path = self._log_dir() / f"{prefix}.html"
            html_path.write_text(page.content(), encoding="utf-8")
            print(f"Saved write failure HTML: {html_path}")
        except Exception as error:
            print(f"Could not save write failure HTML for sequence {sequence}: {error}")

    def approval_write_failure_break_limit(self) -> int:
        approval_settings = getattr(self, "settings", {}).get("approval", {}) or {}
        value = os.getenv("APPROVAL_WRITE_FAILURE_BREAK_LIMIT") or approval_settings.get("write_failure_break_limit", 2)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def prepare_reagent_row_for_write(page: Page, row: Any) -> None:
        try:
            row.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            row.evaluate(
                """
                (node) => {
                  const rect = node.getBoundingClientRect();
                  const targetY = window.scrollY + rect.top - Math.max(80, window.innerHeight * 0.35);
                  window.scrollTo({ top: Math.max(0, targetY), behavior: 'instant' });
                  node.scrollIntoView({ block: 'center', inline: 'nearest' });
                }
                """
            )
        except Exception:
            pass
        try:
            page.wait_for_timeout(250)
        except Exception:
            pass

    def suggestion_work_key(self, suggestion: dict[str, Any]) -> str:
        return self.reagent_work_key(
            {
                "\u5e8f\u53f7": suggestion.get("\u5e8f\u53f7", ""),
                "\u8bd5\u5242\u540d\u79f0": suggestion.get("\u8bd5\u5242\u540d\u79f0", ""),
                "CAS\u53f7": suggestion.get("CAS\u53f7", ""),
                "\u89c4\u683c": suggestion.get("\u89c4\u683c", ""),
                "\u89c4\u683c\u5355\u4f4d": suggestion.get("\u89c4\u683c\u5355\u4f4d", ""),
            }
        )

    @staticmethod
    def property_value_matches(value: str, expected: str, writer: ApprovalWriter) -> bool:
        normalized = " ".join(str(value or "").split())
        if not normalized:
            return False
        candidates = set(writer.property_name_candidates(expected))
        if normalized in candidates:
            return True

        # Ant Design fixed columns can duplicate the same cell text when the
        # table is read from both the main body and the fixed action/body panes.
        # Treat repeated equivalent labels such as "普通类 普通类" as a match.
        parts = [part for part in normalized.split(" ") if part]
        return bool(parts) and all(part in candidates for part in parts)

    def approval_write_mode(self) -> str:
        configured = (getattr(self, "settings", {}).get("approval", {}) or {}).get("write_mode", "multi_page")
        return str(os.getenv("APPROVAL_WRITE_MODE") or configured or "multi_page").strip().lower()

    def high_confidence_write_candidates(self, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        threshold = float(os.getenv("APPROVAL_WRITE_MIN_CONFIDENCE") or getattr(self, "settings", {}).get("approval", {}).get("write_min_confidence", 0.8))
        output: list[dict[str, Any]] = []
        for suggestion in suggestions:
            rule_category = str(suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b") or "").strip()
            if not rule_category:
                continue
            erp_category = to_erp_property(rule_category, self.settings)
            if not erp_category:
                print(f"Skipping write candidate with no ERP property mapping: {rule_category}")
                continue
            if str(suggestion.get("\u9700\u4eba\u5de5\u590d\u6838")).strip().lower() in {"true", "1", "yes"}:
                continue
            try:
                confidence = float(suggestion.get("\u7f6e\u4fe1\u5ea6") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < threshold:
                continue
            normalized = dict(suggestion)
            normalized["\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b"] = erp_category
            if erp_category != rule_category:
                normalized["\u89c4\u5219\u5224\u5b9a\u7c7b\u522b"] = rule_category
            output.append(normalized)
        return output

    def _classification_input(
        self,
        reagent: dict[str, str],
        search_result: dict[str, Any],
        extracted: dict[str, Any],
    ) -> dict[str, Any]:
        text_parts = [
            reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
            reagent.get("CAS\u53f7", ""),
            search_result.get("raw_text", "")[:2000],
            extracted.get("toxicity", ""),
            " ".join(extracted.get("suggested_categories", []) or []),
            " ".join(extracted.get("evidence", []) or []),
        ]
        return {
            "reagent_name": reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
            "name": search_result.get("name", ""),
            "standard_name": (search_result.get("name_normalization", {}) or {}).get("standard_name", ""),
            "cleaned_name": (search_result.get("name_normalization", {}) or {}).get("cleaned_name", ""),
            "english_name": (search_result.get("name_normalization", {}) or {}).get("english_name", ""),
            "cas": reagent.get("CAS\u53f7", ""),
            "text": " ".join(str(part) for part in text_parts if part),
            "flash_point": extracted.get("flash_point", ""),
            "boiling_point": extracted.get("boiling_point", ""),
            "toxicity": extracted.get("toxicity", ""),
            "corrosive": extracted.get("corrosive"),
            "oxidizing": extracted.get("oxidizing"),
            "flammable": extracted.get("flammable"),
            "water_reactive": extracted.get("water_reactive"),
            "explosive_risk": extracted.get("explosive_risk"),
            "heavy_metal": extracted.get("heavy_metal"),
            "suggested_categories": extracted.get("suggested_categories", []),
            "evidence": extracted.get("evidence", []),
            "allow_default_normal": bool(
                not search_result.get("need_manual_review", True)
                and search_result.get("relevance_passed", False)
            ),
        }

    def mark_duplicate_search_url_if_needed(
        self,
        reagent: dict[str, str],
        search_result: dict[str, Any],
        seen_search_urls: dict[str, str],
    ) -> None:
        url = str(search_result.get("url") or "").strip()
        if not url:
            return

        reagent_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "")
        previous_name = seen_search_urls.get(url)
        if previous_name and previous_name != reagent_name:
            search_result["need_manual_review"] = True
            search_result["relevance_passed"] = False
            search_result["raw_text"] = (
                f"Duplicate search URL was returned for different reagents. "
                f"Current reagent: {reagent_name}; previous reagent: {previous_name}; url: {url}"
            )
            print(f"Duplicate search URL detected; forcing manual review: {reagent_name} -> {url}")
            return

        seen_search_urls[url] = reagent_name

    def _approval_suggestion_row(
        self,
        reagent: dict[str, str],
        name_result: dict[str, Any],
        search_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "\u5e8f\u53f7": reagent.get("\u5e8f\u53f7", ""),
            "\u8bd5\u5242\u540d\u79f0": reagent.get("\u8bd5\u5242\u540d\u79f0", ""),
            "CAS\u53f7": reagent.get("CAS\u53f7", ""),
            "\u89c4\u683c": reagent.get("\u89c4\u683c", ""),
            "\u89c4\u683c\u5355\u4f4d": reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
            "\u8bd5\u5242\u6570\u91cf": reagent.get("\u8bd5\u5242\u6570\u91cf", ""),
            "\u6807\u51c6\u5316\u540d\u79f0": name_result.get("standard_name", ""),
            "\u82f1\u6587\u540d\u79f0": name_result.get("english_name", ""),
            "\u6e05\u6d17\u540e\u540d\u79f0": name_result.get("cleaned_name", ""),
            "\u6d53\u5ea6": name_result.get("concentration", ""),
            "\u540d\u79f0\u6807\u51c6\u5316\u7f6e\u4fe1\u5ea6": name_result.get("confidence", 0.0),
            "\u540d\u79f0\u9700\u4eba\u5de5\u590d\u6838": name_result.get("need_manual_review", True),
            "\u540d\u79f0\u6807\u51c6\u5316\u539f\u56e0": name_result.get("reason", ""),
            "\u7591\u4f3c\u9519\u8bef\u540d\u79f0": name_result.get("suspected_invalid_name", False),
            "\u5019\u9009\u4fee\u6b63\u540d\u79f0": ", ".join(name_result.get("candidate_names", []) or []),
            "\u7591\u4f3c\u9519\u8bef\u539f\u56e0": name_result.get("suspected_invalid_reason", ""),
            "\u67e5\u8be2\u6765\u6e90": search_result.get("source", ""),
            "\u67e5\u8be2URL": search_result.get("url", ""),
            "\u67e5\u8be2\u9700\u4eba\u5de5": search_result.get("need_manual_review", False),
            "\u7f51\u7ad9\u5339\u914d\u540d\u79f0": search_result.get("matched_site_name", ""),
            "\u540d\u79f0\u76f8\u4f3c\u5ea6": search_result.get("name_similarity", 0.0),
            "\u67e5\u8be2\u76f8\u5173\u6027\u901a\u8fc7": search_result.get("relevance_passed", False),
            "\u515c\u5e95\u67e5\u8be2\u6765\u6e90": search_result.get("fallback_source", ""),
            "\u515c\u5e95\u67e5\u8be2URL": search_result.get("fallback_url", ""),
            "\u8d44\u6599\u53ef\u4fe1\u5ea6": search_result.get("source_confidence", 0.0),
            "\u8bc1\u636e\u8d28\u91cf": search_result.get("evidence_quality", ""),
            "\u67e5\u8be2\u5931\u8d25\u539f\u56e0": search_result.get("failure_reason", ""),
            "\u662f\u5426\u4f7f\u7528\u5927\u6a21\u578b\u751f\u6210\u5019\u9009\u540d": search_result.get("used_llm_search_candidates", False),
            "\u5927\u6a21\u578b\u5019\u9009\u641c\u7d22\u8bcd": ", ".join(search_result.get("llm_search_candidates", []) or []),
            "\u95ea\u70b9": extracted.get("flash_point", ""),
            "\u6cb8\u70b9": extracted.get("boiling_point", ""),
            "\u6bd2\u6027": extracted.get("toxicity", ""),
            "\u8150\u8680\u6027": extracted.get("corrosive"),
            "\u6c27\u5316\u6027": extracted.get("oxidizing"),
            "\u6613\u71c3": extracted.get("flammable"),
            "\u9047\u6c34\u53cd\u5e94": extracted.get("water_reactive"),
            "\u7206\u70b8\u98ce\u9669": extracted.get("explosive_risk"),
            "\u91cd\u91d1\u5c5e": extracted.get("heavy_metal"),
            "\u5927\u6a21\u578b\u5019\u9009\u7c7b\u522b": ", ".join(extracted.get("suggested_categories", []) or []),
            "\u8bc1\u636e": " | ".join(extracted.get("evidence", []) or []),
            "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b": classification.get("final_category", ""),
            "\u547d\u4e2d\u7c7b\u522b": ", ".join(classification.get("matched_categories", []) or []),
            "\u89c4\u5219\u539f\u56e0": classification.get("reason", ""),
            "\u7f6e\u4fe1\u5ea6": classification.get("confidence", 0.0),
            "\u9700\u4eba\u5de5\u590d\u6838": self._suggestion_needs_manual_review(
                search_result,
                name_result,
                extracted,
                classification,
            ),
        }

    def _suggestion_needs_manual_review(
        self,
        search_result: dict[str, Any],
        name_result: dict[str, Any],
        extracted: dict[str, Any],
        classification: dict[str, Any],
    ) -> bool:
        evidence = extracted.get("evidence", []) or []
        llm_failed = any("LLM extraction failed" in str(item) for item in evidence)
        return bool(
            search_result.get("need_manual_review", True)
            or name_result.get("need_manual_review", True)
            or llm_failed
            or classification.get("need_manual_review", True)
        )

    def try_auto_pass_current_task(self, page: Page) -> None:
        if not self.auto_pass_enabled():
            print("Auto-pass skipped; AUTO_PASS is not true.")
            return

        detail_info = self.read_detail_info(page)
        list_number = detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7", "").strip()
        print(f"Auto-pass precheck for list: {list_number or '<unknown>'}")

        blocked_reasons: list[str] = []

        if not self.auto_match_succeeded:
            blocked_reasons.append("Auto-match did not complete cleanly.")

        if not list_number:
            blocked_reasons.append("Current reagent list number could not be read.")

        unmatched_records: list[dict[str, str]] = []
        try:
            unmatched_records = self.find_unmatched_reagents_across_all_pages(page)
            if not self.pagination_check_succeeded:
                blocked_reasons.append("Sorted unmatched reagent pages could not be verified.")
        except Exception as error:  # noqa: BLE001 - auto-pass must fail closed, not crash the run
            blocked_reasons.append(f"Could not verify unmatched reagent pages: {error}")

        if unmatched_records:
            blocked_reasons.append(
                f"Found {len(unmatched_records)} reagent row(s) with physicochemical property '-'."
            )

        has_manual_review, manual_reason = self.current_list_has_manual_review_item(list_number)
        if has_manual_review:
            blocked_reasons.append(manual_reason)

        if not self.all_save_operations_successful():
            blocked_reasons.append("One or more save operations failed.")

        if blocked_reasons:
            print("Auto-pass blocked; the top approve button was not clicked.")
            for reason in blocked_reasons:
                print(f"- {reason}")
            return

        self.click_top_approve_button(page)

    def auto_pass_enabled(self) -> bool:
        value = os.getenv("AUTO_PASS", "")
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    def record_save_result(self, name: str, success: bool, detail: str = "") -> None:
        if self.save_results is None:
            self.save_results = []
        self.save_results.append({"name": name, "success": success, "detail": detail})

    def all_save_operations_successful(self) -> bool:
        if not self.save_results:
            print("No save operations were recorded; save precheck is treated as failed.")
            return False

        approval_save_prefixes = ("erp_", "page_", "reagent_save")
        approval_saves = [
            result
            for result in self.save_results
            if str(result.get("name", "")).startswith(approval_save_prefixes)
        ]

        if not approval_saves:
            local_saves = [
                result
                for result in self.save_results
                if str(result.get("name", "")).startswith("local_")
            ]
            failed_local = [result for result in local_saves if not result.get("success")]
            if local_saves and not failed_local:
                print("No ERP/page save operations were needed; local save operation(s) succeeded.")
                return True
            print("No ERP/page save operations were recorded; auto-pass save precheck failed.")
            return False

        failed = [result for result in approval_saves if not result.get("success")]
        if failed:
            print(f"Failed save operation(s): {failed}")
            return False

        print(f"All recorded ERP/page save operation(s) succeeded: {len(approval_saves)}")
        return True

    def click_top_approve_button(self, page: Page) -> None:
        approve_button = self.find_top_approve_button(page)
        if not approve_button:
            raise RuntimeError("AUTO_PASS is true and checks passed, but the top approve button was not found.")

        print("AUTO_PASS checks passed; clicking the top approve button.")
        approve_button.click()
        page.wait_for_timeout(1000)

        prompt_text = self.capture_prompt_if_present(page, "auto_pass_prompt.png")
        if prompt_text:
            print(f"Prompt after clicking approve: {prompt_text}")
        else:
            print("Top approve button clicked; no prompt was detected.")

    def find_top_approve_button(self, page: Page) -> Locator | None:
        selectors = self.settings.get("selectors", {})
        configured_selector = selectors.get("approve_button", "").strip()

        candidates: list[Locator] = []
        if configured_selector:
            candidates.append(page.locator(configured_selector).first)

        candidates.extend(
            [
                page.locator(".ant-page-header button").filter(has_text="\u901a\u8fc7").first,
                page.get_by_role("button", name="\u901a\u8fc7").first,
                page.locator("button").filter(has_text="\u901a\u8fc7").first,
            ]
        )

        for candidate in candidates:
            try:
                if candidate.count() and candidate.is_visible():
                    return candidate
            except Error:
                continue

        return None

    def approval_suggestion_columns(self) -> list[str]:
        return [
            "\u5e8f\u53f7",
            "\u8bd5\u5242\u540d\u79f0",
            "CAS\u53f7",
            "\u89c4\u683c",
            "\u89c4\u683c\u5355\u4f4d",
            "\u8bd5\u5242\u6570\u91cf",
            "\u6807\u51c6\u5316\u540d\u79f0",
            "\u82f1\u6587\u540d\u79f0",
            "\u6e05\u6d17\u540e\u540d\u79f0",
            "\u6d53\u5ea6",
            "\u540d\u79f0\u6807\u51c6\u5316\u7f6e\u4fe1\u5ea6",
            "\u540d\u79f0\u9700\u4eba\u5de5\u590d\u6838",
            "\u540d\u79f0\u6807\u51c6\u5316\u539f\u56e0",
            "\u7591\u4f3c\u9519\u8bef\u540d\u79f0",
            "\u5019\u9009\u4fee\u6b63\u540d\u79f0",
            "\u7591\u4f3c\u9519\u8bef\u539f\u56e0",
            "\u67e5\u8be2\u6765\u6e90",
            "\u67e5\u8be2URL",
            "\u67e5\u8be2\u9700\u4eba\u5de5",
            "\u7f51\u7ad9\u5339\u914d\u540d\u79f0",
            "\u540d\u79f0\u76f8\u4f3c\u5ea6",
            "\u67e5\u8be2\u76f8\u5173\u6027\u901a\u8fc7",
            "\u515c\u5e95\u67e5\u8be2\u6765\u6e90",
            "\u515c\u5e95\u67e5\u8be2URL",
            "\u8d44\u6599\u53ef\u4fe1\u5ea6",
            "\u8bc1\u636e\u8d28\u91cf",
            "\u67e5\u8be2\u5931\u8d25\u539f\u56e0",
            "\u662f\u5426\u4f7f\u7528\u5927\u6a21\u578b\u751f\u6210\u5019\u9009\u540d",
            "\u5927\u6a21\u578b\u5019\u9009\u641c\u7d22\u8bcd",
            "\u95ea\u70b9",
            "\u6cb8\u70b9",
            "\u6bd2\u6027",
            "\u8150\u8680\u6027",
            "\u6c27\u5316\u6027",
            "\u6613\u71c3",
            "\u9047\u6c34\u53cd\u5e94",
            "\u7206\u70b8\u98ce\u9669",
            "\u91cd\u91d1\u5c5e",
            "\u5927\u6a21\u578b\u5019\u9009\u7c7b\u522b",
            "\u8bc1\u636e",
            "\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b",
            "\u547d\u4e2d\u7c7b\u522b",
            "\u89c4\u5219\u539f\u56e0",
            "\u7f6e\u4fe1\u5ea6",
            "\u9700\u4eba\u5de5\u590d\u6838",
        ]
