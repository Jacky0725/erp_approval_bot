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


def approval_records(number, size):
    return [{"试剂清单号": number, "_erp_record_id": str(i), "序号": str(i),
             "试剂名称": f"虚拟试剂 {i}", "物化特性": "-", "_erp_parent_state": "wait"}
            for i in range(1, size + 1)]


def test_unknown_identity_policy_is_in_new_batch_rule_snapshot(tmp_path):
    from openpyxl import load_workbook

    source = Path(__file__).resolve().parents[1] / "config" / "rules_structured.xlsx"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "rules_structured.xlsx").write_bytes(source.read_bytes())
    bridge = WorkBuddyBridge(tmp_path, {"workbuddy": {"batch_size": 1}})
    record = {"试剂清单号": "SJ001", "_erp_record_id": "26", "序号": "26", "试剂名称": "绿三角"}
    state = bridge.prepare([record], scope={"SJ001"})
    assert state is not None
    workbook = load_workbook(bridge.batch_path(state) / "rules.xlsx", read_only=True, data_only=True)
    try:
        row = next(row for row in workbook["rules"].values if row[0] == "BUS-UNK-IDENTITY-COMPOSITION-001")
        assert "品牌、系列名、内部称谓" in row[7]
        assert "不能唯一确认" in row[7]
        assert "可靠CAS" in row[7]
        assert "不得凭相似商品名称推断组成" in row[7]
    finally:
        workbook.close()


def test_legacy_review_only_batch_unblocks_remaining_rows_without_resending(tmp_path, monkeypatch):
    from openpyxl import load_workbook
    from reagent_memory import ReagentMemory

    bot = bot_at(tmp_path)
    bot.dry_run_enabled = lambda: False
    bot.approval_write_mode = lambda: "generate_library"
    bot.erp_write_backend = lambda: "api_write_with_web_verify"
    bot.settings["workbuddy"]["write_enabled"] = True
    bridge = WorkBuddyBridge(tmp_path, bot.settings)
    rows = approval_records("SJ001", 22)
    writes, libraries, dispatched = [], [], []

    def write(key, category, *_):
        writes.append(key)
        rows[int(key) - 1]["物化特性"] = category
        return SimpleNamespace(saved=True, verified=True, detail="ERP saved")

    def generate(key, *_):
        libraries.append(key)
        return {"status": "created_verified", "detail": "ERP verified"}

    client = SimpleNamespace(write_enabled=lambda: True, fetch_reagent_detail=lambda _: rows,
                             match_reagent_list=Mock(side_effect=AssertionError("held row must stay blank")),
                             save_physicochemical_property_by_id=write, generate_reagent_library_by_id=generate)
    monkeypatch.setattr(approval_flow, "ErpApiClient", lambda *_: client)

    def deliver(batch_id, *, review_last=False):
        path = bridge.directory / batch_id
        book = load_workbook(path / "result_template.xlsx")
        for row in book.active.iter_rows(min_row=2):
            row[3].value = "需人工复核" if review_last and row[0].row == book.active.max_row else "普通类"
            row[4].value = "模拟证据或资料不足"
        book.save(path / "result.xlsx")
        book.close()

    previous = bridge.prepare(rows)
    deliver(previous["batch_id"], review_last=True)
    bridge.process(previous, client, ReagentMemory(tmp_path), dry_run=False, generate_library=True)
    previous.update(status="needs_attention", dispatch_state="submitted")
    bridge.save(previous)
    writes.clear()
    libraries.clear()

    def send(_, prompt, *, batch_id, before_submit):
        before_submit()
        dispatched.append(batch_id)
        deliver(batch_id)

    monkeypatch.setattr(WorkBuddyDesktop, "send", send)
    monkeypatch.setattr(WorkBuddyDesktop, "wait_for_batch_completion", lambda *_: None)
    bot.run_workbuddy_loop()
    assert len(dispatched) == 1 and previous["batch_id"] not in dispatched
    assert writes == libraries == ["21", "22"]
    state = next(item for item in bridge.batches() if item["batch_id"] == previous["batch_id"])
    assert state["status"] == "review_pending"
    assert rows[19]["物化特性"] == "-"
    bot.run_workbuddy_loop()
    assert len(dispatched) == 1 and writes == libraries == ["21", "22"]


