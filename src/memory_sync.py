from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from reagent_memory import ReagentMemory


MANIFEST_NAME = "manifest.json"
LATEST_NAME = "reagent_memory_latest.sqlite"


class MemorySyncError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class MemorySyncConflict(MemorySyncError):
    def __init__(self, message: str, *, payload: dict[str, Any]) -> None:
        super().__init__(message, status_code=409, payload=payload)


@dataclass(frozen=True)
class WebDavResponse:
    status: int
    headers: dict[str, str]
    body: bytes


RequestFunc = Callable[[str, str, bytes | None, dict[str, str]], WebDavResponse]


def memory_sync_config(settings: dict[str, Any] | None) -> dict[str, Any]:
    configured = ((settings or {}).get("memory_sync", {}) or {}).copy()
    return {
        "enabled": bool(configured.get("enabled", False)),
        "provider": str(configured.get("provider") or "webdav"),
        "base_url": str(configured.get("base_url") or "https://dav.jianguoyun.com/dav/"),
        "remote_dir": str(configured.get("remote_dir") or "reagent-approval-bot"),
        "username_env": str(configured.get("username_env") or "JIANGUOYUN_WEBDAV_USER"),
        "password_env": str(configured.get("password_env") or "JIANGUOYUN_WEBDAV_PASSWORD"),
        "keep_versions": max(1, int(configured.get("keep_versions") or 10)),
        "auto_upload_after_memory_change": bool(configured.get("auto_upload_after_memory_change", False)),
        "check_remote_on_startup": bool(configured.get("check_remote_on_startup", False)),
    }


