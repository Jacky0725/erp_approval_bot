from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Error, Locator, Page, TimeoutError


DEFAULT_PROPERTY_ALIASES = {
    "\u5f3a\u53cd\u5e94\u6027": ["\u5f3a\u53cd\u5e94"],
    "\u5f3a\u53cd\u5e94": ["\u5f3a\u53cd\u5e94\u6027"],
}


@dataclass
class ApprovalWriter:
    settings: dict[str, Any] | None = None

    def open_technical_judgement(self, row: Locator) -> bool:
        try:
            row.get_by_role("button", name="\u6280\u672f\u5224\u5b9a").first.click(timeout=3000)
            return True
        except (Error, TimeoutError):
            try:
                row.locator("button, a").filter(has_text="\u6280\u672f\u5224\u5b9a").first.click(timeout=3000)
                return True
            except (Error, TimeoutError):
                return False

    def choose_property(self, page: Page, property_name: str, row: Locator | None = None) -> bool:
        if not property_name.strip():
            return False

        if not self._open_property_dropdown(page, row):
            return False

        for candidate_name in self.property_name_candidates(property_name):
            candidates = [
                page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option")
                .filter(has_text=candidate_name)
                .first,
                page.get_by_text(candidate_name, exact=True).first,
            ]
            for candidate in candidates:
                try:
                    if candidate.count() and candidate.is_visible():
                        candidate.click(timeout=3000)
                        return True
                except (Error, TimeoutError):
                    continue
        return False

    def property_name_candidates(self, property_name: str) -> list[str]:
        property_name = str(property_name or "").strip()
        if not property_name:
            return []

        aliases = dict(DEFAULT_PROPERTY_ALIASES)
        configured_aliases = (
            (self.settings or {})
            .get("reagent", {})
            .get("physicochemical_property_aliases", {})
        )
        if isinstance(configured_aliases, dict):
            for key, values in configured_aliases.items():
                key_text = str(key or "").strip()
                if not key_text:
                    continue
                if isinstance(values, str):
                    value_list = [values]
                elif isinstance(values, list):
                    value_list = values
                else:
                    value_list = []
                aliases.setdefault(key_text, [])
                aliases[key_text].extend(str(value or "").strip() for value in value_list if str(value or "").strip())

        candidates = [property_name, *aliases.get(property_name, [])]
        return list(dict.fromkeys(candidate for candidate in candidates if candidate))

    def save(self, row: Locator) -> bool:
        return self._click_row_action(row, "\u4fdd\u5b58")

    def generate_reagent_library(self, row: Locator) -> bool:
        return self._click_row_action(row, "\u751f\u6210\u8bd5\u5242\u5e93")

    def _open_property_dropdown(self, page: Page, row: Locator | None = None) -> bool:
        selectors = (self.settings or {}).get("selectors", {})
        configured = str(selectors.get("property_select", "") or "").strip()
        candidates = []
        if row is not None:
            candidates.extend(
                [
                    row.locator(".ant-select").first,
                    row.locator("input[role='combobox']").first,
                ]
            )
        if configured:
            candidates.append(page.locator(configured).first)
        candidates.extend(
            [
                page.locator(".ant-drawer .ant-select").first,
                page.locator(".ant-table-row .ant-select").first,
                page.locator("input[role='combobox']").first,
            ]
        )
        for candidate in candidates:
            try:
                if candidate.count() and candidate.is_visible():
                    candidate.click(timeout=3000)
                    return True
            except (Error, TimeoutError):
                continue
        return False

    @staticmethod
    def _click_row_action(row: Locator, action_text: str) -> bool:
        candidates = [
            row.get_by_role("button", name=action_text).first,
            row.locator("button, a").filter(has_text=action_text).first,
        ]
        for candidate in candidates:
            try:
                if candidate.count() and candidate.is_visible():
                    candidate.click(timeout=3000)
                    return True
            except (Error, TimeoutError):
                continue
        return False
