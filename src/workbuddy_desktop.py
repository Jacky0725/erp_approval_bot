"""Minimal Windows adapter: visible WorkBuddy project, real keyboard input.

Start from the configured project or one of its chats. Never changes permissions,
uses private app APIs, or silently sends to another project.
"""
from __future__ import annotations

import time
import os
import re
from typing import Any


def literal_keys(value: str) -> str:
    return "".join("{" + char + "}" if char in "{}+^%~()" else char for char in value)


def editor_value(value: str) -> str:
    value = value.strip("\ufeff \n\r")
    # This version exposes the empty editor's placeholder through UIA Value.
    if value in {
        "今天帮你做些什么？@ 引用资产文件、项目待办或调用技能",
        "今天帮你做些什么？@ 添加上下文，/调用技能与指令",
    }:
        return ""
    return value


class WorkBuddyDesktop:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def _window(self):
        try:
            from pywinauto import Desktop
            from pywinauto.application import process_module
        except ImportError as error:
            raise RuntimeError("WorkBuddy 自动投递需要 Windows pywinauto，请安装项目 requirements.txt。") from error
        executable = str(self.config.get("executable") or "").strip()
        if not executable:
            raise RuntimeError("请在 workbuddy.executable 配置实际 WorkBuddy.exe 路径。")
        # Electron has several processes with the same executable. Connecting
        # by path may select a renderer with no top-level window.
        expected = os.path.normcase(os.path.abspath(executable))
        windows = []
        for candidate in Desktop(backend="uia").windows(title="WorkBuddy", visible_only=True):
            actual = os.path.normcase(os.path.abspath(process_module(candidate.process_id())))
            if actual == expected:
                windows.append(candidate)
        if len(windows) != 1:
            raise RuntimeError("无法唯一定位 WorkBuddy 主窗口。")
        return windows[0]

    def wait_until_idle(self, timeout_seconds: int = 900) -> None:
        """Published XLSX may precede the agent's final verification commands."""
        window = self._window()
        deadline = time.monotonic() + timeout_seconds
        stable = 0
        while time.monotonic() < deadline:
            busy = any(re.match(r"^已处理\s*\d", e.window_text()) for e in window.descendants(control_type="Text") if e.is_visible())
            stable = 0 if busy else stable + 1
            if stable >= 2:
                return
            time.sleep(0.5)
        raise RuntimeError("结果文件已生成，但 WorkBuddy 尚未结束任务；保留文件，未处理下一批。")

    def send(self, prompt: str) -> None:
        window = self._window()
        from pywinauto.keyboard import send_keys
        from win32gui import GetForegroundWindow
        def require_focus():
            if GetForegroundWindow() != window.handle:
                raise RuntimeError("WorkBuddy 已失去前台焦点，停止输入；请核对草稿，未点击发送。")
        window.set_focus()
        project = str(self.config.get("project_name") or "试剂审批")
        links = [e for e in window.descendants(title=project, control_type="Hyperlink") if e.is_visible()]
        project_tabs = [e for e in window.descendants(title=project, control_type="Tab") if e.is_visible()]
        if len(links) == 1 and not self.config.get("reuse_chat", False):
            links[0].click_input()
            deadline = time.monotonic() + 15
            stable = 0
            while time.monotonic() < deadline:
                project_tabs = [e for e in window.descendants(title=project, control_type="Tab") if e.is_visible()]
                still_chat = any(e.is_visible() for e in window.descendants(title=project, control_type="Hyperlink"))
                stable = stable + 1 if project_tabs and not still_chat else 0
                if stable >= 2:
                    break
                time.sleep(0.3)
            else:
                raise RuntimeError("项目页面切换尚未完成，未输入或发送。")
        if not project_tabs and len(window.descendants(title=project, control_type="Hyperlink")) == 1:
            print("[workbuddy] 项目新建入口未就绪，复用当前项目聊天，串行处理。")
        elif not project_tabs:
            raise RuntimeError(f"请先打开 WorkBuddy 项目“{project}”；未确认项目身份，未发送。")
        editors = [e for e in window.descendants(control_type="Edit") if e.is_visible() and not e.window_text().startswith("搜索")]
        if len(editors) != 1:
            raise RuntimeError("项目任务输入框不唯一，未发送。")
        editor = editors[0]
        if editor_value(editor.get_value()):
            raise RuntimeError("WorkBuddy 输入框已有草稿，未覆盖或发送。")
        editor.click_input()
        # UIA set_value changes visible text without updating the rich editor's
        # application state. Real keyboard input was verified in the desktop pilot.
        # Enter may submit the rich editor. Type one paragraph, then click Send.
        prompt = " ".join(prompt.splitlines())
        for offset in range(0, len(prompt), 20):
            require_focus()
            send_keys(literal_keys(prompt[offset:offset + 20]), with_spaces=True, pause=0.001)
        deadline = time.monotonic() + 5
        while True:
            current = editor_value(editor.get_value())
            if "".join(current.split()) == "".join(prompt.split()):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("输入内容核对失败，保留草稿，未点击发送。")
            time.sleep(0.2)
        if not current:
            raise RuntimeError("输入内容核对失败，保留草稿，未点击发送。")
        rect = editor.rectangle()
        buttons = [b for b in window.descendants(control_type="Button") if not b.window_text() and b.is_visible() and b.is_enabled()]
        candidates = []
        for button in buttons:
            center = button.rectangle().mid_point()
            if rect.right - 90 <= center.x <= rect.right + 50 and rect.top <= center.y <= rect.bottom + 90:
                candidates.append(button)
        if len(candidates) != 1:
            raise RuntimeError("发送按钮不唯一或不可用，保留草稿，未发送。")
        require_focus()
        candidates[0].click_input()
        # Caller saves dispatch state BEFORE entry and never retries uncertain sends.
