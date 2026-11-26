"""Unit tests for the read-only headless CodeBuddy CLI adapter."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from task_control import SafeStop
from workbuddy_bridge import WorkBuddyBridge
import workbuddy_headless as wh


def _make_fake_cli_env(tmp_path: Path) -> dict[str, Any]:
    node = tmp_path / "node.exe"
    node.write_bytes(b"fake-node")
    cli = tmp_path / "codebuddy"
    cli.write_text("// fake cli", encoding="utf-8")
    creds = tmp_path / ".codebuddy"
    creds.mkdir(exist_ok=True)
    return {
        "headless": {
            "enabled": True,
            "node_executable": str(node),
            "cli_script": str(cli),
            "credentials_dir": str(creds),
            "timeout_seconds": 30,
        }
    }


def _make_minimal_rules_workbook(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "rules"
    ws.append(["rule_id", "category", "condition"])
    ws.append(["R1", "易燃液体", "flash_point < 60"])
    wb.save(path)


def _prepare_isolated_batch(tmp_path: Path) -> tuple[WorkBuddyBridge, dict[str, Any]]:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    _make_minimal_rules_workbook(tmp_path / "config" / "rules_structured.xlsx")
    settings = {
        "paths": {"structured_rules_excel": "config/rules_structured.xlsx"},
        "workbuddy": {
            "batch_size": 2,
            "batch_dir": "data/workbuddy",
            "compact_rules": True,
            "fixed_result_writer": True,
            **_make_fake_cli_env(tmp_path),
        },
    }
    bridge = WorkBuddyBridge(tmp_path, settings)
    bridge.publish_compact_rules()
    records = [
        {"清单号": "SJTEST01", "序号": "1", "_erp_record_id": "rec-1", "试剂名称": "示例试剂A", "CAS号": "64-17-5", "规格单位": "500ml", "试剂数量": "1"},
        {"清单号": "SJTEST01", "序号": "2", "_erp_record_id": "rec-2", "试剂名称": "示例试剂B", "CAS号": "7647-14-5", "规格单位": "500g", "试剂数量": "1"},
    ]
    state = bridge.prepare(records, compact=True)
    assert state is not None
    return bridge, state


class _FakePopen:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0, delay_seconds: float = 0.0, pid: int = 99999):
        self.pid = pid
        self.stdin = io.StringIO()
        self._stdin_captured = ""
        orig_close = self.stdin.close

        def _capture_close() -> None:
            self._stdin_captured = self.stdin.getvalue()
            orig_close()

        self.stdin.close = _capture_close  # type: ignore[method-assign]
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self._returncode = returncode
        self._started = time.monotonic()
        self._delay = delay_seconds
        self._terminated = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self._terminated:
            self.returncode = -1
            return self.returncode
        if time.monotonic() - self._started >= self._delay:
            self.returncode = self._returncode
            return self.returncode
        return None

    def terminate(self) -> None:
        self._terminated = True
        self.returncode = -1

    def kill(self) -> None:
        self._terminated = True
        self.returncode = -1

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._returncode if not self._terminated else -1
        return self.returncode


def test_preflight_checks_node_cli_and_credentials(tmp_path: Path) -> None:
    cfg = _make_fake_cli_env(tmp_path)
    assert wh.preflight(cfg) == ""

    missing_creds = {"headless": {**cfg["headless"], "credentials_dir": str(tmp_path / "missing")}}
    assert "登录目录" in wh.preflight(missing_creds)

    missing_cli = {"headless": {**cfg["headless"], "cli_script": str(tmp_path / "missing_cli")}}
    assert "未找到" in wh.preflight(missing_cli)


def test_build_command_enforces_readonly_tools_and_scoped_add_dir(tmp_path: Path) -> None:
    cfg = _make_fake_cli_env(tmp_path)
    cfg["headless"]["model"] = "hy4-preview"
    cfg["headless"]["fallback_model"] = "deepseek-v4.1-flash"
    adapter = wh.WorkBuddyHeadless(cfg)
    paths = wh.discover_headless_cli(cfg)
    assert paths is not None

    batch_dir = tmp_path / "batch_1"
    cmd = adapter.build_command(paths, batch_dir, use_json_schema=True)
    assert cmd[0] == str(paths.node_executable)
    assert cmd[1] == str(paths.cli_script)
    assert "--strict-mcp-config" in cmd
    assert "--no-session-persistence" in cmd
    assert "--json-schema" in cmd
    assert cmd[cmd.index("--add-dir") + 1] == str(batch_dir)
    tools = cmd[cmd.index("--tools") + 1]
    assert "StructuredOutput" in tools
    assert "Bash" not in tools and "Write" not in tools and "PowerShell" not in tools
    assert cmd[cmd.index("--model") + 1] == "hy4-preview"
    assert cmd[cmd.index("--fallback-model") + 1] == "deepseek-v4.1-flash"


def test_parse_cli_output_accepts_structured_output_and_plain_json() -> None:
    expected_ids = {"k1", "k2"}
    judgments = [
        {"记录ID": "k1", "物化特性类别": "易燃液体", "判断依据": "规则命中1"},
        {"记录ID": "k2", "物化特性类别": "普通类", "判断依据": "规则命中2"},
    ]
    structured_stdout = json.dumps([
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1234,
            "num_turns": 3,
            "permission_denials": [],
            "structured_output": {"batch_id": "B001", "judgments": judgments},
        },
    ], ensure_ascii=False)
    parsed, meta = wh.parse_cli_output(structured_stdout, expected_batch_id="B001", expected_record_ids=expected_ids)
    assert len(parsed) == 2
    assert meta["duration_ms"] == 1234
    assert meta["permission_denials_count"] == 0

    plain_stdout = json.dumps([
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "```json\n" + json.dumps({"batch_id": "B001", "judgments": judgments}, ensure_ascii=False) + "\n```",
        }
    ], ensure_ascii=False)
    parsed2, _ = wh.parse_cli_output(plain_stdout, expected_batch_id="B001", expected_record_ids=expected_ids)
    assert parsed2 == parsed


@pytest.mark.parametrize("bad_case", [
    "wrong_batch", "missing_record", "duplicate_record", "extra_record", "empty_category", "truncated_json",
])
def test_parse_cli_output_rejects_protocol_violations(bad_case: str) -> None:
    expected_ids = {"k1", "k2"}
    if bad_case == "truncated_json":
        with pytest.raises(wh.HeadlessError) as exc:
            wh.parse_cli_output('[{"type":"result", "result": "{broken', expected_batch_id="B1", expected_record_ids=expected_ids)
        assert exc.value.code == "protocol_error"
        return

    payload: dict[str, Any] = {
        "batch_id": "B2" if bad_case == "wrong_batch" else "B1",
        "judgments": [
            {"记录ID": "k1", "物化特性类别": "" if bad_case == "empty_category" else "易燃液体", "判断依据": "依据1"},
            {"记录ID": "k2", "物化特性类别": "普通类", "判断依据": "依据2"},
        ],
    }
    if bad_case == "missing_record":
        payload["judgments"].pop()
    elif bad_case == "duplicate_record":
        payload["judgments"][1]["记录ID"] = "k1"
    elif bad_case == "extra_record":
        payload["judgments"].append({"记录ID": "k999", "物化特性类别": "普通类", "判断依据": "跨批"})

    raw = json.dumps([{"type": "result", "is_error": False, "structured_output": payload}], ensure_ascii=False)
    with pytest.raises(wh.HeadlessError) as exc:
        wh.parse_cli_output(raw, expected_batch_id="B1", expected_record_ids=expected_ids)
    assert exc.value.code == "protocol_error"


@pytest.mark.parametrize("message,expected_code", [
    ("Error: 401 unauthorized - not logged in", "auth_failed"),
    ("HTTP 429 rate limit exceeded", "rate_limited"),
    ("Unexpected internal failure", "process_failed"),
])
def test_parse_cli_output_classifies_error_types(message: str, expected_code: str) -> None:
    raw = json.dumps([{"type": "result", "is_error": True, "result": message}], ensure_ascii=False)
    with pytest.raises(wh.HeadlessError) as exc:
        wh.parse_cli_output(raw, expected_batch_id="B1", expected_record_ids={"k1"})
    assert exc.value.code == expected_code


def test_execute_batch_passes_long_utf8_stdin_freezes_digest_and_publishes_result(tmp_path: Path, monkeypatch) -> None:
    bridge, state = _prepare_isolated_batch(tmp_path)
    keys = [item["key"] for item in state["items"]]
    fake_stdout = json.dumps([
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 800,
            "num_turns": 2,
            "permission_denials": [],
            "structured_output": {
                "batch_id": state["batch_id"],
                "judgments": [
                    {"记录ID": keys[0], "物化特性类别": "易燃液体", "判断依据": "脱敏依据A"},
                    {"记录ID": keys[1], "物化特性类别": "普通类", "判断依据": "脱敏依据B"},
                ],
            },
        }
    ], ensure_ascii=False)
    captured_popen: dict[str, Any] = {}
    fake_proc = _FakePopen(stdout=fake_stdout)

    def _factory(cmd, **kwargs):
        captured_popen["cmd"] = cmd
        captured_popen["kwargs"] = kwargs
        return fake_proc

    monkeypatch.setattr(wh, "_pid_is_alive", lambda _pid: False)
    adapter = wh.WorkBuddyHeadless(bridge.config, popen_factory=_factory, poll_interval_seconds=0.05)
    long_instructions = "规则说明：" + ("中文脱敏占位" * 6000)  # >36KB UTF-8 via stdin

    meta = adapter.execute_batch(bridge, state, instructions_text=long_instructions, check_stop=lambda: None)

    assert meta["duration_ms"] == 800
    assert len(fake_proc._stdin_captured.encode("utf-8")) > 40000
    assert state["dispatch_mode"] == "headless"
    assert state["headless_pid"] == 0
    assert state.get("judgments_sha256")
    batch_dir = bridge.batch_path(state)
    assert (batch_dir / "result.xlsx").is_file()

    # Verify published result.xlsx passes WorkBuddyBridge's full production validator
    bridge.load_results(state)
    categories = {item["input"]["试剂名称"]: item["result"]["物化特性类别"] for item in state["items"]}
    assert categories == {"示例试剂A": "易燃液体", "示例试剂B": "普通类"}


def test_execute_batch_timeout_and_safe_stop_terminate_process_tree(tmp_path: Path, monkeypatch) -> None:
    bridge, state = _prepare_isolated_batch(tmp_path)
    killed: list[int] = []
    monkeypatch.setattr(wh, "terminate_process_tree", lambda proc: (killed.append(proc.pid), proc.terminate()))
    monkeypatch.setattr(wh, "_pid_is_alive", lambda _pid: False)

    # 1. Timeout kills process tree
    slow_proc = _FakePopen(delay_seconds=10.0, pid=11111)
    adapter = wh.WorkBuddyHeadless(bridge.config, popen_factory=lambda *a, **k: slow_proc, poll_interval_seconds=0.05)
    with pytest.raises(wh.HeadlessError) as exc:
        adapter.execute_batch(bridge, state, timeout_seconds=1, check_stop=lambda: None)
    assert exc.value.code == "timeout"
    assert 11111 in killed

    # 2. SafeStop kills process tree and propagates SafeStop
    stop_proc = _FakePopen(delay_seconds=10.0, pid=22222)
    adapter2 = wh.WorkBuddyHeadless(bridge.config, popen_factory=lambda *a, **k: stop_proc, poll_interval_seconds=0.05)

    def _trigger_stop() -> None:
        raise SafeStop("operator requested stop")

    with pytest.raises(SafeStop):
        adapter2.execute_batch(bridge, state, timeout_seconds=10, check_stop=_trigger_stop)
    assert 22222 in killed


def test_freeze_and_publish_rejects_rule_tampering_and_frozen_digest_change(tmp_path: Path) -> None:
    bridge, state = _prepare_isolated_batch(tmp_path)
    keys = [item["key"] for item in state["items"]]
    judgments = [
        {"记录ID": keys[0], "物化特性类别": "易燃液体", "判断依据": "依据A"},
        {"记录ID": keys[1], "物化特性类别": "普通类", "判断依据": "依据B"},
    ]
    state["judgments_sha256"] = hashlib.sha256(b"other-frozen-content").hexdigest()
    with pytest.raises(wh.HeadlessError, match="冻结后发生变化"):
        wh.freeze_and_publish_headless_result(bridge, state, judgments)

    state.pop("judgments_sha256", None)
    (bridge.batch_path(state) / "rules.xlsx").write_bytes(b"tampered")
    with pytest.raises(wh.HeadlessError, match="规则快照"):
        wh.freeze_and_publish_headless_result(bridge, state, judgments)


# ---------------------------------------------------------------------------
# Phase 2: Dual-mode orchestration, crash recovery, scheduler, Web & DingTalk
# ---------------------------------------------------------------------------

def test_recover_frozen_headless_result_republishes_without_cli_and_rejects_tamper(tmp_path: Path) -> None:
    bridge, state = _prepare_isolated_batch(tmp_path)
    keys = [item["key"] for item in state["items"]]
    judgments = [
        {"记录ID": keys[0], "物化特性类别": "易燃液体", "判断依据": "冻结依据A"},
        {"记录ID": keys[1], "物化特性类别": "普通类", "判断依据": "冻结依据B"},
    ]
    payload = json.dumps(judgments, ensure_ascii=False, indent=2).encode("utf-8")
    batch_dir = bridge.batch_path(state)
    (batch_dir / "judgments.json").write_bytes(payload)
    state["judgments_sha256"] = hashlib.sha256(payload).hexdigest()
    bridge.save(state)

    assert not (batch_dir / "result.xlsx").exists()
    assert wh.recover_frozen_headless_result(bridge, state) is True
    assert (batch_dir / "result.xlsx").is_file()

    # Tampering frozen judgments.json must fail closed
    (batch_dir / "judgments.json").write_bytes(b"[]")
    with pytest.raises(wh.HeadlessError, match="摘要不匹配"):
        wh.recover_frozen_headless_result(bridge, state)


def test_scheduler_preflight_allows_headless_without_interactive_desktop(tmp_path: Path, monkeypatch) -> None:
    import scheduler as sched

    cfg = _make_fake_cli_env(tmp_path)
    settings = {
        "app": {"dry_run": False},
        "workbuddy": {
            "dispatch_mode": "headless",
            "desktop_enabled": False,
            "write_enabled": True,
            "executable": "",
            **cfg,
        },
        "erp_api": {"enabled_for_write": True, "discovery": {"status": "verified"}},
    }
    monkeypatch.setattr(sched, "interactive_desktop_available", lambda: False)
    options = {"APPROVAL_WRITE_MODE": "generate_library", "ERP_WRITE_BACKEND": "api_write_with_web_verify"}
    assert sched.workbuddy_preflight(settings, options) == ""

    # Headless default keeps auto_pass disabled until explicitly opted in
    manager_started: list[tuple[str, dict[str, Any]]] = []

    class _Manager:
        def status(self):
            return {"running": False}

        def start(self, action, opts):
            manager_started.append((action, dict(opts)))
            return {"started": True}

    settings["scheduler"] = {"enabled": True, "use_default_run_policy": False, "approval_write_mode": "generate_library"}
    s = sched.ApprovalScheduler(tmp_path, lambda: settings, _Manager())
    monkeypatch.setenv("APP_DRY_RUN", "false")
    res = s.trigger_due_run()
    assert res["started"] is True
    assert manager_started[0][0] == "workbuddy_loop"
    assert manager_started[0][1]["WORKBUDDY_AUTO_PASS"] == "false"
    assert manager_started[0][1]["AUTO_PASS"] == "false"

    # Explicit opt-in enables auto_pass in formal write mode
    settings["workbuddy"]["headless"]["auto_pass_enabled"] = True
    res2 = s.trigger_due_run()
    assert res2["started"] is True
    assert manager_started[1][1]["WORKBUDDY_AUTO_PASS"] == "true"
    assert manager_started[1][1]["AUTO_PASS"] == "true"


def test_web_and_dingtalk_respect_headless_mode_and_auto_pass_opt_in(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import Mock
    from fastapi.testclient import TestClient
    import web_app

    cfg = _make_fake_cli_env(tmp_path)
    settings = {
        "app": {"dry_run": False},
        "workbuddy": {
            "dispatch_mode": "headless",
            "desktop_enabled": False,
            "write_enabled": True,
            "executable": "",
            "fixed_result_writer": True,
            "accepted_batch_sizes": [20, 30, 50],
            **cfg,
        },
        "erp_api": {"enabled_for_write": True, "discovery": {"status": "verified"}},
    }
    monkeypatch.setenv("APP_DRY_RUN", "false")
    monkeypatch.setattr(web_app, "load_settings", lambda: settings)
    monkeypatch.setattr(web_app, "runtime_config_snapshot", lambda: {
        "approval_write_mode": "generate_library",
        "erp_write_backend": "api_write_with_web_verify",
    })

    # DingTalk options succeed in headless mode without desktop_enabled / WorkBuddy.exe, and default auto_pass=false
    dt_opts = web_app.dingtalk_run_options()
    assert dt_opts["WORKBUDDY_AUTO_PASS"] == "false"
    assert dt_opts["AUTO_PASS"] == "false"

    # Web UI /api/run also keeps WORKBUDDY_AUTO_PASS=false by default in headless mode
    start = Mock(return_value={"started": True})
    monkeypatch.setattr(web_app.manager, "start", start)
    resp = TestClient(web_app.app).post("/api/run", data={"action": "workbuddy_loop", "approval_write_mode": "generate_library"})
    assert resp.status_code == 200
    assert start.call_args.args[1]["WORKBUDDY_AUTO_PASS"] == "false"

    # Explicit opt-in unlocks auto_pass for both DingTalk and Web UI
    settings["workbuddy"]["headless"]["auto_pass_enabled"] = True
    assert web_app.dingtalk_run_options()["WORKBUDDY_AUTO_PASS"] == "true"
    resp2 = TestClient(web_app.app).post("/api/run", data={"action": "workbuddy_loop", "approval_write_mode": "generate_library"})
    assert resp2.status_code == 200
    assert start.call_args.args[1]["WORKBUDDY_AUTO_PASS"] == "true"


def test_approval_flow_headless_recovery_and_failure_isolation(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock
    import approval_flow
    from browser_bot import BrowserBot

    bridge, state = _prepare_isolated_batch(tmp_path)
    keys = [item["key"] for item in state["items"]]
    judgments = [
        {"记录ID": keys[0], "物化特性类别": "易燃液体", "判断依据": "崩溃前已冻结依据A"},
        {"记录ID": keys[1], "物化特性类别": "普通类", "判断依据": "崩溃前已冻结依据B"},
    ]
    # Simulate crash right after judgments.json was frozen and result.xlsx published, before dispatch_state="submitted"
    wh.freeze_and_publish_headless_result(bridge, state, judgments)
    state["dispatch_mode"] = "headless"
    state["dispatch_state"] = "sending"
    bridge.save(state)

    # Even if global settings switched back to desktop, in-flight headless batch recovers via headless path without invoking CLI or Desktop
    settings = {
        "paths": {"structured_rules_excel": "config/rules_structured.xlsx"},
        "app": {"dry_run": True},
        "workbuddy": {
            "dispatch_mode": "desktop",
            "desktop_enabled": True,
            "batch_size": 2,
            "batch_dir": "data/workbuddy",
            "compact_rules": True,
            "fixed_result_writer": True,
            **_make_fake_cli_env(tmp_path),
        },
    }
    bot = BrowserBot(settings=settings, root_dir=tmp_path)
    bot.target_list_numbers = ["SJTEST01"]
    bot.dry_run_enabled = lambda: True
    bot.approval_write_mode = lambda: "disabled"
    bot.run_after_login_capture = lambda _png, _html, cb: cb(object())

    client = SimpleNamespace(
        protocol="odoo_jsonrpc",
        write_enabled=lambda: False,
        fetch_reagent_detail=lambda _n: [],
    )
    monkeypatch.setattr(approval_flow, "ErpApiClient", lambda *_: client)
    cli_called = Mock(side_effect=AssertionError("CLI must not be re-invoked when result.xlsx already exists"))
    monkeypatch.setattr(wh.WorkBuddyHeadless, "execute_batch", cli_called)
    desktop_send = Mock(side_effect=AssertionError("Desktop send must not be called for in-flight headless batch"))
    import workbuddy_desktop
    monkeypatch.setattr(workbuddy_desktop.WorkBuddyDesktop, "send", desktop_send)
    monkeypatch.setattr(workbuddy_desktop.WorkBuddyDesktop, "confirm_existing_submission", desktop_send)

    bot.run_workbuddy_loop()

    cli_called.assert_not_called()
    desktop_send.assert_not_called()
    reloaded = bridge.batches()[0]
    assert reloaded["dispatch_state"] == "submitted"
    assert reloaded["status"] == "imported"


def test_packaged_runtime_defaults_reset_headless_settings(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging" / "windows"))
    import build_runtime_defaults as brd
    import yaml

    src = tmp_path / "src.yaml"
    dst = tmp_path / "dst.yaml"
    src.write_text(yaml.safe_dump({
        "workbuddy": {
            "dispatch_mode": "headless",
            "headless": {"enabled": True, "auto_pass_enabled": True, "cli_script": "C:/secret/cli"},
        }
    }), encoding="utf-8")
    brd.write_defaults(src, dst)
    actual = yaml.safe_load(dst.read_text(encoding="utf-8"))
    assert actual["workbuddy"]["dispatch_mode"] == "desktop"
    assert actual["workbuddy"]["headless"]["enabled"] is False
    assert actual["workbuddy"]["headless"]["auto_pass_enabled"] is False
    assert actual["workbuddy"]["headless"]["cli_script"] == ""


def test_dashboard_renders_headless_badge_and_saves_headless_settings(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    import web_app
    import web_runner

    cfg = _make_fake_cli_env(tmp_path)
    settings = {
        "workbuddy": {
            "dispatch_mode": "headless",
            "desktop_enabled": False,
            "headless": {**cfg["headless"], "auto_pass_enabled": False, "model": "hy4-preview"},
        }
    }
    monkeypatch.setattr(web_runner, "load_settings", lambda: settings)
    client = TestClient(web_app.app)
    run_html = client.get("/run").text
    settings_html = client.get("/settings").text
    assert "当前投递：CodeBuddy CLI 无头模式" in run_html
    assert "workbuddyDispatchModeSelect" in settings_html
    assert "workbuddy_headless_auto_pass_enabled" in settings_html

    target = {"workbuddy": {}}
    web_runner._update_workbuddy_headless_settings(
        target,
        {
            "workbuddy_dispatch_mode": "headless",
            "workbuddy_headless_enabled": "true",
            "workbuddy_headless_auto_pass_enabled": "true",
            "workbuddy_headless_model": "deepseek-v4.1-flash",
        },
    )
    assert target["workbuddy"]["dispatch_mode"] == "headless"
    assert target["workbuddy"]["headless"]["enabled"] is True
    assert target["workbuddy"]["headless"]["auto_pass_enabled"] is True
    assert target["workbuddy"]["headless"]["model"] == "deepseek-v4.1-flash"
