from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from erp_session import ErpSessionMixin  # noqa: E402


class ErpSessionRuntimeTest(unittest.TestCase):
    def test_open_login_page_raises_after_all_navigation_attempts_fail(self) -> None:
        page = MagicMock()
        page.goto.side_effect = PlaywrightTimeoutError("navigation timed out")

        with self.assertRaisesRegex(RuntimeError, "after 3 navigation attempts"):
            ErpSessionMixin().open_login_page(page, "https://erp.invalid")

        self.assertEqual(page.goto.call_count, 3)
        self.assertEqual(page.wait_for_timeout.call_count, 3)

    def test_open_login_page_can_recover_before_business_callback(self) -> None:
        page = MagicMock()
        page.goto.side_effect = [PlaywrightTimeoutError("temporary timeout"), None]

        ErpSessionMixin().open_login_page(page, "https://erp.invalid")

        self.assertEqual(page.goto.call_count, 2)
        page.wait_for_load_state.assert_called_once_with("domcontentloaded", timeout=30000)

    def test_unverified_app_shell_fails_before_business_callback(self) -> None:
        page = MagicMock()
        page.wait_for_selector.side_effect = PlaywrightTimeoutError("not visible")

        with self.assertRaisesRegex(RuntimeError, "verified application shell"):
            ErpSessionMixin().wait_for_app_shell(page)

    def test_app_shell_can_be_verified_by_menu_text_fallback(self) -> None:
        page = MagicMock()
        page.wait_for_selector.side_effect = [
            PlaywrightTimeoutError("selector unavailable") for _ in range(6)
        ] + [None]

        ErpSessionMixin().wait_for_app_shell(page)

        self.assertEqual(page.wait_for_selector.call_count, 7)

    def test_packaged_headless_only_forces_headless_browser(self) -> None:
        with patch.dict(os.environ, {"REAGENT_APPROVAL_HEADLESS_ONLY": "true"}, clear=False):
            self.assertTrue(ErpSessionMixin.effective_browser_headless({"headless": False}))

    def test_source_runtime_respects_configured_headed_browser(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ErpSessionMixin.effective_browser_headless({"headless": False}))

    def test_configured_headless_stays_headless(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(ErpSessionMixin.effective_browser_headless({"headless": True}))

    def test_browser_closed_on_unhandled_callback_error(self) -> None:
        bot = ErpSessionMixin()
        bot.settings = {"browser": {"headless": False}}
        bot.open_login_page = MagicMock()
        bot.login = MagicMock()
        bot.wait_for_app_shell = MagicMock()
        with tempfile.TemporaryDirectory() as tmp, patch("erp_session.sync_playwright") as playwright, patch.dict(
            os.environ, {"ERP_URL": "https://erp.invalid", "ERP_USERNAME": "test", "ERP_PASSWORD": "test"}
        ):
            bot._log_dir = lambda: Path(tmp)
            browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
            with self.assertRaisesRegex(ValueError, "callback failed"):
                bot.run_after_login_capture("screen.png", "page.html", MagicMock(side_effect=ValueError("callback failed")))
            browser.close.assert_called_once()

    def test_browser_closed_when_context_creation_fails(self) -> None:
        bot = ErpSessionMixin()
        bot.settings = {"browser": {"headless": False}}
        with tempfile.TemporaryDirectory() as tmp, patch("erp_session.sync_playwright") as playwright, patch.dict(
            os.environ, {"ERP_URL": "https://erp.invalid", "ERP_USERNAME": "test", "ERP_PASSWORD": "test"}
        ):
            bot._log_dir = lambda: Path(tmp)
            browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
            browser.new_context.side_effect = ValueError("context failed")
            with self.assertRaisesRegex(ValueError, "context failed"):
                bot.run_after_login_capture("screen.png", "page.html", None)
            browser.close.assert_called_once()

    def test_runtime_error_after_callback_start_is_not_replayed(self) -> None:
        bot = ErpSessionMixin()
        bot.settings = {"browser": {"headless": False}}
        bot.open_login_page = MagicMock()
        bot.login = MagicMock()
        bot.wait_for_app_shell = MagicMock()
        callback = MagicMock(side_effect=RuntimeError("write outcome unknown"))
        with tempfile.TemporaryDirectory() as tmp, patch("erp_session.sync_playwright") as playwright, patch.dict(
            os.environ, {"ERP_URL": "https://erp.invalid", "ERP_USERNAME": "test", "ERP_PASSWORD": "test"}
        ):
            bot._log_dir = lambda: Path(tmp)
            chromium = playwright.return_value.__enter__.return_value.chromium
            chromium.launch.return_value.new_context.return_value.new_page.return_value.content.return_value = "test page"
            with self.assertRaisesRegex(RuntimeError, "write outcome unknown"):
                bot.run_after_login_capture("screen.png", "page.html", callback)
            callback.assert_called_once()
            chromium.launch.assert_called_once()
            chromium.launch.return_value.close.assert_called_once()

    def test_failure_before_callback_still_retries(self) -> None:
        bot = ErpSessionMixin()
        bot.settings = {"browser": {"headless": False}}
        bot.open_login_page = MagicMock(side_effect=RuntimeError("login unavailable"))
        callback = MagicMock()
        with tempfile.TemporaryDirectory() as tmp, patch("erp_session.sync_playwright") as playwright, patch.dict(
            os.environ, {"ERP_URL": "https://erp.invalid", "ERP_USERNAME": "test", "ERP_PASSWORD": "test"}
        ):
            bot._log_dir = lambda: Path(tmp)
            chromium = playwright.return_value.__enter__.return_value.chromium
            chromium.launch.return_value.new_context.return_value.new_page.return_value.content.return_value = "test page"
            with self.assertRaisesRegex(RuntimeError, "login unavailable"):
                bot.run_after_login_capture("screen.png", "page.html", callback)
            callback.assert_not_called()
            self.assertEqual(chromium.launch.call_count, 3)
            self.assertEqual(chromium.launch.return_value.close.call_count, 3)


if __name__ == "__main__":
    unittest.main()
