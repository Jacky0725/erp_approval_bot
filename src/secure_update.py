from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
from typing import Any, Callable
from urllib.parse import urlparse
import urllib.request
import zipfile


ALLOWED_DOWNLOAD_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
MAX_ASSET_SIZE = 512 * 1024 * 1024
MAX_EXPANDED_SIZE = 2 * 1024 * 1024 * 1024
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:\.[0-9]+)?$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ManifestAsset:
    name: str
    kind: str
    arch: str
    size: int
    sha256: str
    url: str = ""
    browser_revision: str = ""


@dataclass(frozen=True)
class ReleaseManifest:
    release_version: str
    repository: str
    channel: str
    assets: tuple[ManifestAsset, ...]

    def asset(self, kind: str) -> ManifestAsset | None:
        return next((asset for asset in self.assets if asset.kind == kind), None)


class UpdateState:
    VALID = {
        "idle", "checking", "downloading", "verifying", "staging", "waiting_exit",
        "switching", "health_check", "rollback", "succeeded", "failed",
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "idle"
        self._message = ""

    def set(self, state: str, message: str = "") -> None:
        if state not in self.VALID:
            raise ValueError(f"Invalid update state: {state}")
        with self._lock:
            self._state, self._message = state, message

    def as_dict(self) -> dict[str, str]:
        with self._lock:
            return {"state": self._state, "message": self._message}


UPDATE_STATE = UpdateState()


def validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_DOWNLOAD_HOSTS:
        raise ValueError("Update assets must use an approved GitHub HTTPS download host.")


def parse_manifest(payload: dict[str, Any], *, expected_repository: str, expected_version: str) -> ReleaseManifest:
    version = str(payload.get("release_version") or "")
    repository = str(payload.get("repository") or "")
    channel = str(payload.get("channel") or "")
    if not SEMVER.fullmatch(version) or version != expected_version:
        raise ValueError("Manifest version does not match the release tag.")
    if repository != expected_repository or channel != "stable":
        raise ValueError("Manifest repository or channel is invalid.")
    assets: list[ManifestAsset] = []
    seen: set[str] = set()
    for raw in payload.get("assets") or []:
        name = str(raw.get("name") or "")
        kind = str(raw.get("kind") or "")
        arch = str(raw.get("arch") or "")
        size = int(raw.get("size") or 0)
        digest = str(raw.get("sha256") or "").lower()
        revision = str(raw.get("browser_revision") or "")
        expected_core = f"ReagentApprovalBot-core-{version}-windows-amd64.zip"
        if kind == "core" and name != expected_core:
            raise ValueError("Core asset name is invalid.")
        if kind == "browser" and not re.fullmatch(r"ReagentApprovalBot-browser-chromium-headless-[A-Za-z0-9._-]+-windows-amd64\.zip", name):
            raise ValueError("Browser asset name is invalid.")
        if kind not in {"core", "browser"} or arch != "amd64" or name in seen:
            raise ValueError("Manifest contains an unsupported or duplicate asset.")
        if not 0 < size <= MAX_ASSET_SIZE or not SHA256.fullmatch(digest):
            raise ValueError("Manifest asset size or SHA-256 is invalid.")
        if kind == "browser" and (not revision or revision not in name):
            raise ValueError("Browser revision is missing or inconsistent.")
        seen.add(name)
        assets.append(ManifestAsset(name, kind, arch, size, digest, browser_revision=revision))
    if not any(asset.kind == "core" for asset in assets):
        raise ValueError("Manifest does not contain a core asset.")
    return ReleaseManifest(version, repository, channel, tuple(assets))


def attach_asset_urls(manifest: ReleaseManifest, release_assets: list[dict[str, Any]]) -> ReleaseManifest:
    urls = {str(item.get("name") or ""): str(item.get("browser_download_url") or "") for item in release_assets}
    attached: list[ManifestAsset] = []
    for asset in manifest.assets:
        url = urls.get(asset.name, "")
        validate_download_url(url)
        attached.append(ManifestAsset(**{**asset.__dict__, "url": url}))
    return ReleaseManifest(manifest.release_version, manifest.repository, manifest.channel, tuple(attached))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_download(
    asset: ManifestAsset,
    destination_dir: Path,
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 600,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Path:
    validate_download_url(asset.url)
    destination_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(destination_dir).free
    if free < asset.size * 2 + 64 * 1024 * 1024:
        raise OSError("Insufficient disk space for the verified update.")
    fd, temp_name = tempfile.mkstemp(prefix="download-", suffix=".part", dir=destination_dir)
    os.close(fd)
    temporary = Path(temp_name)
    destination = destination_dir / asset.name
    digest = hashlib.sha256()
    downloaded = 0
    try:
        request = urllib.request.Request(asset.url, headers=headers or {})
        with opener(request, timeout=timeout_seconds) as response, temporary.open("wb") as output:
            final_url = response.geturl()
            validate_download_url(final_url)
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length and content_length != asset.size:
                raise ValueError("Download Content-Length does not match the manifest.")
            while chunk := response.read(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > asset.size or downloaded > MAX_ASSET_SIZE:
                    raise ValueError("Downloaded asset exceeds its declared size.")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if downloaded != asset.size:
            raise ValueError("Downloaded asset is truncated.")
        if not hmac.compare_digest(digest.hexdigest(), asset.sha256):
            raise ValueError("Downloaded asset SHA-256 does not match the manifest.")
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def safe_extract_zip(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    expanded = 0
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            parts = PurePosixPath(info.filename.replace("\\", "/"))
            if parts.is_absolute() or ".." in parts.parts:
                raise ValueError(f"Unsafe archive path: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"Archive links are not allowed: {info.filename}")
            expanded += info.file_size
            if expanded > MAX_EXPANDED_SIZE:
                raise ValueError("Expanded archive exceeds the safety limit.")
            target = (destination / Path(*parts.parts)).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"Archive path escapes destination: {info.filename}") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_pointer(install_root: Path, name: str = "current.json") -> dict[str, Any] | None:
    path = install_root / name
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        version = str(payload.get("version") or "")
        relative = Path(str(payload.get("path") or ""))
        target = (install_root / relative).resolve()
        target.relative_to((install_root / "versions").resolve())
        if not SEMVER.fullmatch(version) or not target.is_dir() or target.name != version:
            return None
        return payload
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return None


def stage_version(core_zip: Path, install_root: Path, version: str) -> Path:
    if not SEMVER.fullmatch(version):
        raise ValueError("Invalid target version.")
    versions = install_root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    staging = versions / f".{version}.staging-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    safe_extract_zip(core_zip, staging)
    candidates = list(staging.rglob("ReagentApprovalBot.exe"))
    if len(candidates) != 1:
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError("Core archive must contain exactly one application executable.")
    payload_root = candidates[0].parent
    target = versions / version
    if target.exists():
        shutil.rmtree(staging, ignore_errors=True)
        return target
    if payload_root != staging:
        normalized = versions / f".{version}.normalized-{os.getpid()}"
        payload_root.rename(normalized)
        shutil.rmtree(staging, ignore_errors=True)
        staging = normalized
    staging.rename(target)
    return target


def switch_current(install_root: Path, version: str, browser_revision: str = "") -> None:
    target = install_root / "versions" / version
    if not target.is_dir():
        raise FileNotFoundError(target)
    current = read_pointer(install_root)
    if current:
        atomic_json(install_root / "previous.json", current)
    atomic_json(
        install_root / "current.json",
        {"version": version, "path": f"versions/{version}", "browser_revision": browser_revision},
    )


def rollback_current(install_root: Path) -> bool:
    previous = read_pointer(install_root, "previous.json")
    if not previous:
        return False
    current = read_pointer(install_root)
    atomic_json(install_root / "current.json", previous)
    if current:
        atomic_json(install_root / "previous.json", current)
    return True
