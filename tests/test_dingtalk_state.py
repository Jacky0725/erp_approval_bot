from __future__ import annotations

import re
import sys
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dingtalk_state import DingTalkState
from dingtalk_stream_bot import DingTalkCommandService
from dingtalk_notifier import (build_task_result_message, default_notification_binding, load_stream_binding,
                               save_stream_binding, send_task_result_notification)


def message(text="执行审批", *, group="group-a", message_id="msg-a", kind="2"):
    return {"conversationId": group, "openConversationId": group, "senderStaffId": "user-a", "msgId": message_id,
            "conversationType": kind, "sessionWebhook": f"https://example.test/{group}", "robotCode": "robot-a", "text": {"content": text}}


def service_at(root):
    manager = SimpleNamespace(start=Mock(return_value={"started": True}))
    service = DingTalkCommandService(job_manager=manager, run_options_builder=lambda number, pipeline: {},
        status_provider=lambda: {"running": False}, root_dir=root,
        settings_loader=lambda: {"dingtalk_chat": {"enabled": True}},
        intent_resolver=lambda *args: {"intent": "run", "pipeline": "workbuddy", "list_number": ""})
    return service, manager


def test_duplicate_delivery_survives_service_restart(tmp_path):
    service, manager = service_at(tmp_path)
    assert "已启动" in service.handle_message(message())
    replacement, next_manager = service_at(tmp_path)
    assert "不会重复执行" in replacement.handle_message(message())
    assert manager.start.call_count == 1
    next_manager.start.assert_not_called()


def test_ambiguous_admission_is_not_replayed(tmp_path):
    state = DingTalkState(tmp_path)
    assert state.claim("group-a", "msg-a", effectful=True)
    service, manager = service_at(tmp_path)
    assert "不会重复执行" in service.handle_message(message())
    manager.start.assert_not_called()


def test_claim_is_visible_to_another_python_process(tmp_path):
    assert DingTalkState(tmp_path).claim("g", "msg", effectful=True)
    code = "import sys; sys.path.insert(0, sys.argv[1]); from dingtalk_state import DingTalkState; print(DingTalkState(sys.argv[2]).claim('g', 'msg', effectful=True))"
    result = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parents[1] / "src"), str(tmp_path)],
                            check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "False"


def test_concurrent_services_only_admit_one_delivery(tmp_path):
    first, first_manager = service_at(tmp_path)
    second, second_manager = service_at(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda service: service.handle_message(message()), [first, second]))
    assert first_manager.start.call_count + second_manager.start.call_count == 1
    assert sum("已启动" in result for result in results) == 1


def test_new_message_id_is_a_deliberate_new_request(tmp_path):
    service, manager = service_at(tmp_path)
    service.handle_message(message())
    service.handle_message(message(message_id="msg-b"))
    assert manager.start.call_count == 2


@pytest.mark.parametrize("missing", ["msgId", "conversationId"])
def test_effectful_message_without_identity_fails_closed(tmp_path, missing):
    service, manager = service_at(tmp_path)
    data = message()
    data.pop(missing)
    assert "缺少可靠标识" in service.handle_message(data)
    manager.start.assert_not_called()


def test_storage_failure_does_not_start_approval(tmp_path, monkeypatch):
    service, manager = service_at(tmp_path)
    monkeypatch.setattr(service.state, "claim", Mock(side_effect=sqlite3.OperationalError("locked")))
    assert "本次未执行" in service.handle_message(message())
    manager.start.assert_not_called()
    assert "当前任务状态" in service.handle_message(message("状态", message_id="status-msg"))


def test_effectful_records_are_retained_but_old_chat_records_expire(tmp_path, monkeypatch):
    import dingtalk_state
    now = [100.0]
    monkeypatch.setattr(dingtalk_state.time, "time", lambda: now[0])
    state = DingTalkState(tmp_path)
    assert state.claim("g", "run", effectful=True)
    assert state.claim("g", "chat", effectful=False)
    now[0] += 31 * 86400
    assert not state.claim("g", "run", effectful=True)
    assert state.claim("g", "chat", effectful=False)


def test_group_queries_and_private_messages_do_not_rebind_default(tmp_path):
    save_stream_binding({"openConversationId": "default", "sessionWebhook": "https://example.test/default"}, tmp_path)
    service, manager = service_at(tmp_path)
    service.handle_message(message())
    service.handle_message(message("状态", group="group-b", message_id="msg-b"))
    service.handle_message(message("帮助", group="private", message_id="msg-c", kind="1"))
    assert load_stream_binding(tmp_path)["openConversationId"] == "default"
    assert manager.start.call_args.kwargs["notification_target"]["openConversationId"] == "group-a"