class MemorySyncService:
    def __init__(
        self,
        *,
        root_dir: Path,
        settings: dict[str, Any] | None,
        request_func: RequestFunc | None = None,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.settings = settings or {}
        self.config = memory_sync_config(self.settings)
        self.request_func = request_func or self._urllib_request
        self.now_func = now_func or datetime.now

    @property
    def state_path(self) -> Path:
        return self.root_dir / "data" / "memory_sync_state.yaml"

    @property
    def memory_path(self) -> Path:
        return ReagentMemory.from_settings(self.settings, self.root_dir).path

    def status(self, *, check_remote: bool = False) -> dict[str, Any]:
        self._load_env()
        state = self.read_state()
        local = self.local_summary()
        payload: dict[str, Any] = {
            "ok": True,
            "enabled": self.config["enabled"],
            "provider": self.config["provider"],
            "base_url": self.config["base_url"],
            "remote_dir": self.config["remote_dir"],
            "username_env": self.config["username_env"],
            "password_env": self.config["password_env"],
            "username": os.getenv(self.config["username_env"], ""),
            "username_configured": bool(os.getenv(self.config["username_env"], "").strip()),
            "password_configured": bool(os.getenv(self.config["password_env"], "").strip()),
            "auto_upload_after_memory_change": self.config["auto_upload_after_memory_change"],
            "check_remote_on_startup": self.config["check_remote_on_startup"],
            "keep_versions": self.config["keep_versions"],
            "local": local,
            "state": state,
            "remote": {},
            "remote_newer": False,
            "conflict": False,
            "message": state.get("last_error") or "",
        }
        if check_remote and self.config["enabled"] and payload["username_configured"] and payload["password_configured"]:
            try:
                manifest = self.fetch_manifest(default={})
                latest = (manifest or {}).get("latest") or {}
                payload["remote"] = latest
                if latest:
                    payload["remote_newer"] = self._is_remote_newer(latest, local)
                    payload["conflict"] = self._has_possible_conflict(latest, local, state)
                else:
                    payload["message"] = "云端尚未上传试剂库，可先执行上传本地试剂库。"
            except MemorySyncError as error:
                payload["ok"] = False
                payload["message"] = str(error)
        return payload

    def test_connection(self) -> dict[str, Any]:
        self._ensure_enabled()
        self._ensure_credentials()
        self._ensure_remote_dirs()
        return {
            "ok": True,
            "message": "WebDAV 连接成功。",
            "local": self.local_summary(),
            "remote": self.status(check_remote=True).get("remote") or {},
        }

    def upload(self) -> dict[str, Any]:
        self._ensure_enabled()
        self._ensure_credentials()
        self._ensure_remote_dirs()
        source = self.memory_path
        if not source.exists():
            raise MemorySyncError(f"本地试剂记忆库不存在：{source}", status_code=404)

        version = self.now_func().strftime("%Y%m%d_%H%M%S")
        version_name = f"versions/reagent_memory_{version}.sqlite"
        snapshot = self.create_sqlite_snapshot(source)
        try:
            local = self.local_summary(snapshot)
            data = snapshot.read_bytes()
            self._request("PUT", self._remote_url(LATEST_NAME), data, {"Content-Type": "application/octet-stream"})
            self._request("PUT", self._remote_url(version_name), data, {"Content-Type": "application/octet-stream"})

            latest = {
                "version": version,
                "filename": LATEST_NAME,
                "version_filename": version_name,
                "updated_at": local.get("updated_at", ""),
                "uploaded_at": self.now_func().isoformat(timespec="seconds"),
                "records": local.get("records", 0),
                "size": snapshot.stat().st_size,
            }
            manifest = self.fetch_manifest(default={})
            versions = [latest, *[item for item in manifest.get("versions", []) if item.get("version") != version]]
            keep_versions = int(self.config["keep_versions"])
            manifest = {
                "schema_version": 1,
                "provider": self.config["provider"],
                "remote_dir": self.config["remote_dir"],
                "latest": latest,
                "versions": versions[:keep_versions],
            }
            self._request(
                "PUT",
                self._remote_url(MANIFEST_NAME),
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                {"Content-Type": "application/json; charset=utf-8"},
            )
            for stale in versions[keep_versions:]:
                filename = str(stale.get("version_filename") or "")
                if filename:
                    self._delete_remote(filename)
            self.update_state(last_upload_at=latest["uploaded_at"], last_remote_version=version, last_error="")
            return {"ok": True, "message": "试剂记忆库上传成功。", "local": local, "remote": latest}
        finally:
            snapshot.unlink(missing_ok=True)

    def download(self, *, force: bool = False) -> dict[str, Any]:
        self._ensure_enabled()
        self._ensure_credentials()
        manifest = self.fetch_manifest()
        latest = (manifest or {}).get("latest") or {}
        remote_file = str(latest.get("filename") or LATEST_NAME)
        if not latest:
            raise MemorySyncError("云端未找到试剂记忆库 manifest。", status_code=404)

        state = self.read_state()
        local = self.local_summary()
        if not force:
            if self._is_local_newer(latest, local) or self._has_possible_conflict(latest, local, state):
                raise MemorySyncConflict(
                    "本地和云端试剂库可能存在冲突，未自动覆盖本地库。",
                    payload={"ok": False, "conflict": True, "local": local, "remote": latest},
                )

        response = self._request("GET", self._remote_url(remote_file), None, {})
        temp_path = self._temp_sqlite_path("download")
        temp_path.write_bytes(response.body)
        try:
            self.validate_sqlite(temp_path)
            backup_path = ""
            if self.memory_path.exists():
                backup = self.root_dir / "data" / "logs" / f"reagent_memory_backup_before_webdav_download_{self.now_func():%Y%m%d_%H%M%S}.sqlite"
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup_snapshot = self.create_sqlite_snapshot(self.memory_path)
                shutil.move(str(backup_snapshot), backup)
                backup_path = str(backup)
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp_path, self.memory_path)
            self._remove_sqlite_sidecars(self.memory_path)
            downloaded_at = self.now_func().isoformat(timespec="seconds")
            self.update_state(
                last_download_at=downloaded_at,
                last_remote_version=str(latest.get("version") or ""),
                last_local_backup=backup_path,
                last_error="",
            )
            return {
                "ok": True,
                "message": "试剂记忆库下载并恢复成功。",
                "local": self.local_summary(),
                "remote": latest,
                "backup": backup_path,
            }
        finally:
            temp_path.unlink(missing_ok=True)

    def versions(self) -> dict[str, Any]:
        self._ensure_enabled()
        self._ensure_credentials()
        manifest = self.fetch_manifest()
        return {
            "ok": True,
            "versions": manifest.get("versions", []),
            "latest": manifest.get("latest", {}),
            "local": self.local_summary(),
        }

    def fetch_manifest(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._request("GET", self._remote_url(MANIFEST_NAME), None, {})
        except MemorySyncError as error:
            if error.status_code == 404 and default is not None:
                return default
            raise
        try:
            return json.loads(response.body.decode("utf-8")) if response.body else (default or {})
        except json.JSONDecodeError as error:
            raise MemorySyncError(f"云端 manifest.json 不是有效 JSON：{error}", status_code=502) from error

    def create_sqlite_snapshot(self, source: Path) -> Path:
        snapshot = self._temp_sqlite_path("upload")
        try:
            with closing(sqlite3.connect(str(source), timeout=15.0)) as src, closing(
                sqlite3.connect(str(snapshot))
            ) as dst:
                src.execute("PRAGMA busy_timeout = 15000")
                src.backup(dst)
            self.validate_sqlite(snapshot)
            return snapshot
        except Exception:
            snapshot.unlink(missing_ok=True)
            raise

    def validate_sqlite(self, path: Path) -> None:
        try:
            with closing(sqlite3.connect(str(path))) as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if not row or str(row[0]).lower() != "ok":
                    raise MemorySyncError(f"SQLite 完整性校验失败：{row[0] if row else 'empty result'}", status_code=422)
        except sqlite3.DatabaseError as error:
            raise MemorySyncError(f"SQLite 文件无法打开或已损坏：{error}", status_code=422) from error

    def local_summary(self, path: Path | None = None) -> dict[str, Any]:
        target = path or self.memory_path
        relative = str(target.relative_to(self.root_dir)) if target.is_relative_to(self.root_dir) else str(target)
        summary: dict[str, Any] = {
            "path": relative,
            "exists": target.exists(),
            "records": 0,
            "updated_at": "",
            "size": target.stat().st_size if target.exists() else 0,
        }
        if not target.exists():
            return summary
        try:
            with closing(sqlite3.connect(str(target))) as conn:
                row = conn.execute("SELECT COUNT(*), MAX(updated_at), MAX(created_at) FROM reagent_memory").fetchone()
                summary["records"] = int(row[0] or 0)
                summary["updated_at"] = str(row[1] or row[2] or "")
        except sqlite3.DatabaseError:
            summary["updated_at"] = datetime.fromtimestamp(target.stat().st_mtime).isoformat(timespec="seconds")
        return summary

    def read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            with self.state_path.open("r", encoding="utf-8") as file:
                return yaml.safe_load(file) or {}
        except Exception:
            return {}

    def update_state(self, **updates: Any) -> None:
        state = self.read_state()
        state.update({key: value for key, value in updates.items() if value is not None})
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as file:
            yaml.safe_dump(state, file, allow_unicode=True, sort_keys=False)
        os.replace(tmp, self.state_path)

    def _ensure_enabled(self) -> None:
        if not self.config["enabled"]:
            raise MemorySyncError("试剂库同步未启用。", status_code=400)
        if self.config["provider"] != "webdav":
            raise MemorySyncError(f"不支持的同步提供方：{self.config['provider']}", status_code=400)

    def _ensure_credentials(self) -> None:
        if not self._username() or not self._password():
            raise MemorySyncError("WebDAV 用户名或应用密码未配置。", status_code=400)

    def _ensure_remote_dirs(self) -> None:
        segments = self._remote_dir_segments()
        for index in range(1, len(segments) + 1):
            self._ensure_remote_dir_path(segments[:index])
        self._ensure_remote_dir_path([*segments, "versions"])

    def _ensure_remote_dir_path(self, parts: list[str]) -> None:
        if self._remote_path_exists(parts):
            return
        last_error: MemorySyncError | None = None
        for attempt in range(4):
            try:
                self._mkcol_path(parts)
                return
            except MemorySyncError as error:
                last_error = error
                if error.status_code != 409 or not _looks_like_missing_ancestor(str(error)):
                    raise
                if attempt == 3:
                    raise MemorySyncError(
                        "WebDAV 远端目录创建失败：父目录不存在。请检查 WebDAV 地址是否为 "
                        "https://dav.jianguoyun.com/dav/，远端目录不要填写不存在的多级路径；"
                        "建议先使用 reagent-approval-bot，或在坚果云中手动创建父目录。",
                        status_code=409,
                    ) from error
                time.sleep(0.5)
        if last_error:
            raise last_error

    def _mkcol_path(self, parts: list[str]) -> None:
        try:
            self._request("MKCOL", self._remote_url_from_parts(parts), None, {})
        except MemorySyncError as error:
            if error.status_code == 405 or (error.status_code == 409 and self._remote_path_exists(parts)):
                return
            raise

    def _remote_path_exists(self, parts: list[str]) -> bool:
        try:
            self._request("PROPFIND", self._remote_url_from_parts(parts), None, {"Depth": "0"})
            return True
        except MemorySyncError as error:
            if error.status_code in {404, 409}:
                return False
            raise

    def _delete_remote(self, relative: str) -> None:
        try:
            self._request("DELETE", self._remote_url(relative), None, {})
        except MemorySyncError as error:
            if error.status_code != 404:
                raise

    def _remote_url(self, relative: str) -> str:
        parts = [*self._remote_dir_segments(), *[part for part in relative.split("/") if part]]
        return self._remote_url_from_parts(parts)

    def _remote_url_from_parts(self, parts: list[str]) -> str:
        base = self.config["base_url"].rstrip("/") + "/"
        encoded = "/".join(urllib.parse.quote(part.strip("/")) for part in parts)
        return urllib.parse.urljoin(base, encoded)

    def _remote_dir_path(self) -> str:
        return "/".join(self._remote_dir_segments())

    def _remote_dir_segments(self) -> list[str]:
        return [part for part in str(self.config["remote_dir"] or "").replace("\\", "/").split("/") if part]

    def _request(self, method: str, url: str, data: bytes | None, headers: dict[str, str]) -> WebDavResponse:
        request_headers = {
            "Authorization": self._basic_auth_header(),
            "User-Agent": "reagent-approval-bot-memory-sync",
            **headers,
        }
        return self.request_func(method, url, data, request_headers)

    def _urllib_request(self, method: str, url: str, data: bytes | None, headers: dict[str, str]) -> WebDavResponse:
        request = urllib.request.Request(url=url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - user configured WebDAV URL
                return WebDavResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            body = error.read()
            message = body.decode("utf-8", errors="replace").strip() or error.reason
            raise MemorySyncError(f"WebDAV 请求失败（{error.code}）：{message}", status_code=int(error.code)) from error
        except OSError as error:
            raise MemorySyncError(f"WebDAV 连接失败：{error}", status_code=502) from error

    def _basic_auth_header(self) -> str:
        token = f"{self._username()}:{self._password()}".encode("utf-8")
        return "Basic " + b64encode(token).decode("ascii")

    def _username(self) -> str:
        return os.getenv(self.config["username_env"], "").strip()

    def _password(self) -> str:
        return os.getenv(self.config["password_env"], "").strip()

    def _load_env(self) -> None:
        env_path = self.root_dir / ".env"
        if not env_path.exists():
            return
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")

    def _temp_sqlite_path(self, label: str) -> Path:
        temp_dir = self.root_dir / "data" / "logs"
        temp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"reagent_memory_{label}_", suffix=".sqlite", dir=temp_dir)
        os.close(fd)
        return Path(name)

    @staticmethod
    def _remove_sqlite_sidecars(path: Path) -> None:
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)

    @staticmethod
    def _is_remote_newer(remote: dict[str, Any], local: dict[str, Any]) -> bool:
        return _parse_iso(remote.get("updated_at")) > _parse_iso(local.get("updated_at"))

    @staticmethod
    def _is_local_newer(remote: dict[str, Any], local: dict[str, Any]) -> bool:
        return _parse_iso(local.get("updated_at")) > _parse_iso(remote.get("updated_at"))

    @staticmethod
    def _has_possible_conflict(remote: dict[str, Any], local: dict[str, Any], state: dict[str, Any]) -> bool:
        remote_version = str(remote.get("version") or "")
        last_remote_version = str(state.get("last_remote_version") or "")
        last_sync = max(_parse_iso(state.get("last_upload_at")), _parse_iso(state.get("last_download_at")))
        local_updated = _parse_iso(local.get("updated_at"))
        return bool(remote_version and last_remote_version and remote_version != last_remote_version and local_updated > last_sync)


def _parse_iso(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.min


def _looks_like_missing_ancestor(message: str) -> bool:
    lowered = message.lower()
    return "ancestorsnotfound" in lowered or "ancestors of this location" in lowered
