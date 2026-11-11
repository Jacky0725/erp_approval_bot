import hashlib
import io
import json
import sys
from pathlib import Path
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from secure_update import ManifestAsset, atomic_json, parse_manifest, read_pointer, rename_staged_path, safe_extract_zip, stage_version, switch_current, verified_download


def manifest(version="0.1.21"):
    return {
        "release_version": version,
        "repository": "owner/repo",
        "channel": "stable",
        "assets": [{
            "name": f"ReagentApprovalBot-core-{version}-windows-amd64.zip",
            "kind": "core", "arch": "amd64", "size": 10,
            "sha256": "a" * 64,
        }],
    }


def test_manifest_requires_exact_identity():
    parsed = parse_manifest(manifest(), expected_repository="owner/repo", expected_version="0.1.21")
    assert parsed.asset("core").name.endswith("amd64.zip")
    with pytest.raises(ValueError):
        parse_manifest(manifest(), expected_repository="other/repo", expected_version="0.1.21")


def test_safe_extract_rejects_zip_slip(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../escape.txt", "no")
    with pytest.raises(ValueError, match="Unsafe"):
        safe_extract_zip(archive, tmp_path / "out")


def test_staged_version_switch_preserves_previous_pointer(tmp_path):
    archive = tmp_path / "core.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("payload/ReagentApprovalBot.exe", "stub")
    root = tmp_path / "install"
    old = root / "versions" / "0.1.20"
    old.mkdir(parents=True)
    (old / "ReagentApprovalBot.exe").write_text("old")
    atomic_json(root / "current.json", {"version": "0.1.20", "path": "versions/0.1.20"})
    stage_version(archive, root, "0.1.21")
    switch_current(root, "0.1.21")
    assert read_pointer(root)["version"] == "0.1.21"
    assert read_pointer(root, "previous.json")["version"] == "0.1.20"
    stage_version(archive, root, "0.1.21", repair=True)
    switch_current(root, "0.1.21")
    assert read_pointer(root, "previous.json")["version"] == "0.1.20"


def test_verified_download_reports_bytes_without_weakening_verification(tmp_path):
    payload = b"x" * (1024 * 1024 + 50)
    url = "https://github.com/owner/repo/releases/download/v0.1.23/browser.zip"

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

        def geturl(self):
            return url

    asset = ManifestAsset("browser.zip", "browser", "amd64", len(payload), hashlib.sha256(payload).hexdigest(), url)
    updates = []
    output = verified_download(asset, tmp_path, opener=lambda *args, **kwargs: Response(payload),
                               progress=lambda received, total: updates.append((received, total)))
    assert output.read_bytes() == payload
    assert updates == [(0, len(payload)), (1024 * 1024, len(payload)), (len(payload), len(payload))]


def test_staged_rename_retries_transient_windows_lock(tmp_path, monkeypatch):
    source = tmp_path / "stage"
    target = tmp_path / "ready"
    source.mkdir()
    original_rename = Path.rename
    waits = []

    def rename(path, destination):
        if not waits:
            lock = PermissionError("locked during scan")
            lock.winerror = 5
            raise lock
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr("secure_update.time.sleep", waits.append)
    rename_staged_path(source, target)
    assert target.is_dir() and not source.exists()
    assert waits == [0.2]
