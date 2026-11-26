import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from erp_api_client import ErpApiClient, ErpApiUnsupported
from task_control import SafeStop, TaskControl
from workbuddy_autopass import WorkBuddyAutoPass


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOMATION_TASK_ID", raising=False)
    monkeypatch.delenv("RESUME_STOPPED_TASK_ID", raising=False)
    row = {"清单号": "SJ001", "_erp_record_id": "1", "_erp_parent_id": "10",
           "_erp_parent_state": "wait", "试剂名称": "sample", "CAS号": "1", "物化特性": "普通类"}
    rows = [row]
    todos = [{"name": "SJ001"}]
    client = SimpleNamespace(fetch_reagent_detail=Mock(side_effect=lambda _: copy.deepcopy(rows)),
        fetch_todo_tasks=Mock(side_effect=lambda **_: copy.deepcopy(todos)))
    button = Mock()
    def submit():
        todos.clear()
        row["_erp_parent_state"] = "done"
    button.click.side_effect = submit
    page = Mock()
    bot = SimpleNamespace(root_dir=tmp_path, settings={}, pagination_check_succeeded=True,
        approval_write_mode=lambda: "multi_page",
        current_list_has_manual_review_item=Mock(return_value=(False, "")),
        enter_reagent_judgement_page=Mock(), open_task_detail_by_list_number=Mock(return_value=True),
        read_detail_info=Mock(return_value={"当前清单号": "SJ001"}),
        find_unmatched_reagents_across_all_pages=Mock(return_value=[]),
        find_top_approve_button=Mock(return_value=button))
    bridge = SimpleNamespace(directory=tmp_path / "workbuddy", batches=Mock(return_value=[]),
                             audit=Mock())
    service = WorkBuddyAutoPass(bot, bridge, client, page)
    return SimpleNamespace(bot=bot, bridge=bridge, client=client, button=button, page=page,
                           service=service, rows=rows, row=row, todos=todos)


def test_submit_only_after_full_validation_and_confirm_twice(harness):
    h = harness
    h.service.run(["SJ001", "SJ001"])
    h.button.click.assert_called_once()
    assert h.service.receipt("SJ001") == "submitted_verified"
    assert h.client.fetch_reagent_detail.call_count == 4
    WorkBuddyAutoPass(h.bot, h.bridge, h.client, h.page).run(["SJ001"])
    h.button.click.assert_called_once()


@pytest.mark.parametrize("kind", ["empty", "unmatched", "unknown_category", "reject", "no_id",
    "no_parent", "not_wait", "duplicate", "wrong_list", "wrong_page", "pagination", "manual",
    "pending_batch", "stopped_batch", "review", "library", "library_confirm", "no_button"])
def test_unsafe_lists_never_submit(harness, kind):
    h = harness
    if kind == "empty": h.rows.clear()
    if kind == "unmatched": h.row["物化特性"] = "-"
    if kind == "unknown_category": h.row["物化特性"] = "ERP未配置的类别"
    if kind == "reject": h.row["物化特性"] = "拒收类"
    if kind == "no_id": h.row.pop("_erp_record_id")
    if kind == "no_parent": h.row.pop("_erp_parent_id")
    if kind == "not_wait": h.row["_erp_parent_state"] = "done"
    if kind == "duplicate": h.rows.append(copy.deepcopy(h.row))
    if kind == "wrong_list": h.row["清单号"] = "SJ002"
    if kind == "wrong_page": h.bot.read_detail_info.return_value = {"当前清单号": "SJ002"}
    if kind == "pagination": h.bot.pagination_check_succeeded = False
    if kind == "manual": h.bot.current_list_has_manual_review_item.return_value = (True, "manual")
    if kind == "no_button": h.bot.find_top_approve_button.return_value = None
    if kind in {"pending_batch", "stopped_batch", "review", "library", "library_confirm"}:
        batch = {"status": "completed", "items": [{"input": {"试剂清单号": "SJ001"}, "status": "saved"}]}
        if kind == "pending_batch": batch["status"] = "waiting_result"
        if kind == "stopped_batch": batch["operator_stop_task"] = "old"
        if kind == "review": batch["items"][0]["status"] = "review"
        if kind.startswith("library"):
            batch["generate_library"] = True
            batch["items"][0]["library"] = {"status": "needs_confirmation" if kind == "library_confirm" else "sending"}
        h.bridge.batches.return_value = [batch]
    h.service.run(["SJ001"])
    h.button.click.assert_not_called()
    assert not h.service.receipt("SJ001")


