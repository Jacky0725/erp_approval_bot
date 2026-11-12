"""Portable encrypted settings and Windows-local rollback for the two runtime files."""
from __future__ import annotations

from base64 import b64decode, b64encode
from copy import deepcopy
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from uuid import uuid4

from dotenv import dotenv_values
from dotenv.parser import parse_stream
import yaml

from llm_providers import LLM_PROVIDERS


class ConfigTransferError(ValueError):
    pass


HEADER = b"RABCONFIG\x01"
MAX_BYTES = 256 * 1024
SECRET_KEYS = {
    "ERP_PASSWORD", "LLM_API_KEY", "DINGTALK_STREAM_CLIENT_SECRET", "DINGTALK_STREAM_API_TOKEN",
    "DINGTALK_CHAT_API_KEY", "JIANGUOYUN_WEBDAV_PASSWORD", "REAGENT_APPROVAL_UPDATE_TOKEN",
    *(provider.api_key_env for provider in LLM_PROVIDERS.values()),
}
ENV_KEYS = SECRET_KEYS | {
    "ERP_URL", "ERP_USERNAME", "DINGTALK_STREAM_CLIENT_ID", "JIANGUOYUN_WEBDAV_USER",
    "AUTO_PASS", "PROCESS_ALL_TODOS", "PROCESS_ALL_TODOS_MAX", "APP_DRY_RUN",
    "APPROVAL_WRITE_MODE", "APPROVAL_WRITE_MIN_CONFIDENCE", "APPROVAL_WRITE_BATCH_SIZE", "ERP_WRITE_BACKEND",
    "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "SILICONFLOW_BASE_URL", "SILICONFLOW_MODEL",
}

# Each section is limited to its portable leaf fields. Runtime trust and file paths stay local.
SETTING_FIELDS = {
    "app": {"dry_run": bool},
    "browser": {"headless": bool},
    "workbuddy": {"batch_size": int, "result_timeout_seconds": int, "max_write_attempts": int,
                  "reuse_chat": bool, "compact_rules": bool, "freeze_web_todo_scope": bool},
    "llm": {"provider": str, "base_url": str, "model": str, "timeout_seconds": int, "max_retries": int},
    "approval": {"write_mode": str, "erp_write_backend": str, "write_min_confidence": float,
                 "write_batch_size": int, "parallel_workers": int},
    "approval.identity_enrichment": {"enabled": bool, "base_url": str, "model": str, "timeout_seconds": int,
                                     "max_calls_per_batch": int, "max_calls_per_day": int},
    "scheduler": {"enabled": bool, "mode": str, "interval_hours": int, "daily_time": str,
                  "use_default_run_policy": bool, "process_all_todos_max": int, "approval_write_mode": str,
                  "approval_write_min_confidence": float, "auto_pass": bool, "skip_manual_review_lists": bool},
    "notification.dingtalk": {"enabled": bool, "at_all": bool},
    "dingtalk_stream": {"enabled": bool, "corp_id": str, "agent_id": str, "robot_code": str, "open_conversation_id": str},
    "dingtalk_chat": {"enabled": bool, "use_main_llm": bool, "base_url": str, "model": str,
                     "timeout_seconds": int, "context_turns": int, "requests_per_minute": int},
    "dingtalk_control": {"admin_user_ids": list, "management_group_ids": list},
    "memory_sync": {"enabled": bool, "base_url": str, "remote_dir": str, "keep_versions": int,
                    "auto_upload_after_memory_change": bool, "check_remote_on_startup": bool},
}
LEAF_TYPES = {f"{group}.{name}": kind for group, fields in SETTING_FIELDS.items() for name, kind in fields.items()}
SAFETY_FIELDS = {
    "APP_DRY_RUN", "AUTO_PASS", "APPROVAL_WRITE_MODE", "ERP_WRITE_BACKEND", "ERP_URL", "ERP_USERNAME",
    "app.dry_run", "approval.write_mode", "approval.erp_write_backend", "notification.dingtalk.enabled",
    "dingtalk_stream.enabled", "dingtalk_stream.open_conversation_id", "memory_sync.auto_upload_after_memory_change",
}
WRITE_MODES = {"disabled", "multi_page", "generate_library"}
WRITE_BACKENDS = {"web_ui", "api_read_web_write", "api_write_with_web_verify"}