@pytest.mark.parametrize("old_status", ["waiting_result", "needs_attention", "operator_stopped"])
def test_other_list_is_not_blocked_by_old_batch(tmp_path, monkeypatch, old_status):
    bot = bot_at(tmp_path)
    bridge = WorkBuddyBridge(tmp_path, bot.settings)
    old = bridge.prepare(approval_records("SJ002", 1))
    old.update(status=old_status, generate_library=True)
    if old_status == "operator_stopped":
        old["operator_stop_task"] = "old-task"
    bridge.save(old)
    monkeypatch.setattr(approval_flow, "ErpApiClient", lambda *_: SimpleNamespace(
        fetch_reagent_detail=lambda number: approval_records(number, 1)
    ))
    bot.run_workbuddy_handoff()
    created = next(state for state in bridge.batches() if state["batch_id"] != old["batch_id"])
    assert created["items"][0]["input"]["试剂清单号"] == "SJ001"
    assert next(state for state in bridge.batches() if state["batch_id"] == old["batch_id"])["status"] == old_status


@pytest.mark.parametrize("stop", [False, True])
def test_selected_scope_does_not_bypass_partially_overlapping_batch(tmp_path, monkeypatch, stop):
    bot = bot_at(tmp_path)
    bridge = WorkBuddyBridge(tmp_path, bot.settings)
    old = bridge.prepare(approval_records("SJ001", 1) + approval_records("SJ002", 1))
    if stop:
        old.update(status="operator_stopped", operator_stop_task="old-task")
        bridge.save(old)
    read = Mock()
    monkeypatch.setattr(approval_flow, "ErpApiClient", lambda *_: SimpleNamespace(fetch_reagent_detail=read))
    with pytest.raises(RuntimeError, match="部分重合"):
        bot.run_workbuddy_loop()
    read.assert_not_called()


def test_reported_pending_reviews_have_distinct_outcome(tmp_path):
    from web_runner import light_run_summary, run_health, run_summary
    lines = ["[workbuddy review_pending] 1"]
    kwargs = dict(action="workbuddy_loop", options={}, running=False, success=True, error="")
    for summary in (light_run_summary(lines, **kwargs), run_summary(lines, root_dir=tmp_path, **kwargs)):
        assert summary["outcome"] == "自动处理完成，待人工复核 1 条"
        assert summary["manual_review_count"] == 1
        assert summary["write_failure_count"] == 0
    assert run_health(lines, True, "") == "warning"


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
    state = {"batch_id": "abc", "rules_sha256": hashlib.sha256(b"rules").hexdigest(), "status": "waiting_result", "dispatch_state": "submitted", "items": [{"input": {"试剂清单号": "SJ001"}}]}
    monkeypatch.setattr(WorkBuddyBridge, "batches", lambda self: [state])
    monkeypatch.setattr(approval_flow.ErpApiClient, "fetch_reagent_detail", lambda self, number: [])
    process = Mock()
    monkeypatch.setattr(WorkBuddyBridge, "process", process)
    monkeypatch.setattr(WorkBuddyBridge, "load_results", lambda self, state: state.update(status="imported", result_sha256="test"))
    bot.run_workbuddy_loop()
    assert process.call_count == 1
    assert process.call_args.kwargs["dry_run"] is True


