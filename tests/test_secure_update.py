import hashlib
import json
import sys
from pathlib import Path
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from secure_update import atomic_json, parse_manifest, read_pointer, safe_extract_zip, stage_version, switch_current


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
