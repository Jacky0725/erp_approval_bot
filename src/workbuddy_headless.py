"""Read-only headless CodeBuddy CLI adapter for WorkBuddy reagent batches.

This module isolates headless CLI execution from ``workbuddy_desktop.py`` and
``workbuddy_recovery.py``. The CLI process is restricted to read-only/search
tools (no Write/Bash/PowerShell); all JSON validation, digest freezing, and
atomic Excel publication are performed locally by Python.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from task_control import SafeStop, checkpoint
from workbuddy_result_writer import JUDGMENTS_NAME, parse_judgments, publish_result, valid_basis
from workbuddy_rules import verify_compact


DEFAULT_HEADLESS_TOOLS = "Read,WebSearch,WebFetch,StructuredOutput"
DEFAULT_HEADLESS_TOOLS_PLAIN = "Read,WebSearch,WebFetch"
ALLOWED_HEADLESS_TOOLS = frozenset({"Read", "WebSearch", "WebFetch", "StructuredOutput"})
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
MAX_CLI_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_MODEL_NAME_LENGTH = 128

AUTH_ERROR_RE = re.compile(
    r"(not logged in|unauthorized|authentication|invalid api key|token expired|login required|未登录|登录已过期|鉴权失败|认证失败|401)",
    re.IGNORECASE,
)
RATE_LIMIT_RE = re.compile(
    r"(rate limit|too many requests|quota exceeded|overloaded|限流|额度不足|频率超限|429)",
    re.IGNORECASE,
)


class HeadlessError(RuntimeError):
    """Structured error raised by the headless CodeBuddy CLI adapter."""

    def __init__(self, message: str, *, code: str = "process_failed", metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.metadata = dict(metadata or {})


@dataclass(frozen=True)
class HeadlessCliPaths:
    node_executable: Path
    cli_script: Path
    credentials_dir: Path


def headless_settings(workbuddy_config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (workbuddy_config or {}).get("headless")
    return dict(raw) if isinstance(raw, dict) else {}


def workbuddy_dispatch_mode(workbuddy_config: dict[str, Any] | None) -> str:
    mode = str((workbuddy_config or {}).get("dispatch_mode") or "desktop").strip().lower()
    return "headless" if mode == "headless" else "desktop"


def workbuddy_mode_enabled(workbuddy_config: dict[str, Any] | None) -> bool:
    """Unified WorkBuddy mode availability check across UI, DingTalk, scheduler, and loop."""
    cfg = workbuddy_config or {}
    if workbuddy_dispatch_mode(cfg) == "headless":
        return bool(headless_settings(cfg).get("enabled", False))
    return bool(cfg.get("desktop_enabled", False))


def workbuddy_headless_auto_pass_allowed(workbuddy_config: dict[str, Any] | None) -> bool:
    """Headless mode keeps WorkBuddyAutoPass opt-in until separately accepted."""
    return bool(headless_settings(workbuddy_config).get("auto_pass_enabled", False))


def validate_model_name(value: Any, *, field_label: str = "无头模型") -> str:
    model = str(value or "").strip()
    if not model:
        return ""
    if (
        len(model) > MAX_MODEL_NAME_LENGTH
        or model.startswith("-")
        or any(ord(ch) < 32 or ch in "\r\n\0" for ch in model)
        or any(ch.isspace() for ch in model)
    ):
        raise HeadlessError(
            f"{field_label}名称无效：不能以 '-' 开头、包含空白或控制字符，且长度不超过 {MAX_MODEL_NAME_LENGTH}。",
            code="config_error",
        )
    return model


def validate_headless_tools(tools_raw: str, *, use_json_schema: bool) -> str:
    if not tools_raw.strip():
        return DEFAULT_HEADLESS_TOOLS if use_json_schema else DEFAULT_HEADLESS_TOOLS_PLAIN
    parsed = [part.strip() for part in tools_raw.split(",") if part.strip()]
    disallowed = [tool for tool in parsed if tool not in ALLOWED_HEADLESS_TOOLS]
    if disallowed:
        raise HeadlessError(
            f"无头模式仅允许只读/检索工具（{', '.join(sorted(ALLOWED_HEADLESS_TOOLS))}），禁止配置：{', '.join(disallowed)}。",
            code="config_error",
        )
    deduped = list(dict.fromkeys(parsed))
    if use_json_schema and "StructuredOutput" not in deduped:
        deduped.append("StructuredOutput")
    elif not use_json_schema:
        deduped = [tool for tool in deduped if tool != "StructuredOutput"]
    return ",".join(deduped)


def recover_frozen_headless_result(bridge: Any, state: dict[str, Any]) -> bool:
    """Republish result.xlsx from a previously frozen judgments.json after a crash."""
    batch_dir = bridge.batch_path(state)
    judgments_path = batch_dir / JUDGMENTS_NAME
    frozen_digest = str(state.get("judgments_sha256") or "").strip()
    if not frozen_digest or not judgments_path.is_file():
        return False
    if judgments_path.resolve() != judgments_path:
        raise HeadlessError("恢复时发现已冻结的判定文件路径越界，禁止发布结果。", code="protocol_error")
    payload = judgments_path.read_bytes()
    actual_digest = hashlib.sha256(payload).hexdigest()
    if actual_digest != frozen_digest:
        raise HeadlessError("恢复时发现已冻结的判定文件摘要不匹配，保留现场并停止。", code="protocol_error")
    rules_path = batch_dir / "rules.xlsx"
    if not rules_path.is_file() or hashlib.sha256(rules_path.read_bytes()).hexdigest() != state["rules_sha256"]:
        raise HeadlessError("本批规则快照缺失或已被修改，禁止恢复发布结果。", code="protocol_error")
    if state.get("compact_rules_sha256"):
        verify_compact(batch_dir, state["rules_sha256"], state["compact_rules_sha256"])
    expected_ids = {str(item["key"]) for item in state["items"]}
    parse_judgments(payload, expected_ids)
    publish_result(batch_dir, judgments_payload=payload)
    return True


def _default_cli_script_candidates() -> list[Path]:
    candidates: list[Path] = []
    appdata = os.getenv("APPDATA", "").strip()
    if appdata:
        candidates.append(
            Path(appdata) / "npm" / "node_modules" / "@tencent-ai" / "codebuddy-code" / "bin" / "codebuddy"
        )
    npm_prefix = os.getenv("NPM_CONFIG_PREFIX", "").strip()
    if npm_prefix:
        candidates.append(
            Path(npm_prefix) / "node_modules" / "@tencent-ai" / "codebuddy-code" / "bin" / "codebuddy"
        )
    home = Path.home()
    candidates.append(home / ".npm-global" / "lib" / "node_modules" / "@tencent-ai" / "codebuddy-code" / "bin" / "codebuddy")
    return candidates


def discover_headless_cli(workbuddy_config: dict[str, Any] | None = None) -> HeadlessCliPaths | None:
    cfg = headless_settings(workbuddy_config)
    configured_node = str(cfg.get("node_executable") or "").strip()
    node_path: Path | None = None
    if configured_node:
        candidate = Path(configured_node)
        if candidate.is_file():
            node_path = candidate
    else:
        found = shutil.which("node")
        if found:
            node_path = Path(found)
    if node_path is None:
        return None

    configured_cli = str(cfg.get("cli_script") or "").strip()
    cli_path: Path | None = None
    if configured_cli:
        candidate = Path(configured_cli)
        if candidate.is_file():
            cli_path = candidate
    else:
        for candidate in _default_cli_script_candidates():
            if candidate.is_file():
                cli_path = candidate
                break
    if cli_path is None:
        return None

    creds_dir = Path(str(cfg.get("credentials_dir") or "").strip() or (Path.home() / ".codebuddy"))
    return HeadlessCliPaths(node_executable=node_path, cli_script=cli_path, credentials_dir=creds_dir)


def preflight(workbuddy_config: dict[str, Any] | None = None) -> str:
    """Perform local dependency checks without invoking the model."""
    paths = discover_headless_cli(workbuddy_config)
    if paths is None:
        return "未找到 Node.js 或全局 @tencent-ai/codebuddy-code CLI 脚本"
    if not paths.credentials_dir.exists():
        return f"未检测到 CodeBuddy 登录目录（{paths.credentials_dir}），请先在终端执行 codebuddy.cmd 登录"
    return ""


def build_headless_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "batch_id": {"type": "string"},
            "judgments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "记录ID": {"type": "string"},
                        "物化特性类别": {"type": "string"},
                        "判断依据": {"type": "string"},
                    },
                    "required": ["记录ID", "物化特性类别", "判断依据"],
                },
            },
        },
        "required": ["batch_id", "judgments"],
    }


def sanitize_headless_instructions(instructions_text: str) -> str:
    """Keep rule/classification principles while removing conflicting file-delivery clauses."""
    kept: list[str] = []
    for raw_line in (instructions_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^[34]\.\s*WB-(EXCEL|JSON)-v1", line, re.IGNORECASE):
            continue
        kept.append(line)
    return "\n".join(kept)


def build_headless_prompt(
    state: dict[str, Any],
    batch_dir: Path,
    *,
    instructions_text: str = "",
    use_json_schema: bool = True,
) -> str:
    batch_id = str(state["batch_id"])
    rules_sha256 = str(state.get("rules_sha256") or "")
    items_payload = []
    for item in state["items"]:
        inp = item["input"]
        entry = {
            "记录ID": str(item.get("key") or inp.get("记录ID") or ""),
            "试剂清单号": str(inp.get("试剂清单号") or ""),
            "试剂名称": str(inp.get("试剂名称") or ""),
            "CAS号": str(inp.get("CAS号") or ""),
            "type": str(inp.get("type") or ""),
            "desc": str(inp.get("desc") or ""),
        }
        spec = str(inp.get("规格") or inp.get("规格单位") or "").strip()
        if spec:
            entry["规格"] = spec
        items_payload.append(entry)
    items_json = json.dumps(items_payload, ensure_ascii=False, indent=2)
    header = sanitize_headless_instructions(instructions_text)
    parts = [header] if header else []
    rules_line = (
        f"规则目录：{batch_dir / 'rules_compact'}（先读取 manifest.json 核对源规则 SHA-256 为 {rules_sha256}，"
        "再按编号逐段完整读取所有 part 文本；不要读/写任何 Excel，不要写任何文件，不要运行终端或脚本）。"
        if rules_sha256
        else f"规则目录：{batch_dir / 'rules_compact'}（请完整读取 manifest.json 及所有分段文本；不要写任何文件，不要运行终端或脚本）。"
    )
    output_line = (
        "最终仅通过 StructuredOutput 返回 JSON 对象：{\"batch_id\": \"...\", \"judgments\": [{\"记录ID\": \"...\", \"物化特性类别\": \"...\", \"判断依据\": \"...\"}]}。"
        if use_json_schema
        else "最终仅输出纯 JSON 对象（不要包含其他文字或代码块）：{\"batch_id\": \"...\", \"judgments\": [{\"记录ID\": \"...\", \"物化特性类别\": \"...\", \"判断依据\": \"...\"}]}。"
    )
    parts.append(
        "\n".join([
            "【无头只读结构化判定模式】",
            f"批次ID：{batch_id}",
            rules_line,
            f"本批待判定试剂列表（共 {len(items_payload)} 条）：",
            items_json,
            "",
            "请严格按本批规则逐条独立判定，原样保留每条 记录ID，不得遗漏、重复或篡改。",
            "同一清单（相同 试剂清单号）内 试剂名称、CAS号、type 和 desc 相同的重复试剂须给出一致类别；无法判定时类别填“需人工复核”，不得猜测。",
            "每条 判断依据 须完整写明适用规则及事实来源（模型知识如实标注）。",
            output_line,
        ])
    )
    return "\n\n".join(parts)


def classify_cli_error(text: str) -> str:
    if AUTH_ERROR_RE.search(text or ""):
        return "auth_failed"
    if RATE_LIMIT_RE.search(text or ""):
        return "rate_limited"
    return "process_failed"


def _extract_result_envelope(raw_stdout: str) -> dict[str, Any]:
    text = (raw_stdout or "").strip()
    if not text:
        raise HeadlessError("CodeBuddy 无头 CLI 未返回任何输出。", code="protocol_error")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise HeadlessError(f"CodeBuddy 无头 CLI 输出不是合法 JSON：{error}", code="protocol_error") from error
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list) and parsed:
        for event in reversed(parsed):
            if isinstance(event, dict) and (event.get("type") == "result" or "structured_output" in event):
                return event
        if isinstance(parsed[-1], dict):
            return parsed[-1]
    raise HeadlessError("CodeBuddy 无头 CLI 输出缺少结果事件。", code="protocol_error")


def _extract_structured_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result_text = str(envelope.get("result") or "").strip()
    if not result_text:
        raise HeadlessError("CodeBuddy 无头 CLI 未返回结构化判定内容。", code="protocol_error")
    if result_text.startswith("```"):
        lines = result_text.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            result_text = "\n".join(lines[1:-1]).strip()
    decoder = json.JSONDecoder()
    first_dict: dict[str, Any] | None = None
    index = 0
    while True:
        brace = result_text.find("{", index)
        if brace == -1:
            break
        try:
            candidate, end = decoder.raw_decode(result_text, brace)
        except json.JSONDecodeError:
            index = brace + 1
            continue
        if isinstance(candidate, dict):
            if "batch_id" in candidate and "judgments" in candidate:
                return candidate
            if first_dict is None:
                first_dict = candidate
        index = end
    if first_dict is not None:
        return first_dict
    raise HeadlessError("CodeBuddy 无头 CLI 返回文本中未找到有效 JSON 对象。", code="protocol_error")


def parse_cli_output(
    raw_stdout: str,
    *,
    expected_batch_id: str,
    expected_record_ids: set[str],
    raw_stderr: str = "",
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Independently validate CLI envelope, batch_id, and complete record set."""
    envelope = _extract_result_envelope(raw_stdout)
    metadata = {
        "subtype": str(envelope.get("subtype") or ""),
        "is_error": bool(envelope.get("is_error")),
        "duration_ms": envelope.get("duration_ms"),
        "num_turns": envelope.get("num_turns"),
        "permission_denials_count": len(envelope.get("permission_denials") or []),
    }
    if envelope.get("is_error"):
        detail = str(envelope.get("result") or raw_stderr or "CodeBuddy CLI 返回错误").strip()
        code = classify_cli_error(detail)
        if code == "auth_failed":
            raise HeadlessError(
                "CodeBuddy 登录态已失效，请在终端执行 codebuddy.cmd 重新登录后重试。",
                code=code,
                metadata=metadata,
            )
        if code == "rate_limited":
            raise HeadlessError(f"CodeBuddy 触发限流或额度限制：{detail[:200]}", code=code, metadata=metadata)
        raise HeadlessError(f"CodeBuddy 无头执行失败：{detail[:200]}", code=code, metadata=metadata)

    payload = _extract_structured_payload(envelope)
    actual_batch_id = str(payload.get("batch_id") or "").strip()
    if actual_batch_id != expected_batch_id:
        raise HeadlessError(
            f"无头返回批次ID不匹配：期望 {expected_batch_id}，实际 {actual_batch_id or '<空>'}。",
            code="protocol_error",
            metadata=metadata,
        )
    judgments = payload.get("judgments")
    if not isinstance(judgments, list) or not judgments:
        raise HeadlessError("无头返回缺少 judgments 列表或列表为空。", code="protocol_error", metadata=metadata)

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in judgments:
        if not isinstance(row, dict):
            raise HeadlessError("无头返回包含非对象条目。", code="protocol_error", metadata=metadata)
        record_id = str(row.get("记录ID") or "").strip()
        category = str(row.get("物化特性类别") or "").strip()
        reason = str(row.get("判断依据") or "").strip()
        if not record_id or not category or not reason:
            raise HeadlessError("无头返回存在空记录ID、空类别或空判断依据。", code="protocol_error", metadata=metadata)
        if not valid_basis(reason):
            raise HeadlessError(
                f"无头返回记录 {record_id} 的判断依据为空、仅含符号或不足两个有效字符。",
                code="protocol_error",
                metadata=metadata,
            )
        if record_id in seen:
            raise HeadlessError(f"无头返回包含重复记录ID：{record_id}。", code="protocol_error", metadata=metadata)
        if record_id not in expected_record_ids:
            raise HeadlessError(f"无头返回包含跨批或未知记录ID：{record_id}。", code="protocol_error", metadata=metadata)
        seen.add(record_id)
        normalized.append({"记录ID": record_id, "物化特性类别": category, "判断依据": reason})

    missing = expected_record_ids - seen
    if missing:
        raise HeadlessError(
            f"无头返回遗漏 {len(missing)} 条记录（疑似截断）。",
            code="protocol_error",
            metadata=metadata,
        )
    return normalized, metadata


def terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    """Terminate the CLI child process and any descendants cleanly."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def freeze_and_publish_headless_result(
    bridge: Any,
    state: dict[str, Any],
    judgments: list[dict[str, str]],
) -> Path:
    """Verify rule snapshot integrity, freeze judgments digest, and publish result.xlsx."""
    batch_dir = bridge.batch_path(state)
    rules_path = batch_dir / "rules.xlsx"
    if not rules_path.is_file() or hashlib.sha256(rules_path.read_bytes()).hexdigest() != state["rules_sha256"]:
        raise HeadlessError("本批规则快照缺失或已被修改，禁止发布结果。", code="protocol_error")
    if state.get("compact_rules_sha256"):
        verify_compact(batch_dir, state["rules_sha256"], state["compact_rules_sha256"])

    payload = json.dumps(judgments, ensure_ascii=False, indent=2).encode("utf-8")
    expected_ids = {str(item["key"]) for item in state["items"]}
    parse_judgments(payload, expected_ids)

    digest = hashlib.sha256(payload).hexdigest()
    if state.get("judgments_sha256", digest) != digest:
        raise HeadlessError("判定结果在冻结后发生变化，保留现场并停止。", code="protocol_error")

    _atomic_write_bytes(batch_dir / JUDGMENTS_NAME, payload)
    state["judgments_sha256"] = digest
    bridge.save(state)
    publish_result(batch_dir, judgments_payload=payload)
    return batch_dir / "result.xlsx"


def _pid_is_alive(pid: int) -> bool:
    """Fail-closed check for whether a previously spawned headless node PID is still alive."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        )
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)

        synchronize = 0x00100000
        query_limited = 0x1000
        handle = kernel.OpenProcess(synchronize | query_limited, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err == 87:  # ERROR_INVALID_PARAMETER: process no longer exists
                return False
            handle = kernel.OpenProcess(synchronize, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87
            try:
                return kernel.WaitForSingleObject(handle, 0) != 0
            finally:
                kernel.CloseHandle(handle)
        try:
            if kernel.WaitForSingleObject(handle, 0) == 0:
                return False
            buf_size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(buf_size.value)
            if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_size)):
                image_name = Path(buf.value).name.casefold()
                if image_name and image_name not in {"node.exe", "node"}:
                    return False
            return True
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


