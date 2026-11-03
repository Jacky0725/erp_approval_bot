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
from workbuddy_desktop import WorkBuddyDesktop, literal_keys


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


def test_unsent_batch_preflight_checks_changed_erp_record(tmp_path):
    from workbuddy_bridge import record_identity
    bridge = WorkBuddyBridge(tmp_path, {})
    record = {"试剂清单号": "SJ001", "_erp_record_id": "42", "试剂名称": "测试", "物化特性": "-", "_erp_parent_state": "wait"}
    state = {"items": [{"input": record_identity(record)}]}
    client = SimpleNamespace(fetch_reagent_detail=lambda number: [record])
    assert bridge.check_before_dispatch(state, client) == []
    record["物化特性"] = "普通类"
    assert "ERP 物化特性已为 普通类" in bridge.check_before_dispatch(state, client)[0]
    record["物化特性"] = "-"
    record["试剂名称"] = "不同试剂"
    assert "已变化" in bridge.check_before_dispatch(state, client)[0]


@pytest.mark.parametrize("submit_started", [False, True])
def test_dispatch_state_distinguishes_preflight_from_uncertain_submit(tmp_path, monkeypatch, submit_started):
    import json
    bot = bot_at(tmp_path)
    bridge = WorkBuddyBridge(tmp_path, bot.settings)
    state = {"batch_id": "abc", "rules_sha256": hashlib.sha256(b"rules").hexdigest(), "status": "waiting_result", "items": [{"input": {"试剂清单号": "SJ001"}}]}
    path = bridge.batch_path(state)
    path.mkdir(parents=True)
    (path / "prompt.txt").write_text("batch abc", encoding="utf-8")
    bridge.save(state)
    monkeypatch.setattr(WorkBuddyBridge, "check_before_dispatch", lambda self, state, client: [])
    def send(self, prompt, *, batch_id=None, before_submit):
        if submit_started:
            before_submit()
        raise RuntimeError("submit failed" if submit_started else "window not found")
    monkeypatch.setattr(WorkBuddyDesktop, "send", send)
    with pytest.raises(RuntimeError, match="submit failed|window not found"):
        bot.run_workbuddy_loop()
    saved = json.loads((path / "state.json").read_text(encoding="utf-8"))
    assert saved.get("dispatch_state", "") == ("sending" if submit_started else "")


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
    monkeypatch.setattr("workbuddy_desktop.WorkBuddyDesktop.wait_for_batch_completion", lambda self, batch_id, timeout: None)
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


@pytest.mark.parametrize("light", [False, True])
def test_dashboard_counts_confirmed_workbuddy_results(tmp_path, light):
    from web_runner import light_run_summary, run_summary
    lines = [
        "[workbuddy] 六氰酸钾 saved: ERP JSON-RPC write saved and verified.",
        "[workbuddy] 氯化亚锡 saved: ERP JSON-RPC write saved and verified.",
        "[workbuddy] 二硝基水杨酸 saved: ERP JSON-RPC write saved and verified.",
        "[workbuddy preview] 乙醇 -> 易燃类",
        "[workbuddy] other failed: ERP timeout",
        "[workbuddy] unresolved review: 请补充CAS",
    ]
    kwargs = dict(action="workbuddy_loop", options={}, running=False, success=True, error="")
    summary = light_run_summary(lines, **kwargs) if light else run_summary(lines, root_dir=tmp_path, **kwargs)
    assert summary["write_success_count"] == 3
    assert summary["api_write_success_count"] == 3
    assert summary["api_write_attempt_count"] == 3
    assert summary["write_failure_count"] == 1
    assert summary["manual_review_count"] == 1
    assert summary["suggestion_count"] == 5


def test_recovered_workbuddy_save_is_not_a_new_api_attempt():
    from web_runner import light_run_summary
    summary = light_run_summary(["[workbuddy] recovered saved: 上次写入结果已在 ERP 中确认。"], action="workbuddy_loop", options={}, running=False, success=True, error="")
    assert summary["write_success_count"] == 1
    assert summary["api_write_attempt_count"] == 0
