from pathlib import Path
import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import approval_flow
from browser_bot import BrowserBot
from erp_api_client import ErpApiClient
from workbuddy_bridge import WorkBuddyBridge
from workbuddy_desktop import literal_keys


def bot_at(tmp_path):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/rules_structured.xlsx").write_bytes(b"rules")
    bot = BrowserBot(settings={"app": {"dry_run": True}, "workbuddy": {"desktop_enabled": True}}, root_dir=tmp_path)
    bot.target_list_numbers = ["SJ001"]
    bot.run_after_login_capture = lambda screenshot, html, callback: callback(object())
    return bot


def test_handoff_import_respects_extra_write_gate(tmp_path):
    bot = bot_at(tmp_path)
    bot.dry_run_enabled = lambda: False
    bot.approval_write_mode = lambda: "multi_page"
    with pytest.raises(RuntimeError, match="正式写入尚未启用"):
        bot.run_workbuddy_handoff(import_results=True)


def test_import_requires_explicit_list_scope(tmp_path, monkeypatch):
    bot = bot_at(tmp_path)
    bot.target_list_numbers = []
    monkeypatch.delenv("PROCESS_ALL_TODOS", raising=False)
    with pytest.raises(RuntimeError, match="选择待办清单"):
        bot.run_workbuddy_handoff(import_results=True)


def test_import_selected_scope_does_not_write_mixed_batch(tmp_path, monkeypatch):
    bot = bot_at(tmp_path)
    state = {"batch_id": "abc", "status": "imported", "items": [{"input": {"试剂清单号": "SJ001"}}, {"input": {"试剂清单号": "OTHER"}}]}
    monkeypatch.setattr(WorkBuddyBridge, "batches", lambda self: [state])
    process = Mock()
    monkeypatch.setattr(WorkBuddyBridge, "process", process)
    bot.run_workbuddy_handoff(import_results=True)
    process.assert_not_called()


def test_unknown_dispatch_is_never_resent(tmp_path, monkeypatch):
    bot = bot_at(tmp_path)
    state = {"batch_id": "abc", "rules_sha256": hashlib.sha256(b"rules").hexdigest(), "status": "waiting_result", "dispatch_state": "sending", "items": [{"input": {"试剂清单号": "SJ001"}}]}
    monkeypatch.setattr(WorkBuddyBridge, "batches", lambda self: [state])
    monkeypatch.setattr(approval_flow.ErpApiClient, "fetch_reagent_detail", lambda self, number: [])
    with pytest.raises(RuntimeError, match="投递结果不明"):
        bot.run_workbuddy_loop()


def test_keyboard_escapes_shortcut_characters_and_not_unicode():
    assert literal_keys("中文+^%~{}()") == "中文{+}{^}{%}{~}{{}{}}{(}{)}"


def test_fetch_all_todos_is_paginated_without_daily_limit(monkeypatch):
    client = ErpApiClient(object(), {"erp_api": {"protocol": "odoo_jsonrpc", "rpc": {"todo_limit": 2}}})
    offsets = []
    def fetch(model, domain, fields, **kwargs):
        offsets.append(kwargs["offset"])
        start = kwargs["offset"]
        return [{"id": i, "name": f"SJ{i}"} for i in range(start + 1, min(start + 3, 6))]
    monkeypatch.setattr(client, "_rpc_search_read", fetch)
    assert len(client.fetch_todo_tasks(all_pages=True)) == 5
    assert offsets == [0, 2, 4]


def test_loop_test_mode_stops_after_one_batch_without_writes(tmp_path, monkeypatch):
    monkeypatch.setattr("workbuddy_desktop.WorkBuddyDesktop.wait_until_idle", lambda self, timeout: None)
    bot = bot_at(tmp_path)
    path = tmp_path / "data" / "workbuddy" / "abc"
    path.mkdir(parents=True)
    (path / "result.xlsx").write_bytes(b"existing output")
    state = {"batch_id": "abc", "rules_sha256": hashlib.sha256(b"rules").hexdigest(), "status": "waiting_result", "items": [{"input": {"试剂清单号": "SJ001"}}]}
    monkeypatch.setattr(WorkBuddyBridge, "batches", lambda self: [state])
    monkeypatch.setattr(approval_flow.ErpApiClient, "fetch_reagent_detail", lambda self, number: [])
    process = Mock()
    monkeypatch.setattr(WorkBuddyBridge, "process", process)
    bot.run_workbuddy_loop()
    assert process.call_count == 1
    assert process.call_args.kwargs["dry_run"] is True


def test_web_actions_are_available_and_pass_through_manager(monkeypatch):
    from fastapi.testclient import TestClient
    import web_app
    start = Mock(return_value={"started": True})
    monkeypatch.setattr(web_app.manager, "start", start)
    client = TestClient(web_app.app)
    for action in ("workbuddy_prepare", "workbuddy_import", "workbuddy_loop"):
        response = client.post("/api/run", data={"action": action, "target_list_numbers": "SJ001"})
        assert response.status_code == 200
        assert response.json()["started"]
        assert start.call_args.args[0] == action


def test_partial_workbuddy_result_is_a_dashboard_warning():
    from web_runner import run_health
    assert run_health(["[workbuddy attention] 有未完成记录"], True, "") == "warning"
