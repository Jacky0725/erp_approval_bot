from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_pe(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        return stream.read(2) == b"MZ"


def verify_zip_layout(path: Path, kind: str) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if any(name.startswith("/") or ".." in Path(name).parts for name in names):
                return [f"unsafe {kind} ZIP path: {path.name}"]
            if kind == "core" and not (
                "ReagentApprovalBot.exe" in names and any(name.startswith("_internal/") for name in names)
            ):
                return [f"incomplete core ZIP layout: {path.name}"]
            if kind == "browser" and not any(name.endswith("chrome-headless-shell.exe") for name in names):
                return [f"incomplete browser ZIP layout: {path.name}"]
            if archive.testzip() is not None:
                return [f"corrupt {kind} ZIP: {path.name}"]
    except (OSError, zipfile.BadZipFile):
        return [f"invalid {kind} ZIP: {path.name}"]
    return []


def verify_release_assets(release_dir: Path, version: str) -> list[str]:
    errors: list[str] = []
    manifest_path = release_dir / "release-manifest.json"
    if not manifest_path.is_file():
        return ["release-manifest.json is missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"release-manifest.json is invalid: {exc}"]
    if manifest.get("release_version") != version:
        errors.append("manifest version does not match VERSION/tag")
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        return errors + ["manifest assets must be a list"]
    for kind in ("core", "browser"):
        matched = [asset for asset in assets if isinstance(asset, dict) and asset.get("kind") == kind]
        if len(matched) != 1:
            errors.append(f"expected exactly one {kind} asset")
            continue
        asset = matched[0]
        name = asset.get("name")
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".zip"):
            errors.append(f"invalid {kind} asset name")
            continue
        if version not in name:
            errors.append(f"{kind} asset version mismatch")
        path = release_dir / name
        if not path.is_file():
            errors.append(f"missing {kind} asset: {name}")
            continue
        if path.stat().st_size != asset.get("size"):
            errors.append(f"{kind} asset size mismatch: {name}")
        if sha256_file(path).lower() != str(asset.get("sha256") or "").lower():
            errors.append(f"{kind} asset SHA-256 mismatch: {name}")
        errors.extend(verify_zip_layout(path, kind))
        if not (release_dir / f"{name}.size.json").is_file():
            errors.append(f"missing {kind} size report: {name}.size.json")
    setup = release_dir / f"ReagentApprovalBot-setup-{version}-windows-amd64.exe"
    if not setup.is_file():
        errors.append(f"missing setup asset: {setup.name}")
    elif not is_pe(setup):
        errors.append(f"invalid setup PE header: {setup.name}")
    if not (release_dir / f"{setup.name}.size.json").is_file():
        errors.append(f"missing setup size report: {setup.name}.size.json")
    for name in ("ReagentApprovalBotBootstrap.exe", "ReagentApprovalBotUpdater.exe"):
        path = release_dir / name
        if not is_pe(path):
            errors.append(f"missing or invalid installer component: {name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    errors = verify_release_assets(args.release_dir, args.version)
    for error in errors:
        print(f"ERROR: {error}")
    if not errors:
        print("Release assets: local preflight passed")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