class WorkBuddyHeadless:
    """Execute WorkBuddy reagent batches via read-only CodeBuddy CLI."""

    def __init__(
        self,
        workbuddy_config: dict[str, Any] | None = None,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ):
        self.workbuddy_config = dict(workbuddy_config or {})
        self.config = headless_settings(self.workbuddy_config)
        self._popen = popen_factory
        self._poll_interval = max(0.05, float(poll_interval_seconds))

    def build_command(self, paths: HeadlessCliPaths, batch_dir: Path, *, use_json_schema: bool = True) -> list[str]:
        configured_tools = validate_headless_tools(
            str(self.config.get("tools") or ""),
            use_json_schema=use_json_schema,
        )

        command = [
            str(paths.node_executable),
            str(paths.cli_script),
            "-p",
            "--output-format",
            "json",
            "--tools",
            configured_tools,
            "--strict-mcp-config",
            "--no-session-persistence",
            "--add-dir",
            str(batch_dir),
        ]
        if use_json_schema:
            command.extend(["--json-schema", json.dumps(build_headless_schema(), ensure_ascii=False)])
        model = validate_model_name(self.config.get("model"), field_label="无头主模型")
        if model:
            command.extend(["--model", model])
        fallback_model = validate_model_name(self.config.get("fallback_model"), field_label="无头备用模型")
        if fallback_model:
            command.extend(["--fallback-model", fallback_model])
        return command

    def probe_auth(self, *, timeout_seconds: int = 30) -> dict[str, Any]:
        """Run a zero-business-data probe to verify live CodeBuddy authentication."""
        issue = preflight(self.workbuddy_config)
        if issue:
            raise HeadlessError(issue, code="dependency_missing")
        paths = discover_headless_cli(self.workbuddy_config)
        assert paths is not None
        command = [
            str(paths.node_executable),
            str(paths.cli_script),
            "-p",
            "--output-format",
            "json",
            "--tools",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        with tempfile.TemporaryDirectory(prefix="codebuddy-probe-cwd-") as isolated_cwd:
            stdout_text, stderr_text, returncode = self._run_process(
                command,
                stdin_text="Reply with OK only.",
                cwd=Path(isolated_cwd),
                timeout_seconds=timeout_seconds,
                on_pid=None,
                on_exit=None,
                check_stop=None,
                batch_id="",
            )
        if returncode != 0 and not stdout_text.strip():
            detail = (stderr_text or f"CLI exited with code {returncode}").strip()
            raise HeadlessError(detail[:200], code=classify_cli_error(detail))
        envelope = _extract_result_envelope(stdout_text)
        if returncode != 0 or envelope.get("is_error"):
            detail = str(envelope.get("result") or stderr_text or "认证探针失败").strip()
            code = classify_cli_error(detail)
            raise HeadlessError(detail[:200], code=code)
        return {"ok": True, "duration_ms": envelope.get("duration_ms")}

    def execute_batch(
        self,
        bridge: Any,
        state: dict[str, Any],
        *,
        instructions_text: str = "",
        timeout_seconds: int | None = None,
        check_stop: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Run a single batch in read-only headless mode and atomically publish result.xlsx."""
        issue = preflight(self.workbuddy_config)
        if issue:
            raise HeadlessError(issue, code="dependency_missing")
        paths = discover_headless_cli(self.workbuddy_config)
        assert paths is not None

        old_pid = int(state.get("headless_pid") or 0)
        if old_pid and _pid_is_alive(old_pid):
            raise HeadlessError(
                f"批次 {state['batch_id']} 原无头进程（PID {old_pid}）仍在运行，禁止重复启动。",
                code="process_failed",
            )

        batch_dir = bridge.batch_path(state)
        if not (batch_dir / "rules_compact" / "manifest.json").is_file():
            raise HeadlessError("无头只读模式要求本批已生成 rules_compact 分片规则。", code="protocol_error")
        if state.get("compact_rules_sha256"):
            verify_compact(batch_dir, state["rules_sha256"], state["compact_rules_sha256"])

        use_json_schema = bool(self.config.get("use_json_schema", True))
        budget = int(timeout_seconds or self.config.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
        prompt = build_headless_prompt(
            state,
            batch_dir,
            instructions_text=instructions_text,
            use_json_schema=use_json_schema,
        )
        command = self.build_command(paths, batch_dir, use_json_schema=use_json_schema)

        def record_pid(pid: int) -> None:
            state["dispatch_mode"] = "headless"
            state["headless_pid"] = pid
            bridge.save(state)

        def clear_pid() -> None:
            if state.get("headless_pid"):
                state["headless_pid"] = 0
                bridge.save(state)

        with tempfile.TemporaryDirectory(prefix="codebuddy-headless-cwd-") as isolated_cwd:
            stdout_text, stderr_text, returncode = self._run_process(
                command,
                stdin_text=prompt,
                cwd=Path(isolated_cwd),
                timeout_seconds=budget,
                on_pid=record_pid,
                on_exit=clear_pid,
                check_stop=check_stop,
                batch_id=str(state["batch_id"]),
            )

        if returncode != 0:
            detail = ""
            if stdout_text.strip():
                try:
                    env = _extract_result_envelope(stdout_text)
                    detail = str(env.get("result") or "").strip()
                except HeadlessError:
                    detail = ""
            detail = (detail or stderr_text or f"CLI exited with code {returncode}").strip()
            code = classify_cli_error(detail)
            if code == "auth_failed":
                raise HeadlessError(
                    "CodeBuddy 登录态已失效，请在终端执行 codebuddy.cmd 重新登录后重试。",
                    code=code,
                )
            if code == "rate_limited":
                raise HeadlessError(f"CodeBuddy 触发限流或额度限制：{detail[:200]}", code=code)
            raise HeadlessError(f"CodeBuddy 无头进程退出异常（exit={returncode}）：{detail[:200]}", code=code)

        expected_ids = {str(item["key"]) for item in state["items"]}
        judgments, metadata = parse_cli_output(
            stdout_text,
            expected_batch_id=str(state["batch_id"]),
            expected_record_ids=expected_ids,
            raw_stderr=stderr_text,
        )
        freeze_and_publish_headless_result(bridge, state, judgments)
        return metadata

    def _run_process(
        self,
        command: list[str],
        *,
        stdin_text: str,
        cwd: Path,
        timeout_seconds: int,
        on_pid: Callable[[int], None] | None,
        on_exit: Callable[[], None] | None,
        check_stop: Callable[[], None] | None,
        batch_id: str,
    ) -> tuple[str, str, int]:
        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = self._popen(command, **popen_kwargs)
        if on_pid is not None:
            on_pid(int(proc.pid))

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        truncated = {"stdout": False}

        def _drain(stream: Any, target: list[str], mark_key: str = "") -> None:
            if stream is None:
                return
            remaining = MAX_CLI_OUTPUT_BYTES
            try:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    if remaining > 0:
                        encoded_len = len(chunk.encode("utf-8", errors="replace"))
                        if encoded_len <= remaining:
                            target.append(chunk)
                            remaining -= encoded_len
                        else:
                            target.append(chunk[:remaining])
                            remaining = 0
                            if mark_key:
                                truncated[mark_key] = True
                    elif mark_key:
                        truncated[mark_key] = True
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks, "stdout"), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks), daemon=True)
        t_out.start()
        t_err.start()

        if proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        deadline = time.monotonic() + max(1, int(timeout_seconds))
        try:
            while proc.poll() is None:
                if check_stop is not None:
                    check_stop()
                else:
                    if batch_id:
                        checkpoint("WorkBuddy 无头 CLI 等待结果", batch_id=batch_id)
                    else:
                        checkpoint("WorkBuddy 无头 CLI 等待结果")
                if time.monotonic() >= deadline:
                    terminate_process_tree(proc)
                    raise HeadlessError(
                        f"CodeBuddy 无头判定超时（>{timeout_seconds}s），已终止子进程树。",
                        code="timeout",
                    )
                time.sleep(self._poll_interval)
        except SafeStop:
            terminate_process_tree(proc)
            raise
        finally:
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            if on_exit is not None and proc.poll() is not None:
                on_exit()

        if truncated["stdout"]:
            raise HeadlessError(
                f"CodeBuddy 无头输出超出安全上限（>{MAX_CLI_OUTPUT_BYTES} 字节），已拒绝解析。",
                code="protocol_error",
            )
        return "".join(stdout_chunks), "".join(stderr_chunks), int(proc.returncode or 0)