def test_confirmation_uses_origin_group_and_refreshed_reply_url(tmp_path):
    service, manager = service_at(tmp_path)
    response = service.handle_message(message("请处理当前所有待办"))
    code = re.search(r"确认 ([0-9A-F]{6})", response).group(1)
    service.handle_message(message("状态", group="group-b", message_id="msg-b"))
    confirmation = message(f"确认 {code}", message_id="msg-confirm")
    confirmation["sessionWebhook"] = "https://example.test/refreshed"
    service.handle_message(confirmation)
    target = manager.start.call_args.kwargs["notification_target"]
    assert target["openConversationId"] == "group-a"
    assert target["sessionWebhook"] == "https://example.test/refreshed"
    replacement, next_manager = service_at(tmp_path)
    replacement.handle_message(confirmation)
    next_manager.start.assert_not_called()


def test_configured_default_never_uses_other_groups_reply_url(tmp_path):
    save_stream_binding({"openConversationId": "other", "sessionWebhook": "https://example.test/other"}, tmp_path)
    settings = {"dingtalk_stream": {"open_conversation_id": "configured", "robot_code": "robot"}}
    target = default_notification_binding(settings, tmp_path)
    assert target["openConversationId"] == "configured"
    assert "sessionWebhook" not in target


def test_private_binding_is_not_used_as_default_group(tmp_path):
    save_stream_binding({"openConversationId": "dm", "sessionWebhook": "https://example.test/dm", "conversationType": "1"}, tmp_path)
    target = default_notification_binding({}, tmp_path)
    assert not target["openConversationId"]
    assert "sessionWebhook" not in target


def test_rebinding_does_not_mix_old_group_credentials(tmp_path):
    save_stream_binding({"openConversationId": "a", "sessionWebhook": "https://example.test/a"}, tmp_path)
    updated = save_stream_binding({"openConversationId": "b"}, tmp_path)
    assert "sessionWebhook" not in updated


def test_explicit_empty_target_never_falls_back_to_default(tmp_path, monkeypatch):
    import dingtalk_notifier
    post = Mock()
    monkeypatch.setattr(dingtalk_notifier, "post_json", post)
    save_stream_binding({"openConversationId": "other", "sessionWebhook": "https://example.test/other"}, tmp_path)
    settings = {"notification": {"dingtalk": {"enabled": True}}, "dingtalk_stream": {"enabled": True, "open_conversation_id": "default"}}
    result = send_task_result_notification(settings, build_task_result_message(action="suggestions", success=True), root_dir=tmp_path, binding={})
    assert not result["sent"]
    post.assert_not_called()


def test_task_target_is_durable_and_isolated_from_newer_task(tmp_path, monkeypatch):
    import web_runner
    from web_runner import AutomationJobManager
    monkeypatch.setattr(web_runner, "load_settings", lambda: {})
    monkeypatch.setattr(web_runner.threading, "Thread", Mock())
    manager = AutomationJobManager(root_dir=tmp_path)
    first = manager.start("workbuddy_loop", notification_target={"openConversationId": "a", "sessionWebhook": "https://example.test/a"})
    first_context = {"task_id": first["task_id"], "started_at": manager.started_at}
    monkeypatch.setattr(manager, "_run_worker_process", lambda *args: 0)
    rows = [23]
    monkeypatch.setattr(web_runner, "approval_summary", lambda *args: {"exists": True, "rows": rows[0]})
    monkeypatch.setattr(web_runner, "review_queue_summary", lambda *args: {})
    sent = Mock(return_value={"sent": True})
    monkeypatch.setattr(web_runner, "send_task_result_notification", sent)
    def start_next(**kwargs):
        rows[0] = 99
        manager.start("workbuddy_loop", notification_target={"openConversationId": "b", "sessionWebhook": "https://example.test/b"})
    manager.add_completion_callback(start_next)
    manager._run("workbuddy_loop", {}, first_context)
    assert sent.call_args.kwargs["binding"]["openConversationId"] == "a"
    assert "23 条" in sent.call_args.args[1].markdown
    assert DingTalkState(tmp_path).load_target(first["task_id"])["sessionWebhook"] == "https://example.test/a"
    assert "sessionWebhook" not in (tmp_path / "data/logs/web_run_state.yaml").read_text(encoding="utf-8")


def test_task_does_not_start_if_target_cannot_be_saved(tmp_path, monkeypatch):
    import web_runner
    monkeypatch.setattr(web_runner, "load_settings", lambda: {})
    monkeypatch.setattr(web_runner.DingTalkState, "save_target", Mock(side_effect=OSError("disk full")))
    thread = Mock()
    monkeypatch.setattr(web_runner.threading, "Thread", thread)
    manager = web_runner.AutomationJobManager(root_dir=tmp_path)
    assert not manager.start("workbuddy_loop")["started"]
    assert not manager.running
    thread.assert_not_called()


def test_old_notification_failure_does_not_pollute_new_task(tmp_path, monkeypatch):
    import web_runner
    monkeypatch.setattr(web_runner, "load_settings", lambda: {})
    manager = web_runner.AutomationJobManager(root_dir=tmp_path)
    manager._task_id = "new-task"
    manager.lines = ["new task log"]
    manager._record_result_notification_failure("old notification failed", "old-task")
    assert manager.lines == ["new task log"]
    log = (tmp_path / "data/logs/bot.log").read_text(encoding="utf-8")
    assert '"task_id": "old-task"' in log