def test_unknown_class_can_submit_after_full_validation(harness):
    h = harness
    h.row['物化特性'] = '未知类'
    h.service.run(['SJ001'])
    h.button.click.assert_called_once()
    assert h.service.receipt('SJ001') == 'submitted_verified'


def test_duplicate_categories_cannot_submit(harness):
    h = harness
    h.rows.append({**h.row, '_erp_record_id': '2', '物化特性': '特殊酸'})
    h.service.run(['SJ001'])
    h.button.click.assert_not_called()


def test_library_recheck_failure_cannot_submit(harness):
    h = harness
    h.bridge.recheck_library_confirmations = Mock(side_effect=ValueError('库分类不一致'))
    h.service.run(['SJ001'])
    h.button.click.assert_not_called()
    assert '库分类不一致' in h.service.local_block('SJ001')


def test_unknown_class_does_not_bypass_library_confirmation(harness):
    h = harness
    h.row['物化特性'] = '未知类'
    h.bridge.batches.return_value = [{
        'status': 'completed', 'generate_library': True,
        'items': [{'input': {'试剂清单号': 'SJ001', '试剂名称': 'sample'}, 'status': 'saved',
                   'library': {'status': 'needs_confirmation'}}],
    }]
    h.service.run(['SJ001'])
    h.button.click.assert_not_called()
    assert not h.service.receipt('SJ001')
    assert 'sample' in h.service.local_block('SJ001')
    assert 'needs_confirmation' in h.service.local_block('SJ001')


def test_detail_changes_before_click_block_submission(harness):
    h = harness
    calls = [copy.deepcopy(h.rows), [{**h.row, "CAS号": "changed"}]]
    h.client.fetch_reagent_detail.side_effect = calls
    h.service.run(["SJ001"])
    h.button.click.assert_not_called()


@pytest.mark.parametrize("outcome", ["timeout", "still_pending", "cancelled", "changed_identity"])
def test_unknown_submission_never_replays_even_after_restart(harness, outcome):
    h = harness
    def submit():
        if outcome == "timeout": raise TimeoutError("unknown")
        if outcome == "still_pending": return
        h.todos.clear()
        h.row["_erp_parent_state"] = "cancel" if outcome == "cancelled" else "done"
        if outcome == "changed_identity": h.row["_erp_record_id"] = "2"
    h.button.click.side_effect = submit
    with pytest.raises(RuntimeError, match="禁止自动重发"):
        h.service.run(["SJ001"])
    assert h.service.receipt("SJ001") == "needs_review"
    with pytest.raises(RuntimeError, match="禁止自动重发"):
        WorkBuddyAutoPass(h.bot, h.bridge, h.client, h.page).run(["SJ001"])
    h.button.click.assert_called_once()


def test_configuration_or_operator_change_does_not_unlock_unknown_send(harness, monkeypatch):
    h = harness
    h.button.click.side_effect = TimeoutError("unknown")
    with pytest.raises(RuntimeError):
        h.service.run(["SJ001"])
    h.bot.settings["erp_api"] = {"enabled_for_write": False, "discovery": {"status": "candidate"}}
    monkeypatch.setenv("ERP_USERNAME", "another-operator")
    with pytest.raises(RuntimeError, match="禁止自动重发"):
        WorkBuddyAutoPass(h.bot, h.bridge, h.client, h.page).run(["SJ001"])
    h.button.click.assert_called_once()


