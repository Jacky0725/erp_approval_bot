"""Minimal Windows adapter: visible WorkBuddy project, real keyboard input.

Start from the configured project or one of its chats. Never changes permissions,
uses private app APIs, or silently sends to another project.
"""
from __future__ import annotations

import time
import os
import csv
import subprocess
import ctypes
from typing import Any, Callable
from task_control import checkpoint


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

    @staticmethod
    def _security_context(process_id: int) -> dict[str, Any]:
        """Read identity, session, and integrity without changing permissions."""
        import win32api
        import win32con
        import win32security

        current = process_id == os.getpid()
        process = win32api.GetCurrentProcess() if current else win32api.OpenProcess(
            getattr(win32con, "PROCESS_QUERY_LIMITED_INFORMATION", 0x1000), False, process_id
        )
        token = None
        try:
            token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
            user_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            integrity_sid = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)[0]
            session_id = ctypes.c_uint32()
            process_id_to_session = ctypes.WinDLL("kernel32", use_last_error=True).ProcessIdToSessionId
            process_id_to_session.argtypes = (ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32))
            process_id_to_session.restype = ctypes.c_int
            if not process_id_to_session(process_id, ctypes.byref(session_id)):
                raise ctypes.WinError(ctypes.get_last_error())
            return {
                "user_sid": win32security.ConvertSidToStringSid(user_sid),
                "session_id": session_id.value,
                "integrity": int(win32security.ConvertSidToStringSid(integrity_sid).rsplit("-", 1)[-1]),
            }
        finally:
            if token is not None:
                win32api.CloseHandle(token)
            if not current:
                win32api.CloseHandle(process)

    @staticmethod
    def _integrity_label(level: int) -> str:
        if level < 0x2000:
            return f"低完整性({level:#x})"
        if level < 0x3000:
            return f"普通权限({level:#x})"
        if level < 0x4000:
            return f"高权限({level:#x})"
        return f"系统级({level:#x})"

    def _require_matching_security_context(self, process_id: int) -> None:
        try:
            current = self._security_context(os.getpid())
            target = self._security_context(process_id)
        except Exception as error:
            raise RuntimeError(f"无法读取项目或 WorkBuddy 的用户、会话或权限级别；停止操作：{error}") from error
        if current["user_sid"] != target["user_sid"]:
            raise RuntimeError("WorkBuddy 与项目运行用户不同；请使用同一 Windows 用户启动，停止输入。")
        if current["session_id"] != target["session_id"]:
            raise RuntimeError("WorkBuddy 与项目不在同一桌面会话；请在当前登录会话启动客户端，停止输入。")
        if current["integrity"] != target["integrity"]:
            raise RuntimeError(
                "WorkBuddy 与项目权限级别不一致：项目为"
                f"{self._integrity_label(current['integrity'])}，WorkBuddy 为"
                f"{self._integrity_label(target['integrity'])}。请从统一入口启动；"
                "程序不会自动提权或结束客户端。"
            )
        if current["integrity"] >= 0x3000:
            raise RuntimeError("项目和 WorkBuddy 当前都以高权限运行；为减少 ERP 自动化进程权限，已暂停，请改用普通权限入口。")

    def _activate_window(self, window):
        self._require_matching_security_context(window.process_id())
        try:
            if hasattr(window, "is_minimized") and window.is_minimized():
                window.restore()
            if hasattr(window, "maximize"):
                window.maximize()
            window.set_focus()
        except Exception as error:
            raise RuntimeError(f"WorkBuddy 主窗口无法恢复或置于可操作位置：{error}") from error
        self._wait_for_client_ready(window)
        return window

    def _wait_for_client_ready(self, window) -> None:
        """A native Electron window alone is not a loaded client."""
        deadline = time.monotonic() + int(self.config.get("startup_timeout_seconds", 45))
        stable = 0
        observed = "尚未检查窗口"
        while time.monotonic() < deadline:
            try:
                rect = window.rectangle()
                visible = window.is_visible()
                minimized = window.is_minimized()
                editors = [e for e in window.descendants(control_type="Edit") if e.is_visible()]
                observed = (f"pid={window.process_id()}, handle={window.handle}, visible={visible}, "
                            f"minimized={minimized}, rect={rect}, input_controls={len(editors)}")
                ready = visible and not minimized and rect.width() > 0 and rect.height() > 0 and bool(editors)
                stable = stable + 1 if ready else 0
                if stable >= 5:
                    print(f"[workbuddy] 客户端输入界面已连续就绪：{observed}", flush=True)
                    return
            except Exception as error:
                stable = 0
                observed = f"窗口已消失或不可读取：{type(error).__name__}: {error}"
            time.sleep(0.5)
        raise RuntimeError(
            f"WorkBuddy 窗口出现过，但客户端输入界面未持续就绪；{observed}。"
            "可能仍在加载、登录或更新；未发送任务，请查看客户端状态。"
        )

    def _restore_taskbar_or_tray(self, desktop) -> bool:
        """Restore one uniquely named WorkBuddy taskbar or notification icon."""
        try:
            shell = desktop.window(class_name="Shell_TrayWnd")
            buttons = [
                control for control in shell.descendants(control_type="Button")
                if control.is_visible()
                and control.friendly_class_name() == "Button"
                and control.element_info.class_name == "Taskbar.TaskListButtonAutomationPeer"
                and control.window_text().strip().casefold().startswith("workbuddy")
            ]
            if len(buttons) == 1:
                buttons[0].click_input()
                return True

            overflow = [
                control for control in shell.descendants(control_type="Button")
                if control.is_visible() and any(label in control.window_text().casefold()
                    for label in ("显示隐藏的图标", "show hidden icons"))
            ]
            if len(overflow) != 1:
                return False
            overflow[0].click_input()
            time.sleep(0.4)
            tray_icons = []
            for popup in desktop.windows(visible_only=True):
                for control in popup.descendants(control_type="Button"):
                    try:
                        if (control.is_visible() and "workbuddy" in control.window_text().casefold()
                                and "tasklistbuttonautomationpeer" not in control.element_info.class_name.casefold()):
                            tray_icons.append(control)
                    except Exception:
                        continue
            if len(tray_icons) == 1:
                tray_icons[0].click_input()
                return True
        except Exception:
            return False
        return False

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
        desktop = Desktop(backend="uia")
        candidates = desktop.windows(title="WorkBuddy", visible_only=False)
        windows = []
        diagnostics = []
        for candidate in candidates:
            try:
                process_id = candidate.process_id()
                actual_path = process_module(process_id)
                actual = os.path.normcase(os.path.abspath(actual_path))
                diagnostics.append(f"pid={process_id}, path={actual_path!r}, handle={candidate.handle}")
                if actual == expected:
                    windows.append(candidate)
            except Exception as error:
                diagnostics.append(f"pid=?, path lookup failed: {type(error).__name__}: {error}")
        if len(windows) > 1:
            raise RuntimeError(
                f"无法唯一定位 WorkBuddy 主窗口（标题候选 {len(candidates)} 个，路径匹配 {len(windows)} 个；"
                f"配置路径={executable!r}；候选：{' | '.join(diagnostics) or '无'}）。"
                "检测到多个匹配窗口，无法安全选择。"
            )
        if windows:
            return self._activate_window(windows[0])

        # Avoid launching a second Electron instance when it is already running
        # in the tray or has a startup dialog without the expected title.
        try:
            listing = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                text=True, encoding="mbcs", errors="replace", timeout=10, check=True,
            ).stdout
            running_pids = []
            for row in csv.reader(listing.splitlines()):
                if row and row[0].casefold().startswith("workbuddy-setup-") and row[0].casefold().endswith(".exe"):
                    raise RuntimeError("检测到 WorkBuddy 更新安装程序正在运行；等待更新完成后再试，未启动重复客户端。")
                if not row or os.path.normcase(row[0]) != os.path.normcase(os.path.basename(executable)):
                    continue
                try:
                    pid = int(row[1])
                    if os.path.normcase(os.path.abspath(process_module(pid))) == expected:
                        running_pids.append(pid)
                except Exception:
                    # Same-name processes with an unreadable path are ambiguous.
                    running_pids.append(-1)
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(f"无法确认 WorkBuddy 是否已在后台运行，停止自动启动以避免重复实例：{error}") from error
        if running_pids:
            if self._restore_taskbar_or_tray(desktop):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    time.sleep(0.4)
                    for candidate in desktop.windows(title="WorkBuddy", visible_only=False):
                        try:
                            if os.path.normcase(os.path.abspath(process_module(candidate.process_id()))) == expected:
                                print("[workbuddy] 已从任务栏/通知区域恢复现有客户端。", flush=True)
                                return self._activate_window(candidate)
                        except RuntimeError:
                            raise
                        except Exception:
                            continue
            if -1 in running_pids:
                raise RuntimeError("检测到同名 WorkBuddy 进程，但无法读取其程序路径；为避免启动重复客户端，已暂停。")
            raise RuntimeError("WorkBuddy 进程仍在运行，但任务栏/通知区域未能恢复可操作主窗口；请检查登录或弹窗。")
        print(f"[workbuddy] 客户端未启动，按当前项目用户权限启动：{executable}", flush=True)
        try:
            launched = subprocess.Popen([executable], cwd=os.path.dirname(executable) or None,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
        except Exception as error:
            raise RuntimeError(f"WorkBuddy 自动启动失败：{error}") from error
        deadline = time.monotonic() + max(5, int(self.config.get("startup_timeout_seconds", 45)))
        while time.monotonic() < deadline:
            time.sleep(1)
            candidates = desktop.windows(title="WorkBuddy", visible_only=False)
            for candidate in candidates:
                try:
                    matches = os.path.normcase(os.path.abspath(process_module(candidate.process_id()))) == expected
                except Exception:
                    continue
                if matches:
                    try:
                        self._activate_window(candidate)
                    except RuntimeError as error:
                        raise RuntimeError(f"{error} 启动进程 pid={launched.pid}, exit_code={launched.poll()}。") from error
                    print("[workbuddy] 客户端已启动，窗口可见且输入界面持续就绪。", flush=True)
                    return candidate
        raise RuntimeError(
            f"WorkBuddy 启动未就绪：pid={launched.pid}, exit_code={launched.poll()}；"
            "等待时间内未找到可操作主窗口，可能正在更新或已退出；未发送任务。"
        )

    @staticmethod
    def _visible_text(window) -> str:
        texts = []
        for kind in ("Text", "Document"):
            try:
                texts.extend(e.window_text() for e in window.descendants(control_type=kind) if e.is_visible())
            except Exception:
                continue
        return "\n".join(texts)

    def _wait_for_submitted_message(self, window, editor, batch_id: str, timeout_seconds: int = 12) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_error = ""
        while time.monotonic() < deadline:
            # A new task replaces the project composer; inspect the current
            # composer rather than the stale UIA reference passed by send().
            try:
                editors = [e for e in window.descendants(control_type="Edit") if e.is_visible()
                           and not e.window_text().startswith(("搜索", "当前页码"))]
                if len(editors) == 1 and not editor_value(editors[0].get_value()) and batch_id in self._visible_text(window):
                    return
            except Exception as error:
                # Electron can replace the current composer between enumeration
                # and Value access. Reobserve; do not click Send a second time.
                last_error = type(error).__name__
            time.sleep(0.25)
        raise RuntimeError(f"发送按钮已点击，但未确认批次 {batch_id} 出现在对话中；发送状态不明，禁止自动重发。最后控件错误：{last_error or '无'}")

    def wait_for_batch_completion(self, batch_id: str, timeout_seconds: int = 900) -> None:
        """Require a positive completion message that names this unique batch."""
        window = self._window()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            checkpoint("等待 WorkBuddy 界面完成确认", batch_id=batch_id)
            if f"【批次完成】{batch_id}" in self._visible_text(window):
                return
            time.sleep(0.5)
        raise RuntimeError(f"已发现批次 {batch_id} 的结果文件，但界面未出现对应的完成确认；保留文件并暂停循环。")

    def confirm_existing_submission(self, batch_id: str) -> None:
        """Read-only reconciliation of a request whose Send was already clicked."""
        window = self._window()
        project = str(self.config.get("project_name") or "试剂审批")
        if not any(e.is_visible() for e in window.descendants(title=project, control_type="Hyperlink")):
            raise RuntimeError("未确认当前对话属于配置项目，未恢复或重发任务。")
        self._wait_for_submitted_message(window, None, batch_id,
                                         timeout_seconds=int(self.config.get("submission_timeout_seconds", 12)))

    def _project_list_entry(self, window, project):
        # Sidebar project headings are labels, not navigation links.
        anchors = [e for e in window.descendants(title="新建项目", control_type="Button") if e.is_visible()]
        if not anchors:
            more = [e for e in window.descendants(title="更多", control_type="TabItem") if e.is_visible()]
            if len(more) != 1:
                return []
            more[0].click_input()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                menus = [e for e in window.descendants(control_type="MenuItem")
                         if e.is_visible() and e.window_text() in {"项目", "项目 BETA"}]
                if len(menus) == 1:
                    menus[0].click_input()
                    break
                time.sleep(0.3)
            while time.monotonic() < deadline:
                anchors = [e for e in window.descendants(title="新建项目", control_type="Button") if e.is_visible()]
                if anchors:
                    break
                time.sleep(0.3)
        if len(anchors) != 1:
            return []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            cards = [e for e in window.descendants(title=project, control_type="Text")
                     if e.is_visible() and e.window_text() == project
                     and e.rectangle().left >= anchors[0].rectangle().left]
            if cards:
                return cards
            time.sleep(0.3)
        return []

    def _try_sidebar_project(self, window, project) -> bool:
        sidebar = [e for e in window.descendants(title="更多", control_type="TabItem") if e.is_visible()]
        if len(sidebar) != 1:
            return False
        headings = [e for e in window.descendants(title=project, control_type="Text")
                    if e.is_visible() and e.rectangle().right <= sidebar[0].rectangle().right]
        if len(headings) != 1:
            return False
        rect = headings[0].rectangle()
        outer = window.rectangle()
        window.move_mouse_input(coords=(rect.mid_point().x - outer.left, rect.mid_point().y - outer.top))
        time.sleep(0.3)
        buttons = [e for e in window.descendants(title="新建任务", control_type="Button")
                   if e.is_visible() and e.rectangle().right <= sidebar[0].rectangle().right
                   and abs(e.rectangle().mid_point().y - rect.mid_point().y) <= 12]
        if len(buttons) != 1:
            return False
        buttons[0].click_input()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if any(e.is_visible() for kind in ("Hyperlink", "Tab")
                   for e in window.descendants(title=project, control_type=kind)):
                print(f"[workbuddy] 已通过左侧项目快捷入口进入项目：{project}", flush=True)
                return True
            time.sleep(0.3)
        raise RuntimeError("左侧项目新任务已点击，但未确认项目身份；停止，未发送。")

    def _open_project(self, window) -> None:
        """Navigate without typing; require the resulting project tab."""
        project = str(self.config.get("project_name") or "试剂审批")
        links = [e for e in window.descendants(title=project, control_type="Hyperlink") if e.is_visible()]
        project_tabs = [e for e in window.descendants(title=project, control_type="Tab") if e.is_visible()]
        if not project_tabs and not links:
            if self._try_sidebar_project(window, project):
                return
            links = self._project_list_entry(window, project)
        if not project_tabs and len(links) == 1:
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
        if not project_tabs:
            raise RuntimeError(f"请先打开 WorkBuddy 项目“{project}”；未确认项目身份，未发送。")

    def _wait_for_project_editor(self, window, *, allow_navigation: bool = True):
        """Wait for one stable editor; retry navigation only before any input."""
        last = "未采样"
        for attempt in range(2 if allow_navigation else 1):
            if attempt:
                print(f"[workbuddy] 输入框识别未就绪，重新检查项目（2/2）：{last}", flush=True)
                self._open_project(window)
            deadline = time.monotonic() + max(1, int(self.config.get("editor_ready_timeout_seconds", 8)))
            stable = 0
            previous = None
            while time.monotonic() < deadline:
                controls = [e for e in window.descendants(control_type="Edit") if e.is_visible()]
                editors = [e for e in controls if not e.window_text().startswith(("搜索", "当前页码"))]
                details = []
                for e in controls:
                    try:
                        kind = "search" if e.window_text().startswith("搜索") else "pagination" if e.window_text().startswith("当前页码") else "candidate"
                        details.append(f"class={e.element_info.class_name!r}, rect={e.rectangle()}, kind={kind}")
                    except Exception:
                        details.append("控件信息不可读取")
                last = (f"pid={window.process_id()}, handle={window.handle}, rect={window.rectangle()}, "
                        f"visible_edits={len(controls)}, candidates={len(editors)}, controls={details}")
                signature = str(editors[0].rectangle()) if len(editors) == 1 else None
                stable = stable + 1 if signature is not None and signature == previous else (1 if signature else 0)
                previous = signature
                if stable >= 3:
                    return editors[0]
                time.sleep(0.3)
        raise RuntimeError(f"项目任务输入框未稳定为唯一候选，未发送；{last}")

    def _is_completed_chat(self, window, batch_id: str) -> bool:
        project = str(self.config.get("project_name") or "试剂审批")
        try:
            links = [e for e in window.descendants(title=project, control_type="Hyperlink") if e.is_visible()]
            return len(links) == 1 and f"【批次完成】{batch_id}" in self._visible_text(window).splitlines()
        except (OSError, RuntimeError):
            return False

    def send(self, prompt: str, *, batch_id: str | None = None,
             before_submit: Callable[[], None] | None = None,
             reuse: tuple[str, str] | None = None) -> bool:
        """Return whether the follow-up was sent in the verified previous chat."""
        window = self._window()
        from pywinauto.keyboard import send_keys
        from win32gui import GetForegroundWindow
        def require_focus():
            if GetForegroundWindow() != window.handle:
                raise RuntimeError("WorkBuddy 已失去前台焦点，停止输入；请核对草稿，未点击发送。")
        window.set_focus()
        project = str(self.config.get("project_name") or "试剂审批")
        editor = None
        if reuse is not None and self._is_completed_chat(window, reuse[0]):
            try:
                editor = self._wait_for_project_editor(window, allow_navigation=False)
            except (OSError, RuntimeError):
                print("[workbuddy] 原会话输入框未就绪，改用新会话。", flush=True)
            if editor is not None and not self._is_completed_chat(window, reuse[0]):
                editor = None
        reused = editor is not None
        if reused:
            prompt = reuse[1]
        else:
            if reuse is not None:
                print("[workbuddy] 未能确认原会话可续聊，新开任务并完整读取规则。", flush=True)
            self._open_project(window)
            editor = self._wait_for_project_editor(window)
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
        deadline = time.monotonic() + int(self.config.get("input_settle_timeout_seconds", 15))
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
        if reused and not self._is_completed_chat(window, reuse[0]):
            raise RuntimeError("续聊会话在输入后发生变化，保留草稿，未发送。")
        # Persist the uncertain-send guard only once all pre-send checks pass.
        # Failure to persist it must prevent the submit click.
        if before_submit is not None:
            before_submit()
        require_focus()
        print(f"[workbuddy] 已核对项目“{project}”及输入内容，准备提交任务。", flush=True)
        candidates[0].click_input()
        print("[workbuddy] 已点击发送，等待本批结果文件。", flush=True)
        if batch_id:
            self._wait_for_submitted_message(
                window, editor, batch_id,
                timeout_seconds=int(self.config.get("submission_timeout_seconds", 12)),
            )
            print(f"[workbuddy] 批次 {batch_id} 已出现在当前对话，发送确认通过。", flush=True)
        # A failed click can be uncertain; the caller must never auto-resend it.
        return reused