def _cipher(passphrase: str, salt: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if not 12 <= len(passphrase) <= 256:
        raise ConfigTransferError("配置口令长度须为 12–256 个字符。")
    key = Scrypt(salt=salt, length=32, n=2**16, r=8, p=2).derive(passphrase.encode("utf-8"))
    return AESGCM(key)


def encrypt_profile(profile: dict, passphrase: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    header = HEADER + salt + nonce
    data = json.dumps(profile, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) + len(header) + 16 > MAX_BYTES:
        raise ConfigTransferError("配置文件超出 256 KiB。")
    return header + _cipher(passphrase, salt).encrypt(nonce, data, header)


def decrypt_profile(blob: bytes, passphrase: str) -> dict:
    from cryptography.exceptions import InvalidTag

    offset = len(HEADER)
    if not blob.startswith(HEADER) or not offset + 44 <= len(blob) <= MAX_BYTES:
        raise ConfigTransferError("配置文件格式、版本或大小无效。")
    salt, nonce = blob[offset:offset + 16], blob[offset + 16:offset + 28]
    boundary = offset + 28
    try:
        payload = _cipher(passphrase, salt).decrypt(nonce, blob[boundary:], blob[:boundary])
        value = json.loads(payload)
    except (InvalidTag, UnicodeError, json.JSONDecodeError):
        raise ConfigTransferError("配置文件口令错误或文件损坏。") from None
    if not isinstance(value, dict) or set(value) != {"settings", "env"}:
        raise ConfigTransferError("配置文件内容无效。")
    return value


def _lookup(data: dict, key: str):
    for part in key.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def _flatten(settings: dict) -> dict:
    fields = {key: value for key in LEAF_TYPES if (value := _lookup(settings, key)) is not None}
    # Older Web UI versions persisted confidence inputs as strings.
    for key, value in fields.items():
        if LEAF_TYPES[key] is float and isinstance(value, str):
            try:
                fields[key] = float(value)
            except ValueError:
                raise ConfigTransferError(f"本机配置字段取值无效：{key}") from None
    return fields


def export_profile(settings: dict, env_path: Path, *, environment: dict | None = None) -> dict:
    source = dotenv_values(env_path, encoding="utf-8") if env_path.is_file() else {}
    if environment is not None:
        source = {**source, **{key: environment[key] for key in ENV_KEYS if key in environment}}
    return {"settings": _flatten(settings), "env": {key: source[key] for key in sorted(ENV_KEYS)
                                                   if source.get(key) is not None}}


def _validate(key: str, value, kind: type) -> None:
    if kind is float:
        valid = type(value) in (int, float) and 0 <= value <= 1
    elif kind is int:
        valid = type(value) is int and 0 <= value <= 1_000_000
    elif kind is str:
        valid = isinstance(value, str) and len(value) <= 4096 and not any(ch in value for ch in "\0\r\n")
    elif kind is list:
        valid = isinstance(value, list) and len(value) <= 500 and all(
            isinstance(x, str) and len(x) <= 512 and not any(c in x for c in "\0\r\n") for x in value)
    else:
        valid = type(value) is bool
    if not valid:
        raise ConfigTransferError(f"配置字段类型或取值无效：{key}")
    if key in {"approval.write_mode", "scheduler.approval_write_mode", "APPROVAL_WRITE_MODE"}:
        valid = value in WRITE_MODES
    elif key in {"approval.erp_write_backend", "ERP_WRITE_BACKEND"}:
        valid = value in WRITE_BACKENDS
    elif key == "scheduler.mode":
        valid = value in {"interval", "daily"}
    elif key == "scheduler.daily_time":
        valid = bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value))
    elif key == "dingtalk_control.admin_user_ids":
        valid = all(item.count("/") == 1 and all(item.split("/")) for item in value)
    elif key in {"workbuddy.result_timeout_seconds", "workbuddy.max_write_attempts"}:
        valid = value > 0
    elif key in {"APP_DRY_RUN", "AUTO_PASS", "PROCESS_ALL_TODOS"}:
        valid = value.lower() in {"true", "false", "1", "0", "yes", "no", "on", "off"}
    if not valid:
        raise ConfigTransferError(f"配置字段取值无效：{key}")
    if (key.endswith(".base_url") or key in {"ERP_URL", "LLM_BASE_URL", "SILICONFLOW_BASE_URL"}) and value:
        try:
            url = urlsplit(value)
            valid = url.scheme in {"http", "https"} and bool(url.hostname) and not (url.username or url.password)
        except ValueError:
            valid = False
        if not valid:
            raise ConfigTransferError(f"配置连接地址无效：{key}")