def test_stop_before_click_does_not_submit_and_leaves_no_unknown_receipt(harness, monkeypatch):
    h = harness
    import workbuddy_autopass
    def stop(phase, **_):
        if phase == "WorkBuddy ERP 整单通过发送前": raise SafeStop(phase)
    monkeypatch.setattr(workbuddy_autopass, "checkpoint", stop)
    with pytest.raises(SafeStop):
        h.service.run(["SJ001"])
    h.button.click.assert_not_called()
    assert not h.service.receipt("SJ001")


def test_stop_during_submission_finishes_readback(harness, monkeypatch):
    h = harness
    import runtime_paths
    monkeypatch.setattr(runtime_paths, "runtime_root", lambda: h.bot.root_dir)
    control = TaskControl(h.bot.root_dir)
    control.register("task", "workbuddy_loop", {}, {"source": "web"}, {})
    monkeypatch.setenv("AUTOMATION_TASK_ID", "task")
    original = h.button.click.side_effect
    def submit():
        original()
        control.request("task", {"source": "web"})
    h.button.click.side_effect = submit
    with pytest.raises(SafeStop):
        h.service.run(["SJ001"])
    assert h.service.receipt("SJ001") == "submitted_verified"
    assert h.client.fetch_reagent_detail.call_count == 4


@pytest.mark.parametrize("line_ids,records", [
    ([1, 2], [{"id": 1}]), ([1, 2], [{"id": 1}, {"id": 1}]),
    ([1], [{"id": 2}]), ([1, 1], [{"id": 1}]),
])
def test_odoo_incomplete_detail_is_not_a_complete_list(monkeypatch, line_ids, records):
    client = ErpApiClient(object(), {"erp_api": {"protocol": "odoo_jsonrpc"}})
    monkeypatch.setattr(client, "_rpc_search_read", lambda model, *a, **k:
        [{"id": 10, "name": "SJ001", "state": "wait", "reagent_list_line_ids": line_ids}]
        if model == "reagent.list" else records)
    with pytest.raises(ErpApiUnsupported, match="incomplete"):
        client.fetch_reagent_detail("SJ001")


@pytest.mark.parametrize("dry_run,auto_pass,authorized,expected", [
    (False, "true", "true", True), (False, "false", "true", False),
    (False, "true", "false", False), (True, "true", "true", False),
])
def test_only_authorized_formal_loop_reaches_final_submission(tmp_path, monkeypatch, dry_run, auto_pass, authorized, expected):
    import approval_flow
    from browser_bot import BrowserBot
    from workbuddy_bridge import WorkBuddyBridge

    monkeypatch.delenv("AUTOMATION_TASK_ID", raising=False)
    monkeypatch.delenv("RESUME_STOPPED_TASK_ID", raising=False)
    monkeypatch.setenv("AUTO_PASS", auto_pass)
    monkeypatch.setenv("WORKBUDDY_AUTO_PASS", authorized)
    bot = BrowserBot(settings={"app": {"dry_run": dry_run},
        "workbuddy": {"desktop_enabled": True, "write_enabled": True}}, root_dir=tmp_path)
    bot.target_list_numbers = ["SJ001"]
    bot.dry_run_enabled = lambda: dry_run
    bot.approval_write_mode = lambda: "multi_page"
    bot.erp_write_backend = lambda: "api_write_with_web_verify"
    bot.run_after_login_capture = lambda _png, _html, callback: callback(object())
    client = SimpleNamespace(protocol="odoo_jsonrpc", write_enabled=lambda: True)
    monkeypatch.setattr(approval_flow, "ErpApiClient", lambda *_: client)
    monkeypatch.setattr(WorkBuddyBridge, "prepare", lambda *a, **k: None)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/rules_structured.xlsx").write_bytes(b"test")
    submit = Mock()
    monkeypatch.setattr(WorkBuddyAutoPass, "run", submit)
    bot.run_workbuddy_loop()
    assert bool(submit.call_count) is expected
    if expected: submit.assert_called_once_with(["SJ001"])


