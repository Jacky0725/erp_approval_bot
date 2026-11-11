from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile


MODULE_PATH = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "verify_release_assets.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("verify_release_assets", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SIZE_CHECKER = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "check_package_size.py"


def test_size_checker_writes_default_report_next_to_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "ReagentApprovalBot-core-0.1.21-windows-amd64.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("ReagentApprovalBot.exe", b"MZ")

    subprocess.run(
        [sys.executable, str(SIZE_CHECKER), "--kind", "core", "--artifact", str(artifact)],
        check=True,
    )

    report = json.loads((tmp_path / f"{artifact.name}.size.json").read_text(encoding="utf-8"))
    assert report["artifact"] == artifact.name


def test_release_asset_preflight_detects_missing_and_tampered_assets(tmp_path: Path) -> None:
    version = "0.1.20"
    assets = []
    for kind in ("core", "browser"):
        name = (
            f"ReagentApprovalBot-core-{version}-windows-amd64.zip"
            if kind == "core"
            else "ReagentApprovalBot-browser-chromium-headless-1248-windows-amd64.zip"
        )
        with zipfile.ZipFile(tmp_path / name, "w") as archive:
            if kind == "core":
                archive.writestr("ReagentApprovalBot.exe", b"MZ")
                archive.writestr("_internal/app.txt", b"app")
            else:
                archive.writestr("chromium_headless_shell-123/chrome-headless-shell.exe", b"MZ")
        (tmp_path / f"{name}.size.json").write_text("{}", encoding="utf-8")
        payload = (tmp_path / name).read_bytes()
        asset = {"kind": kind, "name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        if kind == "browser":
            asset["browser_revision"] = "1248"
        assets.append(asset)
    setup = f"ReagentApprovalBot-setup-{version}-windows-amd64.exe"
    (tmp_path / setup).write_bytes(b"MZsetup")
    (tmp_path / f"{setup}.size.json").write_text("{}", encoding="utf-8")
    for name in ("ReagentApprovalBotBootstrap.exe", "ReagentApprovalBotUpdater.exe"):
        (tmp_path / name).write_bytes(b"MZcomponent")
    (tmp_path / "release-manifest.json").write_text(
        json.dumps({"release_version": version, "assets": assets}), encoding="utf-8"
    )

    assert MODULE.verify_release_assets(tmp_path, version) == []
    errors = []
    oversized = tmp_path / "oversized.exe"
    with oversized.open("wb") as stream:
        stream.truncate(MODULE.LIMITS["setup"] * 1024 * 1024 + 1)
    MODULE.check_budget(oversized, "setup", errors)
    assert errors == ["setup asset exceeds 130 MiB hard limit: oversized.exe"]
    (tmp_path / assets[0]["name"]).write_bytes(b"modified")
    assert any("SHA-256 mismatch" in error for error in MODULE.verify_release_assets(tmp_path, version))
    (tmp_path / assets[1]["name"]).unlink()
    assert any("missing browser asset" in error for error in MODULE.verify_release_assets(tmp_path, version))
