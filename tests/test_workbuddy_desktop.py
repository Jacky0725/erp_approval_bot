"""Check the adapter without sending Windows input or starting WorkBuddy."""
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workbuddy_desktop import WorkBuddyDesktop, editor_value


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
        def process_id(self): return 123
        def set_focus(self): pass
        def descendants(self, **kwargs):
            return {"Tab": self.tabs, "Hyperlink": [], "Edit": [Control("搜索任务标题"), editor], "Button": [button]}.get(kwargs["control_type"], [])
    window = Window()
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
    return SimpleNamespace(adapter=WorkBuddyDesktop({"executable": "WorkBuddy.exe"}), editor=editor, button=button, window=window, calls=calls)


def test_keyboard_input_and_single_send(desktop):
    desktop.adapter.send("本批说明\n文件路径")
    assert desktop.editor.value == "本批说明 文件路径"
    assert desktop.calls == ["click", "type", "click"]


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


def test_disabled_send_not_clicked(desktop):
    desktop.button.enabled = False
    with pytest.raises(RuntimeError, match="发送按钮"):
        desktop.adapter.send("test")
    assert desktop.calls == ["click", "type"]


def test_window_from_different_executable_never_used(desktop, monkeypatch):
    monkeypatch.setattr(sys.modules["pywinauto.application"], "process_module", lambda pid: "Other.exe")
    with pytest.raises(RuntimeError, match="主窗口"):
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
    desktop.adapter.wait_until_idle()
    assert checked == [True, True, False, False]


def test_focus_loss_blocks_keyboard_input(desktop, monkeypatch):
    monkeypatch.setattr(sys.modules["win32gui"], "GetForegroundWindow", lambda: 999)
    with pytest.raises(RuntimeError, match="前台焦点"):
        desktop.adapter.send("test")
    assert "type" not in desktop.calls


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
