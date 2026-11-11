import copy
import json
import re
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from task_control import TaskControl, SafeStop, can_stop, checkpoint, SAFE_STOP_EXIT_CODE
from dingtalk_stream_bot import DingTalkCommandService
from dingtalk_notifier import build_task_result_message


OWNER = {"source": "dingtalk", "corp_id": "corp", "user_id": "owner", "conversation_id": "origin", "conversation_type": "2"}
POLICY = {"dingtalk_control": {"admin_user_ids": ["corp/admin"], "management_group_ids": ["management"]}}


@pytest.mark.parametrize("changes,allowed", [({}, True), ({"user_id": "other"}, False), ({"corp_id": "other"}, False),
    ({"conversation_id": "another"}, False), ({"conversation_type": "1"}, False), ({"user_id": "admin"}, True),
    ({"user_id": "admin", "conversation_id": "management"}, True), ({"user_id": "admin", "conversation_id": "another"}, False)])
def test_dingtalk_permission_matrix(changes, allowed):
    assert can_stop({"actor": OWNER}, {**OWNER, **changes}, POLICY) is allowed


def test_web_and_scheduled_tasks_require_admin_in_management_group():
    assert not can_stop({"actor": {"source": "web"}}, OWNER, POLICY)
    assert can_stop({"actor": {"source": "scheduler"}}, {**OWNER, "user_id": "admin", "conversation_id": "management"}, POLICY)


@pytest.fixture
def managed(tmp_path, monkeypatch):
    import web_runner
    settings = copy.deepcopy(POLICY)
    settings["approval"] = {"write_mode": "multi_page"}
    client = tmp_path / "WorkBuddy.exe"
    client.touch()
    settings["workbuddy"] = {"desktop_enabled": True, "executable": str(client)}
    (tmp_path / "config").mkdir()
    (tmp_path / "config/rules_structured.xlsx").write_bytes(b"test-rules")
    monkeypatch.setattr(web_runner, "load_settings", lambda: settings)
    monkeypatch.setattr(web_runner.threading, "Thread", Mock())
    manager = web_runner.AutomationJobManager(root_dir=tmp_path)
    result = manager.start("workbuddy_loop", {"TARGET_LIST_NUMBERS": "SJ001"}, actor=OWNER)
    assert result["started"]
    return manager, TaskControl(tmp_path), settings, result["task_id"]


def test_request_is_durable_and_does_not_kill_process(managed, monkeypatch):
    manager, control, _, task_id = managed
    kill = Mock()
    monkeypatch.setattr(manager, "_terminate_process_tree", kill)
    assert manager.request_safe_stop(task_id, OWNER)["requested"]
    assert TaskControl(manager.root_dir).get(task_id)["state"] == "stop_requested"
    with pytest.raises(SafeStop):
        control.checkpoint(task_id, "当前操作已完成")
    kill.assert_not_called()
    assert manager.running


def test_completed_task_or_new_task_never_consumes_old_stop(managed):
    manager, control, _, task_id = managed
    manager._task_id = "new-task"
    assert not manager.request_safe_stop(task_id, OWNER)["requested"]
    assert control.get(task_id)["state"] == "running"


def test_stopped_task_scope_blocks_scheduled_restart(managed):
    manager, control, _, task_id = managed
    control.record_details(task_id, scope=["SJ001"], scope_frozen=True)
    control.finish(task_id, "stopped")
    with pytest.raises(ValueError, match="人工停止"):
        control.ensure_allowed_start("workbuddy_loop", {"PROCESS_ALL_TODOS": "true"})
    control.ensure_allowed_start("workbuddy_loop", {"TARGET_LIST_NUMBERS": "SJ002"})
    with pytest.raises(ValueError, match="人工停止"):
        control.ensure_allowed_start("suggestions", {"TARGET_LIST_NUMBER": "SJ001"})
    control.ensure_allowed_start("todo_export", {})


def test_failed_resume_launch_returns_parent_to_stopped(managed):
    import web_runner
    manager, control, _, task_id = managed
    control.finish(task_id, "stopped")
    manager.running = False
    web_runner.threading.Thread.return_value.start.side_effect = OSError("launch failed")
    assert not manager.resume_stopped_task(task_id)["started"]
    assert not manager.running
    assert control.get(task_id)["state"] == "stopped"
    assert control.get(manager._task_id)["state"] == "failed"


def test_manual_resume_reuses_last_observed_dynamic_scope(managed):
    manager, control, settings, _ = managed
    control.register("dynamic", "suggestions", {"PROCESS_ALL_TODOS": "true"}, {"source": "web"}, settings)
    control.record_details("dynamic", scope=["SJ001", "SJ002"])
    control.finish("dynamic", "stopped")
    manager.running = False
    resumed = manager.resume_stopped_task("dynamic")
    assert resumed["started"]
    options = control.get(resumed["task_id"])["options"]
    assert options["TARGET_LIST_NUMBERS"] == "SJ001,SJ002"
    assert options["PROCESS_ALL_TODOS"] == "false"


