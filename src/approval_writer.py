from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Error, Locator, Page, TimeoutError

from category_mapper import erp_candidates_for_rule_category


@dataclass
class ApprovalWriter:
    settings: dict[str, Any] | None = None

    def row_is_editing(self, page: Page, row: Locator) -> bool:
        try:
            if row.locator(".ant-select, input[role='combobox']").count():
                return True
            if row.locator("button, a").filter(has_text="\u4fdd\u5b58").count():
                return True
        except (Error, TimeoutError):
            pass
        return self._row_peer_is_editing(page, row)

    def open_technical_judgement(self, row: Locator, page: Page | None = None) -> bool:
        if page is not None and self.row_is_editing(page, row):
            return True
        try:
            row.get_by_role("button", name="\u6280\u672f\u5224\u5b9a").first.click(timeout=3000)
            return True
        except (Error, TimeoutError):
            try:
                row.locator("button, a").filter(has_text="\u6280\u672f\u5224\u5b9a").first.click(timeout=3000)
                return True
            except (Error, TimeoutError):
                if page is not None:
                    return self._click_row_peer_action(page, row, "\u6280\u672f\u5224\u5b9a")
                return False

    def choose_property(self, page: Page, property_name: str, row: Locator | None = None) -> bool:
        if not property_name.strip():
            return False

        for candidate_name in self.property_name_candidates(property_name):
            self.dismiss_open_dropdown(page)
            if not self._open_property_dropdown(page, row):
                continue
            if self._select_property_option(page, candidate_name):
                self.commit_property_selection(page)
                if row is None or self._row_property_selection_matches(page, row, candidate_name):
                    return True
                self._commit_row_property_selection(page, row)
                if self._row_property_selection_matches(page, row, candidate_name):
                    return True
        self.dismiss_open_dropdown(page)
        return False

    def property_name_candidates(self, property_name: str) -> list[str]:
        property_name = str(property_name or "").strip()
        if not property_name:
            return []
        return erp_candidates_for_rule_category(property_name, self.settings or {})

    def save(self, page: Page, row: Locator) -> bool:
        return self._click_row_action(page, row, "\u4fdd\u5b58")

    def generate_reagent_library(self, page: Page, row: Locator) -> bool:
        return self._click_row_action(page, row, "\u751f\u6210\u8bd5\u5242\u5e93")

    def cancel_edit(self, page: Page, row: Locator | None = None) -> bool:
        self.dismiss_open_dropdown(page)
        clicked = False
        if row is not None:
            clicked = self._click_row_action(page, row, "\u53d6\u6d88")
        if not clicked:
            clicked = self._click_first_visible_action(page, "\u53d6\u6d88")
        if not clicked:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(150)
                page.keyboard.press("Escape")
            except (Error, TimeoutError):
                pass
        page.wait_for_timeout(350)
        if row is not None:
            try:
                return not self.row_is_editing(page, row)
            except (Error, TimeoutError):
                return not self.any_row_is_editing(page)
        return not self.any_row_is_editing(page)

    def any_row_is_editing(self, page: Page) -> bool:
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
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                      const rows = Array.from(document.querySelectorAll('tbody tr.ant-table-row')).filter(visible);
                      return rows.some((tr) => {
                        const hasEditor = Array.from(tr.querySelectorAll('.ant-select, input[role="combobox"]')).some(visible);
                        const hasSave = Array.from(tr.querySelectorAll('button, a')).some(
                          (node) => visible(node) && text(node).includes('\u4fdd\u5b58')
                        );
                        return hasEditor || hasSave;
                      });
                    }
                    """
                )
            )
        except (Error, TimeoutError):
            return False

    def cancel_any_edit(self, page: Page) -> bool:
        return self.cancel_edit(page, None)

    def _open_property_dropdown(self, page: Page, row: Locator | None = None) -> bool:
        selectors = (self.settings or {}).get("selectors", {})
        configured = str(selectors.get("property_select", "") or "").strip()
        candidates = []
        self._scroll_property_column_into_view(page)
        if row is not None:
            candidates.extend(
                [
                    row.locator("td").filter(has_text="\u9009\u62e9\u641c\u7d22").locator(".ant-select").first,
                    row.locator("td").filter(has_text="\u9009\u62e9\u641c\u7d22").locator("input[role='combobox']").first,
                ]
            )
        if configured:
            candidates.append(page.locator(configured).first)
        if row is None:
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
                    page.wait_for_timeout(200)
                    if self._visible_property_dropdown(page):
                        return True
            except (Error, TimeoutError):
                continue
        if self._click_row_property_select(page, row):
            page.wait_for_timeout(200)
            return self._visible_property_dropdown(page)
        if self._click_row_peer_select(page, row):
            page.wait_for_timeout(200)
            return self._visible_property_dropdown(page)
        return False

    @staticmethod
    def _select_property_option(page: Page, candidate_name: str) -> bool:
        if ApprovalWriter._click_property_option(page, candidate_name):
            return True
        if ApprovalWriter._type_property_search_text(page, candidate_name):
            page.wait_for_timeout(250)
            if ApprovalWriter._click_property_option(page, candidate_name):
                return True
            if ApprovalWriter._confirm_property_option_by_keyboard(page, candidate_name):
                return True
        return False

    @staticmethod
    def _click_property_option(page: Page, candidate_name: str) -> bool:
        for _ in range(8):
            if ApprovalWriter._click_property_option_in_active_dropdown(page, candidate_name):
                return True
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

            if ApprovalWriter._click_property_option_by_dom(page, candidate_name):
                return True

            if not ApprovalWriter._scroll_open_dropdown(page):
                break
        return False

    @staticmethod
    def _scroll_property_column_into_view(page: Page) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    () => {
                      const wantedHeader = '物化特性';
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, '').trim();
                      const header = Array.from(document.querySelectorAll('thead th'))
                        .filter((node) => text(node).includes(wantedHeader))
                        .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width)[0];
                      const scrollBody = Array.from(document.querySelectorAll('.ant-table-body'))
                        .filter(visible)
                        .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width)[0];
                      if (!header || !scrollBody) return false;

                      const bodyRect = scrollBody.getBoundingClientRect();
                      const headerRect = header.getBoundingClientRect();
                      const headerCenter = headerRect.left + headerRect.width / 2;
                      if (headerCenter > bodyRect.left + 80 && headerCenter < bodyRect.right - 80) {
                        return true;
                      }

                      scrollBody.scrollLeft += headerCenter - (bodyRect.left + bodyRect.width * 0.55);
                      scrollBody.dispatchEvent(new Event('scroll', { bubbles: true }));
                      return true;
                    }
                    """
                )
            )
        except (Error, TimeoutError):
            return False

    @staticmethod
    def commit_property_selection(page: Page) -> None:
        try:
            page.wait_for_timeout(150)
            page.keyboard.press("Enter")
            page.wait_for_timeout(150)
            page.keyboard.press("Tab")
            page.wait_for_timeout(250)
        except (Error, TimeoutError):
            try:
                page.mouse.click(20, 20)
                page.wait_for_timeout(150)
            except (Error, TimeoutError):
                pass

    @staticmethod
    def _commit_row_property_selection(page: Page, row: Locator) -> None:
        identity = ApprovalWriter._row_identity(row)
        try:
            page.evaluate(
                """
                ({ rowKey, top, height }) => {
                  const visible = (node) => {
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                      && style.visibility !== 'hidden'
                      && style.display !== 'none';
                  };
                  const rowCenter = Number.isFinite(top)
                    ? top + (Number.isFinite(height) ? height / 2 : 0)
                    : null;
                  const sameRow = (tr) => {
                    if (!tr) return false;
                    if (rowKey && tr.getAttribute('data-row-key') === rowKey) return true;
                    if (Number.isFinite(rowCenter)) {
                      const rect = tr.getBoundingClientRect();
                      return Math.abs((rect.top + rect.height / 2) - rowCenter) < 12;
                    }
                    return false;
                  };
                  const rowNode = Array.from(document.querySelectorAll('tbody tr.ant-table-row')).find(sameRow);
                  const input = rowNode
                    ? Array.from(rowNode.querySelectorAll('input[role="combobox"], .ant-select-selection-search-input')).find(visible)
                    : null;
                  if (input) {
                    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
                    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', bubbles: true, cancelable: true }));
                    input.blur();
                  }
                  const active = document.activeElement;
                  if (active && active.blur) active.blur();
                  document.body.click();
                }
                """,
                identity,
            )
            page.wait_for_timeout(300)
        except (Error, TimeoutError):
            try:
                page.keyboard.press("Enter")
                page.wait_for_timeout(150)
                page.mouse.click(20, 20)
                page.wait_for_timeout(150)
            except (Error, TimeoutError):
                pass

    @staticmethod
    def _row_property_selection_matches(page: Page, row: Locator, candidate_name: str) -> bool:
        identity = ApprovalWriter._row_identity(row)
        try:
            for _ in range(8):
                matched = bool(
                    page.evaluate(
                        """
                        ({ rowKey, top, height, candidateName }) => {
                          const visible = (node) => {
                            const rect = node.getBoundingClientRect();
                            const style = window.getComputedStyle(node);
                            return rect.width > 0 && rect.height > 0
                              && style.visibility !== 'hidden'
                              && style.display !== 'none';
                          };
                          const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                          const rowCenter = Number.isFinite(top)
                            ? top + (Number.isFinite(height) ? height / 2 : 0)
                            : null;
                          const sameRow = (tr) => {
                            if (!tr) return false;
                            if (rowKey && tr.getAttribute('data-row-key') === rowKey) return true;
                            if (Number.isFinite(rowCenter)) {
                              const rect = tr.getBoundingClientRect();
                              return Math.abs((rect.top + rect.height / 2) - rowCenter) < 12;
                            }
                            return false;
                          };
                          const rows = Array.from(document.querySelectorAll('tbody tr.ant-table-row')).filter(sameRow);
                          for (const tr of rows) {
                            const selects = Array.from(tr.querySelectorAll('.ant-select')).filter(visible);
                            for (const select of selects) {
                              const selected = text(select);
                              if (selected && selected.includes(candidateName) && !selected.includes('选择搜索')) {
                                return true;
                              }
                            }
                            const rowText = text(tr);
                            if (rowText.includes(candidateName) && !rowText.includes('选择搜索')) {
                              return true;
                            }
                          }
                          return false;
                        }
                        """,
                        {**identity, "candidateName": candidate_name},
                    )
                )
                if matched:
                    return True
                page.wait_for_timeout(150)
        except (Error, TimeoutError):
            return False
        return False

    @staticmethod
    def _click_property_option_in_active_dropdown(page: Page, candidate_name: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    (candidateName) => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                      const active = document.activeElement;
                      const activeControls = active?.getAttribute?.('aria-controls') || '';
                      const dropdowns = Array.from(document.querySelectorAll(
                        '.ant-select-dropdown:not(.ant-select-dropdown-hidden)'
                      )).filter(visible);
                      const ordered = dropdowns
                        .map((dropdown, index) => ({
                          dropdown,
                          index,
                          activeScore: activeControls && dropdown.id === activeControls ? 0 : 1,
                        }))
                        .sort((a, b) => a.activeScore - b.activeScore || a.index - b.index)
                        .map((item) => item.dropdown);
                      for (const dropdown of ordered) {
                        const options = Array.from(dropdown.querySelectorAll('.ant-select-item-option')).filter(visible);
                        const option = options.find((node) => text(node) === candidateName)
                          || options.find((node) => text(node).includes(candidateName));
                        if (!option) continue;
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
                      return false;
                    }
                    """,
                    candidate_name,
                )
            )
        except (Error, TimeoutError):
            return False

    @staticmethod
    def _type_property_search_text(page: Page, candidate_name: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    (candidateName) => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const dropdowns = Array.from(document.querySelectorAll(
                        '.ant-select-dropdown:not(.ant-select-dropdown-hidden)'
                      )).filter(visible);
                      const inputs = Array.from(document.querySelectorAll(
                        'input[role="combobox"], .ant-select-selection-search-input'
                      )).filter(visible);
                      const active = document.activeElement;
                      const input = inputs.find((node) => node === active)
                        || inputs.find((node) => dropdowns.some((dropdown) => dropdown.id && node.getAttribute('aria-controls') === dropdown.id))
                        || inputs[inputs.length - 1];
                      if (!input) return false;
                      input.focus();
                      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                      if (setter) {
                        setter.call(input, '');
                      } else {
                        input.value = '';
                      }
                      input.dispatchEvent(new Event('input', { bubbles: true }));
                      if (setter) {
                        setter.call(input, candidateName);
                      } else {
                        input.value = candidateName;
                      }
                      input.dispatchEvent(new Event('input', { bubbles: true }));
                      input.dispatchEvent(new Event('change', { bubbles: true }));
                      input.dispatchEvent(new KeyboardEvent('keydown', {
                        key: candidateName.slice(-1) || ' ',
                        bubbles: true,
                        cancelable: true,
                      }));
                      return true;
                    }
                    """,
                    candidate_name,
                )
            )
        except (Error, TimeoutError):
            try:
                page.keyboard.press("Control+A")
                page.keyboard.type(candidate_name, delay=20)
                return True
            except (Error, TimeoutError):
                return False

    @staticmethod
    def _confirm_property_option_by_keyboard(page: Page, candidate_name: str) -> bool:
        try:
            before_text = ApprovalWriter._selected_option_text(page)
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(80)
            page.keyboard.press("Enter")
            page.wait_for_timeout(250)
            after_text = ApprovalWriter._selected_option_text(page)
            return bool(after_text and after_text != before_text and candidate_name in after_text)
        except (Error, TimeoutError):
            return False

    @staticmethod
    def _selected_option_text(page: Page) -> str:
        try:
            return str(
                page.evaluate(
                    """
                    () => {
                      const active = document.activeElement;
                      const select = active?.closest?.('.ant-select') || document.querySelector('.ant-select-focused');
                      return (select?.innerText || '').replace(/\\s+/g, ' ').trim();
                    }
                    """
                )
                or ""
            )
        except (Error, TimeoutError):
            return ""

    @staticmethod
    def _click_property_option_by_dom(page: Page, candidate_name: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    (candidateName) => {
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
                      )).filter(visible);
                      const option = options.find((node) => text(node) === candidateName)
                        || options.find((node) => text(node).includes(candidateName));
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
                    candidate_name,
                )
            )
        except (Error, TimeoutError):
            return False

    @staticmethod
    def _visible_property_dropdown(page: Page) -> bool:
        try:
            dropdown = page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)").first
            return bool(dropdown.count() and dropdown.is_visible())
        except (Error, TimeoutError):
            return False

    @staticmethod
    def _scroll_open_dropdown(page: Page) -> bool:
        try:
            dropdown = page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)").first
            if not dropdown.count() or not dropdown.is_visible():
                return False
            box = dropdown.bounding_box()
            if not box:
                return False
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.wheel(0, 220)
            page.wait_for_timeout(120)
            return True
        except (Error, TimeoutError):
            return False

    @staticmethod
    def dismiss_open_dropdown(page: Page) -> None:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        except (Error, TimeoutError):
            return

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
    def _click_first_visible_action(page: Page, action_text: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    """
                    ({ actionText }) => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                      const action = Array.from(document.querySelectorAll('button, a'))
                        .find((node) => visible(node) && text(node).includes(actionText));
                      if (!action) return false;
                      action.scrollIntoView({ block: 'center', inline: 'center' });
                      action.click();
                      return true;
                    }
                    """,
                    {"actionText": action_text},
                )
            )
        except (Error, TimeoutError):
            return False

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
                          const target = select.querySelector?.('.ant-select-selector') || select;
                          if (target.focus) target.focus();
                          for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                            target.dispatchEvent(new MouseEvent(type, {
                              bubbles: true,
                              cancelable: true,
                              view: window,
                            }));
                          }
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
    def _click_row_property_select(page: Page, row: Locator | None) -> bool:
        if row is None:
            return False
        identity = ApprovalWriter._row_identity(row)
        try:
            return bool(
                page.evaluate(
                    """
                    ({ rowKey, top, height }) => {
                      const wantedHeader = '\u7269\u5316\u7279\u6027';
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, '').trim();
                      const headers = Array.from(document.querySelectorAll('thead th'))
                        .filter((node) => visible(node) && text(node).includes(wantedHeader))
                        .map((node) => ({ node, rect: node.getBoundingClientRect() }))
                        .sort((a, b) => b.rect.width - a.rect.width);
                      const header = headers[0];
                      const headerCenter = header ? header.rect.left + header.rect.width / 2 : null;
                      const rowCenter = Number.isFinite(top)
                        ? top + (Number.isFinite(height) ? height / 2 : 0)
                        : null;
                      const sameRow = (tr) => {
                        if (!tr) return false;
                        if (rowKey && tr.getAttribute('data-row-key') === rowKey) return true;
                        if (Number.isFinite(rowCenter)) {
                          const rect = tr.getBoundingClientRect();
                          const center = rect.top + rect.height / 2;
                          return Math.abs(center - rowCenter) < 12;
                        }
                        return false;
                      };
                      const rows = Array.from(document.querySelectorAll('tbody tr.ant-table-row')).filter(sameRow);
                      const cells = rows.flatMap((tr) => Array.from(tr.querySelectorAll('td')).filter(visible));
                      let propertyCell = null;
                      if (Number.isFinite(headerCenter)) {
                        propertyCell = cells
                          .map((td) => ({ td, rect: td.getBoundingClientRect() }))
                          .filter((item) => item.rect.left - 4 <= headerCenter && item.rect.right + 4 >= headerCenter)
                          .sort((a, b) => Math.abs((a.rect.left + a.rect.width / 2) - headerCenter)
                            - Math.abs((b.rect.left + b.rect.width / 2) - headerCenter))[0]?.td || null;
                      }
                      if (!propertyCell) {
                        propertyCell = cells.find((td) => (td.innerText || '').includes('\u9009\u62e9\u641c\u7d22')) || null;
                      }
                      if (!propertyCell) return false;
                      const select = Array.from(propertyCell.querySelectorAll('.ant-select, input[role="combobox"]'))
                        .find(visible);
                      if (!select) return false;
                      select.scrollIntoView({ block: 'center', inline: 'center' });
                      const target = select.querySelector?.('.ant-select-selector') || select;
                      if (target.focus) target.focus();
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
                    identity,
                )
            )
        except Error:
            return False

    @staticmethod
    def _row_peer_is_editing(page: Page, row: Locator) -> bool:
        identity = ApprovalWriter._row_identity(row)
        try:
            return bool(
                page.evaluate(
                    """
                    ({ rowKey, top, height }) => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                      const rowCenter = Number.isFinite(top)
                        ? top + (Number.isFinite(height) ? height / 2 : 0)
                        : null;
                      const sameRow = (tr) => {
                        if (!tr) return false;
                        if (rowKey && tr.getAttribute('data-row-key') === rowKey) return true;
                        if (Number.isFinite(rowCenter)) {
                          const rect = tr.getBoundingClientRect();
                          const center = rect.top + rect.height / 2;
                          return Math.abs(center - rowCenter) < 12;
                        }
                        return false;
                      };
                      const rows = Array.from(document.querySelectorAll('tbody tr.ant-table-row')).filter(sameRow);
                      if (rows.some((tr) => Array.from(tr.querySelectorAll('.ant-select, input[role="combobox"]')).some(visible))) {
                        return true;
                      }
                      const actions = Array.from(document.querySelectorAll('button, a'))
                        .filter((node) => visible(node) && text(node).includes('\u4fdd\u5b58'));
                      return actions.some((node) => sameRow(node.closest('tr')));
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
                    ({ rowKey, top, height, actionText }) => {
                      const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 && rect.height > 0
                          && style.visibility !== 'hidden'
                          && style.display !== 'none';
                      };
                      const text = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                      const rowCenter = Number.isFinite(top) ? top + (Number.isFinite(height) ? height / 2 : 0) : null;
                      const sameRow = (tr) => {
                        if (!tr) return false;
                        if (rowKey && tr.getAttribute('data-row-key') === rowKey) return true;
                        if (Number.isFinite(rowCenter)) {
                          const rect = tr.getBoundingClientRect();
                          const center = rect.top + rect.height / 2;
                          return Math.abs(center - rowCenter) < 12;
                        }
                        return false;
                      };
                      const clickAction = (action) => {
                        action.scrollIntoView({ block: 'center', inline: 'center' });
                        action.click();
                        return true;
                      };
                      const actions = Array.from(document.querySelectorAll('button, a'))
                        .filter((node) => visible(node) && text(node).includes(actionText));
                      const rowActions = actions.filter((node) => sameRow(node.closest('tr')));
                      if (rowActions.length) {
                        return clickAction(rowActions[0]);
                      }
                      if (Number.isFinite(rowCenter)) {
                        const aligned = actions
                          .map((node) => {
                            const rect = node.getBoundingClientRect();
                            return { node, distance: Math.abs((rect.top + rect.height / 2) - rowCenter) };
                          })
                          .filter((item) => item.distance < 18)
                          .sort((left, right) => left.distance - right.distance);
                        if (aligned.length) {
                          return clickAction(aligned[0].node);
                        }
                      }
                      if (actions.length === 1) {
                        return clickAction(actions[0]);
                      }
                      return false;
                    }
                    """,
                    {**identity, "actionText": action_text},
                )
            )
        except Error:
            return False
