import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from task_control import TaskControl, SafeStop
from workbuddy_bridge import WorkBuddyBridge, record_identity, record_key


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    import runtime_paths
    monkeypatch.setattr(runtime_paths, "runtime_root", lambda: tmp_path)
    monkeypatch.setenv("AUTOMATION_TASK_ID", "task-1")
    monkeypatch.delenv("RESUME_STOPPED_TASK_ID", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/rules_structured.xlsx").write_bytes(b"rules")
    settings = {"workbuddy": {"desktop_enabled": True}, "app": {"dry_run": True}}
    control = TaskControl(tmp_path)
    control.register("task-1", "workbuddy_loop", {}, {"source": "web"}, settings)
    bridge = WorkBuddyBridge(tmp_path, settings)
    records = [{"试剂清单号": "SJ001", "_erp_record_id": str(index), "试剂名称": f"reagent-{index}", "CAS号": "-", "物化特性": "-", "_erp_parent_state": "wait"} for index in (1, 2)]
    state = {"batch_id": "abc", "status": "imported", "rules_sha256": hashlib.sha256(b"rules").hexdigest(),
        "items": [{"key": record_key(record), "input": record_identity(record), "status": "pending", "attempts": 0,
                   "result": {"物化特性类别": "普通类", "判断依据": "test"}} for record in records]}
    path = bridge.batch_path(state)
    path.mkdir(parents=True)
    (path / "result.xlsx").write_bytes(b"preserved-result")
    monkeypatch.setattr(bridge, "load_results", lambda state: None)
    bridge.save(state)
    return SimpleNamespace(root=tmp_path, settings=settings, control=control, bridge=bridge, records=records, state=state, path=path)


def test_stop_during_save_finishes_readback_then_skips_next_item(workflow):
    h = workflow
    calls = []
    def save(record_id, *args):
        calls.append(record_id)
        h.control.request("task-1", {"source": "web"})
        return SimpleNamespace(saved=True, verified=True, detail="saved and verified")
    client = SimpleNamespace(fetch_reagent_detail=lambda number: h.records, save_physicochemical_property_by_id=save)
    memory = Mock()
    with pytest.raises(SafeStop):
        try:
            h.bridge.process(h.state, client, memory, dry_run=False)
        except SafeStop:
            h.bridge.pause_operator_batch(h.state)
            raise
    assert calls == ["1"]
    assert h.state["items"][0]["status"] == "saved"
    assert h.state["items"][1]["status"] == "pending"
    assert h.state["status"] == "operator_stopped"
    assert h.path.joinpath("result.xlsx").exists()
    assert h.control.get("task-1")["details"]["saved"] == 1
    with pytest.raises(RuntimeError, match="人工停止"):
        h.bridge.process(h.state, client, memory, dry_run=False)


def test_stop_after_categories_preserves_pending_library(workflow):
    h = workflow
    count = [0]
    def save(*args):
        count[0] += 1
        if count[0] == 2:
            h.control.request("task-1", {"source": "web"})
        return SimpleNamespace(saved=True, verified=True, detail="saved")
    library = Mock()
    client = SimpleNamespace(fetch_reagent_detail=lambda number: h.records, save_physicochemical_property_by_id=save, generate_reagent_library_by_id=library)
    with pytest.raises(SafeStop):
        try:
            h.bridge.process(h.state, client, Mock(), dry_run=False, generate_library=True)
        except SafeStop:
            h.bridge.pause_operator_batch(h.state)
            raise
    assert h.control.get("task-1")["details"]["pending_library"] == 2
    library.assert_not_called()


def test_stop_during_library_finishes_current_call_only(workflow):
    h = workflow
    for item in h.state["items"]:
        item.update(status="saved", erp_category="普通类")
    def generate(*args):
        h.control.request("task-1", {"source": "web"})
        return {"status": "created_verified", "detail": "verified"}
    library = Mock(side_effect=generate)
    client = SimpleNamespace(generate_reagent_library_by_id=library)
    with pytest.raises(SafeStop):
        try:
            h.bridge.process(h.state, client, Mock(), dry_run=False, generate_library=True)
        except SafeStop:
            h.bridge.pause_operator_batch(h.state)
            raise
    assert library.call_count == 1
    assert h.state["items"][0]["library"]["status"] == "created_verified"
    assert h.control.get("task-1")["details"]["pending_library"] == 1


def test_wait_stop_retains_batch_and_late_result_is_not_imported(workflow, monkeypatch):
    import approval_flow
    import time
    from browser_bot import BrowserBot
    h = workflow
    h.path.joinpath("result.xlsx").unlink()
    h.state.update(status="waiting_result", dispatch_state="submitted")
    h.bridge.save(h.state)
    bot = BrowserBot(settings=h.settings, root_dir=h.root)
    bot.target_list_numbers = ["SJ001"]
    bot.run_after_login_capture = lambda screenshot, html, callback: callback(object())
    monkeypatch.setattr(approval_flow.ErpApiClient, "fetch_reagent_detail", lambda *args: h.records)
    monkeypatch.setattr(time, "sleep", lambda seconds: h.control.request("task-1", {"source": "web"}))
    process = Mock()
    monkeypatch.setattr(WorkBuddyBridge, "process", process)
    with pytest.raises(SafeStop):
        bot.run_workbuddy_loop()
    state = h.bridge.batches()[0]
    assert state["status"] == "operator_stopped"
    h.path.joinpath("result.xlsx").write_bytes(b"late-result")
    process.assert_not_called()
    with pytest.raises(RuntimeError, match="人工停止"):
        h.bridge.restore_authorized_pause()


def test_resume_after_early_second_stop_recognizes_original_batch(workflow, monkeypatch):
    h = workflow
    h.bridge.pause_operator_batch(h.state)
    h.control.finish("task-1", "stopped")
    h.control.register("task-2", "workbuddy_loop", {}, {"source": "web"}, h.settings, resume="task-1")
    h.control.finish("task-2", "stopped")
    h.control.register("task-3", "workbuddy_loop", {}, {"source": "web"}, h.settings, resume="task-2")
    monkeypatch.setenv("AUTOMATION_TASK_ID", "task-3")
    monkeypatch.setenv("RESUME_STOPPED_TASK_ID", "task-2")
    h.bridge.restore_authorized_pause()
    assert h.bridge.batches()[0]["status"] == "imported"


def test_v1_stop_prevents_top_approval_click(workflow):
    from approval_flow import ApprovalFlowMixin
    h = workflow
    h.control.request("task-1", {"source": "web"})
    bot = ApprovalFlowMixin()
    bot.click_top_approve_button = Mock()
    with pytest.raises(SafeStop):
        bot.try_auto_pass_current_task(object())
    bot.click_top_approve_button.assert_not_called()


def test_v1_stop_during_button_lookup_prevents_approval_request(workflow):
    from approval_flow import ApprovalFlowMixin
    h = workflow
    bot = ApprovalFlowMixin()
    button = Mock()

    def lookup(_page):
        h.control.request("task-1", {"source": "web"})
        return button

    bot.find_top_approve_button = lookup
    with pytest.raises(SafeStop):
        bot.click_top_approve_button(object())
    button.click.assert_not_called()


def test_reviewed_prepared_batch_can_be_used_without_submitting_on_resume(workflow, monkeypatch):
    h = workflow
    h.path.joinpath("result.xlsx").unlink()
    h.state["status"] = "waiting_result"
    h.bridge.pause_operator_batch(h.state)
    monkeypatch.setenv("RESUME_STOPPED_TASK_ID", "task-1")
    h.bridge.restore_authorized_pause()
    state = h.bridge.batches()[0]
    h.bridge.release_prepared_pause(state)
    monkeypatch.delenv("RESUME_STOPPED_TASK_ID")
    assert h.bridge.prepare([])["batch_id"] == state["batch_id"]
    assert not h.bridge.batches()[0].get("dispatch_state")
