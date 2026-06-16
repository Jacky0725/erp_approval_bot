from __future__ import annotations

import os
from typing import Any

from playwright.sync_api import Error, Locator, Page

import pandas as pd

from chemical_searcher import ChemicalSearcher
from llm_extractor import LlmExtractor
from rule_engine import RuleEngine


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
        self.run_after_login_capture(
            screenshot_name="after_auto_match.png",
            html_name="after_auto_match.html",
            after_login=self.generate_approval_suggestions,
        )

    def generate_approval_suggestions(self, page: Page) -> None:
        self.save_results = []
        self.auto_match_succeeded = False
        self.pagination_check_succeeded = False
        if not self.perform_auto_match(page):
            print("Semi-auto approval suggestions stopped because no detail page or auto-match result is available.")
            return
        self.wait_for_reagent_table_ready(page)
        self._current_detail_info = self.read_detail_info(page)

        property_key = "\u7269\u5316\u7279\u6027"
        name_key = "\u8bd5\u5242\u540d\u79f0"
        cas_key = "CAS\u53f7"
        sort_succeeded = self.sort_property_column_until_unmatched_visible(page)
        if not sort_succeeded:
            print("Sorting did not bring '-' into the first rows within 4 clicks; reading current page '-' rows anyway.")

        unmatched_reagents = [
            record
            for record in self.read_current_page_reagents(page)
            if record.get(property_key, "").strip() == "-"
        ]

        print(f"Found {len(unmatched_reagents)} current-page reagent row(s) with physicochemical property '-'.")

        searcher = ChemicalSearcher(settings=self.settings, root_dir=self.root_dir)
        extractor = LlmExtractor(settings=self.settings)
        rule_engine = RuleEngine.from_settings(self.settings, self.root_dir)
        suggestions: list[dict[str, Any]] = []
        seen_search_urls: dict[str, str] = {}

        for index, reagent in enumerate(unmatched_reagents, start=1):
            reagent_name = reagent.get(name_key, "").strip()
            cas = reagent.get(cas_key, "").strip()
            print(f"Processing reagent {index}/{len(unmatched_reagents)}: {reagent_name} / {cas}")

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
                self.add_manual_review_item_from_search_failure(reagent, name_result, search_result)
            extracted = extractor.extract_properties(
                raw_text=search_result.get("raw_text", ""),
                name=f"{reagent_name} / {search_result.get('name') or str(search_name)}",
                cas=search_result.get("cas") or str(search_cas),
            )
            classification = rule_engine.classify(self._classification_input(reagent, search_result, extracted))
            suggestions.append(
                self._approval_suggestion_row(reagent, name_result, search_result, extracted, classification)
            )

        output_path = self._log_dir() / "approval_suggestions.xlsx"
        output_path = self.write_excel_with_fallback(
            pd.DataFrame(suggestions, columns=self.approval_suggestion_columns()),
            output_path,
        )
        print(f"Saved approval suggestions: {output_path}")
        self.record_save_result("local_approval_suggestions", True, str(output_path))

        self.try_auto_pass_current_task(page)

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
        low_extraction_confidence = float(extracted.get("confidence") or 0.0) < 0.35
        return bool(
            search_result.get("need_manual_review", True)
            or name_result.get("need_manual_review", True)
            or llm_failed
            or low_extraction_confidence
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
