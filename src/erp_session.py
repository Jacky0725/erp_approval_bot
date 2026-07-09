from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Error, Locator, Page, TimeoutError, sync_playwright

from stage_logger import StageLogger


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


class ErpSessionMixin:

    def run_after_login_capture(self, screenshot_name: str, html_name: str, after_login: Any | None) -> None:
        stage_logger = getattr(self, "stage_logger", None) or StageLogger()
        self.stage_logger = stage_logger
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
                    stage_logger.event(f"Browser session attempt {attempt}/3")
                    with stage_logger.stage("open_login_page", erp_url):
                        self.open_login_page(page, erp_url)

                    with stage_logger.stage("login"):
                        self.login(page, username, password, log_dir)
                    with stage_logger.stage("wait_for_app_shell"):
                        self.wait_for_app_shell(page)

                    if after_login:
                        with stage_logger.stage(getattr(after_login, "__name__", "after_login")):
                            after_login(page)

                    with stage_logger.stage("save_final_capture"):
                        page.screenshot(path=str(screenshot_path), full_page=True)
                        html_path.write_text(page.content(), encoding="utf-8")

                    print(f"Saved homepage screenshot: {screenshot_path}")
                    print(f"Saved homepage HTML: {html_path}")
                    self.print_page_structure(page)
                    browser.close()
                    return
                except (Error, RuntimeError) as error:
                    last_error = error
                    failure_screenshot_path = log_dir / f"browser_failure_attempt_{attempt}.png"
                    failure_html_path = log_dir / f"browser_failure_attempt_{attempt}.html"
                    try:
                        page.screenshot(path=str(failure_screenshot_path), full_page=True)
                        failure_html_path.write_text(page.content(), encoding="utf-8")
                        print(f"Saved browser failure screenshot: {failure_screenshot_path}")
                        print(f"Saved browser failure HTML: {failure_html_path}")
                    except Error as capture_error:
                        print(f"Could not capture browser failure page: {capture_error}")
                    print(f"Browser session failed: {error}")
                    browser.close()

            if last_error:
                raise last_error

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
        self.dismiss_transient_overlays(page)
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
                        item.click(timeout=3000)
                        return
            except Exception:
                continue

        if self.click_text_by_dom(page, text):
            return

        raise RuntimeError(f"Could not find visible text to click: {text}")

    @staticmethod
    def dismiss_transient_overlays(page: Page) -> None:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(120)
        except Exception:
            pass
        try:
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
                  for (const node of document.querySelectorAll('.ant-drawer-close, .ant-modal-close')) {
                    if (visible(node)) node.click();
                  }
                }
                """
            )
            page.wait_for_timeout(150)
        except Exception:
            pass

    @staticmethod
    def click_text_by_dom(page: Page, text: str) -> bool:
        try:
            box = page.evaluate(
                """
                (wanted) => {
                  const visible = (node) => {
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0
                      && style.visibility !== 'hidden'
                      && style.display !== 'none';
                  };
                  const textOf = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                  const nodes = Array.from(document.querySelectorAll('li, a, span, div, button'))
                    .filter((node) => visible(node) && textOf(node).includes(wanted));
                  nodes.sort((a, b) => {
                    const at = textOf(a) === wanted ? 0 : 1;
                    const bt = textOf(b) === wanted ? 0 : 1;
                    if (at !== bt) return at - bt;
                    return a.getBoundingClientRect().width - b.getBoundingClientRect().width;
                  });
                  const node = nodes[0];
                  if (!node) return null;
                  node.scrollIntoView({block: 'center', inline: 'center'});
                  const rect = node.getBoundingClientRect();
                  return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, text: textOf(node)};
                }
                """,
                text,
            )
            if not box:
                return False
            page.mouse.click(float(box["x"]), float(box["y"]))
            page.wait_for_timeout(250)
            return True
        except Exception:
            return False

    def login(self, page: Page, username: str, password: str, log_dir: Path) -> None:
        selectors = self.settings.get("selectors", {})

        if self.is_logged_in(page):
            print("ERP session already appears logged in; skipping login form fill.")
            return

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

        self.fill_login_field(scope, username_selector, username, "username", log_dir)
        self.fill_login_field(scope, password_selector, password, "password", log_dir)

        self.click_login_button(scope, login_selector, log_dir)

    def find_login_scope(self, page: Page, selectors: dict[str, str]) -> tuple[Any, str, str, str] | None:
        scopes = [page, *page.frames]

        for scope in scopes:
            username_selector = self.first_usable_selector(
                scope,
                [selectors.get("username_input", ""), *USERNAME_SELECTORS],
                needs_editable=True,
            )
            password_selector = self.first_usable_selector(
                scope,
                [selectors.get("password_input", ""), *PASSWORD_SELECTORS],
                needs_editable=True,
            )
            login_selector = self.first_usable_selector(
                scope,
                [selectors.get("login_button", ""), *LOGIN_BUTTON_SELECTORS],
                needs_editable=False,
            )

            if username_selector and password_selector and login_selector:
                return scope, username_selector, password_selector, login_selector

        return None

    def fill_login_field(self, scope: Any, selector: str, value: str, field_name: str, log_dir: Path) -> None:
        last_error: Exception | None = None
        field = scope.locator(selector).first
        for attempt in range(1, 4):
            try:
                field.wait_for(state="visible", timeout=10000)
                field.scroll_into_view_if_needed(timeout=5000)
                if not field.is_enabled(timeout=3000):
                    raise RuntimeError(f"{field_name} input is not enabled")
                if not field.is_editable(timeout=3000):
                    raise RuntimeError(f"{field_name} input is not editable")

                field.click(timeout=5000)
                field.fill("", timeout=5000)
                field.fill(value, timeout=5000)
                written = self.input_value(field)
                if written == value:
                    return

                field.click(timeout=5000)
                field.press("Control+A")
                field.type(value, delay=35, timeout=15000)
                written = self.input_value(field)
                if written == value:
                    return

                raise RuntimeError(
                    f"{field_name} input value verification failed after fill/type; "
                    f"read back {written!r}"
                )
            except (Error, TimeoutError, RuntimeError) as error:
                last_error = error
                self.capture_login_failure(scope, log_dir, f"login_fill_{field_name}_failed_{attempt}")
                print(f"Login {field_name} fill attempt {attempt}/3 failed: {error}")
                try:
                    page = scope.page if hasattr(scope, "page") else scope
                    page.wait_for_timeout(500)
                except Error:
                    pass

        raise RuntimeError(f"Could not fill ERP login {field_name} field using selector {selector}: {last_error}")

    def click_login_button(self, scope: Any, selector: str, log_dir: Path) -> None:
        button = scope.locator(selector).first
        try:
            button.wait_for(state="visible", timeout=10000)
            button.scroll_into_view_if_needed(timeout=5000)
            if not button.is_enabled(timeout=3000):
                raise RuntimeError("login button is not enabled")
            button.click(timeout=10000)
        except (Error, TimeoutError, RuntimeError) as error:
            self.capture_login_failure(scope, log_dir, "login_button_failed")
            raise RuntimeError(f"Could not click ERP login button using selector {selector}: {error}") from error

    @staticmethod
    def input_value(locator: Locator) -> str:
        try:
            return str(locator.input_value(timeout=3000))
        except Error:
            try:
                return str(locator.evaluate("(node) => node.value || ''"))
            except Error:
                return ""

    def capture_login_failure(self, scope: Any, log_dir: Path, stem: str) -> None:
        screenshot_path = log_dir / f"{stem}.png"
        html_path = log_dir / f"{stem}.html"
        try:
            page = scope.page if hasattr(scope, "page") else scope
            page.screenshot(path=str(screenshot_path), full_page=True)
            html_path.write_text(page.content(), encoding="utf-8")
            print(f"Saved login failure screenshot: {screenshot_path}")
            print(f"Saved login failure HTML: {html_path}")
        except Error as error:
            print(f"Could not capture login failure page: {error}")

    def is_logged_in(self, page: Page) -> bool:
        for text in ("\u8bd5\u5242\u7ba1\u7406", "\u5e02\u573a\u7ba1\u7406"):
            try:
                locator = page.get_by_text(text, exact=True).first
                if locator.count() and locator.is_visible():
                    return True
            except Error:
                continue
        return False

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

    def wait_for_app_shell(self, page: Page) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except TimeoutError:
            print("DOM content was not fully confirmed after login; continuing with current page.")

        try:
            page.wait_for_selector("text=\u8bd5\u5242\u7ba1\u7406, text=\u5e02\u573a\u7ba1\u7406", timeout=8000)
        except TimeoutError:
            print("ERP shell menu text was not confirmed; continuing so target page click can retry/fail explicitly.")

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

    def first_usable_selector(self, page: Page, selectors: list[str], needs_editable: bool = False) -> str | None:
        for selector in dict.fromkeys(str(item or "").strip() for item in selectors if str(item or "").strip()):
            try:
                element = page.locator(selector).first
                if not element.count() or not element.is_visible():
                    continue
                if not element.is_enabled(timeout=1000):
                    continue
                if needs_editable and not element.is_editable(timeout=1000):
                    continue
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
