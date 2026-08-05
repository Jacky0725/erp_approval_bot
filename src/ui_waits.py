from __future__ import annotations

import time
from typing import Any, Callable

from playwright.sync_api import Error, TimeoutError


def wait_until_spinner_hidden(page: Any, timeout_ms: int = 5000) -> bool:
    try:
        page.wait_for_selector(".ant-spin-spinning", state="hidden", timeout=timeout_ms)
        return True
    except (Error, TimeoutError):
        return False


def wait_until_dropdown_options(page: Any, timeout_ms: int = 3000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            options = page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option")
            if options.count() > 0:
                return True
        except (Error, TimeoutError):
            pass
        _short_ui_pause(page)
    return False


def wait_until_row_value(
    page: Any,
    reader: Callable[[], str],
    predicate: Callable[[str], bool],
    timeout_ms: int = 3000,
) -> str:
    deadline = time.monotonic() + timeout_ms / 1000
    last_value = ""
    while time.monotonic() < deadline:
        try:
            last_value = str(reader() or "")
            if predicate(last_value):
                return last_value
        except (Error, TimeoutError):
            pass
        _short_ui_pause(page)
    return last_value


def wait_until_table_stable(
    page: Any,
    signature_reader: Callable[[], str],
    previous_signature: str = "",
    timeout_ms: int = 5000,
) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    last_signature = ""
    stable_seen = 0
    while time.monotonic() < deadline:
        wait_until_spinner_hidden(page, timeout_ms=500)
        try:
            signature = str(signature_reader() or "")
        except (Error, TimeoutError):
            signature = ""
        if signature and signature != previous_signature and signature == last_signature:
            stable_seen += 1
            if stable_seen >= 1:
                return True
        else:
            stable_seen = 0
        last_signature = signature
        _short_ui_pause(page)
    return False


def _short_ui_pause(page: Any, timeout_ms: int = 100) -> None:
    try:
        page.wait_for_timeout(timeout_ms)
    except AttributeError:
        time.sleep(timeout_ms / 1000)