def test_manual_resume_preserves_scope_and_rejects_changed_rules(managed):
    manager, control, settings, task_id = managed
    control.record_details(task_id, scope=["SJ001"], scope_frozen=True)
    control.finish(task_id, "stopped")
    manager.running = False
    (manager.root_dir / "config/rules_structured.xlsx").write_bytes(b"changed-rules")
    assert not manager.resume_stopped_task(task_id)["started"]
    assert control.get(task_id)["state"] == "stopped"
    (manager.root_dir / "config/rules_structured.xlsx").write_bytes(b"test-rules")
    resumed = manager.resume_stopped_task(task_id)
    assert resumed["started"]
    child = control.get(resumed["task_id"])
    assert child["options"]["TARGET_LIST_NUMBERS"] == "SJ001"
    assert child["actor"] == {"source": "web"}
    assert task_id in child["details"]["resume_ancestors"]
    assert control.get(task_id)["state"] == "resuming"


def callback(text, sender="owner", group="origin", message_id="m1"):
    return {"conversationId": group, "conversationType": "2", "senderCorpId": "corp", "senderStaffId": sender,
            "msgId": message_id, "text": {"content": text}}


def stop_service(managed):
    manager, _, settings, _ = managed
    now = [100.0]
    service = DingTalkCommandService(job_manager=manager, run_options_builder=Mock(), status_provider=manager.status,
        settings_loader=lambda: settings, root_dir=manager.root_dir, clock=lambda: now[0])
    return service, now


def test_stop_requires_separate_one_time_confirmation(managed):
    manager, control, _, task_id = managed
    service, _ = stop_service(managed)
    draft = service.handle_message(callback(f"停止审批 {task_id[:12].upper()}"))
    assert "链路：WorkBuddy" in draft
    assert "当前清单：SJ001" in draft
    code = re.search(r"确认停止 ([A-F0-9]{6})", draft).group(1)
    assert control.get(task_id)["state"] == "running"
    assert "停止请求已受理" in service.handle_message(callback(f"确认停止 {code}", message_id="m2"))
    assert control.get(task_id)["state"] == "stop_requested"
    assert "不存在" in service.handle_message(callback(f"确认停止 {code}", message_id="m3"))


@pytest.mark.parametrize("change", ["expire", "cancel", "switch", "other_user", "other_group"])
def test_stop_confirmation_boundaries(managed, change):
    manager, control, _, task_id = managed
    service, now = stop_service(managed)
    code = re.search(r"确认停止 ([A-F0-9]{6})", service.handle_message(callback("停止审批"))).group(1)
    sender, group = "owner", "origin"
    if change == "expire": now[0] += 121
    if change == "cancel": service.handle_message(callback("取消", message_id="cancel"))
    if change == "switch": manager._task_id = "new-task"
    if change == "other_user": sender = "other"
    if change == "other_group": group = "another"
    service.handle_message(callback(f"确认停止 {code}", sender=sender, group=group, message_id="m2"))
    assert control.get(task_id)["state"] == "running"


def test_admin_removed_after_draft_cannot_confirm(managed):
    manager, control, settings, task_id = managed
    service, _ = stop_service(managed)
    draft = service.handle_message(callback("停止审批", sender="admin", group="management"))
    code = re.search(r"确认停止 ([A-F0-9]{6})", draft).group(1)
    settings["dingtalk_control"]["admin_user_ids"] = []
    assert "仅任务发起人" in service.handle_message(callback(f"确认停止 {code}", sender="admin", group="management", message_id="m2"))
    assert control.get(task_id)["state"] == "running"


def test_status_after_restart_reports_unconfirmed_worker_state(managed):
    manager, _, _, task_id = managed
    from web_runner import AutomationJobManager
    status = AutomationJobManager(root_dir=manager.root_dir).status(light=True)
    assert status["control"]["state"] == "needs_review"
    assert status["task_id"] == task_id


def test_stopped_notification_is_neither_failure_nor_success():
    message = build_task_result_message(action="workbuddy_loop", success=None, stopped=True,
        stop_details={"saved": 3, "pending_library": 1, "remaining": 4, "uncertain": 0})
    assert "已人工停止" in message.title
    assert "失败" not in message.title
    assert "待生成试剂库：1 条" in message.markdown


def test_safe_worker_exit_keeps_task_stopped(managed, monkeypatch):
    manager, control, _, task_id = managed
    manager.request_safe_stop(task_id, OWNER)
    def worker(*args):
        control.finish(task_id, "stopped")
        return SAFE_STOP_EXIT_CODE
    monkeypatch.setattr(manager, "_run_worker_process", worker)
    notify = Mock()
    monkeypatch.setattr(manager, "_notify_task_result", notify)
    manager._run("workbuddy_loop", manager._last_options, {"task_id": task_id, "started_at": manager.started_at})
    assert manager.success is None
    assert not manager.error
    assert manager.status()["result_label"] == "已人工停止"
    assert control.get(task_id)["state"] == "stopped"
    assert notify.call_args.kwargs["success"] is None