@pytest.mark.parametrize("outcome,valid,submitted,changing,accepted", [
    ("unknown", True, True, False, True),
    ("completed", True, True, False, True),
    ("failed", True, True, False, False),
    ("unknown", False, True, False, False),
    ("unknown", True, False, False, False),
    ("unknown", True, True, True, False),
])
def test_existing_result_uses_file_validation_without_ui_marker(tmp_path, monkeypatch, outcome, valid, submitted, changing, accepted):
    from openpyxl import load_workbook

    bot = bot_at(tmp_path)
    bridge = WorkBuddyBridge(tmp_path, bot.settings)
    state = bridge.prepare([{"试剂清单号": "SJ001", "_erp_record_id": "1", "试剂名称": "测试试剂",
                             "CAS号": "", "序号": "1", "物化特性": "-", "_erp_parent_state": "wait"}])
    if submitted:
        state["dispatch_state"] = "submitted"
    if outcome != "unknown":
        state["workbuddy_request"] = {"session": "test", "request": "test", "transcript": "test"}
    bridge.save(state)
    path = bridge.batch_path(state)
    book = load_workbook(path / "result_template.xlsx")
    book.active.cell(2, 4, "普通类")
    book.active.cell(2, 5, "模拟判定依据" if valid else None)
    book.save(path / "result.xlsx")
    book.close()
    if changing:
        def change_result(_):
            with (path / "result.xlsx").open("ab") as stream:
                stream.write(b"changed")
        monkeypatch.setattr("time.sleep", change_result)

    monkeypatch.setattr("workbuddy_recovery.dispatch_outcome", lambda *args: outcome)
    monkeypatch.setattr(WorkBuddyDesktop, "wait_for_batch_completion",
                        lambda *args: pytest.fail("file-based completion must not wait for UI marker"))
    if accepted:
        bot.run_workbuddy_loop()
        assert bridge.batches()[0]["result_sha256"]
    else:
        process = Mock()
        monkeypatch.setattr(WorkBuddyBridge, "process", process)
        with pytest.raises((RuntimeError, ValueError)):
            bot.run_workbuddy_loop()
        assert process.call_count == 0
        assert bridge.batches()[0]["status"] == "waiting_result"


def test_dingtalk_loop_freezes_scope_across_batches(tmp_path, monkeypatch):
    bot = bot_at(tmp_path)
    bot.target_list_numbers = []
    bot.settings["workbuddy"]["write_enabled"] = True
    bot.dry_run_enabled = lambda: False
    bot.approval_write_mode = lambda: "multi_page"
    bot.erp_write_backend = lambda: "api_write_with_web_verify"
    monkeypatch.setenv("PROCESS_ALL_TODOS", "true")
    monkeypatch.setenv("FREEZE_TODO_SCOPE", "true")
    fetch = Mock(side_effect=[[{"name": "SJ001"}], [{"name": "SJ002"}]])
    monkeypatch.setattr(ErpApiClient, "fetch_todo_tasks", lambda self, **kw: fetch(**kw))
    monkeypatch.setattr(ErpApiClient, "write_enabled", lambda self: True)
    monkeypatch.setattr(WorkBuddyBridge, "batches", lambda self: [])
    monkeypatch.setattr(WorkBuddyBridge, "matched_records", lambda self, client, number, **kw: [{"试剂名称": number}])
    path = tmp_path / "data/workbuddy/abc"
    path.mkdir(parents=True)
    (path / "result.xlsx").write_bytes(b"fake")
    state = {"batch_id": "abc", "rules_sha256": hashlib.sha256(b"rules").hexdigest(), "status": "waiting_result", "dispatch_state": "submitted",
        "items": [{"input": {"试剂清单号": "SJ001"}}]}
    scopes = []
    def prepare(self, records, *, compact=False, scope=None):
        assert scope == {"SJ001"} and not compact
        scopes.append([r["试剂清单号"] for r in records])
        return state if len(scopes) == 1 else None
    monkeypatch.setattr(WorkBuddyBridge, "prepare", prepare)
    monkeypatch.setattr(WorkBuddyBridge, "reconcile_unsent_batch", lambda *a: False)
    monkeypatch.setattr(WorkBuddyBridge, "process", lambda *a, **kw: None)
    monkeypatch.setattr(WorkBuddyBridge, "load_results", lambda self, state: state.update(status="imported", result_sha256="test"))
    monkeypatch.setattr(WorkBuddyDesktop, "wait_for_batch_completion", lambda *a: None)
    bot.run_workbuddy_loop()
    assert fetch.call_count == 1
    assert scopes == [["SJ001"], ["SJ001"]]


