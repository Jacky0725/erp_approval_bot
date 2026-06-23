from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Error, Locator, Page, TimeoutError


DEFAULT_PROPERTY_ALIASES = {
    "\u5f3a\u53cd\u5e94\u6027": ["\u5f3a\u53cd\u5e94"],
    "\u5f3a\u53cd\u5e94": ["\u5f3a\u53cd\u5e94\u6027"],
    "\u6613\u71c3\u6db2\u4f53": ["\u6613\u71c3\u7c7b"],
    "\u6613\u71c3\u7c7b": ["\u6613\u71c3\u6db2\u4f53"],
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

    def save(self, page: Page, row: Locator) -> bool:
        return self._click_row_action(page, row, "\u4fdd\u5b58")

    def generate_reagent_library(self, page: Page, row: Locator) -> bool:
        return self._click_row_action(page, row, "\u751f\u6210\u8bd5\u5242\u5e93")

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
        return self._click_row_peer_select(page, row)

    @staticmethod
    def _click_row_action(page: Page, row: Locator, action_text: str) -> bool:
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
        return ApprovalWriter._click_row_peer_action(page, row, action_text)

    @staticmethod
    def _row_identity(row: Locator) -> dict[str, Any]:
        try:
            return row.evaluate(
                """
                (node) => {
                  const rect = node.getBoundingClientRect();
                  return {
                    rowKey: node.getAttribute('data-row-key') || '',
                    top: rect.top,
                    height: rect.height,
                  };
                }
                """
            )
        except Error:
            return {"rowKey": "", "top": None, "height": None}

    @staticmethod
    def _click_row_peer_select(page: Page, row: Locator | None) -> bool:
        if row is None:
            return False
        identity = ApprovalWriter._row_identity(row)
        try:
            return bool(
                page.evaluate(
                    """
                    ({ rowKey, top }) => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const sameRow = (tr) => {
                        if (!tr) return false;
                        if (rowKey && tr.getAttribute('data-row-key') === rowKey) return true;
                        if (Number.isFinite(top)) {
                          const rect = tr.getBoundingClientRect();
                          return Math.abs(rect.top - top) < 3;
                        }
                        return false;
                      };
                      const rows = Array.from(document.querySelectorAll('tbody tr.ant-table-row')).filter(sameRow);
                      for (const tr of rows) {
                        const select = Array.from(tr.querySelectorAll('.ant-select, input[role="combobox"]')).find(visible);
                        if (select) {
                          select.scrollIntoView({ block: 'center', inline: 'center' });
                          select.click();
                          return true;
                        }
                      }
                      return false;
                    }
                    """,
                    identity,
                )
            )
        except Error:
            return False

    @staticmethod
    def _click_row_peer_action(page: Page, row: Locator, action_text: str) -> bool:
        identity = ApprovalWriter._row_identity(row)
        try:
            return bool(
                page.evaluate(
                    """
                    ({ rowKey, top, actionText }) => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                      const sameRow = (tr) => {
                        if (!tr) return false;
                        if (rowKey && tr.getAttribute('data-row-key') === rowKey) return true;
                        if (Number.isFinite(top)) {
                          const rect = tr.getBoundingClientRect();
                          return Math.abs(rect.top - top) < 3;
                        }
                        return false;
                      };
                      const rows = Array.from(document.querySelectorAll('tbody tr.ant-table-row')).filter(sameRow);
                      for (const tr of rows) {
                        const action = Array.from(tr.querySelectorAll('button, a'))
                          .find((node) => visible(node) && text(node).includes(actionText));
                        if (action) {
                          action.scrollIntoView({ block: 'center', inline: 'center' });
                          action.click();
                          return true;
                        }
                      }
                      return false;
                    }
                    """,
                    {**identity, "actionText": action_text},
                )
            )
        except Error:
            return False
