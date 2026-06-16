from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from playwright.sync_api import Error, Locator, Page, TimeoutError, sync_playwright

from chemical_searcher import ChemicalSearcher
from llm_extractor import LlmExtractor
from rule_engine import RuleEngine


USERNAME_SELECTORS = [
    "input[name='username']",
    "input[name='userName']",
    "input[name='loginName']",
    "input[name='account']",
    "input[name='userid']",
    "#userName",
    "input[id*='user' i]",
    "input[placeholder*='\u7528\u6237\u540d']",
    "input[placeholder*='\u8d26\u53f7']",
    "input[placeholder*='\u5de5\u53f7']",
    "input[type='text']",
]

PASSWORD_SELECTORS = [
    "input[name='password']",
    "input[name='pwd']",
    "#password",
    "input[id*='pass' i]",
    "input[placeholder*='\u5bc6\u7801']",
    "input[type='password']",
]

LOGIN_BUTTON_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('\u767b\u5f55')",
    "button:has-text('\u767b\u9646')",
    "button:has-text('Login')",
    "a:has-text('\u767b\u5f55')",
    "text=\u767b\u5f55",
]


@dataclass
class BrowserBot:
    settings: dict[str, Any]
    root_dir: Path
    save_results: list[dict[str, Any]] | None = None
    auto_match_succeeded: bool = False
    pagination_check_succeeded: bool = False

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

    def run_after_login_capture(self, screenshot_name: str, html_name: str, after_login: Any | None) -> None:
        erp_url = os.getenv("ERP_URL")
        username = os.getenv("ERP_USERNAME")
        password = os.getenv("ERP_PASSWORD")

        if not erp_url or not username or not password:
            raise RuntimeError("Missing ERP_URL, ERP_USERNAME, or ERP_PASSWORD in .env")

        browser_settings = self.settings.get("browser", {})
        log_dir = self._log_dir()
        screenshot_path = log_dir / screenshot_name
        html_path = log_dir / html_name

        with sync_playwright() as playwright:
            last_error: Exception | None = None

            for attempt in range(1, 4):
                browser = playwright.chromium.launch(
                    headless=bool(browser_settings.get("headless", False)),
                    slow_mo=int(browser_settings.get("slow_mo_ms", 0)),
                )
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()
                page.set_default_timeout(int(browser_settings.get("timeout_ms", 30000)))

                try:
                    print(f"Opening ERP login page: {erp_url}")
                    print(f"Browser session attempt {attempt}/3")
                    self.open_login_page(page, erp_url)

                    self.login(page, username, password, log_dir)
                    self.wait_for_home(page)

                    if after_login:
                        after_login(page)

                    page.screenshot(path=str(screenshot_path), full_page=True)
                    html_path.write_text(page.content(), encoding="utf-8")

                    print(f"Saved homepage screenshot: {screenshot_path}")
                    print(f"Saved homepage HTML: {html_path}")
                    self.print_page_structure(page)
                    browser.close()
                    return
                except (Error, RuntimeError) as error:
                    last_error = error
                    print(f"Browser session failed: {error}")
                    browser.close()

            if last_error:
                raise last_error

    def enter_reagent_judgement_page(self, page: Page) -> None:
        menu_name = "\u8bd5\u5242\u7ba1\u7406"
        target_name = "\u8bd5\u5242\u5224\u5b9a"

        print(f"Opening menu: {menu_name}")
        self.click_visible_text(page, menu_name)
        page.wait_for_timeout(1000)

        print(f"Opening page: {target_name}")
        self.click_visible_text(page, target_name)
        self.wait_for_home(page)
        self.wait_for_table_ready(page)

        try:
            page.wait_for_selector(f"text={target_name}", timeout=10000)
        except TimeoutError:
            print(f"Target page text was not confirmed: {target_name}")

    def export_todo_tasks(self, page: Page) -> None:
        self.enter_reagent_judgement_page(page)

        tasks = self.read_todo_tasks(page)
        output_path = self._log_dir() / "todo_tasks.xlsx"

        if tasks:
            print(f"Found {len(tasks)} todo task(s):")
            for index, task in enumerate(tasks, start=1):
                print(f"{index}. {task}")
        else:
            print("No todo tasks found.")

        output_path = self.write_excel_with_fallback(pd.DataFrame(tasks, columns=self.todo_columns()), output_path)
        print(f"Saved todo tasks: {output_path}")

    def read_todo_tasks(self, page: Page) -> list[dict[str, str]]:
        return page.evaluate(
            """
            (wantedColumns) => {
              const displayText = (text) => (text || '').replace(/\\s+/g, ' ').trim();
              const columnIndexes = {
                ['\\u8bd5\\u5242\\u6e05\\u5355\\u53f7']: 1,
                ['\\u5ba2\\u6237\\u7f16\\u53f7']: 2,
                ['\\u5ba2\\u6237\\u540d\\u79f0']: 3,
                ['\\u6280\\u672f\\u5ba1\\u6279\\u8fdb\\u5ea6']: 4,
                ['\\u6280\\u672f\\u5ba1\\u6279\\u72b6\\u6001']: 5,
                ['\\u4e1a\\u52a1\\u5458']: 6,
                ['\\u7533\\u8bf7\\u4eba']: 7,
                ['\\u8054\\u7cfb\\u4eba']: 8,
              };
              const rows = Array.from(document.querySelectorAll('tbody tr.ant-table-row'));

              return rows.map((row) => {
                const cells = Array.from(row.querySelectorAll('td'));
                const task = {};

                for (const wanted of wantedColumns) {
                  const index = columnIndexes[wanted];
                  task[wanted] = cells[index] ? displayText(cells[index].innerText) : '';
                }

                return task;
              }).filter((task) => Object.values(task).some(Boolean));
            }
            """,
            self.todo_columns(),
        )

    def todo_columns(self) -> list[str]:
        return [
            "\u8bd5\u5242\u6e05\u5355\u53f7",
            "\u5ba2\u6237\u7f16\u53f7",
            "\u5ba2\u6237\u540d\u79f0",
            "\u6280\u672f\u5ba1\u6279\u8fdb\u5ea6",
            "\u6280\u672f\u5ba1\u6279\u72b6\u6001",
            "\u4e1a\u52a1\u5458",
            "\u7533\u8bf7\u4eba",
            "\u8054\u7cfb\u4eba",
        ]

    def open_first_task_detail(self, page: Page) -> bool:
        self.enter_reagent_judgement_page(page)

        tasks = self.read_todo_tasks(page)
        if not tasks:
            print("No todo tasks found; detail page was not opened.")
            return False

        list_number_key = "\u8bd5\u5242\u6e05\u5355\u53f7"
        customer_name_key = "\u5ba2\u6237\u540d\u79f0"
        approval_state_key = "\u6280\u672f\u5ba1\u6279\u72b6\u6001"
        applicant_key = "\u7533\u8bf7\u4eba"

        first_task = tasks[0]
        first_row = page.locator("tbody tr.ant-table-row").first
        detail_button = first_row.locator("button").filter(has_text="\u8be6\u60c5").first

        if not detail_button.count():
            raise RuntimeError("Could not find the first task detail button.")

        print(f"Opening first task detail: {first_task.get(list_number_key, '')}")
        detail_button.click()
        self.wait_for_detail_ready(page, first_task)

        detail_info = self.read_detail_info(page)
        merged_info = {
            "\u5f53\u524d\u6e05\u5355\u53f7": detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7") or first_task.get(list_number_key, ""),
            customer_name_key: detail_info.get(customer_name_key) or first_task.get(customer_name_key, ""),
            "\u72b6\u6001": detail_info.get("\u72b6\u6001") or first_task.get(approval_state_key, ""),
            applicant_key: detail_info.get(applicant_key) or first_task.get(applicant_key, ""),
        }

        print("Task detail:")
        for key, value in merged_info.items():
            print(f"{key}: {value}")
        return True

    def perform_auto_match(self, page: Page) -> bool:
        self.auto_match_succeeded = False
        if not self.open_first_task_detail(page):
            return False

        self.wait_for_reagent_table_ready(page)
        before_snapshot = self._auto_match_snapshot(page)

        auto_match_button = page.get_by_role("button", name="\u4e00\u952e\u5339\u914d").first
        if not auto_match_button.count():
            raise RuntimeError("Could not find the auto-match button.")

        auto_match_button.wait_for(state="visible", timeout=10000)
        auto_match_button.scroll_into_view_if_needed(timeout=10000)
        if not auto_match_button.is_enabled():
            print("Auto-match button is visible but disabled; auto-match was not clicked.")
            return False

        print("Clicking auto-match button.")
        auto_match_button.click(timeout=15000)
        page.wait_for_timeout(1500)

        prompt_text = self.capture_prompt_if_present(page, "auto_match_prompt.png")
        if prompt_text:
            print(f"Prompt after auto-match: {prompt_text}")
            return False

        self.wait_for_auto_match_ready(page)
        self.auto_match_succeeded = self._confirm_auto_match_result(page, before_snapshot)
        return self.auto_match_succeeded

    def wait_for_auto_match_ready(self, page: Page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except TimeoutError:
            print("Network did not become idle after auto-match; continuing with current page.")

        try:
            page.wait_for_selector(".ant-spin-spinning", state="hidden", timeout=15000)
        except TimeoutError:
            print("Auto-match loading state was not fully settled; capturing current page.")

        prompt_text = self.capture_prompt_if_present(page, "auto_match_prompt.png")
        if prompt_text:
            print(f"Prompt after auto-match: {prompt_text}")

    def _auto_match_snapshot(self, page: Page) -> dict[str, Any]:
        property_key = "\u7269\u5316\u7279\u6027"
        try:
            reagents = self.read_current_page_reagents(page)
        except Exception as error:
            print(f"Could not read auto-match snapshot: {error}")
            return {"rows": 0, "unmatched": 0, "signature": ""}

        properties = [str(record.get(property_key, "")).strip() for record in reagents]
        return {
            "rows": len(reagents),
            "unmatched": sum(1 for value in properties if value == "-"),
            "signature": "|".join(properties),
        }

    def _confirm_auto_match_result(self, page: Page, before_snapshot: dict[str, Any]) -> bool:
        prompt_text = self.capture_prompt_if_present(page, "auto_match_prompt.png")
        if prompt_text:
            print(f"Prompt after auto-match: {prompt_text}")
            return False

        if int(before_snapshot.get("unmatched") or 0) == 0:
            print("Auto-match confirmation skipped because no '-' rows existed before auto-match.")
            return True

        deadline = time.time() + 20
        last_snapshot: dict[str, Any] = {}
        while time.time() < deadline:
            last_snapshot = self._auto_match_snapshot(page)
            if int(last_snapshot.get("rows") or 0) and (
                int(last_snapshot.get("unmatched") or 0) < int(before_snapshot.get("unmatched") or 0)
                or str(last_snapshot.get("signature") or "") != str(before_snapshot.get("signature") or "")
            ):
                print(
                    "Auto-match confirmed: "
                    f"'-' rows {before_snapshot.get('unmatched')} -> {last_snapshot.get('unmatched')}."
                )
                return True
            page.wait_for_timeout(1000)

        screenshot_path = self._log_dir() / "auto_match_unconfirmed.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(
            "Auto-match was not confirmed: reagent table did not change after clicking. "
            f"Before '-' rows: {before_snapshot.get('unmatched')}; "
            f"after '-' rows: {last_snapshot.get('unmatched', '<unknown>')}. "
            f"Saved screenshot: {screenshot_path}"
        )
        return False

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

    def find_unmatched_reagents_across_all_pages(self, page: Page) -> list[dict[str, str]]:
        self.pagination_check_succeeded = False
        self.wait_for_reagent_table_ready(page)
        if not self.goto_first_reagent_page(page):
            return []

        sort_succeeded = self.sort_property_column_until_unmatched_visible(page)
        if not sort_succeeded:
            print("Could not confirm global sorting with '-' first; sorted-page check failed.")
            return []

        if not self.goto_first_reagent_page(page):
            return []

        property_key = "\u7269\u5316\u7279\u6027"
        unmatched: list[dict[str, str]] = []
        visited_pages = 0

        while True:
            visited_pages += 1
            current_page = self.current_reagent_page_number(page)
            reagents = self.read_current_page_reagents(page)
            page_unmatched = [
                record
                for record in reagents
                if record.get(property_key, "").strip() == "-"
            ]
            unmatched.extend(page_unmatched)
            print(
                f"Checked reagent page {current_page or visited_pages}: "
                f"{len(reagents)} row(s), {len(page_unmatched)} unmatched."
            )

            if not page_unmatched:
                print("Current sorted page has no '-' rows; remaining pages are considered complete.")
                self.pagination_check_succeeded = True
                break

            moved_next, terminal_or_error = self.click_next_reagent_page(page)
            if not moved_next:
                if not terminal_or_error:
                    print("Pagination check stopped before confirming the last page.")
                    return unmatched
                print("Reached the end of sorted pages while '-' rows were still present.")
                self.pagination_check_succeeded = True
                break

            if visited_pages >= 200:
                raise RuntimeError("Stopped pagination check after 200 pages; page navigation may be stuck.")

        if unmatched:
            output_path = self._log_dir() / "auto_pass_blocked_unmatched_reagents.xlsx"
            output_path = self.write_excel_with_fallback(
                pd.DataFrame(unmatched, columns=self.reagent_columns()),
                output_path,
            )
            print(f"Saved unmatched reagent rows that blocked auto-pass: {output_path}")

        return unmatched

    def goto_first_reagent_page(self, page: Page) -> bool:
        first_page = page.locator(".ant-pagination-item-1").first
        try:
            if first_page.count() and first_page.is_visible():
                class_name = first_page.get_attribute("class") or ""
                if "ant-pagination-item-active" not in class_name:
                    first_page.click()
                    self.wait_for_reagent_table_ready(page)
                    page.wait_for_timeout(500)
            return True
        except Error:
            print("Could not move to the first reagent page; continuing from current page.")
            return False

    def current_reagent_page_number(self, page: Page) -> str:
        try:
            active = page.locator(".ant-pagination-item-active").first
            if active.count():
                return self.safe_inner_text(active)
        except Error:
            return ""
        return ""

    def total_reagent_pages(self, page: Page) -> int:
        try:
            page_items = page.locator(".ant-pagination-item")
            values = []
            for index in range(page_items.count()):
                text = self.safe_inner_text(page_items.nth(index))
                if text.isdigit():
                    values.append(int(text))
            return max(values) if values else 1
        except Error:
            return 0

    def click_next_reagent_page(self, page: Page) -> tuple[bool, bool]:
        next_button = page.locator(".ant-pagination-next").first
        try:
            if not next_button.count() or not next_button.is_visible():
                return False, True

            class_name = next_button.get_attribute("class") or ""
            aria_disabled = (next_button.get_attribute("aria-disabled") or "").lower()
            if "ant-pagination-disabled" in class_name or aria_disabled == "true":
                return False, True

            before_page = self.current_reagent_page_number(page)
            next_button.click()
            self.wait_for_reagent_table_ready(page)
            page.wait_for_timeout(600)
            after_page = self.current_reagent_page_number(page)
            moved = after_page != before_page or not after_page
            return moved, moved
        except Error as error:
            print(f"Could not click next reagent page: {error}")
            return False, False

    def current_list_has_manual_review_item(self, list_number: str) -> tuple[bool, str]:
        if not list_number:
            return True, "Cannot check review queue because current list number is empty."

        paths = self.settings.get("paths", {})
        review_queue_path = self.root_dir / paths.get("review_queue_excel", "data/review_queue.xlsx")

        if not review_queue_path.exists():
            return True, f"Review queue file does not exist: {review_queue_path}"

        try:
            queue = pd.read_excel(review_queue_path, dtype=str).fillna("")
        except Exception as error:
            return True, f"Could not read review queue: {error}"

        if queue.empty:
            print(f"Review queue is empty: {review_queue_path}")
            return False, ""

        list_columns = [
            "\u5f53\u524d\u6e05\u5355\u53f7",
            "\u8bd5\u5242\u6e05\u5355\u53f7",
            "\u6e05\u5355\u53f7",
            "list_number",
            "reagent_list_no",
            "reagent_list_number",
            "order_no",
        ]
        present_list_columns = [column for column in list_columns if column in queue.columns]

        if not present_list_columns:
            return (
                True,
                f"Review queue has {len(queue)} row(s) but no list-number column; auto-pass is blocked.",
            )

        matched = pd.DataFrame()
        for column in present_list_columns:
            column_matched = queue[queue[column].astype(str).str.strip() == list_number]
            if not column_matched.empty:
                matched = pd.concat([matched, column_matched], ignore_index=True)

        if matched.empty:
            print(f"No manual review item for current list in review queue: {list_number}")
            return False, ""

        output_path = self._log_dir() / "auto_pass_blocked_review_queue.xlsx"
        output_path = self.write_excel_with_fallback(matched, output_path)
        return (
            True,
            f"Review queue contains {len(matched)} manual review row(s) for current list; saved {output_path}.",
        )

    def add_manual_review_item_from_search_failure(
        self,
        reagent: dict[str, str],
        name_result: dict[str, Any],
        search_result: dict[str, Any],
    ) -> None:
        detail_info = getattr(self, "_current_detail_info", None)
        if not detail_info:
            detail_info = {}

        paths = self.settings.get("paths", {})
        review_queue_path = self.root_dir / paths.get("review_queue_excel", "data/review_queue.xlsx")
        review_queue_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            queue = pd.read_excel(review_queue_path, dtype=str).fillna("") if review_queue_path.exists() else pd.DataFrame()
        except Exception:
            queue = pd.DataFrame()

        list_number = detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7", "")
        chemical_name = reagent.get("\u8bd5\u5242\u540d\u79f0", "")
        cas = reagent.get("CAS\u53f7", "")
        reason = str(search_result.get("raw_text") or "Chemical website lookup failed after name normalization.")

        existing_match = pd.Series(dtype=bool)
        if not queue.empty:
            list_columns = [
                "\u8bd5\u5242\u6e05\u5355\u53f7",
                "\u5f53\u524d\u6e05\u5355\u53f7",
                "\u6e05\u5355\u53f7",
                "list_number",
            ]
            name_columns = ["chemical_name", "\u8bd5\u5242\u540d\u79f0"]
            list_column = next((column for column in list_columns if column in queue.columns), "")
            name_column = next((column for column in name_columns if column in queue.columns), "")
            if list_column and name_column:
                existing_match = (
                    (queue[list_column].astype(str).str.strip() == list_number)
                    & (queue[name_column].astype(str).str.strip() == chemical_name)
                )

        if not existing_match.empty and bool(existing_match.any()):
            print(f"Manual review queue already contains search-failure item: {list_number} / {chemical_name}")
            return

        row = {
            "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
            "\u8bd5\u5242\u6e05\u5355\u53f7": list_number,
            "applicant": detail_info.get("\u7533\u8bf7\u4eba", ""),
            "chemical_name": chemical_name,
            "\u8bd5\u5242\u540d\u79f0": chemical_name,
            "cas": cas,
            "quantity": reagent.get("\u8bd5\u5242\u6570\u91cf", ""),
            "specification": reagent.get("\u89c4\u683c", ""),
            "unit": reagent.get("\u89c4\u683c\u5355\u4f4d", ""),
            "standard_name": name_result.get("standard_name", ""),
            "cleaned_name": name_result.get("cleaned_name", ""),
            "decision": "manual_review",
            "reason": reason,
            "status": "pending",
        }
        queue = pd.concat([queue, pd.DataFrame([row])], ignore_index=True)
        review_queue_path = self.write_excel_with_fallback(queue, review_queue_path)
        print(f"Added search-failure item to manual review queue: {review_queue_path}")

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

    def export_current_page_reagents(self, page: Page) -> None:
        self.open_first_task_detail(page)
        self.wait_for_reagent_table_ready(page)

        reagents = self.read_current_page_reagents(page)
        output_path = self._log_dir() / "current_page_reagents.xlsx"
        output_path = self.write_excel_with_fallback(pd.DataFrame(reagents, columns=self.reagent_columns()), output_path)

        print(f"Saved current page reagents: {output_path}")
        print(f"Found {len(reagents)} reagent row(s) on current page.")

        property_key = "\u7269\u5316\u7279\u6027"
        missing_property_records = [record for record in reagents if record.get(property_key, "").strip() == "-"]

        if missing_property_records:
            print("Records with physicochemical property '-':")
            for index, record in enumerate(missing_property_records, start=1):
                print(f"{index}. {record}")
        else:
            print("No records with physicochemical property '-'.")

    def sort_and_export_unmatched_reagents(self, page: Page) -> None:
        self.open_first_task_detail(page)
        self.wait_for_reagent_table_ready(page)

        sort_succeeded = self.sort_property_column_until_unmatched_visible(page)

        property_key = "\u7269\u5316\u7279\u6027"
        reagents = self.read_current_page_reagents(page)
        unmatched_reagents = [record for record in reagents if record.get(property_key, "").strip() == "-"]
        output_path = self._log_dir() / "unmatched_reagents.xlsx"
        output_path = self.write_excel_with_fallback(
            pd.DataFrame(unmatched_reagents, columns=self.reagent_columns()),
            output_path,
        )

        if not sort_succeeded:
            print("Sorting did not bring '-' into the first rows within 4 clicks; exporting current page '-' rows anyway.")

        print(f"Saved unmatched reagents: {output_path}")
        print(f"Found {len(unmatched_reagents)} current-page reagent row(s) with physicochemical property '-'.")

    def inspect_first_unmatched_property_options(self, page: Page) -> None:
        self.open_first_task_detail(page)
        self.wait_for_reagent_table_ready(page)
        self.sort_property_column_until_unmatched_visible(page)

        row = self.first_unmatched_reagent_row(page)
        if not row:
            print("No current-page reagent row with physicochemical property '-'.")
            return

        row_text = self.safe_inner_text(row)
        print(f"Opening technical judgement for first unmatched row: {row_text}")
        judgement_button = row.locator("button").filter(has_text="\u6280\u672f\u5224\u5b9a").first
        if not judgement_button.count():
            raise RuntimeError("Could not find technical judgement button in the first unmatched row.")

        judgement_button.click()
        self.wait_for_property_editor_ready(page)
        self.click_property_input(page)
        page.wait_for_timeout(1000)

        options = self.collect_all_dropdown_options_with_screenshots(page)
        print("Physicochemical property dropdown options:")
        for index, option in enumerate(options, start=1):
            print(f"{index}. {option}")

    def sort_property_column_until_unmatched_visible(self, page: Page) -> bool:
        property_key = "\u7269\u5316\u7279\u6027"

        for attempt in range(1, 5):
            print(f"Clicking physicochemical property header, attempt {attempt}/4.")
            self.click_reagent_property_header(page)
            self.wait_for_reagent_table_ready(page)
            page.wait_for_timeout(500)

            first_properties = [
                record.get(property_key, "").strip()
                for record in self.read_current_page_reagents(page)[:5]
            ]
            print(f"First 5 physicochemical properties after attempt {attempt}: {first_properties}")

            if "-" in first_properties:
                print("Sorting considered successful because '-' appeared in the first rows.")
                return True

        return False

    def first_unmatched_reagent_row(self, page: Page) -> Locator | None:
        rows = page.locator("tbody tr.ant-table-row")
        count = rows.count()

        for index in range(count):
            row = rows.nth(index)
            cells = row.locator("td")
            if cells.count() > 8 and self.safe_inner_text(cells.nth(8)).strip() == "-":
                return row

        return None

    def wait_for_property_editor_ready(self, page: Page) -> None:
        try:
            page.wait_for_selector(".ant-modal:visible, .ant-drawer:visible, .ant-form:visible, tbody tr.ant-table-row", timeout=15000)
        except TimeoutError:
            print("Property editor did not expose an obvious container; continuing with current page.")

    def click_property_input(self, page: Page) -> None:
        candidates = [
            page.locator("label").filter(has_text="\u7269\u5316\u7279\u6027").locator("..").locator(".ant-select, input").first,
            page.locator(".ant-form-item").filter(has_text="\u7269\u5316\u7279\u6027").locator(".ant-select, input").first,
            page.locator(".ant-modal:visible .ant-select").first,
            page.locator(".ant-drawer:visible .ant-select").first,
            page.locator("tbody tr.ant-table-row").first.locator(".ant-select").first,
        ]

        for candidate in candidates:
            try:
                if candidate.count() and candidate.is_visible():
                    candidate.click()
                    return
            except Exception:
                continue

        raise RuntimeError("Could not find physicochemical property input.")

    def visible_dropdown_options(self, page: Page) -> list[str]:
        return page.evaluate(
            """
            () => {
              const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim();
              const selectors = [
                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content',
                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item',
                '.ant-dropdown:not(.ant-dropdown-hidden) li',
              ];
              const options = [];

              for (const selector of selectors) {
                for (const node of document.querySelectorAll(selector)) {
                  const text = normalize(node.innerText);
                  if (text && !options.includes(text)) {
                    options.push(text);
                  }
                }
              }

              return options;
            }
            """
        )

    def collect_all_dropdown_options_with_screenshots(self, page: Page) -> list[str]:
        all_options: list[str] = []
        log_dir = self._log_dir()
        unchanged_rounds = 0

        for screen_index in range(1, 11):
            page.wait_for_timeout(500)

            before_count = len(all_options)
            for option in self.visible_dropdown_options(page):
                if option not in all_options:
                    all_options.append(option)

            screenshot_path = log_dir / f"dropdown_options_{screen_index:02d}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            print(f"Saved dropdown screenshot {screen_index}: {screenshot_path}")

            if len(all_options) == before_count:
                unchanged_rounds += 1
            else:
                unchanged_rounds = 0

            if screen_index >= 3 and unchanged_rounds >= 2:
                print("No new dropdown options appeared after repeated wheel scrolls; stopping.")
                break

            self.wheel_dropdown(page)

        summary_path = log_dir / "dropdown_options.png"
        page.screenshot(path=str(summary_path), full_page=True)
        print(f"Saved dropdown summary screenshot: {summary_path}")

        return all_options

    def wheel_dropdown(self, page: Page) -> None:
        dropdown = page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)").first
        if not dropdown.count():
            print("Dropdown was not found for wheel scrolling.")
            return

        box = dropdown.bounding_box()
        if not box:
            print("Dropdown bounding box was not available for wheel scrolling.")
            return

        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, 260)

    def click_reagent_property_header(self, page: Page) -> None:
        header = page.locator("thead th").filter(has_text="\u7269\u5316\u7279\u6027").first
        if not header.count():
            raise RuntimeError("Could not find the physicochemical property header.")

        header.click()

    def wait_for_reagent_table_ready(self, page: Page) -> None:
        try:
            page.wait_for_function(
                """
                () => Array.from(document.querySelectorAll('thead th'))
                  .some((th) => th.innerText.includes('\\u8bd5\\u5242\\u540d\\u79f0'))
                  && document.querySelectorAll('tbody tr.ant-table-row').length > 0
                """,
                timeout=15000,
            )
        except TimeoutError:
            print("Reagent table was not fully settled; reading current page state.")

    def read_current_page_reagents(self, page: Page) -> list[dict[str, str]]:
        return page.evaluate(
            """
            (wantedColumns) => {
              const displayText = (text) => (text || '').replace(/\\s+/g, ' ').trim();
              const normalizeHeader = (text) => displayText(text).replace(/\\s+/g, '');
              const fallbackColumnIndexes = {
                ['\\u5e8f\\u53f7']: 1,
                ['\\u5206\\u7c7b\\u8bf4\\u660e']: 2,
                ['\\u8bd5\\u5242\\u540d\\u79f0']: 3,
                ['CAS\\u53f7']: 4,
                ['\\u89c4\\u683c']: 5,
                ['\\u89c4\\u683c\\u5355\\u4f4d']: 6,
                ['\\u8bd5\\u5242\\u6570\\u91cf']: 7,
                ['\\u7269\\u5316\\u7279\\u6027']: 8,
                ['\\u6280\\u672f\\u5ba1\\u6279\\u5907\\u6ce8']: 9,
              };

              const tables = Array.from(document.querySelectorAll('table'));
              const reagentTable = tables.find((table) => {
                const headers = Array.from(table.querySelectorAll('thead th')).map((th) => normalizeHeader(th.innerText));
                return headers.some((header) => header.includes('\\u8bd5\\u5242\\u540d\\u79f0'))
                  && headers.some((header) => header.includes('\\u7269\\u5316\\u7279\\u6027'));
              }) || document;

              const headers = Array.from(reagentTable.querySelectorAll('thead th')).map((th) => normalizeHeader(th.innerText));
              const columnIndexes = {};
              for (const wanted of wantedColumns) {
                const wantedHeader = normalizeHeader(wanted);
                const index = headers.findIndex((header) => header === wantedHeader || header.includes(wantedHeader));
                columnIndexes[wanted] = index >= 0 ? index : fallbackColumnIndexes[wanted];
              }

              let rows = Array.from(reagentTable.querySelectorAll('tbody tr.ant-table-row'));
              if (!rows.length) {
                rows = Array.from(document.querySelectorAll('tbody tr.ant-table-row'));
              }
              return rows.map((row) => {
                const cells = Array.from(row.querySelectorAll('td'));
                const reagent = {};

                for (const wanted of wantedColumns) {
                  const index = columnIndexes[wanted];
                  reagent[wanted] = cells[index] ? displayText(cells[index].innerText) : '';
                }

                return reagent;
              }).filter((record) => Object.values(record).some(Boolean));
            }
            """,
            self.reagent_columns(),
        )

    def reagent_columns(self) -> list[str]:
        return [
            "\u5e8f\u53f7",
            "\u5206\u7c7b\u8bf4\u660e",
            "\u8bd5\u5242\u540d\u79f0",
            "CAS\u53f7",
            "\u89c4\u683c",
            "\u89c4\u683c\u5355\u4f4d",
            "\u8bd5\u5242\u6570\u91cf",
            "\u7269\u5316\u7279\u6027",
            "\u6280\u672f\u5ba1\u6279\u5907\u6ce8",
        ]

    def capture_prompt_if_present(self, page: Page, screenshot_name: str) -> str:
        prompt_locator = page.locator(
            ".ant-modal:visible, "
            ".ant-message-notice:visible, "
            ".ant-notification-notice:visible, "
            ".ant-popover:visible, "
            ".ant-popconfirm:visible"
        )

        try:
            count = prompt_locator.count()
        except Error:
            return ""

        messages: list[str] = []
        for index in range(count):
            item = prompt_locator.nth(index)
            try:
                if item.is_visible():
                    text = self.safe_inner_text(item)
                    if text:
                        messages.append(text)
            except Error:
                continue

        prompt_text = "\n".join(messages).strip()
        if prompt_text:
            prompt_path = self._log_dir() / screenshot_name
            page.screenshot(path=str(prompt_path), full_page=True)
            print(f"Saved prompt screenshot: {prompt_path}")

        return prompt_text

    def wait_for_detail_ready(self, page: Page, first_task: dict[str, str]) -> None:
        list_number = first_task.get("\u8bd5\u5242\u6e05\u5355\u53f7", "")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except TimeoutError:
            print("Network did not become idle after opening detail; continuing with current page.")

        if list_number:
            try:
                page.wait_for_selector(f"text={list_number}", timeout=15000)
            except TimeoutError:
                print(f"Detail list number was not confirmed: {list_number}")

    def read_detail_info(self, page: Page) -> dict[str, str]:
        return page.evaluate(
            """
            () => {
              const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim();
              const result = {};

              const title = document.querySelector('.ant-page-header-heading-title');
              const titleText = normalize(title ? title.innerText : '');
              const listNumberMatch = titleText.match(/SJ\\d+/);
              if (listNumberMatch) {
                result['\\u5f53\\u524d\\u6e05\\u5355\\u53f7'] = listNumberMatch[0];
              }

              for (const row of document.querySelectorAll('.ant-page-header-heading-title .smallContent')) {
                const values = Array.from(row.querySelectorAll('.ant-col')).map((node) => normalize(node.innerText)).filter(Boolean);
                if (values.length < 2) {
                  continue;
                }

                const label = values[0];
                const value = values.slice(1).join(' ');

                if (label === '\\u5ba2\\u6237\\u540d\\u79f0') {
                  result['\\u5ba2\\u6237\\u540d\\u79f0'] = value;
                } else if (label === '\\u72b6\\u6001') {
                  result['\\u72b6\\u6001'] = value;
                } else if (label === '\\u7533\\u8bf7\\u4eba') {
                  result['\\u7533\\u8bf7\\u4eba'] = value;
                }
              }

              return result;
            }
            """
        )

    def wait_for_table_ready(self, page: Page) -> None:
        try:
            page.wait_for_selector(".ant-table", timeout=15000)
            page.wait_for_selector(".ant-spin-spinning", state="hidden", timeout=15000)
            page.wait_for_function(
                """
                () => document.querySelectorAll('tbody tr.ant-table-row').length > 0
                  || document.body.innerText.includes('\\u6682\\u65e0\\u6570\\u636e')
                  || document.body.innerText.includes('No Data')
                """,
                timeout=15000,
            )
        except TimeoutError:
            print("Table loading state was not fully settled; capturing current page.")

    def click_visible_text(self, page: Page, text: str) -> None:
        candidates = [
            page.get_by_text(text, exact=True),
            page.locator(f"span:has-text('{text}')"),
            page.locator(f"li:has-text('{text}')"),
            page.locator(f"a:has-text('{text}')"),
            page.locator(f"text={text}"),
        ]

        for locator in candidates:
            try:
                count = locator.count()
                for index in range(count):
                    item = locator.nth(index)
                    if item.is_visible():
                        item.click()
                        return
            except Exception:
                continue

        raise RuntimeError(f"Could not find visible text to click: {text}")

    def login(self, page: Page, username: str, password: str, log_dir: Path) -> None:
        selectors = self.settings.get("selectors", {})

        login_scope = self.find_login_scope(page, selectors)

        if not login_scope:
            login_screenshot_path = log_dir / "login.png"
            login_html_path = log_dir / "login.html"
            page.screenshot(path=str(login_screenshot_path), full_page=True)
            login_html_path.write_text(page.content(), encoding="utf-8")
            print(f"Saved login screenshot for selector debugging: {login_screenshot_path}")
            print(f"Saved login HTML for selector debugging: {login_html_path}")
            self.print_page_structure(page)
            raise RuntimeError(
                "Could not find login controls. Configure selectors.username_input, "
                "selectors.password_input, and selectors.login_button in config/settings.yaml."
            )

        scope, username_selector, password_selector, login_selector = login_scope

        print(f"Using login frame: {getattr(scope, 'url', 'main page')}")
        print(f"Using username selector: {username_selector}")
        print(f"Using password selector: {password_selector}")
        print(f"Using login selector: {login_selector}")

        scope.fill(username_selector, username)
        scope.fill(password_selector, password)

        scope.click(login_selector)

    def find_login_scope(self, page: Page, selectors: dict[str, str]) -> tuple[Any, str, str, str] | None:
        scopes = [page, *page.frames]

        for scope in scopes:
            username_selector = selectors.get("username_input") or self.first_visible(scope, USERNAME_SELECTORS)
            password_selector = selectors.get("password_input") or self.first_visible(scope, PASSWORD_SELECTORS)
            login_selector = selectors.get("login_button") or self.first_visible(scope, LOGIN_BUTTON_SELECTORS)

            if username_selector and password_selector and login_selector:
                return scope, username_selector, password_selector, login_selector

        return None

    def open_login_page(self, page: Page, erp_url: str) -> None:
        last_error: Exception | None = None

        for attempt in range(1, 4):
            try:
                print(f"Navigation attempt {attempt}/3")
                page.goto(erp_url, wait_until="commit", timeout=60000)
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                return
            except (TimeoutError, Error) as error:
                last_error = error
                print(f"Login page navigation failed: {error}")
                try:
                    page.wait_for_timeout(3000)
                except Error:
                    raise

        print(f"Continuing after navigation failures. Last error: {last_error}")

    def wait_for_home(self, page: Page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except TimeoutError:
            print("Network did not become idle after login; continuing with current page.")

        try:
            page.wait_for_selector("text=\u5e02\u573a\u7ba1\u7406", timeout=15000)
        except TimeoutError:
            print("Home menu text was not confirmed; continuing with current page.")

    def print_page_structure(self, page: Page) -> None:
        print("\n=== buttons ===")
        self.print_locator_info(page.locator("button"), ["type", "id", "name", "class", "disabled"])

        print("\n=== inputs ===")
        self.print_locator_info(
            page.locator("input"),
            ["type", "id", "name", "placeholder", "value", "class", "disabled", "readonly"],
        )

        print("\n=== tables ===")
        self.print_table_info(page)

        print("\n=== iframes ===")
        self.print_locator_info(page.locator("iframe"), ["id", "name", "src", "class"])

    def print_locator_info(self, locator: Locator, attributes: list[str]) -> None:
        count = locator.count()
        print(f"count: {count}")

        for index in range(count):
            element = locator.nth(index)
            info = {"index": index, "text": self.safe_inner_text(element)}

            for attribute in attributes:
                value = element.get_attribute(attribute)
                if value:
                    info[attribute] = value

            print(info)

    def print_table_info(self, page: Page) -> None:
        tables = page.locator("table")
        count = tables.count()
        print(f"count: {count}")

        for index in range(count):
            table = tables.nth(index)
            rows = table.locator("tr").count()
            headers = [
                table.locator("th").nth(header_index).inner_text().strip()
                for header_index in range(table.locator("th").count())
            ]
            info = {
                "index": index,
                "id": table.get_attribute("id"),
                "class": table.get_attribute("class"),
                "rows": rows,
                "headers": headers,
            }
            print({key: value for key, value in info.items() if value not in (None, "", [])})

    def first_visible(self, page: Page, selectors: list[str]) -> str | None:
        for selector in selectors:
            try:
                element = page.locator(selector).first
                if element.count() and element.is_visible():
                    return selector
            except Exception:
                continue
        return None

    def safe_inner_text(self, locator: Locator) -> str:
        try:
            text = locator.inner_text(timeout=1000).strip()
        except Exception:
            return ""
        return text[:200]

    def _log_dir(self) -> Path:
        paths = self.settings.get("paths", {})
        log_dir = self.root_dir / paths.get("audit_log_dir", "data/logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    def write_excel_with_fallback(self, dataframe: pd.DataFrame, output_path: Path) -> Path:
        try:
            dataframe.to_excel(output_path, index=False)
            return output_path
        except PermissionError:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            fallback_path = output_path.with_name(f"{output_path.stem}_{timestamp}{output_path.suffix}")
            dataframe.to_excel(fallback_path, index=False)
            print(
                f"Could not write {output_path} because it is locked or open; "
                f"saved to {fallback_path} instead."
            )
            return fallback_path