@pytest.mark.parametrize("reuse_enabled,expected_followups", [(True, [False, True, True, False]), (False, [False] * 4)])
def test_workbuddy_four_batches_with_real_bridge_and_fake_erp(tmp_path, monkeypatch, reuse_enabled, expected_followups):
    import shutil
    from openpyxl import load_workbook

    bot = bot_at(tmp_path)
    bot.target_list_numbers = []
    bot.settings["workbuddy"].update(write_enabled=True, batch_size=20, reuse_chat=reuse_enabled, compact_rules=True)
    bot.dry_run_enabled = lambda: False
    bot.approval_write_mode = lambda: "generate_library"
    bot.erp_write_backend = lambda: "api_write_with_web_verify"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "config/rules_structured.xlsx", tmp_path / "config/rules_structured.xlsx")
    bridge = WorkBuddyBridge(tmp_path, bot.settings)
    bridge.publish_compact_rules()
    records = [
        {"试剂清单号": "SJ001", "_erp_record_id": str(i), "试剂名称": f"虚拟试剂 {i}",
         "物化特性": "-", "_erp_parent_state": "wait"}
        for i in range(61)
    ]
    fetch = Mock(side_effect=[[{"name": "SJ001"}], [{"name": "SJ002"}]])
    match = Mock(return_value={"changes": [], "records": records})
    saves, generated, sent = [], [], []

    def save(record_id, category, identity, before):
        records[int(record_id)]["物化特性"] = category
        saves.append(record_id)
        return SimpleNamespace(saved=True, verified=True, detail="模拟回读成功")

    def generate(record_id, identity, category):
        generated.append(record_id)
        return {"status": "created_verified", "detail": "模拟入库成功"}

    def send(self, prompt, *, batch_id, before_submit, reuse):
        assert "rules_compact/manifest.json" in prompt
        if reuse:
            assert reuse[0] == sent[-1][0] and "不全量重读" in reuse[1]
        before_submit()
        sent.append((batch_id, bool(reuse)))
        path = bridge.directory / batch_id
        book = load_workbook(path / "result_template.xlsx")
        for row in book.active.iter_rows(min_row=2):
            row[3].value = "普通类"
            row[4].value = "模拟判定"
        book.save(path / "result.xlsx")
        book.close()
        return bool(reuse)

    monkeypatch.setenv("PROCESS_ALL_TODOS", "true")
    monkeypatch.setenv("FREEZE_TODO_SCOPE", "true")
    monkeypatch.setattr(approval_flow, "ErpApiClient", lambda *_: SimpleNamespace(
        write_enabled=lambda: True, fetch_todo_tasks=fetch, match_reagent_list=match,
        fetch_reagent_detail=lambda number: records if number == "SJ001" else [],
        save_physicochemical_property_by_id=save, generate_reagent_library_by_id=generate,
    ))
    monkeypatch.setattr(WorkBuddyDesktop, "send", send)
    monkeypatch.setattr(WorkBuddyDesktop, "wait_for_batch_completion", lambda *a: None)
    bot.run_workbuddy_loop()
    assert [reuse for _, reuse in sent] == expected_followups
    fetch.assert_called_once_with(all_pages=True)
    match.assert_called_once_with("SJ001")
    assert saves == generated == [str(i) for i in range(61)]
    assert sorted(len(state["items"]) for state in bridge.batches()) == [1, 20, 20, 20]
    assert all(state["status"] == "completed" and state["cleaned"] for state in bridge.batches())


def test_dingtalk_v1_all_scope_is_fixed_and_restored(tmp_path, monkeypatch):
    bot = bot_at(tmp_path)
    bot.target_list_numbers = []
    monkeypatch.setenv("FREEZE_TODO_SCOPE", "true")
    bot.enter_reagent_judgement_page = Mock()
    bot.read_all_todo_tasks = Mock(return_value=[{"试剂清单号": "SJ001"}, {"试剂清单号": "SJ002"}])
    bot.filter_scheduled_todo_list_numbers = lambda numbers: numbers
    scopes = []
    bot.generate_selected_todo_approval_suggestions = lambda page: scopes.append(list(bot.target_list_numbers))
    bot.generate_all_todo_approval_suggestions(object())
    assert scopes == [["SJ001", "SJ002"]]
    assert bot.read_all_todo_tasks.call_count == 1
    assert bot.target_list_numbers == []


