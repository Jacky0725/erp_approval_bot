from __future__ import annotations

import time
import re
from typing import Any

import pandas as pd
from playwright.sync_api import Error, Locator, Page, TimeoutError


class ReagentPageMixin:

    def enter_reagent_judgement_page(self, page: Page) -> None:
        menu_name = "\u8bd5\u5242\u7ba1\u7406"
        target_name = "\u8bd5\u5242\u5224\u5b9a"

        print(f"Opening menu: {menu_name}")
        self.click_visible_text(page, menu_name)
        page.wait_for_timeout(1000)

        print(f"Opening page: {target_name}")
        self.click_visible_text(page, target_name)
        self.wait_for_table_ready(page)

        try:
            page.wait_for_selector(f"text={target_name}", timeout=10000)
        except TimeoutError:
            print(f"Target page text was not confirmed: {target_name}")

    def export_todo_tasks(self, page: Page) -> None:
        self.enter_reagent_judgement_page(page)

        tasks = self.read_all_todo_tasks(page)
        output_path = self._log_dir() / "todo_tasks.xlsx"
        json_path = self._log_dir() / "todo_tasks.json"

        if tasks:
            print(f"Found {len(tasks)} todo task(s):")
            for index, task in enumerate(tasks, start=1):
                print(f"{index}. {task}")
        else:
            print("No todo tasks found.")

        output_path = self.write_excel_with_fallback(pd.DataFrame(tasks, columns=self.todo_columns()), output_path)
        json_path.write_text(pd.DataFrame(tasks, columns=self.todo_columns()).to_json(orient="records", force_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved todo tasks: {output_path}")
        print(f"Saved todo tasks JSON: {json_path}")

    def read_all_todo_tasks(self, page: Page) -> list[dict[str, str]]:
        self.goto_first_todo_page(page)
        tasks: list[dict[str, str]] = []
        seen_list_numbers: set[str] = set()
        visited_pages = 0
        list_number_key = "\u8bd5\u5242\u6e05\u5355\u53f7"

        while True:
            visited_pages += 1
            current_page = self.current_todo_page_number(page) or str(visited_pages)
            page_tasks = self.read_todo_tasks(page)
            print(f"Read todo page {current_page}: {len(page_tasks)} task(s).")

            for task in page_tasks:
                list_number = self.extract_list_number(task.get(list_number_key, ""))
                if not list_number or list_number in seen_list_numbers:
                    continue
                seen_list_numbers.add(list_number)
                tasks.append(task)

            moved_next, terminal_or_error = self.click_next_todo_page(page)
            if not moved_next:
                if terminal_or_error:
                    print("Reached the last todo page.")
                else:
                    print("Todo pagination stopped before confirming the last page.")
                break

            if visited_pages >= 100:
                raise RuntimeError("Stopped todo export after 100 pages; todo pagination may be stuck.")

        return tasks

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

    def goto_first_todo_page(self, page: Page) -> bool:
        pagination = self.todo_pagination(page)
        first_page = pagination.locator(".ant-pagination-item-1").first if pagination else page.locator(".ant-pagination-item-1").first
        try:
            if first_page.count() and first_page.is_visible():
                class_name = first_page.get_attribute("class") or ""
                if "ant-pagination-item-active" not in class_name:
                    first_page.click()
                    self.wait_for_table_ready(page)
                    page.wait_for_timeout(500)
            return True
        except Error:
            print("Could not move to the first todo page; continuing from current page.")
            return False

    def current_todo_page_number(self, page: Page) -> str:
        try:
            pagination = self.todo_pagination(page)
            active = (
                pagination.locator(".ant-pagination-item-active").first
                if pagination
                else page.locator(".ant-pagination-item-active").first
            )
            if active.count():
                return self.safe_inner_text(active)
        except Error:
            return ""
        return ""

    def click_next_todo_page(self, page: Page) -> tuple[bool, bool]:
        pagination = self.todo_pagination(page)
        next_button = pagination.locator(".ant-pagination-next").first if pagination else page.locator(".ant-pagination-next").first
        try:
            if not next_button.count() or not next_button.is_visible():
                return False, True

            class_name = next_button.get_attribute("class") or ""
            aria_disabled = (next_button.get_attribute("aria-disabled") or "").lower()
            if "ant-pagination-disabled" in class_name or aria_disabled == "true":
                return False, True

            before_page = self.current_todo_page_number(page)
            next_button.click()
            self.wait_for_table_ready(page)
            page.wait_for_timeout(600)
            after_page = self.current_todo_page_number(page)
            moved = after_page != before_page or not after_page
            return moved, moved
        except Error as error:
            print(f"Could not click next todo page: {error}")
            return False, False

    def todo_pagination(self, page: Page) -> Locator | None:
        candidates = [
            page.locator(".ant-table-wrapper").first.locator(".ant-pagination").first,
            page.locator(".ant-pagination").first,
        ]
        for candidate in candidates:
            try:
                if candidate.count() and candidate.is_visible():
                    return candidate
            except Error:
                continue
        return None

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

    def open_task_detail_by_list_number(self, page: Page, target_list_number: str) -> bool:
        self.enter_reagent_judgement_page(page)
        target_list_number = str(target_list_number or "").strip()
        if not target_list_number:
            raise RuntimeError("Target reagent list number is empty.")

        list_number_key = "\u8bd5\u5242\u6e05\u5355\u53f7"
        customer_name_key = "\u5ba2\u6237\u540d\u79f0"
        approval_state_key = "\u6280\u672f\u5ba1\u6279\u72b6\u6001"
        applicant_key = "\u7533\u8bf7\u4eba"

        self.goto_first_todo_page(page)
        visited_pages = 0
        seen_list_numbers: list[str] = []

        while True:
            visited_pages += 1
            current_page = self.current_todo_page_number(page) or str(visited_pages)
            tasks = self.read_todo_tasks(page)
            if not tasks and visited_pages == 1:
                print("No todo tasks found; target detail page was not opened.")
                return False

            matched_index = next(
                (
                    index
                    for index, task in enumerate(tasks)
                    if self.extract_list_number(task.get(list_number_key, "")) == target_list_number
                ),
                None,
            )
            if matched_index is not None:
                target_task = tasks[matched_index]
                target_row = page.locator("tbody tr.ant-table-row").nth(matched_index)
                detail_button = target_row.locator("button").filter(has_text="\u8be6\u60c5").first

                if not detail_button.count():
                    raise RuntimeError(f"Could not find detail button for target task: {target_list_number}")

                print(f"Opening target task detail from todo page {current_page}: {target_list_number}")
                detail_button.click()
                self.wait_for_detail_ready(page, target_task)

                detail_info = self.read_detail_info(page)
                merged_info = {
                    "\u5f53\u524d\u6e05\u5355\u53f7": detail_info.get("\u5f53\u524d\u6e05\u5355\u53f7") or target_task.get(list_number_key, ""),
                    customer_name_key: detail_info.get(customer_name_key) or target_task.get(customer_name_key, ""),
                    "\u72b6\u6001": detail_info.get("\u72b6\u6001") or target_task.get(approval_state_key, ""),
                    applicant_key: detail_info.get(applicant_key) or target_task.get(applicant_key, ""),
                }

                print("Task detail:")
                for key, value in merged_info.items():
                    print(f"{key}: {value}")
                return True

            seen_list_numbers.extend(
                self.extract_list_number(task.get(list_number_key, ""))
                for task in tasks
                if self.extract_list_number(task.get(list_number_key, ""))
            )
            moved_next, terminal_or_error = self.click_next_todo_page(page)
            if not moved_next:
                if terminal_or_error:
                    print(f"Target task was not found across todo pages: {target_list_number}")
                else:
                    print(f"Todo pagination stopped before target task was found: {target_list_number}")
                if seen_list_numbers:
                    print("Scanned todo task list numbers:")
                    for list_number in seen_list_numbers:
                        print(f"- {list_number}")
                return False

            if visited_pages >= 100:
                raise RuntimeError(f"Stopped target todo search after 100 pages: {target_list_number}")

    @staticmethod
    def extract_list_number(value: str) -> str:
        text = str(value or "").strip()
        match = re.search(r"SJ\d+", text, flags=re.I)
        return match.group(0) if match else text

    def perform_auto_match(self, page: Page) -> bool:
        self.auto_match_succeeded = False
        target_list_number = str(getattr(self, "target_list_number", "") or "").strip()
        if target_list_number:
            detail_opened = self.open_task_detail_by_list_number(page, target_list_number)
        else:
            detail_opened = self.open_first_task_detail(page)
        if not detail_opened:
            return False

        self.wait_for_reagent_table_ready(page)
        self.ensure_reagent_page_size(page)
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
            if self._is_error_prompt(prompt_text):
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
            return not self._is_error_prompt(prompt_text)

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

        final_snapshot = last_snapshot or self._auto_match_snapshot(page)
        print(
            "Auto-match click completed but the reagent table did not change. "
            "Continuing because this can happen when ERP has already matched all possible rows. "
            f"Before '-' rows: {before_snapshot.get('unmatched')}; "
            f"after '-' rows: {final_snapshot.get('unmatched', '<unknown>')}."
        )
        return True

    @staticmethod
    def _is_error_prompt(prompt_text: str) -> bool:
        normalized = (prompt_text or "").lower()
        error_tokens = (
            "\u5931\u8d25",
            "\u9519\u8bef",
            "\u5f02\u5e38",
            "\u4e0d\u80fd",
            "\u65e0\u6cd5",
            "\u8bf7\u5148",
            "error",
            "failed",
            "failure",
            "exception",
        )
        return any(token in normalized for token in error_tokens)

    def find_unmatched_reagents_across_all_pages(self, page: Page) -> list[dict[str, str]]:
        self.pagination_check_succeeded = False
        self.wait_for_reagent_table_ready(page)
        if not self.goto_first_reagent_page(page):
            return []

        sort_succeeded = self.sort_property_column_until_unmatched_visible(page)
        if not sort_succeeded:
            current_unmatched = self.current_page_unmatched_reagents(page)
            if not current_unmatched:
                print(
                    "Sorting did not show '-' in the first rows, and the current sorted page has no '-' rows; "
                    "remaining pages are considered complete."
                )
                self.pagination_check_succeeded = True
                return []
            print(
                "Sorting did not show '-' in the first rows, but current page still contains '-' rows; "
                "auto-pass will be blocked by unmatched records."
            )
            self.pagination_check_succeeded = True
            self.save_auto_pass_blocking_unmatched(current_unmatched)
            return current_unmatched

        if not self.goto_first_reagent_page(page):
            return []

        property_key = "\u7269\u5316\u7279\u6027"
        unmatched: list[dict[str, str]] = []
        visited_pages = 0

        while True:
            visited_pages += 1
            current_page = self.current_reagent_page_number(page)
            reagents = self.read_current_page_reagents(page)
            page_unmatched = self.unmatched_reagents_from_records(reagents)
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
            self.save_auto_pass_blocking_unmatched(unmatched)

        return unmatched

    def current_page_unmatched_reagents(self, page: Page) -> list[dict[str, str]]:
        return self.unmatched_reagents_from_records(self.read_current_page_reagents(page))

    @staticmethod
    def unmatched_reagents_from_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
        property_key = "\u7269\u5316\u7279\u6027"
        return [record for record in records if record.get(property_key, "").strip() == "-"]

    def save_auto_pass_blocking_unmatched(self, unmatched: list[dict[str, str]]) -> None:
        if not unmatched:
            return
        output_path = self._log_dir() / "auto_pass_blocked_unmatched_reagents.xlsx"
        output_path = self.write_excel_with_fallback(
            pd.DataFrame(unmatched, columns=self.reagent_columns()),
            output_path,
        )
        print(f"Saved unmatched reagent rows that blocked auto-pass: {output_path}")

    def goto_first_reagent_page(self, page: Page) -> bool:
        pagination = self.reagent_pagination(page)
        first_page = pagination.locator(".ant-pagination-item-1").first if pagination else page.locator(".ant-pagination-item-1").last
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

    def ensure_reagent_page_size(self, page: Page, preferred_size: int | None = None) -> bool:
        size = preferred_size or self.preferred_reagent_page_size()
        if size <= 0:
            return False

        print(f"Ensuring reagent page size: {size}.")
        pagination = self.reagent_pagination(page)
        try:
            changer = (
                pagination.locator(".ant-pagination-options-size-changer").first
                if pagination
                else page.locator(".ant-pagination-options-size-changer").last
            )
            if not changer.count() or not changer.is_visible():
                print("Reagent page-size selector was not visible; page size was not changed.")
                return False

            current_text = self.safe_inner_text(changer)
            if str(size) in current_text:
                print(f"Reagent page size is already {size}.")
                return True

            before_page = self.current_reagent_page_number(page)
            try:
                changer.click(timeout=5000)
            except Error as error:
                print(f"Normal page-size selector click failed; retrying with DOM click: {error}")
                if not self.click_page_size_changer_by_dom(page, str(size)):
                    raise
            page.wait_for_timeout(300)

            options = [
                page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option")
                .filter(has_text=str(size))
                .first,
            ]
            for option in options:
                if option.count() and option.is_visible():
                    try:
                        option.click(timeout=5000)
                        return self.confirm_reagent_page_size_change(page, changer, size, before_page)
                    except Error as error:
                        print(f"Normal page-size option click failed; retrying with DOM click: {error}")
                        if self.click_page_size_option_by_text(page, str(size)):
                            return self.confirm_reagent_page_size_change(page, changer, size, before_page)

            if self.click_page_size_option_by_text(page, str(size)):
                return self.confirm_reagent_page_size_change(page, changer, size, before_page)
            print(f"Could not find reagent page-size option: {size}. Current changer text: {current_text or '<empty>'}")
            return False
        except Error as error:
            print(f"Could not change reagent page size to {size}: {error}")
            return False

    @staticmethod
    def click_page_size_changer_by_dom(page: Page, size_text: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    (sizeText) => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                      const changers = Array.from(document.querySelectorAll('.ant-pagination-options-size-changer'))
                        .filter(visible);
                      const changer = changers.find((node) => !text(node).includes(sizeText)) || changers[changers.length - 1];
                      if (!changer) return false;
                      changer.scrollIntoView({ block: 'center', inline: 'center' });
                      const target = changer.querySelector('.ant-select-selector') || changer;
                      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                        target.dispatchEvent(new MouseEvent(type, {
                          bubbles: true,
                          cancelable: true,
                          view: window,
                        }));
                      }
                      return true;
                    }
                    """,
                    size_text,
                )
            )
        except Error:
            return False

    def confirm_reagent_page_size_change(self, page: Page, changer: Locator, size: int, before_page: str) -> bool:
        self.wait_for_reagent_table_ready(page)
        page.wait_for_timeout(1000)
        after_text = self.safe_inner_text(changer)
        if str(size) in after_text:
            print(f"Reagent page size changed to {size}.")
            return True
        row_count = len(self.read_current_page_reagents(page))
        if row_count > 20 and size > 20:
            print(f"Clicked reagent page-size option {size}; current visible reagent rows: {row_count}.")
            return True
        pagination_text = self.safe_inner_text(self.reagent_pagination(page)) if self.reagent_pagination(page) else ""
        print(
            f"Clicked reagent page-size option {size}, but current visible reagent rows remain {row_count}. "
            f"Pagination text: {pagination_text or '<empty>'}"
        )
        return False

    @staticmethod
    def click_page_size_option_by_text(page: Page, size_text: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    (sizeText) => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                      const options = Array.from(document.querySelectorAll(
                        '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option'
                      ));
                      const option = options.find((node) => {
                        if (!visible(node)) return false;
                        const value = text(node);
                        const title = (node.getAttribute('title') || '').trim();
                        return value === sizeText
                          || value.startsWith(sizeText + ' ')
                          || title === sizeText
                          || title.startsWith(sizeText + ' ');
                      });
                      if (!option) return false;
                      option.scrollIntoView({ block: 'center', inline: 'center' });
                      const target = option.querySelector('.ant-select-item-option-content') || option;
                      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                        target.dispatchEvent(new MouseEvent(type, {
                          bubbles: true,
                          cancelable: true,
                          view: window,
                        }));
                      }
                      return true;
                    }
                    """,
                    size_text,
                )
            )
        except Error:
            return False

    def preferred_reagent_page_size(self) -> int:
        approval_settings = getattr(self, "settings", {}).get("approval", {}) or {}
        value = approval_settings.get("reagent_page_size", 20)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 20

    def current_reagent_page_number(self, page: Page) -> str:
        try:
            pagination = self.reagent_pagination(page)
            active = (
                pagination.locator(".ant-pagination-item-active").first
                if pagination
                else page.locator(".ant-pagination-item-active").last
            )
            if active.count():
                return self.safe_inner_text(active)
        except Error:
            return ""
        return ""

    def total_reagent_pages(self, page: Page) -> int:
        try:
            pagination = self.reagent_pagination(page)
            page_items = pagination.locator(".ant-pagination-item") if pagination else page.locator(".ant-pagination-item")
            values = []
            for index in range(page_items.count()):
                text = self.safe_inner_text(page_items.nth(index))
                if text.isdigit():
                    values.append(int(text))
            return max(values) if values else 1
        except Error:
            return 0

    def goto_reagent_page_number(self, page: Page, target_page: str | int) -> bool:
        target = str(target_page or "").strip()
        if not target or target == self.current_reagent_page_number(page):
            return True

        pagination = self.reagent_pagination(page)
        try:
            direct = (
                pagination.locator(f".ant-pagination-item-{target}").first
                if pagination
                else page.locator(f".ant-pagination-item-{target}").last
            )
            if direct.count() and direct.is_visible():
                before_signature = self.reagent_table_signature(page)
                direct.click(timeout=3000)
                if self.wait_for_reagent_page_change(page, "", before_signature):
                    return self.current_reagent_page_number(page) == target
                return self.current_reagent_page_number(page) == target
        except Error:
            pass

        if not self.goto_first_reagent_page(page):
            return False
        for _ in range(250):
            current = self.current_reagent_page_number(page)
            if current == target:
                return True
            try:
                if current and int(current) > int(target):
                    return False
            except ValueError:
                pass
            moved_next, terminal_or_error = self.click_next_reagent_page(page)
            if not moved_next:
                return bool(terminal_or_error and self.current_reagent_page_number(page) == target)
        return False

    def click_next_reagent_page(self, page: Page) -> tuple[bool, bool]:
        pagination = self.reagent_pagination(page)
        next_button = pagination.locator(".ant-pagination-next").first if pagination else page.locator(".ant-pagination-next").last
        try:
            if not next_button.count() or not next_button.is_visible():
                return False, True

            class_name = next_button.get_attribute("class") or ""
            aria_disabled = (next_button.get_attribute("aria-disabled") or "").lower()
            if "ant-pagination-disabled" in class_name or aria_disabled == "true":
                return False, True

            before_page = self.current_reagent_page_number(page)
            before_signature = self.reagent_table_signature(page)
            for attempt in range(2):
                if attempt == 0:
                    next_button.click(timeout=3000)
                else:
                    self.click_next_reagent_page_by_dom(page)
                if self.wait_for_reagent_page_change(page, before_page, before_signature):
                    return True, True
                page.wait_for_timeout(300)
            after_page = self.current_reagent_page_number(page)
            after_signature = self.reagent_table_signature(page)
            moved = bool(after_page and before_page and after_page != before_page) or (
                bool(after_signature) and bool(before_signature) and after_signature != before_signature
            )
            return moved, moved
        except Error as error:
            print(f"Could not click next reagent page: {error}")
            return False, False

    def wait_for_reagent_page_change(self, page: Page, before_page: str, before_signature: str) -> bool:
        for _ in range(12):
            try:
                self.wait_for_reagent_table_ready(page)
            except Error:
                pass
            page.wait_for_timeout(250)
            after_page = self.current_reagent_page_number(page)
            if before_page and after_page and after_page != before_page:
                return True
            after_signature = self.reagent_table_signature(page)
            if before_signature and after_signature and after_signature != before_signature:
                return True
        return False

    @staticmethod
    def reagent_table_signature(page: Page) -> str:
        try:
            return str(
                page.evaluate(
                    """
                    () => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const tables = Array.from(document.querySelectorAll('.ant-table-tbody')).filter(visible);
                      const body = tables[tables.length - 1] || tables[0];
                      if (!body) return '';
                      return Array.from(body.querySelectorAll('tr.ant-table-row'))
                        .filter(visible)
                        .slice(0, 5)
                        .map((row) => (row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim())
                        .join('|')
                        .slice(0, 800);
                    }
                    """
                )
                or ""
            )
        except Error:
            return ""

    @staticmethod
    def click_next_reagent_page_by_dom(page: Page) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    () => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const buttons = Array.from(document.querySelectorAll('.ant-pagination-next')).filter(visible);
                      const button = buttons[buttons.length - 1];
                      if (!button) return false;
                      const cls = button.getAttribute('class') || '';
                      if (cls.includes('ant-pagination-disabled') || button.getAttribute('aria-disabled') === 'true') {
                        return false;
                      }
                      button.scrollIntoView({ block: 'center', inline: 'center' });
                      const target = button.querySelector('button, a') || button;
                      target.click();
                      return true;
                    }
                    """
                )
            )
        except Error:
            return False

    def reagent_pagination(self, page: Page) -> Locator | None:
        candidates = [
            page.locator("xpath=//*[normalize-space()='试剂清单']/ancestor::*[contains(@class,'ant-card') or contains(@class,'ant-table-wrapper') or contains(@class,'ant-row') or contains(@class,'ant-col')][1]").locator(".ant-pagination").last,
            page.locator("xpath=//*[contains(normalize-space(),'试剂清单')]/following::ul[contains(@class,'ant-pagination')][1]"),
            page.locator(".ant-table-wrapper").last.locator(".ant-pagination").last,
            page.locator(".ant-pagination").last,
        ]
        for candidate in candidates:
            try:
                if candidate.count() and candidate.is_visible():
                    return candidate
            except Error:
                continue
        return None

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

        for attempt in range(1, 6):
            print(f"Clicking physicochemical property header, attempt {attempt}/5.")
            self.click_reagent_property_header(page)
            self.wait_for_reagent_table_ready(page)
            page.wait_for_timeout(500)

            properties = [record.get(property_key, "").strip() for record in self.read_current_page_reagents(page)]
            first_properties = properties[:5]
            print(f"First 5 physicochemical properties after attempt {attempt}: {first_properties}")

            if "-" in first_properties:
                print("Sorting considered successful because '-' appeared in the first rows.")
                return True
            if "-" in properties:
                print("Sorting considered usable because current page still contains '-' rows.")
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

    def find_reagent_row_by_sequence(
        self,
        page: Page,
        sequence: str,
        reagent_name: str = "",
        cas: str = "",
    ) -> Locator | None:
        sequence = str(sequence or "").strip()
        if not sequence:
            return None

        try:
            match = page.evaluate(
                """
                ({ sequence, reagentName, cas }) => {
                  const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim();
                  const keyOf = (row) => {
                    const rowKey = row.getAttribute('data-row-key') || '';
                    if (rowKey) return `key:${rowKey}`;
                    const rect = row.getBoundingClientRect();
                    return `top:${Math.round(rect.top)}`;
                  };
                  const visible = (node) => {
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                      && style.visibility !== 'hidden'
                      && style.display !== 'none';
                  };
                  const groups = new Map();
                  for (const row of Array.from(document.querySelectorAll('tbody tr.ant-table-row')).filter(visible)) {
                    const key = keyOf(row);
                    if (!groups.has(key)) groups.set(key, []);
                    groups.get(key).push(row);
                  }
                  const scoreGroup = (rows) => {
                    const text = normalize(rows.map((row) => row.innerText).join(' '));
                    const tokens = text.split(' ').filter(Boolean);
                    let score = 0;
                    if (tokens.includes(sequence) || text.includes(sequence)) score += 10;
                    if (reagentName && text.includes(reagentName)) score += 3;
                    if (cas && cas !== '-' && text.includes(cas)) score += 3;
                    return score;
                  };
                  const scored = Array.from(groups.entries())
                    .map(([key, rows]) => ({ key, rows, score: scoreGroup(rows) }))
                    .filter((item) => item.score >= 10)
                    .sort((a, b) => b.score - a.score);
                  if (!scored.length) return null;
                  const best = scored[0];
                  const actionRow = best.rows.find((row) => normalize(row.innerText).includes('\\u6280\\u672f\\u5224\\u5b9a'));
                  const dataRowKey = best.key.startsWith('key:') ? best.key.slice(4) : '';
                  const targetRow = actionRow || best.rows[0];
                  const allRows = Array.from(document.querySelectorAll('tbody tr.ant-table-row'));
                  return {
                    index: allRows.indexOf(targetRow),
                    dataRowKey,
                    text: normalize(best.rows.map((row) => row.innerText).join(' ')).slice(0, 260),
                  };
                }
                """,
                {"sequence": sequence, "reagentName": str(reagent_name or "").strip(), "cas": str(cas or "").strip()},
            )
        except Error:
            match = None

        if isinstance(match, dict):
            index = match.get("index")
            if isinstance(index, int) and index >= 0:
                try:
                    row = page.locator("tbody tr.ant-table-row").nth(index)
                    print(f"Matched reagent row for sequence {sequence}: {match.get('text', '')}")
                    return row
                except Error:
                    pass

        rows = page.locator("tbody tr.ant-table-row")
        count = rows.count()
        for index in range(count):
            row = rows.nth(index)
            try:
                row_text = self.safe_inner_text(row)
                if sequence in row_text:
                    if reagent_name and reagent_name not in row_text:
                        continue
                    if cas and cas != "-" and cas not in row_text:
                        continue
                    return row
            except Error:
                continue
        return None

    def read_reagent_property_by_sequence(self, page: Page, sequence: str) -> str:
        sequence = str(sequence or "").strip()
        if not sequence:
            return ""
        for record in self.read_current_page_reagents(page):
            if str(record.get("\u5e8f\u53f7", "")).strip() == sequence:
                return str(record.get("\u7269\u5316\u7279\u6027", "")).strip()
        return ""

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
        self.dismiss_open_overlays(page)
        header = page.locator("thead th").filter(has_text="\u7269\u5316\u7279\u6027").first
        if not header.count():
            raise RuntimeError("Could not find the physicochemical property header.")

        try:
            header.click(timeout=5000)
            return
        except Error as error:
            print(f"Normal physicochemical property header click failed; retrying by coordinates: {error}")

        if self.click_reagent_property_header_by_dom(page):
            return

        box = header.bounding_box()
        if not box:
            raise RuntimeError("Could not get physicochemical property header bounding box.")
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    @staticmethod
    def click_reagent_property_header_by_dom(page: Page) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    () => {
                      const wanted = '\\u7269\\u5316\\u7279\\u6027';
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, '').trim();
                      const headers = Array.from(document.querySelectorAll('thead th'))
                        .filter((node) => visible(node) && text(node).includes(wanted));
                      const header = headers.find((node) => node.classList.contains('ant-table-column-has-sorters'))
                        || headers[0];
                      if (!header) return false;
                      header.scrollIntoView({ block: 'center', inline: 'center' });
                      const target = header.querySelector('.ant-table-column-sorters')
                        || header.querySelector('.ant-table-filter-column-title')
                        || header;
                      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                        target.dispatchEvent(new MouseEvent(type, {
                          bubbles: true,
                          cancelable: true,
                          view: window,
                        }));
                      }
                      return true;
                    }
                    """
                )
            )
        except Error:
            return False

    @staticmethod
    def dismiss_open_overlays(page: Page) -> None:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Error:
            return

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
              const isVisible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
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
              const parseLogicalTable = () => {
                const parseRowsWithHeaders = (headers, rows) => {
                  const columnIndexes = {};
                  for (const wanted of wantedColumns) {
                    const wantedHeader = normalizeHeader(wanted);
                    const index = headers.findIndex((header) => header === wantedHeader || header.includes(wantedHeader));
                    columnIndexes[wanted] = index >= 0 ? index : fallbackColumnIndexes[wanted];
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
                };
                const hasUsableRecordProperties = (records) => {
                  const propertyKey = '\\u7269\\u5316\\u7279\\u6027';
                  const nameKey = '\\u8bd5\\u5242\\u540d\\u79f0';
                  return records.some((record) => record[propertyKey] && record[propertyKey] !== nameKey);
                };

                const containers = Array.from(document.querySelectorAll('.ant-table-container'));
                for (const container of containers) {
                  const headers = Array.from(container.querySelectorAll('thead th')).map((th) => normalizeHeader(th.innerText));
                  if (!headers.some((header) => header.includes('\\u8bd5\\u5242\\u540d\\u79f0'))
                    || !headers.some((header) => header.includes('\\u7269\\u5316\\u7279\\u6027'))) {
                    continue;
                  }
                  const rows = Array.from(container.querySelectorAll('tbody tr.ant-table-row'));
                  const records = parseRowsWithHeaders(headers, rows);
                  if (records.length && hasUsableRecordProperties(records)) {
                    return records;
                  }
                }

                const candidateTables = Array.from(document.querySelectorAll(
                  '.ant-table-body table, .ant-table-content table, .ant-table-container table'
                ));
                const reagentTables = candidateTables.filter((table) => {
                  const headers = Array.from(table.querySelectorAll('thead th')).map((th) => normalizeHeader(th.innerText));
                  return headers.some((header) => header.includes('\\u8bd5\\u5242\\u540d\\u79f0'))
                    && headers.some((header) => header.includes('\\u7269\\u5316\\u7279\\u6027'));
                });

                for (const table of reagentTables) {
                  const headers = Array.from(table.querySelectorAll('thead th')).map((th) => normalizeHeader(th.innerText));
                  const rows = Array.from(table.querySelectorAll('tbody tr.ant-table-row'));
                  const records = parseRowsWithHeaders(headers, rows);
                  if (records.length && hasUsableRecordProperties(records)) {
                    return records;
                  }
                }

                return [];
              };

              const logicalRecords = parseLogicalTable();
              if (logicalRecords.length) {
                return logicalRecords;
              }

              const visibleHeaders = Array.from(document.querySelectorAll('thead th')).filter(isVisible);
              const headerCenters = {};
              for (const wanted of wantedColumns) {
                const wantedHeader = normalizeHeader(wanted);
                const matched = visibleHeaders
                  .map((th) => ({ th, text: normalizeHeader(th.innerText), rect: th.getBoundingClientRect() }))
                  .filter((item) => item.text === wantedHeader || item.text.includes(wantedHeader))
                  .sort((a, b) => b.rect.width - a.rect.width)[0];
                if (matched) {
                  headerCenters[wanted] = matched.rect.left + matched.rect.width / 2;
                }
              }

              const visibleRows = Array.from(document.querySelectorAll('tbody tr.ant-table-row')).filter(isVisible);
              const groupedRows = new Map();
              for (const row of visibleRows) {
                const rect = row.getBoundingClientRect();
                const key = row.getAttribute('data-row-key') || String(Math.round(rect.top));
                if (!groupedRows.has(key)) {
                  groupedRows.set(key, []);
                }
                groupedRows.get(key).push(row);
              }

              const recordsByPosition = [];
              for (const rows of groupedRows.values()) {
                const cells = rows.flatMap((row) => Array.from(row.querySelectorAll('td')).filter(isVisible));
                if (!cells.length) {
                  continue;
                }
                const record = {};
                for (const wanted of wantedColumns) {
                  const centerX = headerCenters[wanted];
                  let cell = null;
                  if (Number.isFinite(centerX)) {
                    cell = cells
                      .map((td) => ({ td, rect: td.getBoundingClientRect() }))
                      .filter((item) => item.rect.left - 2 <= centerX && item.rect.right + 2 >= centerX)
                      .sort((a, b) => Math.abs((a.rect.left + a.rect.width / 2) - centerX) - Math.abs((b.rect.left + b.rect.width / 2) - centerX))[0]?.td || null;
                  }
                  if (!cell) {
                    const fallbackIndex = fallbackColumnIndexes[wanted];
                    cell = cells[fallbackIndex] || null;
                  }
                  record[wanted] = cell ? displayText(cell.innerText) : '';
                }
                recordsByPosition.push({ record, top: Math.min(...rows.map((row) => row.getBoundingClientRect().top)) });
              }

              const positioned = recordsByPosition
                .sort((a, b) => a.top - b.top)
                .map((item) => item.record)
                .filter((record) => Object.values(record).some(Boolean));
              if (positioned.length) {
                return positioned;
              }

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
