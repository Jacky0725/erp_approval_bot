from __future__ import annotations

import os
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from memory_sync import MemorySyncConflict, MemorySyncError, MemorySyncService, WebDavResponse  # noqa: E402
from reagent_memory import ReagentMemory  # noqa: E402


class FakeWebDav:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.methods: list[tuple[str, str]] = []

    def request(self, method: str, url: str, data: bytes | None, headers: dict[str, str]) -> WebDavResponse:
        self.methods.append((method, url))
        if "Authorization" not in headers:
            raise AssertionError("missing auth header")
        if method == "PROPFIND":
            if url in self.dirs:
                return WebDavResponse(207, {}, b"")
            raise MemorySyncError("not found", status_code=404)
        if method == "MKCOL":
            self.dirs.add(url)
            return WebDavResponse(201, {}, b"")
        if method == "PUT":
            self.files[url] = data or b""
            return WebDavResponse(201, {}, b"")
        if method == "GET":
            if url not in self.files:
                raise MemorySyncError("not found", status_code=404)
            return WebDavResponse(200, {}, self.files[url])
        if method == "DELETE":
            self.files.pop(url, None)
            return WebDavResponse(204, {}, b"")
        raise AssertionError(f"unexpected method {method}")


class MemorySyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "data" / "logs").mkdir(parents=True)
        self.settings = {
            "paths": {"reagent_memory_sqlite": "data/reagent_memory.sqlite"},
            "memory_sync": {
                "enabled": True,
                "provider": "webdav",
                "base_url": "https://dav.example.test/dav/",
                "remote_dir": "approval/memory",
                "username_env": "SYNC_USER",
                "password_env": "SYNC_PASSWORD",
                "keep_versions": 2,
                "auto_upload_after_memory_change": True,
                "check_remote_on_startup": True,
            },
        }
        os.environ["SYNC_USER"] = "user"
        os.environ["SYNC_PASSWORD"] = "pass"
        self.fake = FakeWebDav()
        self.service = MemorySyncService(
            root_dir=self.root,
            settings=self.settings,
            request_func=self.fake.request,
            now_func=lambda: datetime(2026, 8, 20, 10, 3, 10),
        )
        memory = ReagentMemory.from_settings(self.settings, self.root)
        memory.add_record(
            raw_name="测试试剂",
            cleaned_name="测试试剂",
            standard_name="测试试剂",
            cas="",
            final_category="普通类",
            confidence=1.0,
            reason="unit test",
            source="test",
            manual_verified=True,
        )

    def tearDown(self) -> None:
        os.environ.pop("SYNC_USER", None)
        os.environ.pop("SYNC_PASSWORD", None)
        self.tmp.cleanup()

    def test_remote_url_normalizes_and_encodes_paths(self) -> None:
        url = self.service._remote_url("versions/reagent memory.sqlite")  # noqa: SLF001

        self.assertEqual(url, "https://dav.example.test/dav/approval/memory/versions/reagent%20memory.sqlite")

    def test_upload_writes_latest_version_and_manifest(self) -> None:
        result = self.service.upload()

        self.assertTrue(result["ok"])
        urls = list(self.fake.files)
        self.assertTrue(any(url.endswith("/reagent_memory_latest.sqlite") for url in urls))
        self.assertTrue(any(url.endswith("/versions/reagent_memory_20260820_100310.sqlite") for url in urls))
        manifest_url = next(url for url in urls if url.endswith("/manifest.json"))
        manifest = yaml.safe_load(self.fake.files[manifest_url].decode("utf-8"))
        self.assertEqual(manifest["latest"]["version"], "20260820_100310")
        self.assertEqual(manifest["latest"]["records"], 1)
        self.assertEqual(self.service.read_state()["last_remote_version"], "20260820_100310")

    def test_upload_creates_versions_directory_under_remote_dir_once(self) -> None:
        self.service.upload()

        created_dirs = [url for method, url in self.fake.methods if method == "MKCOL"]
        self.assertIn("https://dav.example.test/dav/approval", created_dirs)
        self.assertIn("https://dav.example.test/dav/approval/memory", created_dirs)
        self.assertIn("https://dav.example.test/dav/approval/memory/versions", created_dirs)
        self.assertFalse(any("/approval/memory/approval" in url for url in created_dirs))

    def test_snapshot_is_valid_sqlite(self) -> None:
        snapshot = self.service.create_sqlite_snapshot(self.root / "data" / "reagent_memory.sqlite")
        try:
            with closing(sqlite3.connect(snapshot)) as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM reagent_memory").fetchone()[0], 1)
        finally:
            snapshot.unlink(missing_ok=True)

    def test_download_rejects_invalid_sqlite_and_keeps_local_file(self) -> None:
        self.service.upload()
        latest_url = next(url for url in self.fake.files if url.endswith("/reagent_memory_latest.sqlite"))
        self.fake.files[latest_url] = b"not sqlite"
        before = (self.root / "data" / "reagent_memory.sqlite").read_bytes()

        with self.assertRaises(MemorySyncError):
            self.service.download(force=True)

        self.assertEqual((self.root / "data" / "reagent_memory.sqlite").read_bytes(), before)

    def test_download_creates_local_backup_before_replace(self) -> None:
        self.service.upload()
        result = self.service.download(force=True)

        self.assertTrue(result["backup"])
        self.assertTrue(Path(result["backup"]).exists())
        self.assertEqual(self.service.read_state()["last_download_at"], "2026-08-20T10:03:10")

    def test_download_blocks_possible_conflict_without_force(self) -> None:
        self.service.upload()
        self.service.update_state(last_remote_version="older", last_download_at="2026-08-19T10:00:00")
        with closing(sqlite3.connect(self.root / "data" / "reagent_memory.sqlite")) as conn:
            conn.execute("UPDATE reagent_memory SET updated_at = '2026-08-21T10:00:00'")
            conn.commit()

        with self.assertRaises(MemorySyncConflict):
            self.service.download()

    def test_missing_credentials_return_clear_error(self) -> None:
        os.environ.pop("SYNC_PASSWORD", None)

        with self.assertRaisesRegex(MemorySyncError, "用户名或应用密码"):
            self.service.test_connection()

    def test_missing_manifest_is_empty_remote_not_connection_failure(self) -> None:
        result = self.service.test_connection()
        status = self.service.status(check_remote=True)

        self.assertTrue(result["ok"])
        self.assertTrue(status["ok"])
        self.assertEqual(status["remote"], {})
        self.assertIn("尚未上传", status["message"])

    def test_missing_webdav_ancestor_is_not_ignored(self) -> None:
        attempts = {"mkcol": 0}

        def request(method: str, url: str, data: bytes | None, headers: dict[str, str]) -> WebDavResponse:
            if method == "PROPFIND":
                raise MemorySyncError("not found", status_code=404)
            if method == "MKCOL":
                attempts["mkcol"] += 1
                raise MemorySyncError(
                    "WebDAV 请求失败（409）：<s:exception>AncestorsNotFound</s:exception>",
                    status_code=409,
                )
            raise AssertionError(f"unexpected method {method}")

        service = MemorySyncService(
            root_dir=self.root,
            settings=self.settings,
            request_func=request,
            now_func=lambda: datetime(2026, 8, 20, 10, 3, 10),
        )

        with patch("memory_sync.time.sleep", return_value=None), self.assertRaisesRegex(MemorySyncError, "父目录不存在"):
            service.test_connection()
        self.assertEqual(attempts["mkcol"], 4)

    def test_missing_webdav_ancestor_retries_before_success(self) -> None:
        attempts = {"mkcol": 0}

        def request(method: str, url: str, data: bytes | None, headers: dict[str, str]) -> WebDavResponse:
            if method == "PROPFIND":
                raise MemorySyncError("not found", status_code=404)
            if method == "MKCOL":
                attempts["mkcol"] += 1
                if attempts["mkcol"] < 3:
                    raise MemorySyncError(
                        "WebDAV 请求失败（409）：<s:exception>AncestorsNotFound</s:exception>",
                        status_code=409,
                    )
                return WebDavResponse(201, {}, b"")
            if method == "GET":
                raise MemorySyncError("not found", status_code=404)
            raise AssertionError(f"unexpected method {method}")

        service = MemorySyncService(
            root_dir=self.root,
            settings=self.settings,
            request_func=request,
            now_func=lambda: datetime(2026, 8, 20, 10, 3, 10),
        )

        with patch("memory_sync.time.sleep", return_value=None):
            result = service.test_connection()

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(attempts["mkcol"], 3)


if __name__ == "__main__":
    unittest.main()
