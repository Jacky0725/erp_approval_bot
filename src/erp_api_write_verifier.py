"""ERP API write verification and webpage sampling helpers."""
from __future__ import annotations

from typing import Any

from playwright.sync_api import Error, Page

from ui_waits import wait_until_spinner_hidden


def should_verify_api_page(row_index: int, is_canary: bool, api_settings: dict[str, Any]) -> bool:
    if is_canary:
        return True
    if not bool(api_settings.get("web_verify_after_api_write", True)):
        return False
    try:
        sample_every = max(1, int(api_settings.get("web_verify_sample_every", 1)))
    except (TypeError, ValueError):
        sample_every = 1
    return row_index % sample_every == 0


def sample_verified_api_write_on_page(
    bot: Any,
    page: Page,
    writer: Any,
    *,
    suggestion: dict[str, Any],
    sequence: str,
    category: str,
    row_index: int,
    is_canary: bool,
    api_settings: dict[str, Any],
) -> tuple[bool, bool]:
    """Optionally sample the live ERP page after stable-ID API confirmation.

    Returns ``(page_check_ok, warning_emitted)``.
    """
    if not should_verify_api_page(row_index, is_canary, api_settings):
        return True, False
    try:
        page.reload(wait_until="domcontentloaded")
        wait_until_spinner_hidden(page, timeout_ms=5000)
    except Error as error:
        print(f"[api_property_verify] sequence={sequence} reload_failed={error}")
    page_value = bot.read_reagent_property_by_sequence(page, sequence)
    if not bot.property_value_matches(page_value, category, writer):
        list_number = str(suggestion.get("试剂清单号") or bot.current_detail_list_number() or "").strip()
        if list_number:
            try:
                if bot.reopen_target_detail_for_recovery(page, list_number):
                    page_value = bot.read_reagent_property_by_sequence(page, sequence)
            except Exception as error:
                print(f"[api_property_verify] sequence={sequence} detail_reopen_failed={error}")
    page_check_ok = bot.property_value_matches(page_value, category, writer)
    warned = False
    if not page_check_ok:
        warning = "stable_id_confirmed_page_mismatch" if page_value else "stable_id_confirmed_page_not_located"
        print(
            f"[api_property_verify] sequence={sequence} warning={warning} "
            f"page_value={page_value or '<not located>'}; no duplicate save"
        )
        warned = True
    if page_value:
        print(f"[api_property_verify] sequence={sequence} webpage_value={page_value}")
    return page_check_ok, warned


def reconcile_api_canary_result(
    api_configurator: Any,
    settings: dict[str, Any],
    api_client: Any,
    *,
    page_check_ok: bool,
) -> bool:
    """Activate or reject a pending API canary based on webpage read-back."""
    if not page_check_ok:
        api_configurator.reject("API canary readback passed, but webpage readback could not be confirmed")
        print("ERP API canary was not activated; subsequent writes use webpage fallback.")
        return False
    api_configurator.activate()
    erp_api_settings = settings.setdefault("erp_api", {})
    erp_api_settings["enabled_for_write"] = True
    erp_api_settings.setdefault("discovery", {})["status"] = "verified"
    if api_client is not None:
        api_client.api_settings["enabled_for_write"] = True
    print("ERP API canary passed API and webpage read-back checks; API writes are now active.")
    return True
