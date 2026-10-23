"""Re-evaluate the quarantined 2026-10-19 Unknown writes by ERP stable ID.

Dry preview is the default. ``--apply`` changes only rows that still exactly
match the prior Unknown write, then verifies ERP and updates reagent memory.
No CAS field is written.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime

import pandas as pd
import yaml
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from browser_bot import BrowserBot  # noqa: E402
from erp_api_client import ErpApiClient  # noqa: E402
from rule_engine import RuleEngine  # noqa: E402
from category_mapper import to_erp_property  # noqa: E402


def _text(value) -> str:
    if pd.isna(value):
        return ""
    return str(value or "").strip()


def run(*, apply: bool, sync_memory: bool = False) -> list[dict]:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    load_dotenv(ROOT / ".env")
    bot = BrowserBot(settings=settings, root_dir=ROOT)
    engine = RuleEngine.from_structured_excel(ROOT / "config" / "rules_structured.xlsx")
    audit = pd.read_excel(ROOT / "outputs" / "unknown_category_audit_20261019.xlsx", engine="openpyxl")
    partial = pd.read_excel(ROOT / "data" / "logs" / "approval_suggestions_partial.xlsx", engine="openpyxl")
    indexed = {(_text(row["试剂清单号"]), _text(row["序号"])): row for _, row in partial.iterrows()}
    requested = []
    for _, row in audit.iterrows():
        list_number, sequence, name = _text(row["清单号"]), _text(row["序号"]), _text(row["试剂名称"])
        if _text(row["ERP记忆库状态"]) != "已核验写入未知类":
            continue
        previous = indexed.get((list_number, sequence))
        if previous is None or _text(previous["试剂名称"]) != name:
            continue
        reagent = {"试剂名称": name, "CAS号": _text(row["ERP CAS"]), "序号": sequence}
        name_result = {
            "standard_name": _text(previous.get("标准化名称")),
            "cleaned_name": _text(previous.get("清洗后名称")),
            "suspected_invalid_name": bool(previous.get("疑似错误名称", False)),
        }
        search = {"source": "", "identity_status": "unresolved", "need_manual_review": True,
                  "relevance_passed": False, "evidence_quality": "none"}
        classification, source = bot.v1_classify_without_web_evidence(
            reagent, name_result, search, {"final_category": "", "need_manual_review": True}, engine,
        )
        category = str(classification.get("final_category") or "").strip()
        if category and not classification.get("need_manual_review"):
            requested.append((list_number, sequence, name, reagent, name_result, source, classification))

    if not requested:
        return []
    if not all(os.getenv(key) for key in ("ERP_URL", "ERP_USERNAME", "ERP_PASSWORD")):
        raise RuntimeError("ERP login environment is unavailable; no writes attempted")

    events: list[dict] = []
    if sync_memory:
        source_db = ROOT / "data" / "reagent_memory.sqlite"
        backup_db = ROOT / "data" / "reagent_memory.before_live_erp_sync_20261019.sqlite"
        with sqlite3.connect(source_db) as source_connection, sqlite3.connect(backup_db) as backup_connection:
            source_connection.backup(backup_connection)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=bot.effective_browser_headless(settings.get("browser") or {}))
        try:
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            page.set_default_timeout(int((settings.get("browser") or {}).get("timeout_ms", 30000)))
            runtime_headers: dict[str, str] = {}
            runtime_rpc_context: dict[str, object] = {}

            def capture(request) -> None:
                session = str(request.headers.get("x-openerp-session-id") or "").strip()
                if session:
                    runtime_headers["x-openerp-session-id"] = session
                try:
                    params = request.post_data_json.get("params") or {}
                    rpc_context = (params.get("kwargs") or {}).get("context") or {}
                except (AttributeError, TypeError, ValueError):
                    return
                for field in ("lang", "tz", "uid"):
                    if field in rpc_context:
                        runtime_rpc_context[field] = rpc_context[field]

            page.on("request", capture)
            setattr(page, "_erp_runtime_headers", runtime_headers)
            setattr(page, "_erp_runtime_rpc_context", runtime_rpc_context)
            bot.open_login_page(page, os.environ["ERP_URL"])
            bot.login(page, os.environ["ERP_USERNAME"], os.environ["ERP_PASSWORD"], ROOT / "data" / "logs")
            bot.wait_for_app_shell(page)
            api = ErpApiClient(page, settings)
            if not api.write_enabled():
                raise RuntimeError("ERP API write is not verified/enabled; no writes attempted")
            for list_number in sorted({item[0] for item in requested}):
                records = api.fetch_reagent_detail(list_number)
                for item in (value for value in requested if value[0] == list_number):
                    _, sequence, name, reagent, name_result, search, decision = item
                    matches = [record for record in records if _text(record.get("序号") or record.get("sequence")) == sequence
                               and _text(record.get("试剂名称") or record.get("name")) == name]
                    event = {"at": datetime.now().isoformat(), "list_number": list_number,
                             "sequence": sequence, "name": name, "new_category": decision["final_category"],
                             "rule_ids": decision.get("matched_rule_ids") or [], "rule_version": engine.rule_version,
                             "policy": search.get("v1_no_web_evidence_policy"), "applied": False}
                    if len(matches) != 1:
                        event["status"] = "identity_not_unique_or_missing"
                        events.append(event)
                        continue
                    record = matches[0]
                    record_id = api.record_id_from_record(record)
                    event["record_id"] = record_id
                    current = _text(record.get("物化特性") or record.get("phchproperty_name"))
                    event["previous_category"] = current
                    if current != "未知类":
                        event["status"] = "changed_since_prior_run"
                        if sync_memory and current in (settings.get("reagent", {}).get("physicochemical_property_options") or []):
                            # Never perpetuate an Ordinary memory value when
                            # the active local rule finds a stronger hazard.
                            if current == "普通类" and decision["final_category"] != "普通类":
                                event["status"] = "memory_sync_blocked_by_local_hazard"
                            else:
                                identity = {"试剂清单号": list_number, "序号": sequence,
                                            "试剂名称": name, "CAS号": _text(record.get("CAS号") or record.get("cas_code"))}
                                if api.confirm_property_twice_by_id(record_id, current, identity):
                                    memory_decision = dict(decision)
                                    memory_decision.update({"final_category": current, "matched_categories": [current],
                                                            "matched_rule_ids": [] if current != decision["final_category"]
                                                            else decision.get("matched_rule_ids") or [],
                                                            "reason": (decision.get("reason") if current == decision["final_category"]
                                                                       else "ERP 当前类别经稳定记录 ID 双次回读；优先复用已核验的本地审批结果。")})
                                    suggestion = bot._approval_suggestion_row(reagent, name_result, search, {}, memory_decision)
                                    suggestion.update(identity)
                                    suggestion["_erp_record_id"] = record_id
                                    event["memory_reusable"] = bot.remember_verified_approval_suggestion(suggestion, current)
                                    event["status"] = "memory_synced_from_live_erp"
                                else:
                                    event["status"] = "memory_sync_readback_failed"
                        events.append(event)
                        continue
                    erp_category = to_erp_property(decision["final_category"], settings)
                    if not erp_category:
                        event["status"] = "unmapped_category"
                        events.append(event)
                        continue
                    event["status"] = "eligible_preview"
                    if apply:
                        identity = {"试剂清单号": list_number, "序号": sequence, "试剂名称": name,
                                    "CAS号": _text(record.get("CAS号") or record.get("cas_code")),
                                    "最终建议类别": decision["final_category"]}
                        result = api.save_physicochemical_property_by_id(record_id, erp_category, identity, record)
                        if result.saved and result.verified and api.confirm_property_twice_by_id(record_id, erp_category, identity):
                            suggestion = bot._approval_suggestion_row(reagent, name_result, search, {}, decision)
                            suggestion.update(identity)
                            suggestion["_erp_record_id"] = record_id
                            stored = bot.remember_verified_approval_suggestion(suggestion, erp_category)
                            event.update({"status": "erp_verified", "applied": True, "memory_reusable": stored})
                        else:
                            event.update({"status": "write_or_verify_failed", "detail": result.detail})
                    events.append(event)
        finally:
            browser.close()
    suffix = "memory_sync" if sync_memory else "apply" if apply else "preview"
    output = ROOT / "outputs" / f"unknown_reclassification_{suffix}_20261019.json"
    output.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    return events


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sync-memory", action="store_true")
    args = parser.parse_args()
    results = run(apply=args.apply, sync_memory=args.sync_memory)
    print(json.dumps({"eligible": sum(event.get("status") in {"eligible_preview", "erp_verified"} for event in results),
                      "verified": sum(event.get("status") == "erp_verified" for event in results),
                      "memory_synced": sum(event.get("status") == "memory_synced_from_live_erp" for event in results),
                      "skipped": len(results) - sum(event.get("status") in {"eligible_preview", "erp_verified"} for event in results)},
                     ensure_ascii=False))
