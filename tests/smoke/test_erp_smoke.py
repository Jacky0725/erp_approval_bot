from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]


pytestmark = pytest.mark.erp_smoke


def _missing_env() -> list[str]:
    required = ("ERP_URL", "ERP_USERNAME", "ERP_PASSWORD", "ERP_SMOKE_TARGET_LIST_NUMBER")
    return [name for name in required if not os.getenv(name, "").strip()]


def test_real_erp_dry_run_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = _missing_env()
    if missing:
        pytest.skip(f"Missing ERP smoke environment variables: {', '.join(missing)}")

    monkeypatch.setenv("APP_DRY_RUN", "true")
    monkeypatch.setenv("APPROVAL_WRITE_MODE", "disabled")
    monkeypatch.setenv("AUTO_PASS", "false")
    monkeypatch.setenv("TARGET_LIST_NUMBER", os.environ["ERP_SMOKE_TARGET_LIST_NUMBER"])
    monkeypatch.setenv("PROCESS_ALL_TODOS", "false")

    from browser_bot import BrowserBot

    smoke_dir = ROOT_DIR / "data" / "logs" / "smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    bot = BrowserBot()
    bot.root_dir = ROOT_DIR
    bot.settings.setdefault("app", {})["dry_run"] = True
    bot.settings.setdefault("approval", {})["write_mode"] = "disabled"
    bot.settings.setdefault("paths", {})["audit_log_dir"] = "data/logs/smoke"
    bot.target_list_number = os.environ["ERP_SMOKE_TARGET_LIST_NUMBER"]

    with bot.erp_session() as page:
        bot.enter_reagent_judgement_page(page)
        bot.open_task_detail_by_list_number(page, bot.target_list_number)
        bot.wait_for_reagent_table_ready(page)
        detail = bot.read_detail_info(page)
        sort_ok = bot.sort_property_column_until_unmatched_visible(page)
        unmatched = bot.current_page_unmatched_reagents(page)
        page.screenshot(path=str(smoke_dir / f"{stamp}_detail.png"), full_page=True)
        (smoke_dir / f"{stamp}_detail.html").write_text(page.content(), encoding="utf-8")

    summary = {
        "target_list_number": bot.target_list_number,
        "dry_run": bot.dry_run_enabled(),
        "approval_write_mode": bot.approval_write_mode(),
        "auto_pass": os.getenv("AUTO_PASS", ""),
        "detail": detail,
        "sort_ok": sort_ok,
        "unmatched_count": len(unmatched),
    }
    (smoke_dir / f"{stamp}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    assert summary["dry_run"] is True
    assert summary["approval_write_mode"] == "disabled"
    assert summary["auto_pass"] == "false"
    assert detail
