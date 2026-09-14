from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
import yaml


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

    settings = yaml.safe_load((ROOT_DIR / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    settings.setdefault("app", {})["dry_run"] = True
    settings.setdefault("approval", {})["write_mode"] = "disabled"
    settings.setdefault("paths", {})["audit_log_dir"] = "data/logs/smoke"
    bot = BrowserBot(settings=settings, root_dir=ROOT_DIR)
    bot.target_list_number = os.environ["ERP_SMOKE_TARGET_LIST_NUMBER"]

    result: dict[str, object] = {}

    def inspect_detail(page) -> None:
        bot.enter_reagent_judgement_page(page)
        assert bot.open_task_detail_by_list_number(page, bot.target_list_number)
        bot.wait_for_reagent_table_ready(page)
        result["detail"] = bot.read_detail_info(page)
        result["sort_ok"] = bot.sort_property_column_until_unmatched_visible(page)
        result["unmatched"] = bot.current_page_unmatched_reagents(page)
        page.screenshot(path=str(smoke_dir / f"{stamp}_detail.png"), full_page=True)
        (smoke_dir / f"{stamp}_detail.html").write_text(page.content(), encoding="utf-8")

    bot.run_after_login_capture(
        f"{stamp}_final.png",
        f"{stamp}_final.html",
        inspect_detail,
    )

    summary = {
        "target_list_number": bot.target_list_number,
        "dry_run": bot.dry_run_enabled(),
        "approval_write_mode": bot.approval_write_mode(),
        "auto_pass": os.getenv("AUTO_PASS", ""),
        "detail": result.get("detail") or {},
        "sort_ok": bool(result.get("sort_ok")),
        "unmatched_count": len(result.get("unmatched") or []),
    }
    (smoke_dir / f"{stamp}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    assert summary["dry_run"] is True
    assert summary["approval_write_mode"] == "disabled"
    assert summary["auto_pass"] == "false"
    assert summary["detail"]
