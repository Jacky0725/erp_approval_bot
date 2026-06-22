from __future__ import annotations

from typing import Any

import pandas as pd


BLOCKING_REVIEW_STATUSES = {
    "",
    "pending",
    "manual_review",
    "open",
    "todo",
    "待处理",
    "待复核",
    "人工复核",
    "需人工复核",
}


class ReviewQueueMixin:
    def clear_manual_review_items_for_list(self, list_number: str) -> None:
        list_number = str(list_number or "").strip()
        if not list_number:
            return

        paths = self.settings.get("paths", {})
        review_queue_path = self.root_dir / paths.get("review_queue_excel", "data/review_queue.xlsx")
        if not review_queue_path.exists():
            return

        try:
            queue = pd.read_excel(review_queue_path, dtype=str).fillna("")
        except Exception as error:
            print(f"Could not clear old manual review items for {list_number}: {error}")
            return

        if queue.empty:
            return

        list_columns = [
            "\u8bd5\u5242\u6e05\u5355\u53f7",
            "\u5f53\u524d\u6e05\u5355\u53f7",
            "\u6e05\u5355\u53f7",
            "list_number",
            "reagent_list_no",
            "reagent_list_number",
            "order_no",
        ]
        present_list_columns = [column for column in list_columns if column in queue.columns]
        if not present_list_columns:
            return

        matched = pd.Series(False, index=queue.index)
        for column in present_list_columns:
            matched = matched | (queue[column].astype(str).str.strip() == list_number)

        removed_count = int(matched.sum())
        if not removed_count:
            return

        queue = queue[~matched].copy()
        self.write_excel_with_fallback(queue, review_queue_path)
        print(f"Cleared {removed_count} old manual review item(s) for list {list_number}.")

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

        blocking = self._blocking_review_rows(matched)
        if blocking.empty:
            print(f"Review queue has no pending manual review item for current list: {list_number}")
            return False, ""

        output_path = self._log_dir() / "auto_pass_blocked_review_queue.xlsx"
        output_path = self.write_excel_with_fallback(blocking, output_path)
        return (
            True,
            f"Review queue contains {len(blocking)} pending manual review row(s) for current list; saved {output_path}.",
        )

    @staticmethod
    def _blocking_review_rows(rows: pd.DataFrame) -> pd.DataFrame:
        if rows.empty:
            return rows
        status_column = next((column for column in ("status", "状态", "处理状态") if column in rows.columns), "")
        if not status_column:
            return rows
        normalized = rows[status_column].astype(str).str.strip().str.lower()
        return rows[normalized.isin(BLOCKING_REVIEW_STATUSES)]

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