def test_previously_matched_library_is_accepted_only_when_verified(harness):
    h = harness
    h.bot.approval_write_mode = lambda: "generate_library"
    h.bridge.batches.return_value = [{"status": "completed", "generate_library": True,
        "items": [{"input": {"试剂清单号": "SJ001"}, "status": "saved",
                   "library": {"status": "existing_unchanged"}}]}]
    h.service.run(["SJ001"])
    h.button.click.assert_called_once()


def test_matching_unknown_never_submits(harness):
    h = harness
    h.bridge.directory.mkdir()
    (h.bridge.directory / "erp_matches.json").write_text(
        '{"test":{"list_number":"SJ001","status":"sending"}}', encoding="utf-8")
    h.service.run(["SJ001"])
    h.button.click.assert_not_called()


class _FakeLocator:
    def __init__(self, items):
        self._items = items

    def count(self):
        return len(self._items)

    def nth(self, index):
        return self._items[index]

    @property
    def first(self):
        return self._items[0] if self._items else _FakeElement(visible=False)


class _FakeElement:
    def __init__(self, *, visible=True, text="", children=None, on_click=None):
        self._visible = visible
        self._text = text
        self._children = children or {}
        self._on_click = on_click
        self.clicks = 0

    def count(self):
        return 1 if self._visible else 0

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text

    def click(self):
        self.clicks += 1
        if self._on_click:
            self._on_click()

    def locator(self, selector):
        return _FakeLocator(self._children.get(selector, []))


def test_submit_confirms_erp_approval_result_modal_with_spaced_ok_text(harness):
    h = harness
    modal_state = {"visible": False}

    def confirm_submit():
        modal_state["visible"] = False
        h.todos.clear()
        h.row["_erp_parent_state"] = "done"

    cancel_btn = _FakeElement(visible=True, text="取 消")
    ok_btn = _FakeElement(visible=True, text="确 定", on_click=confirm_submit)
    title = _FakeElement(visible=True, text="审批结果")
    modal = _FakeElement(
        visible=True,
        children={
            ".ant-modal-title": [title],
            ".ant-modal-footer button.ant-btn-primary": [ok_btn],
            ".ant-modal-footer button": [cancel_btn, ok_btn],
        },
    )
    modal.is_visible = lambda: modal_state["visible"]

    h.button.click.side_effect = lambda: modal_state.__setitem__("visible", True)
    h.page.locator = lambda selector: _FakeLocator([modal] if selector == ".ant-modal" else [])

    h.service.run(["SJ001"])

    h.button.click.assert_called_once()
    assert ok_btn.clicks == 1
    assert cancel_btn.clicks == 0
    assert h.service.receipt("SJ001") == "submitted_verified"


def test_approval_result_modal_without_confirm_button_fails_closed(harness):
    h = harness
    modal_state = {"visible": False}
    cancel_btn = _FakeElement(visible=True, text="取 消")
    title = _FakeElement(visible=True, text="审批结果")
    modal = _FakeElement(
        visible=True,
        children={
            ".ant-modal-title": [title],
            ".ant-modal-footer button.ant-btn-primary": [],
            ".ant-modal-footer button": [cancel_btn],
        },
    )
    modal.is_visible = lambda: modal_state["visible"]

    h.button.click.side_effect = lambda: modal_state.__setitem__("visible", True)
    h.page.locator = lambda selector: _FakeLocator([modal] if selector == ".ant-modal" else [])

    with pytest.raises(RuntimeError, match="禁止自动重发"):
        h.service.run(["SJ001"])

    assert cancel_btn.clicks == 0
    assert h.service.receipt("SJ001") == "needs_review"
