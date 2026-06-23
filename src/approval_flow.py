from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from typing import Any

from playwright.sync_api import Error, Locator, Page

import pandas as pd

from approval_writer import ApprovalWriter
from chemical_searcher import ChemicalSearcher
from llm_extractor import LlmExtractor
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

        output_path = self._log_dir() / "approval_suggestions.xlsx"
        with stage_logger.stage("write_approval_suggestions"):
            output_path = self.write_excel_with_fallback(
                pd.DataFrame(suggestions, columns=self.approval_suggestion_columns()),
                output_path,
            )
        print(f"Saved approval suggestions: {output_path}")
        self.record_save_result("local_approval_suggestions", True, str(output_path))

        with stage_logger.stage("try_auto_pass_current_task"):
            self.try_auto_pass_current_task(page)

    def generate_all_todo_approval_suggestions(self, page: Page) -> None:
        original_target = getattr(self, "target_list_number", "")
        processed_list_numbers: set[str] = set()
        max_todos = self.max_process_all_todos_count()

        try:
            while len(processed_list_numbers) < max_todos:
                self.enter_reagent_judgement_page(page)
                tasks = self.read_todo_tasks(page)
                list_numbers = self.todo_list_numbers(tasks)
                next_list_number = self.next_unprocessed_list_number(tasks, processed_list_numbers)

                print(
                    f"Todo list refresh: {len(list_numbers)} visible task(s), "
                    f"{len(processed_list_numbers)} already processed in this run."
                )
                if not next_list_number:
                    print("No unprocessed todo task remains on the current todo page.")
                    break

                list_number = next_list_number
                print(f"Processing todo detail {len(processed_list_numbers) + 1}: {list_number}")
                self.target_list_number = list_number
                try:
                    self.generate_approval_suggestions(page)
                finally:
                    processed_list_numbers.add(list_number)

            if len(processed_list_numbers) >= max_todos:
                print(f"Stopped all-todo processing after PROCESS_ALL_TODOS_MAX={max_todos}.")
        finally:
            self.target_list_number = original_target

    def generate_selected_todo_approval_suggestions(self, page: Page) -> None:
        original_target = getattr(self, "target_list_number", "")
        selected = self.selected_todo_list_numbers()
        max_todos = self.max_process_all_todos_count()
        processed_count = 0

        try:
            self.enter_reagent_judgement_page(page)
            visible_tasks = set(self.todo_list_numbers(self.read_todo_tasks(page)))
            print(f"Selected todo list number(s): {', '.join(selected)}")
            print(f"Current visible todo list number(s): {', '.join(sorted(visible_tasks)) if visible_tasks else '<none>'}")

            for list_number in selected:
                if processed_count >= max_todos:
                    print(f"Stopped selected-todo processing after PROCESS_ALL_TODOS_MAX={max_todos}.")
                    break
                if visible_tasks and list_number not in visible_tasks:
                    print(f"Selected list is not visible in current todo page and will be skipped: {list_number}")
                    continue

                processed_count += 1
                print(f"Processing selected todo detail {processed_count}: {list_number}")
                self.target_list_number = list_number
                self.generate_approval_suggestions(page)
                self.enter_reagent_judgement_page(page)
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
        handled_reagent_keys: set[str] = set()
        write_attempts_by_key: dict[str, int] = {}
        visited_steps = 0
        refresh_sorted_first_page = True

        while True:
            visited_steps += 1
            if refresh_sorted_first_page:
                self.goto_first_reagent_page(page)
                self.sort_property_column_until_unmatched_visible(page)
                refresh_sorted_first_page = False

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
                    "but all were already processed, queued, or reached the write retry limit; moving to the next sorted page."
                )
                moved_next, terminal_or_error = self.click_next_reagent_page(page)
                if not moved_next:
                    if terminal_or_error:
                        print("Multi-page mode reached the last sorted reagent page.")
                    else:
                        print("Multi-page mode stopped because next-page navigation could not be verified.")
                    break
                continue

            if not current_unmatched:
                print("Current sorted reagent page has no '-' rows; multi-page mode considers this list complete.")
                break

            page_suggestions = self.process_current_unmatched_reagent_page(
                page,
                rule_engine,
                rule_maintainer,
                seen_search_urls,
                page_label=current_page,
                skip_reagent_keys=handled_reagent_keys,
            )
            all_suggestions.extend(page_suggestions)
            if page_suggestions:
                self.write_partial_approval_suggestions(all_suggestions)

            if page_suggestions:
                with self.stage_logger.stage("apply_approval_write_mode", f"page {current_page}"):
                    write_result = self.apply_approval_write_mode(page, page_suggestions) or {
                        "attempted": set(),
                        "handled": set(),
                        "failed": set(),
                    }
                for key in write_result.get("attempted", set()):
                    write_attempts_by_key[key] = write_attempts_by_key.get(key, 0) + 1
                handled_reagent_keys.update(write_result.get("handled", set()))
                for key in write_result.get("failed", set()):
                    if write_attempts_by_key.get(key, 0) >= self.max_reagent_write_attempts():
                        handled_reagent_keys.add(key)
                        print(f"Write retry limit reached for reagent key: {key}")
            else:
                write_result = {"attempted": set(), "handled": set(), "failed": set()}

            if self.approval_write_mode() in {"disabled", "test_one"}:
                handled_reagent_keys.update(self.reagent_work_key(reagent) for reagent in current_unmatched)
            if page_suggestions:
                refresh_sorted_first_page = True
                continue

            moved_next, terminal_or_error = self.click_next_reagent_page(page)
            if not moved_next:
                if terminal_or_error:
                    print("Multi-page mode reached the last reagent page.")
                else:
                    print("Multi-page mode stopped because next-page navigation could not be verified.")
                break

            if visited_steps >= 200:
                raise RuntimeError("Stopped multi-page approval after 200 pages; page navigation may be stuck.")

        return all_suggestions

    def write_partial_approval_suggestions(self, suggestions: list[dict[str, Any]]) -> None:
        if not suggestions:
            return
        output_path = self._log_dir() / "approval_suggestions_partial.xlsx"
        output_path = self.write_excel_with_fallback(
            pd.DataFrame(suggestions, columns=self.approval_suggestion_columns()),
            output_path,
        )
        print(f"Saved partial approval suggestions: {output_path}")

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
        for index, reagent in enumerate(unmatched_reagents, start=1):
            reagent_name = reagent.get(name_key, "").strip()
            cas = reagent.get(cas_key, "").strip()
            progress = f"{index}/{len(unmatched_reagents)}"
            if page_label:
                progress = f"page {page_label} {progress}"
            stage_logger.event(f"Processing reagent {progress}: {reagent_name} / {cas}")

            direct_suggestion = self.direct_business_rule_suggestion(reagent, rule_engine)
            if direct_suggestion:
                suggestions_by_index[index] = direct_suggestion
                sequence = reagent.get("\u5e8f\u53f7", "")
                category = direct_suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b", "")
                print(
                    "Direct business rule suggestion: "
                    f"{sequence} {reagent_name} -> {category}"
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

        return [suggestions_by_index[index] for index in sorted(suggestions_by_index)]

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
        kit_reason = self.product_kit_normal_reason(reagent_name)
        if kit_reason:
            classification = {
                "final_category": "\u666e\u901a\u7c7b",
                "matched_categories": ["\u666e\u901a\u7c7b"],
                "reason": kit_reason,
                "confidence": 0.9,
                "need_manual_review": False,
            }
            return self._direct_normal_suggestion(reagent, reagent_name, classification, kit_reason)

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
        if "\u836f\u5178\u8272\u5ea6" not in str(classification.get("reason", "")):
            return None
        return self._direct_normal_suggestion(
            reagent,
            reagent_name,
            classification,
            "\u547d\u4e2d\u836f\u5178\u8272\u5ea6\u6807\u51c6\u54c1\u4e1a\u52a1\u89c4\u5219",
        )

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

    def _direct_normal_suggestion(
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
            "suggested_categories": ["\u666e\u901a\u7c7b"],
            "evidence": [classification.get("reason", "")],
            "confidence": 0.95,
        }
        return self._approval_suggestion_row(reagent, name_result, search_result, extracted, classification)

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
            pass
        else:
            print(f"Unknown APPROVAL_WRITE_MODE={mode}; no webpage fields will be changed.")
            return result

        writer = ApprovalWriter(settings=self.settings)
        for row_index, suggestion in enumerate(candidates, start=1):
            sequence = str(suggestion.get("\u5e8f\u53f7") or "").strip()
            category = str(suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b") or "").strip()
            reagent_name = str(suggestion.get("\u8bd5\u5242\u540d\u79f0") or "").strip()
            work_key = self.suggestion_work_key(suggestion)
            result["attempted"].add(work_key)
            print(f"Approval write candidate {row_index}/{len(candidates)}: {sequence} {reagent_name} -> {category}")

            row = self.find_reagent_row_by_sequence(page, sequence)
            if row is None:
                self.record_save_result(f"reagent_save_{sequence}", False, "row not found")
                result["failed"].add(work_key)
                print(f"Could not find current-page row for sequence: {sequence}")
                continue

            opened = writer.open_technical_judgement(row)
            if not opened:
                self.record_save_result(f"reagent_save_{sequence}", False, "technical judgement button not found")
                result["failed"].add(work_key)
                print(f"Could not open technical judgement for sequence: {sequence}")
                continue

            page.wait_for_timeout(500)
            selected = writer.choose_property(page, category, row)
            if not selected:
                self.record_save_result(f"reagent_save_{sequence}", False, f"could not select {category}")
                result["failed"].add(work_key)
                print(f"Could not select physicochemical property {category} for sequence: {sequence}")
                continue

            selected_value = self.read_reagent_property_by_sequence(page, sequence)
            if selected_value and selected_value != "-" and not self.property_value_matches(selected_value, category, writer):
                self.record_save_result(
                    f"reagent_save_{sequence}",
                    False,
                    f"selected {category}, but row still shows {selected_value or '<empty>'}",
                )
                result["failed"].add(work_key)
                print(
                    f"Property selection verification failed for sequence {sequence}: "
                    f"expected {category}, got {selected_value or '<empty>'}."
                )
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
            self.record_save_result(
                f"reagent_save_{sequence}",
                verified,
                category if verified else f"saved={saved}, row shows {saved_value or '<empty>'}",
            )
            print(f"Save result for sequence {sequence}: {saved}")
            if verified:
                result["handled"].add(work_key)
            else:
                result["failed"].add(work_key)
                print(
                    f"Save verification failed for sequence {sequence}: "
                    f"expected {category}, got {saved_value or '<empty>'}."
                )

            if mode == "generate_library" and verified:
                generated = writer.generate_reagent_library(page, row)
                self.record_save_result(f"reagent_library_{sequence}", generated, category)
                print(f"Generate reagent library result for sequence {sequence}: {generated}")

            if mode == "save_one":
                return result

        return result

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
        normalized = str(value or "").strip()
        if not normalized:
            return False
        return normalized in writer.property_name_candidates(expected)

    def approval_write_mode(self) -> str:
        configured = (getattr(self, "settings", {}).get("approval", {}) or {}).get("write_mode", "disabled")
        return str(os.getenv("APPROVAL_WRITE_MODE") or configured or "disabled").strip().lower()

    def high_confidence_write_candidates(self, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        threshold = float(os.getenv("APPROVAL_WRITE_MIN_CONFIDENCE") or getattr(self, "settings", {}).get("approval", {}).get("write_min_confidence", 0.8))
        output: list[dict[str, Any]] = []
        for suggestion in suggestions:
            category = str(suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b") or "").strip()
            if not category:
                continue
            if str(suggestion.get("\u9700\u4eba\u5de5\u590d\u6838")).strip().lower() in {"true", "1", "yes"}:
                continue
            try:
                confidence = float(suggestion.get("\u7f6e\u4fe1\u5ea6") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < threshold:
                continue
            output.append(suggestion)
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
        detail_info = self.read_detail_info(page)
        list_number = detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7", "").strip()
        print(f"Auto-pass precheck for list: {list_number or '<unknown>'}")

        blocked_reasons: list[str] = []

        if not self.auto_pass_enabled():
            blocked_reasons.append("AUTO_PASS is not true.")

        if not self.auto_match_succeeded:
            blocked_reasons.append("Auto-match did not complete cleanly.")

        if not list_number:
            blocked_reasons.append("Current reagent list number could not be read.")

        unmatched_records = self.find_unmatched_reagents_across_all_pages(page)
        if not self.pagination_check_succeeded:
            blocked_reasons.append("Sorted unmatched reagent pages could not be verified.")

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
