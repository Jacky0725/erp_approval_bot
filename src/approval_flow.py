from __future__ import annotations

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
        after_login = (
            self.generate_all_todo_approval_suggestions
            if self.process_all_todos_enabled()
            else self.generate_approval_suggestions
        )
        self.run_after_login_capture(
            screenshot_name="after_auto_match.png",
            html_name="after_auto_match.html",
            after_login=after_login,
        )

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

        with stage_logger.stage("sort_property_column"):
            sort_succeeded = self.sort_property_column_until_unmatched_visible(page)
        if not sort_succeeded:
            print("Sorting did not bring '-' into the first rows within 4 clicks; reading current page '-' rows anyway.")

        searcher = ChemicalSearcher(settings=self.settings, root_dir=self.root_dir)
        extractor = LlmExtractor(settings=self.settings)
        rule_engine = RuleEngine.from_settings(self.settings, self.root_dir)
        rule_maintainer = RuleMaintainer.from_settings(self.settings, self.root_dir)
        seen_search_urls: dict[str, str] = {}

        if self.approval_write_mode() == "multi_page":
            suggestions = self.process_unmatched_reagent_pages(
                page,
                searcher,
                extractor,
                rule_engine,
                rule_maintainer,
                seen_search_urls,
            )
        else:
            suggestions = self.process_current_unmatched_reagent_page(
                page,
                searcher,
                extractor,
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
        searcher: ChemicalSearcher,
        extractor: LlmExtractor,
        rule_engine: RuleEngine,
        rule_maintainer: RuleMaintainer,
        seen_search_urls: dict[str, str],
    ) -> list[dict[str, Any]]:
        if not self.goto_first_reagent_page(page):
            print("Could not move to first reagent page; multi-page mode will continue from current page.")

        all_suggestions: list[dict[str, Any]] = []
        visited_pages = 0

        while True:
            visited_pages += 1
            current_page = self.current_reagent_page_number(page) or str(visited_pages)
            page_suggestions = self.process_current_unmatched_reagent_page(
                page,
                searcher,
                extractor,
                rule_engine,
                rule_maintainer,
                seen_search_urls,
                page_label=current_page,
            )
            all_suggestions.extend(page_suggestions)

            if page_suggestions:
                with self.stage_logger.stage("apply_approval_write_mode", f"page {current_page}"):
                    self.apply_approval_write_mode(page, page_suggestions)

            moved_next, terminal_or_error = self.click_next_reagent_page(page)
            if not moved_next:
                if terminal_or_error:
                    print("Multi-page mode reached the last reagent page.")
                else:
                    print("Multi-page mode stopped because next-page navigation could not be verified.")
                break

            next_unmatched = self.current_page_unmatched_reagents(page)
            if not next_unmatched:
                print("Next sorted reagent page has no '-' rows; multi-page mode considers this list complete.")
                break

            if visited_pages >= 200:
                raise RuntimeError("Stopped multi-page approval after 200 pages; page navigation may be stuck.")

        return all_suggestions

    def process_current_unmatched_reagent_page(
        self,
        page: Page,
        searcher: ChemicalSearcher,
        extractor: LlmExtractor,
        rule_engine: RuleEngine,
        rule_maintainer: RuleMaintainer,
        seen_search_urls: dict[str, str],
        page_label: str = "",
    ) -> list[dict[str, Any]]:
        stage_logger = getattr(self, "stage_logger", None) or StageLogger()
        property_key = "\u7269\u5316\u7279\u6027"
        name_key = "\u8bd5\u5242\u540d\u79f0"
        cas_key = "CAS\u53f7"

        with stage_logger.stage("read_current_page_unmatched", f"page {page_label}".strip()):
            unmatched_reagents = [
                record
                for record in self.read_current_page_reagents(page)
                if record.get(property_key, "").strip() == "-"
            ]

        page_text = f" page {page_label}" if page_label else ""
        print(f"Found {len(unmatched_reagents)} current-page{page_text} reagent row(s) with physicochemical property '-'.")

        suggestions: list[dict[str, Any]] = []
        for index, reagent in enumerate(unmatched_reagents, start=1):
            reagent_name = reagent.get(name_key, "").strip()
            cas = reagent.get(cas_key, "").strip()
            progress = f"{index}/{len(unmatched_reagents)}"
            if page_label:
                progress = f"page {page_label} {progress}"
            stage_logger.event(f"Processing reagent {progress}: {reagent_name} / {cas}")

            direct_suggestion = self.direct_business_rule_suggestion(reagent, rule_engine)
            if direct_suggestion:
                suggestions.append(direct_suggestion)
                sequence = reagent.get("\u5e8f\u53f7", "")
                category = direct_suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b", "")
                print(
                    "Direct business rule suggestion: "
                    f"{sequence} {reagent_name} -> {category}"
                )
                continue

            with stage_logger.stage("chemical_search", f"{progress} {reagent_name}"):
                search_result = searcher.search(
                    reagent_name,
                    cas=cas,
                    specification=reagent.get("\u89c4\u683c", ""),
                    unit=reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
                )
            name_result = search_result.get("name_normalization", {})
            self.mark_duplicate_search_url_if_needed(reagent, search_result, seen_search_urls)
            search_name = search_result.get("name") or name_result.get("standard_name") or name_result.get("cleaned_name") or reagent_name
            search_cas = search_result.get("cas") or name_result.get("cas") or cas
            if search_result.get("need_manual_review"):
                with stage_logger.stage("add_manual_review_item", reagent_name):
                    self.add_manual_review_item_from_search_failure(reagent, name_result, search_result)
            with stage_logger.stage("llm_extract", f"{progress} {reagent_name}"):
                extracted = extractor.extract_properties(
                    raw_text=search_result.get("raw_text", ""),
                    name=f"{reagent_name} / {search_result.get('name') or str(search_name)}",
                    cas=search_result.get("cas") or str(search_cas),
                )
            with stage_logger.stage("rule_classify", reagent_name):
                classification = rule_engine.classify(self._classification_input(reagent, search_result, extracted))
            try:
                with stage_logger.stage("record_rule_candidate", reagent_name):
                    if rule_maintainer.record_candidate(reagent, name_result, search_result, extracted, classification):
                        print(f"Recorded pending rule candidate: {reagent_name}")
            except Exception as error:
                print(f"Could not record rule candidate for {reagent_name}: {error}")
            suggestions.append(
                self._approval_suggestion_row(reagent, name_result, search_result, extracted, classification)
            )

        return suggestions

    def direct_business_rule_suggestion(
        self,
        reagent: dict[str, str],
        rule_engine: RuleEngine,
    ) -> dict[str, Any] | None:
        reagent_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "")
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

        name_result = {
            "standard_name": reagent_name,
            "cleaned_name": reagent_name,
            "confidence": 0.95,
            "need_manual_review": False,
            "reason": "\u547d\u4e2d\u836f\u5178\u8272\u5ea6\u6807\u51c6\u54c1\u4e1a\u52a1\u89c4\u5219",
        }
        search_result = {
            "name": reagent_name,
            "cas": reagent.get("CAS\u53f7", ""),
            "source": "business_rule",
            "url": "",
            "raw_text": "",
            "need_manual_review": False,
            "relevance_passed": True,
            "source_confidence": 0.95,
            "evidence_quality": "business_rule",
            "name_normalization": name_result,
        }
        extracted = {
            "suggested_categories": ["\u666e\u901a\u7c7b"],
            "evidence": [classification.get("reason", "")],
            "confidence": 0.95,
        }
        return self._approval_suggestion_row(reagent, name_result, search_result, extracted, classification)

    def apply_approval_write_mode(self, page: Page, suggestions: list[dict[str, Any]]) -> None:
        mode = self.approval_write_mode()
        if mode == "disabled":
            print("Approval write mode is disabled; no webpage fields will be changed.")
            return

        candidates = self.high_confidence_write_candidates(suggestions)
        if not candidates:
            print("No high-confidence approval suggestion is eligible for webpage writing.")
            return

        if mode in {"test_one", "save_one"}:
            candidates = candidates[:1]
        elif mode in {"single_page", "generate_library"}:
            pass
        elif mode == "multi_page":
            pass
        else:
            print(f"Unknown APPROVAL_WRITE_MODE={mode}; no webpage fields will be changed.")
            return

        writer = ApprovalWriter(settings=self.settings)
        for row_index, suggestion in enumerate(candidates, start=1):
            sequence = str(suggestion.get("\u5e8f\u53f7") or "").strip()
            category = str(suggestion.get("\u6700\u7ec8\u5efa\u8bae\u7c7b\u522b") or "").strip()
            reagent_name = str(suggestion.get("\u8bd5\u5242\u540d\u79f0") or "").strip()
            print(f"Approval write candidate {row_index}/{len(candidates)}: {sequence} {reagent_name} -> {category}")

            row = self.find_reagent_row_by_sequence(page, sequence)
            if row is None:
                self.record_save_result(f"reagent_save_{sequence}", False, "row not found")
                print(f"Could not find current-page row for sequence: {sequence}")
                continue

            opened = writer.open_technical_judgement(row)
            if not opened:
                self.record_save_result(f"reagent_save_{sequence}", False, "technical judgement button not found")
                print(f"Could not open technical judgement for sequence: {sequence}")
                continue

            page.wait_for_timeout(500)
            selected = writer.choose_property(page, category)
            if not selected:
                self.record_save_result(f"reagent_save_{sequence}", False, f"could not select {category}")
                print(f"Could not select physicochemical property {category} for sequence: {sequence}")
                continue

            screenshot_path = self._log_dir() / f"write_mode_{mode}_{sequence}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"Saved approval write screenshot before save: {screenshot_path}")

            if mode == "test_one":
                print("Test write mode: selected value for inspection only; not saving.")
                return

            saved = writer.save(row)
            self.record_save_result(f"reagent_save_{sequence}", saved, category)
            print(f"Save result for sequence {sequence}: {saved}")
            page.wait_for_timeout(800)

            if mode == "generate_library" and saved:
                generated = writer.generate_reagent_library(row)
                self.record_save_result(f"reagent_library_{sequence}", generated, category)
                print(f"Generate reagent library result for sequence {sequence}: {generated}")

            if mode == "save_one":
                return

    def approval_write_mode(self) -> str:
        configured = (self.settings.get("approval", {}) or {}).get("write_mode", "disabled")
        return str(os.getenv("APPROVAL_WRITE_MODE") or configured or "disabled").strip().lower()

    def high_confidence_write_candidates(self, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        threshold = float(os.getenv("APPROVAL_WRITE_MIN_CONFIDENCE") or self.settings.get("approval", {}).get("write_min_confidence", 0.8))
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