def test_dingtalk_v1_fixed_scope_does_not_truncate_at_default_limit(tmp_path, monkeypatch):
    bot = bot_at(tmp_path)
    bot.target_list_numbers = [f"SJ{i:03d}" for i in range(51)]
    monkeypatch.setenv("FREEZE_TODO_SCOPE", "true")
    monkeypatch.setenv("PROCESS_ALL_TODOS_MAX", "50")
    bot.enter_reagent_judgement_page = Mock()
    bot.read_all_todo_tasks = lambda page: [{"试剂清单号": number} for number in bot.target_list_numbers]
    processed = []
    bot.generate_approval_suggestions = lambda page: processed.append(bot.target_list_number)
    bot.generate_selected_todo_approval_suggestions(object())
    assert processed == bot.target_list_numbers


def test_dingtalk_options_preserve_write_mode_and_validate_workbuddy(tmp_path, monkeypatch):
    import web_app
    executable = tmp_path / "WorkBuddy.exe"
    executable.write_bytes(b"fake")
    settings = {"app": {"dry_run": False}, "workbuddy": {"desktop_enabled": True, "write_enabled": True, "executable": str(executable)},
        "erp_api": {"enabled_for_write": True, "discovery": {"status": "verified"}}}
    monkeypatch.delenv("APP_DRY_RUN", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setattr(web_app, "load_settings", lambda: settings)
    monkeypatch.setattr(web_app, "runtime_config_snapshot", lambda: {"approval_write_mode": "generate_library", "erp_write_backend": "api_write_with_web_verify"})
    options = web_app.dingtalk_run_options()
    assert options["FREEZE_TODO_SCOPE"] == "true"
    assert options["APPROVAL_WRITE_MODE"] == "generate_library"
    assert options["DINGTALK_TRIAL_MODE"] == "false"
    assert web_app.manager._env_overrides(options)["FREEZE_TODO_SCOPE"] == "true"
    assert web_app.manager._env_overrides(options)["ERP_WRITE_BACKEND"] == "api_write_with_web_verify"
    settings["workbuddy"]["desktop_enabled"] = False
    with pytest.raises(ValueError, match="未启用"):
        web_app.dingtalk_run_options()
    assert web_app.dingtalk_run_options(pipeline="v1")["PROCESS_ALL_TODOS"] == "true"
    settings["workbuddy"]["desktop_enabled"] = True
    settings["erp_api"]["enabled_for_write"] = False
    with pytest.raises(ValueError, match="ERP API"):
        web_app.dingtalk_run_options()
    monkeypatch.setenv("APP_DRY_RUN", "true")
    assert web_app.dingtalk_run_options()["DINGTALK_TRIAL_MODE"] == "true"


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


@pytest.mark.parametrize("enabled,action,selected,expected", [
    (True, "workbuddy_loop", "", True),
    (True, "workbuddy_loop", "SJ001 加急", False),
    (True, "suggestions", "", False),
    (False, "workbuddy_loop", "", False),
])
def test_web_workbuddy_scope_flag_is_opt_in(monkeypatch, enabled, action, selected, expected):
    from fastapi.testclient import TestClient
    import web_app
    start = Mock(return_value={"started": True})
    monkeypatch.setattr(web_app.manager, "start", start)
    monkeypatch.setattr(web_app, "load_settings", lambda: {"workbuddy": {"freeze_web_todo_scope": enabled}})
    response = TestClient(web_app.app).post("/api/run", data={
        "action": action, "target_list_numbers": selected, "process_all_todos": "true",
    })
    assert response.status_code == 200
    options = start.call_args.args[1]
    assert (options.get("FREEZE_TODO_SCOPE") == "true") is expected
    assert options["TARGET_LIST_NUMBERS"] == selected.removesuffix(" 加急")


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
