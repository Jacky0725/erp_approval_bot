"""Check the adapter without sending Windows input or starting WorkBuddy."""
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workbuddy_desktop import WorkBuddyDesktop, editor_value


def test_editor_retries_then_reports_candidate_counts(monkeypatch):
    import itertools
    ticks = itertools.count(step=0.4)
    monkeypatch.setattr("workbuddy_desktop.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("workbuddy_desktop.time.sleep", lambda _: None)
    adapter = WorkBuddyDesktop({"editor_ready_timeout_seconds": 1})
    reopened = []
    adapter._open_project = lambda window: reopened.append(True)
    window = SimpleNamespace(process_id=lambda: 5, handle=7, rectangle=lambda: "rect", descendants=lambda **kw: [])
    with pytest.raises(RuntimeError, match="candidates=0"):
        adapter._wait_for_project_editor(window)
    assert reopened == [True]


def test_editor_ignores_search_and_pagination(monkeypatch):
    monkeypatch.setattr("workbuddy_desktop.time.sleep", lambda _: None)
    def control(name):
        return SimpleNamespace(is_visible=lambda: True, window_text=lambda: name,
                               rectangle=lambda: name, element_info=SimpleNamespace(class_name=""))
    search, pagination, editor = [control(s) for s in ("搜索任务标题", "当前页码，共 2 页", "")]
    window = SimpleNamespace(process_id=lambda: 5, handle=7, rectangle=lambda: "window",
                             descendants=lambda **kw: [search, pagination, editor])
    assert WorkBuddyDesktop({})._wait_for_project_editor(window) is editor


def test_submission_confirmation_uses_current_editor_not_old_composer(desktop):
    stale = SimpleNamespace(get_value=lambda: "batch abc")
    desktop.window.messages = [SimpleNamespace(is_visible=lambda: True, window_text=lambda: "batch abc")]
    desktop.adapter._wait_for_submitted_message(desktop.window, stale, "abc", timeout_seconds=1)


@pytest.fixture
def desktop(monkeypatch):
    calls = []
    class Control:
        def __init__(self, name="", value="", enabled=True):
            self.name, self.value, self.enabled = name, value, enabled
        def window_text(self): return self.name
        def get_value(self): return self.value
        def is_visible(self): return True
        def is_enabled(self): return self.enabled
        def click_input(self): calls.append(self.name or "click")
        def rectangle(self): return SimpleNamespace(right=700, top=100, bottom=250, mid_point=lambda: SimpleNamespace(x=680, y=270))
    editor = Control()
    button = Control()
    class Window:
        handle = 123
        tabs = [Control("试剂审批")]
        links = []
        def process_id(self): return 123
        def rectangle(self): return SimpleNamespace(left=0, top=0, right=800, bottom=600)
        def set_focus(self): pass
        def descendants(self, **kwargs):
            return {"Tab": self.tabs, "Hyperlink": self.links, "Edit": [Control("搜索任务标题"), editor], "Button": [button]}.get(kwargs["control_type"], [])
    window = Window()
    window.messages = []
    button.click_action = None
    original_click = button.click_input
    def click_button():
        original_click()
        if button.click_action:
            button.click_action()
    button.click_input = click_button
    original_descendants = window.descendants
    def descendants(**kwargs):
        if kwargs.get("control_type") == "Text":
            return window.messages
        return original_descendants(**kwargs)
    window.descendants = descendants
    def keyboard(value, **kwargs):
        assert "with_newlines" not in kwargs
        assert "\n" not in value
        editor.value = (editor.value if editor_value(editor.value) else "") + value
        calls.append("type")
    package = ModuleType("pywinauto")
    package.Desktop = lambda **kwargs: SimpleNamespace(windows=lambda **kwargs: [window])
    app_module = ModuleType("pywinauto.application")
    app_module.process_module = lambda pid: "WorkBuddy.exe"
    key_module = ModuleType("pywinauto.keyboard")
    key_module.send_keys = keyboard
    monkeypatch.setitem(sys.modules, "pywinauto", package)
    monkeypatch.setitem(sys.modules, "pywinauto.application", app_module)
    monkeypatch.setitem(sys.modules, "pywinauto.keyboard", key_module)
    gui_module = ModuleType("win32gui")
    gui_module.GetForegroundWindow = lambda: 123
    monkeypatch.setitem(sys.modules, "win32gui", gui_module)
    monkeypatch.setattr("workbuddy_desktop.time.sleep", lambda _: None)
    adapter = WorkBuddyDesktop({"executable": "WorkBuddy.exe"})
    adapter._require_matching_security_context = lambda pid: None
    adapter._wait_for_client_ready = lambda window: None
    adapter._project_list_entry = lambda window, project: []
    adapter._try_sidebar_project = lambda window, project: False
    return SimpleNamespace(adapter=adapter, editor=editor, button=button, window=window, calls=calls)


@pytest.mark.parametrize("mode", ["hidden", "blank", "transient", "disappeared"])
def test_startup_rejects_unusable_or_transient_window(mode, monkeypatch):
    import itertools
    monkeypatch.setattr("workbuddy_desktop.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("workbuddy_desktop.time.sleep", lambda _: None)
    clock = itertools.count(step=0.5)
    samples = itertools.count()
    def editors(**kwargs):
        sample = next(samples)
        if mode == "disappeared":
            raise RuntimeError("window gone")
        if mode == "blank" or (mode == "transient" and sample > 0):
            return []
        return [SimpleNamespace(is_visible=lambda: True, window_text=lambda: "")]
    window = SimpleNamespace(
        handle=123, process_id=lambda: 456, is_visible=lambda: mode != "hidden",
        is_minimized=lambda: False, descendants=editors,
        rectangle=lambda: SimpleNamespace(width=lambda: 100, height=lambda: 100),
    )
    with pytest.raises(RuntimeError, match="未持续就绪"):
        WorkBuddyDesktop({"startup_timeout_seconds": 4})._wait_for_client_ready(window)


def test_startup_waits_for_stable_loaded_input(monkeypatch):
    import itertools
    clock = itertools.count(step=0.1)
    monkeypatch.setattr("workbuddy_desktop.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("workbuddy_desktop.time.sleep", lambda _: None)
    checked = []
    def editors(**kwargs):
        checked.append(True)
        return [] if len(checked) < 3 else [SimpleNamespace(is_visible=lambda: True, window_text=lambda: "")]
    window = SimpleNamespace(
        handle=123, process_id=lambda: 456, is_visible=lambda: True,
        is_minimized=lambda: False, descendants=editors,
        rectangle=lambda: SimpleNamespace(width=lambda: 100, height=lambda: 100),
    )
    WorkBuddyDesktop({})._wait_for_client_ready(window)
    assert len(checked) == 7


def test_keyboard_input_and_single_send(desktop):
    desktop.adapter.send("本批说明\n文件路径")
    assert desktop.editor.value == "本批说明 文件路径"
    assert desktop.calls == ["click", "type", "click"]


def test_dispatch_guard_is_persisted_immediately_before_submit(desktop):
    def persist_guard():
        assert desktop.editor.value == "本批说明"
        assert desktop.calls == ["click", "type"]
        desktop.calls.append("persist")
    desktop.adapter.send("本批说明", before_submit=persist_guard)
    assert desktop.calls == ["click", "type", "persist", "click"]


def test_send_waits_for_batch_message_and_empty_editor(desktop):
    def submitted():
        desktop.window.messages = [SimpleNamespace(is_visible=lambda: True, window_text=lambda: "已发送 batch-123")]
        desktop.editor.value = ""
    desktop.button.click_action = submitted
    desktop.adapter.send("执行批次 batch-123", batch_id="batch-123")
    assert desktop.editor.value == ""


def test_followup_requires_exact_previous_completion_in_configured_chat(desktop):
    link = SimpleNamespace(is_visible=lambda: True)
    desktop.window.links = [link]
    desktop.window.messages = [SimpleNamespace(is_visible=lambda: True, window_text=lambda: "【批次完成】prior")]
    desktop.adapter._open_project = Mock(side_effect=AssertionError("should stay in chat"))
    assert desktop.adapter.send("完整规则", reuse=("prior", "继续下一批"))
    assert desktop.editor.value == "继续下一批"


def test_followup_with_only_a_quoted_completion_opens_new_task(desktop):
    desktop.window.links = [SimpleNamespace(is_visible=lambda: True)]
    desktop.window.messages = [SimpleNamespace(is_visible=lambda: True, window_text=lambda: "示例：【批次完成】prior")]
    desktop.adapter._open_project = Mock()
    assert not desktop.adapter.send("完整规则", reuse=("prior", "继续下一批"))
    desktop.adapter._open_project.assert_called_once()
    assert desktop.editor.value == "完整规则"


def test_followup_change_after_typing_never_submits(desktop):
    desktop.adapter._is_completed_chat = Mock(side_effect=[True, True, False])
    guard = Mock()
    with pytest.raises(RuntimeError, match="续聊会话在输入后发生变化"):
        desktop.adapter.send("完整规则", reuse=("prior", "继续下一批"), before_submit=guard)
    guard.assert_not_called()
    assert desktop.calls == ["click", "type"]


def test_send_ack_timeout_is_uncertain_and_does_not_retry(desktop):
    desktop.adapter.config["submission_timeout_seconds"] = 0
    with pytest.raises(RuntimeError, match="发送状态不明"):
        desktop.adapter.send("执行批次 batch-123", batch_id="batch-123")
    assert desktop.calls.count("click") == 2  # editor focus and one submit click


def test_guard_persistence_failure_prevents_submit(desktop):
    def persist_guard():
        raise OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        desktop.adapter.send("本批说明", before_submit=persist_guard)
    assert desktop.calls == ["click", "type"]


def test_existing_draft_does_not_mark_dispatch_as_started(desktop):
    desktop.editor.value = "user draft"
    with pytest.raises(RuntimeError, match="已有草稿"):
        desktop.adapter.send("test", before_submit=lambda: desktop.calls.append("persist"))
    assert not desktop.calls


def test_existing_draft_not_overwritten(desktop):
    desktop.editor.value = "user draft"
    with pytest.raises(RuntimeError, match="已有草稿"):
        desktop.adapter.send("test")
    assert not desktop.calls


@pytest.mark.parametrize("placeholder", [
    "今天帮你做些什么？@ 引用资产文件、项目待办或调用技能",
    "今天帮你做些什么？@ 添加上下文，/调用技能与指令",
])
def test_empty_editor_placeholder_is_not_a_draft(desktop, placeholder):
    desktop.editor.value = "\ufeff\n" + placeholder
    desktop.adapter.send("test")
    assert desktop.calls == ["click", "type", "click"]


def test_draft_containing_placeholder_is_preserved(desktop):
    desktop.editor.value = "今天帮你做些什么？@ 引用资产文件、项目待办或调用技能 用户草稿"
    with pytest.raises(RuntimeError, match="已有草稿"):
        desktop.adapter.send("test")
    assert not desktop.calls


def test_wrong_project_never_typed(desktop):
    desktop.window.tabs = []
    with pytest.raises(RuntimeError, match="未确认项目身份"):
        desktop.adapter.send("test")
    assert not desktop.calls


def test_home_page_navigates_through_unique_project_link(desktop):
    desktop.window.tabs = []
    project_link = type("ProjectLink", (), {})()
    project_link.is_visible = lambda: getattr(project_link, "visible", True)
    project_link.click_input = lambda: (setattr(desktop.window, "tabs", [SimpleNamespace(is_visible=lambda: True)]), setattr(project_link, "visible", False))
    desktop.window.links = [project_link]
    project_link.window_text = lambda: "试剂审批-程龙"
    desktop.adapter.config["project_name"] = "试剂审批-程龙"
    desktop.adapter.send("test")
    assert project_link.visible is False
    assert "type" in desktop.calls


def test_home_page_with_ambiguous_project_links_stops(desktop):
    desktop.window.tabs = []
    desktop.window.links = [
        SimpleNamespace(is_visible=lambda: True, window_text=lambda: "试剂审批-程龙"),
        SimpleNamespace(is_visible=lambda: True, window_text=lambda: "试剂审批-程龙"),
    ]
    desktop.adapter.config["project_name"] = "试剂审批-程龙"
    with pytest.raises(RuntimeError, match="未确认项目身份"):
        desktop.adapter.send("test")
    assert "type" not in desktop.calls


@pytest.mark.parametrize("matches", [1, 2])
def test_project_card_requires_unique_match_and_project_tab(desktop, matches):
    desktop.window.tabs = []
    def click_project():
        desktop.calls.append("open-project")
        desktop.window.tabs = [SimpleNamespace(is_visible=lambda: True)]
    desktop.window.messages = [SimpleNamespace(
        is_visible=lambda: True, window_text=lambda: "试剂审批", click_input=click_project
    ) for _ in range(matches)]
    desktop.adapter._project_list_entry = lambda window, project: desktop.window.messages
    if matches == 1:
        desktop.adapter._open_project(desktop.window)
        assert desktop.calls == ["open-project"]
    else:
        with pytest.raises(RuntimeError, match="未确认项目身份"):
            desktop.adapter._open_project(desktop.window)
        assert not desktop.calls


def test_project_list_excludes_sidebar_heading():
    anchor = SimpleNamespace(is_visible=lambda: True, rectangle=lambda: SimpleNamespace(left=300))
    def card(left):
        return SimpleNamespace(is_visible=lambda: True, window_text=lambda: "target",
                               rectangle=lambda: SimpleNamespace(left=left))
    sidebar, main = card(40), card(370)
    window = SimpleNamespace(descendants=lambda **kw: [anchor] if kw['control_type'] == 'Button' else [sidebar, main])
    assert WorkBuddyDesktop({})._project_list_entry(window, "target") == [main]


@pytest.mark.parametrize("button_y, expected", [(100, True), (200, False)])
def test_sidebar_shortcut_only_uses_button_in_matching_project_row(button_y, expected, monkeypatch):
    monkeypatch.setattr("workbuddy_desktop.time.sleep", lambda _: None)
    clicked = []
    def rect(right, y):
        return SimpleNamespace(left=0, top=0, right=right, mid_point=lambda: SimpleNamespace(x=right//2, y=y))
    def control(bounds):
        return SimpleNamespace(is_visible=lambda: True, rectangle=lambda: bounds, click_input=lambda: clicked.append(True))
    def descendants(**kw):
        return {"TabItem": [control(rect(270, 30))], "Text": [control(rect(140, 100))],
                "Button": [control(rect(235, button_y))], "Hyperlink": [],
                "Tab": [control(rect(500, 100))] if clicked else []}.get(kw['control_type'], [])
    window = SimpleNamespace(descendants=descendants, rectangle=lambda: rect(1000, 100), move_mouse_input=lambda **kw: None)
    assert WorkBuddyDesktop({})._try_sidebar_project(window, "target") is expected
    assert bool(clicked) is expected


def test_disabled_send_not_clicked(desktop):
    desktop.button.enabled = False
    with pytest.raises(RuntimeError, match="发送按钮"):
        desktop.adapter.send("test")
    assert desktop.calls == ["click", "type"]


def test_window_from_different_executable_never_used(desktop, monkeypatch):
    monkeypatch.setattr(sys.modules["pywinauto.application"], "process_module", lambda pid: "Other.exe")
    monkeypatch.setattr("workbuddy_desktop.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=""))
    monkeypatch.setattr("workbuddy_desktop.client_path", lambda _: Path("WorkBuddy.exe"))
    monkeypatch.setattr("workbuddy_desktop.subprocess.Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("test")))
    with pytest.raises(RuntimeError, match="启动失败"):
        desktop.adapter.send("test")
    assert not desktop.calls


def test_wait_until_idle_waits_for_processing_label(desktop, monkeypatch):
    original = desktop.window.descendants
    samples = iter([True, True, False, False])
    checked = []
    def descendants(**kwargs):
        if kwargs.get("control_type") == "Text":
            busy = next(samples)
            checked.append(busy)
            return [SimpleNamespace(is_visible=lambda: True, window_text=lambda: "已处理 2s")] if busy else []
        return original(**kwargs)
    monkeypatch.setattr(desktop.window, "descendants", descendants)
    # An idle indicator disappearing is no longer sufficient evidence.
    with pytest.raises(RuntimeError, match="完成确认"):
        desktop.adapter.wait_for_batch_completion("batch-123", timeout_seconds=0)


def test_batch_completion_must_name_current_batch(desktop, monkeypatch):
    monkeypatch.setattr(desktop.window, "descendants", lambda **kwargs: [
        SimpleNamespace(is_visible=lambda: True, window_text=lambda: "【批次完成】batch-current")
    ] if kwargs.get("control_type") == "Text" else [])
    desktop.adapter.wait_for_batch_completion("batch-current", timeout_seconds=1)


def test_other_batch_completion_is_not_accepted(desktop, monkeypatch):
    monkeypatch.setattr(desktop.window, "descendants", lambda **kwargs: [
        SimpleNamespace(is_visible=lambda: True, window_text=lambda: "完成。批次 batch-old 的 2 条试剂已判定")
    ] if kwargs.get("control_type") == "Text" else [])
    with pytest.raises(RuntimeError, match="batch-new"):
        desktop.adapter.wait_for_batch_completion("batch-new", timeout_seconds=0)


def test_completion_requires_fixed_machine_marker(desktop, monkeypatch):
    monkeypatch.setattr(desktop.window, "descendants", lambda **kwargs: [
        SimpleNamespace(is_visible=lambda: True, window_text=lambda: "完成。批次 batch-current")
    ] if kwargs.get("control_type") == "Text" else [])
    with pytest.raises(RuntimeError, match="完成确认"):
        desktop.adapter.wait_for_batch_completion("batch-current", timeout_seconds=0)


def test_permission_mismatch_is_reported_without_attempting_to_fix(desktop, monkeypatch):
    contexts = iter([
        {"user_sid": "S-1-1", "session_id": 1, "integrity": 8192},
        {"user_sid": "S-1-1", "session_id": 1, "integrity": 12288},
    ])
    monkeypatch.setattr(WorkBuddyDesktop, "_security_context", staticmethod(lambda pid: next(contexts)))
    with pytest.raises(RuntimeError, match="权限级别不一致.*普通权限.*高权限"):
        WorkBuddyDesktop._require_matching_security_context(desktop.adapter, 999)


def test_different_windows_user_is_reported(desktop, monkeypatch):
    contexts = iter([
        {"user_sid": "S-1-1", "session_id": 1, "integrity": 8192},
        {"user_sid": "S-1-2", "session_id": 1, "integrity": 8192},
    ])
    monkeypatch.setattr(WorkBuddyDesktop, "_security_context", staticmethod(lambda pid: next(contexts)))
    with pytest.raises(RuntimeError, match="运行用户不同"):
        WorkBuddyDesktop._require_matching_security_context(desktop.adapter, 999)


def test_matching_high_privilege_is_rejected(desktop, monkeypatch):
    contexts = iter([
        {"user_sid": "S-1-1", "session_id": 1, "integrity": 12288},
        {"user_sid": "S-1-1", "session_id": 1, "integrity": 12288},
    ])
    monkeypatch.setattr(WorkBuddyDesktop, "_security_context", staticmethod(lambda pid: next(contexts)))
    with pytest.raises(RuntimeError, match="都以高权限运行"):
        WorkBuddyDesktop._require_matching_security_context(desktop.adapter, 999)


def test_taskbar_recovery_requires_unique_target(desktop):
    workbuddy = SimpleNamespace(
        is_visible=lambda: True,
        window_text=lambda: "WorkBuddy - 1 个运行窗口",
        friendly_class_name=lambda: "Button",
        element_info=SimpleNamespace(class_name="Taskbar.TaskListButtonAutomationPeer"),
        click_input=lambda: desktop.calls.append("taskbar"),
    )
    shell = SimpleNamespace(descendants=lambda **kwargs: [workbuddy])
    desktop_window = SimpleNamespace(window=lambda **kwargs: shell)
    assert desktop.adapter._restore_taskbar_or_tray(desktop_window)
    assert desktop.calls == ["taskbar"]


def test_ambiguous_taskbar_icons_are_not_clicked(desktop):
    def make_button(name):
        return SimpleNamespace(
            is_visible=lambda: True,
            window_text=lambda: name,
            friendly_class_name=lambda: "Button",
            element_info=SimpleNamespace(class_name="Taskbar.TaskListButtonAutomationPeer"),
            click_input=lambda: desktop.calls.append(name),
        )
    shell = SimpleNamespace(descendants=lambda **kwargs: [make_button("WorkBuddy - A"), make_button("WorkBuddy - B")])
    desktop_window = SimpleNamespace(window=lambda **kwargs: shell)
    assert not desktop.adapter._restore_taskbar_or_tray(desktop_window)


def test_missing_client_is_started_and_window_prepared(desktop, monkeypatch):
    launched = []
    monkeypatch.setattr("workbuddy_desktop.client_path", lambda _: Path("WorkBuddy.exe"))
    monkeypatch.setattr("workbuddy_desktop.subprocess.run", lambda *a, **k: SimpleNamespace(stdout='"other.exe","1"'))
    monkeypatch.setattr("workbuddy_desktop.subprocess.Popen", lambda *a, **k: launched.append((a, k)))
    monkeypatch.setattr("workbuddy_desktop.time.sleep", lambda _: None)
    monkeypatch.setattr("workbuddy_desktop.time.monotonic", iter([0, 1, 2, 3, 4, 5, 46]).__next__)
    window_states = iter([[], [desktop.window]])
    monkeypatch.setattr(sys.modules["pywinauto"], "Desktop", lambda **kwargs: SimpleNamespace(windows=lambda **kw: next(window_states)))
    window = desktop.adapter._window()
    assert window is desktop.window
    assert launched


def test_running_tray_process_is_not_launched_again(desktop, monkeypatch):
    monkeypatch.setattr("workbuddy_desktop.subprocess.run", lambda *a, **k: SimpleNamespace(stdout='"WorkBuddy.exe","123"'))
    launched = []
    monkeypatch.setattr("workbuddy_desktop.subprocess.Popen", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(sys.modules["pywinauto"], "Desktop", lambda **kwargs: SimpleNamespace(windows=lambda **kwargs: []))
    with pytest.raises(RuntimeError, match="仍在运行"):
        desktop.adapter._window()
    assert not launched


def test_update_installer_blocks_duplicate_start(desktop, monkeypatch):
    monkeypatch.setattr("workbuddy_desktop.subprocess.run", lambda *a, **k: SimpleNamespace(
        stdout='"WorkBuddy-Setup-5.9.3.exe","123"'))
    launched = []
    monkeypatch.setattr("workbuddy_desktop.subprocess.Popen", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(sys.modules["pywinauto"], "Desktop", lambda **kwargs: SimpleNamespace(windows=lambda **kwargs: []))
    with pytest.raises(RuntimeError, match="更新安装程序正在运行"):
        desktop.adapter._window()
    assert not launched


def test_focus_loss_blocks_keyboard_input(desktop, monkeypatch):
    monkeypatch.setattr(sys.modules["win32gui"], "GetForegroundWindow", lambda: 999)
    with pytest.raises(RuntimeError, match="前台焦点"):
        desktop.adapter.send("test")
    assert "type" not in desktop.calls


def test_stale_path_is_not_launched(desktop, monkeypatch):
    monkeypatch.setattr(sys.modules["pywinauto"], "Desktop", lambda **kwargs: SimpleNamespace(windows=lambda **kw: []))
    monkeypatch.setattr("workbuddy_desktop.subprocess.run", lambda *a, **k: SimpleNamespace(stdout=""))
    monkeypatch.setattr("workbuddy_desktop.client_path", lambda _: None)
    monkeypatch.setattr("workbuddy_desktop.subprocess.Popen", lambda *a, **k: pytest.fail("stale path was launched"))
    with pytest.raises(RuntimeError, match="客户端路径无效"):
        desktop.adapter._window()


def test_focus_loss_mid_prompt_keeps_partial_draft_and_does_not_send(desktop, monkeypatch):
    focus = iter([123, 999])
    monkeypatch.setattr(sys.modules["win32gui"], "GetForegroundWindow", lambda: next(focus))
    with pytest.raises(RuntimeError, match="前台焦点"):
        desktop.adapter.send("x" * 45)
    assert desktop.editor.value == "x" * 20
    assert desktop.calls == ["click", "type"]


def test_hidden_project_tab_does_not_authorize_input(desktop):
    desktop.window.tabs[0].is_visible = lambda: False
    with pytest.raises(RuntimeError, match="未确认项目身份"):
        desktop.adapter.send("test")
    assert not desktop.calls


def test_submission_confirmation_reobserves_transient_value_pattern(desktop):
    original=desktop.editor.get_value;calls=[]
    def get_value():
        calls.append(1)
        if len(calls)==1:raise RuntimeError('transient Value pattern unavailable')
        return original()
    desktop.editor.get_value=get_value
    desktop.window.messages=[type(desktop.editor)(name='abc')]
    desktop.adapter._wait_for_submitted_message(desktop.window,None,'abc',timeout_seconds=1)
    assert len(calls)==2 and not desktop.calls


def test_reconcile_existing_submission_requires_configured_project(desktop,monkeypatch):
    monkeypatch.setattr(desktop.adapter,'_window',lambda:desktop.window)
    with pytest.raises(RuntimeError,match='未确认当前对话'):
        desktop.adapter.confirm_existing_submission('abc')
    assert not desktop.calls


def test_reconcile_existing_submission_never_clicks_send(desktop,monkeypatch):
    monkeypatch.setattr(desktop.adapter,'_window',lambda:desktop.window)
    desktop.window.links=[type(desktop.editor)(name='试剂审批')]
    desktop.window.messages=[type(desktop.editor)(name='abc')]
    desktop.adapter.confirm_existing_submission('abc')
    assert not desktop.calls