def merge_profile(profile: dict, settings: dict, env_path: Path) -> tuple[dict, dict, list[dict]]:
    from workbuddy_bridge import web_batch_sizes

    values, updates = profile["settings"], profile["env"]
    if (not isinstance(values, dict) or not isinstance(updates, dict)
            or not set(values) <= LEAF_TYPES.keys() or not set(updates) <= ENV_KEYS):
        raise ConfigTransferError("配置包含此版本不支持的字段。")
    if values.get("workbuddy.batch_size", 20) not in web_batch_sizes(settings.get("workbuddy") or {}):
        raise ConfigTransferError("配置中的 WorkBuddy 默认档位尚未在本机验收。")
    merged = deepcopy(settings)
    old = export_profile(settings, env_path)
    changes = []
    for group, data, allowed in (("settings", values, LEAF_TYPES), ("env", updates, dict.fromkeys(ENV_KEYS, str))):
        for key, value in sorted(data.items()):
            _validate(key, value, allowed[key])
            if group == "settings":
                *parents, leaf = key.split(".")
                target = merged
                for parent in parents:
                    target = target.setdefault(parent, {})
                target[leaf] = value
            previous = old[group].get(key)
            if previous != value:
                credential = key in SECRET_KEYS
                changes.append({
                    "field": key, "credential": credential,
                    "before": ("已配置" if previous else "未配置") if credential else previous,
                    "after": ("将替换" if value else "将清空") if credential else value,
                    "safety": key in SAFETY_FIELDS or key.startswith(("scheduler.", "dingtalk_control.", "dingtalk_stream.")),
                })
    if any(key in updates and old["env"].get(key) != updates[key] for key in ("ERP_URL", "ERP_USERNAME")):
        erp = merged.setdefault("erp_api", {})
        erp.update(base_url="", enabled_for_write=False)
        erp.setdefault("discovery", {})["status"] = "unverified"
        changes.append({"field": "ERP 接口写入验收", "before": "本机当前状态", "after": "需重新验证", "safety": True})
    return merged, updates, changes


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix="config-", suffix=".tmp", delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            f.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        _replace_file(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _replace_file(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        return
    import ctypes
    from ctypes import wintypes

    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    replace_existing, write_through = 0x1, 0x8
    if not move(str(source.resolve()), str(target.resolve()), replace_existing | write_through):
        raise ctypes.WinError(ctypes.get_last_error())


def _pending(root: Path) -> Path:
    return root / "data" / "backups" / "config-import.pending.dpapi"


def import_pending(root: Path) -> bool:
    return _pending(root).exists()


def config_revision(settings_path: Path, env_path: Path) -> str:
    material = bencode_files(settings_path, env_path)
    return hashlib.sha256(material).hexdigest()


def bencode_files(*paths: Path) -> bytes:
    return json.dumps([b64encode(p.read_bytes()).decode("ascii") if p.is_file() else None for p in paths]).encode("ascii")


def recover_pending(root: Path, settings_path: Path, env_path: Path) -> None:
    journal = _pending(root)
    if not journal.is_file():
        return
    import win32crypt

    _, original = win32crypt.CryptUnprotectData(journal.read_bytes(), None, None, None, 1)
    entries = json.loads(original)
    for path, encoded in zip((settings_path, env_path), entries, strict=True):
        if encoded is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_bytes(path, b64decode(encoded, validate=True))
    journal.unlink()


def _render_env(env_path: Path, updates: dict) -> bytes:
    existing = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    lines = []
    for binding in parse_stream(StringIO(existing)):
        if binding.key not in updates:
            lines.append(binding.original.string.rstrip("\r\n"))
    for key, value in sorted(updates.items()):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    output = "\n".join(lines) + "\n"
    rendered = dotenv_values(stream=StringIO(output))
    if any(rendered.get(key) != value for key, value in updates.items()):
        raise ConfigTransferError("凭据或参数无法按现有 .env 格式无损写入。")
    return output.encode("utf-8")


def apply_profile(root: Path, settings_path: Path, env_path: Path, settings: dict, updates: dict) -> None:
    if os.name != "nt":
        raise ConfigTransferError("配置导入目前需要 Windows 本机加密保护。")
    import win32crypt

    candidate_env = _render_env(env_path, updates)
    candidate_settings = yaml.safe_dump(settings, allow_unicode=True, sort_keys=False).encode("utf-8")
    journal = _pending(root)
    if journal.exists():
        raise ConfigTransferError("上次配置导入尚未恢复。")
    backup = journal.with_name(f"config-import-{uuid4().hex}.dpapi")
    original = bencode_files(settings_path, env_path)
    _atomic_bytes(journal, win32crypt.CryptProtectData(original, "Reagent Approval settings", None, None, None, 1))
    try:
        _atomic_bytes(env_path, candidate_env)
        _atomic_bytes(settings_path, candidate_settings)
        _replace_file(journal, backup)
    except OSError:
        recover_pending(root, settings_path, env_path)
        raise ConfigTransferError("配置写入失败，已恢复导入前的配置。") from None
