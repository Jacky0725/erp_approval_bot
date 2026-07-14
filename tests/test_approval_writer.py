from __future__ import annotations

import sys
import unittest
from pathlib import Path

from playwright.sync_api import Error, sync_playwright


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from approval_writer import ApprovalWriter  # noqa: E402


class ApprovalWriterTest(unittest.TestCase):
    def test_strong_reaction_property_alias(self) -> None:
        writer = ApprovalWriter()

        self.assertEqual(writer.property_name_candidates("强反应性"), ["强反应性", "强反应"])
        self.assertEqual(writer.property_name_candidates("易燃液体"), ["易燃液体", "易燃类"])

    def test_configured_property_aliases_are_used(self) -> None:
        writer = ApprovalWriter(
            settings={
                "reagent": {
                    "physicochemical_property_aliases": {
                        "易燃液体": ["易燃类"],
                    }
                }
            }
        )

        self.assertEqual(writer.property_name_candidates("易燃液体"), ["易燃液体", "易燃类"])


class ApprovalWriterDropdownBindingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Error as exc:
            raise unittest.SkipTest(f"Playwright browser is unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        browser = getattr(cls, "browser", None)
        if browser is not None:
            browser.close()
        playwright = getattr(cls, "playwright", None)
        if playwright is not None:
            playwright.stop()

    def setUp(self) -> None:
        self.page = self.browser.new_page()

    def tearDown(self) -> None:
        self.page.close()

    def test_click_option_uses_current_row_bound_dropdown(self) -> None:
        self.page.set_content(
            """
            <style>
              tr, .ant-select, input, .ant-select-dropdown, .ant-select-item-option {
                display: block;
                width: 160px;
                height: 32px;
              }
              .ant-select-dropdown { position: fixed; top: 100px; left: 10px; }
              #list-2 { left: 220px; }
            </style>
            <table><tbody>
              <tr class="ant-table-row" data-row-key="row-1">
                <td><div class="ant-select"><input role="combobox" aria-controls="list-1"></div></td>
              </tr>
              <tr class="ant-table-row" data-row-key="row-2">
                <td><div class="ant-select ant-select-open"><input id="row-2-input" role="combobox" aria-controls="list-2"></div></td>
              </tr>
            </tbody></table>
            <div id="list-1" class="ant-select-dropdown">
              <div class="ant-select-item-option"><span class="ant-select-item-option-content">强反应</span></div>
            </div>
            <div id="list-2" class="ant-select-dropdown">
              <div class="ant-select-item-option"><span class="ant-select-item-option-content">强反应</span></div>
            </div>
            <script>
              window.clickedDropdown = "";
              for (const dropdown of document.querySelectorAll(".ant-select-dropdown")) {
                dropdown.addEventListener("click", () => { window.clickedDropdown = dropdown.id; });
              }
              document.getElementById("row-2-input").focus();
            </script>
            """
        )

        row = self.page.locator("tr[data-row-key='row-2']")

        self.assertTrue(ApprovalWriter._click_property_option_in_bound_dropdown(self.page, "强反应", row))
        self.assertEqual(self.page.evaluate("window.clickedDropdown"), "list-2")

    def test_bound_dropdown_scroll_uses_mouse_wheel_to_render_virtual_option(self) -> None:
        self.page.set_content(
            """
            <style>
              tr, .ant-select, input { display: block; width: 160px; height: 32px; }
              .ant-select-dropdown { position: fixed; top: 80px; left: 20px; width: 180px; height: 120px; }
              .rc-virtual-list-holder { height: 96px; overflow: auto; }
              .scroll-pad { height: 960px; }
              .ant-select-item-option { height: 32px; }
            </style>
            <table><tbody>
              <tr class="ant-table-row" data-row-key="row-1">
                <td><div class="ant-select ant-select-open"><input id="input-1" role="combobox" aria-controls="list-1"></div></td>
              </tr>
            </tbody></table>
            <div id="list-1" class="ant-select-dropdown">
              <div class="rc-virtual-list-holder">
                <div class="scroll-pad">
                  <div class="ant-select-item-option"><span class="ant-select-item-option-content">普通类</span></div>
                  <div class="ant-select-item-option"><span class="ant-select-item-option-content">重金属类</span></div>
                </div>
              </div>
            </div>
            <script>
              const holder = document.querySelector(".rc-virtual-list-holder");
              holder.addEventListener("wheel", () => {
                holder.querySelector(".scroll-pad").innerHTML =
                  '<div class="ant-select-item-option"><span class="ant-select-item-option-content">强反应</span></div>';
              });
              document.getElementById("input-1").focus();
            </script>
            """
        )
        row = self.page.locator("tr[data-row-key='row-1']")
        options = ["普通类", "重金属类", "溴碘类", "常规酸", "强反应"]

        self.assertTrue(ApprovalWriter._scroll_bound_dropdown_to_option(self.page, "强反应", options, row))

    def test_committed_check_ignores_selected_option_from_other_dropdown(self) -> None:
        self.page.set_content(
            """
            <style>
              tr, .ant-select, input, .ant-select-dropdown, .ant-select-item-option {
                display: block;
                width: 160px;
                height: 32px;
              }
              .ant-select-dropdown { position: fixed; top: 100px; left: 10px; }
            </style>
            <table><tbody>
              <tr class="ant-table-row" data-row-key="row-1">
                <td><div class="ant-select"><input role="combobox" aria-controls="list-1"></div></td>
              </tr>
              <tr class="ant-table-row" data-row-key="row-2">
                <td><div class="ant-select"><span class="ant-select-selection-item">强反应</span></div></td>
              </tr>
            </tbody></table>
            <div id="list-1" class="ant-select-dropdown">
              <div class="ant-select-item-option ant-select-item-option-selected">
                <span class="ant-select-item-option-content">强反应</span>
              </div>
            </div>
            """
        )

        row_without_value = self.page.locator("tr[data-row-key='row-1']")
        row_with_value = self.page.locator("tr[data-row-key='row-2']")

        self.assertFalse(
            ApprovalWriter._row_property_selection_looks_committed(self.page, row_without_value, "强反应")
        )
        self.assertTrue(
            ApprovalWriter._row_property_selection_looks_committed(self.page, row_with_value, "强反应")
        )


if __name__ == "__main__":
    unittest.main()
