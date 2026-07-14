from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from app_info import app_repository, app_version
from runtime_paths import runtime_root


GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_LATEST = "https://github.com/{repo}/releases/latest"
GITHUB_DOWNLOAD = "https://github.com/{repo}/releases/download/{tag}/{asset}"
SETUP_ASSET_MARKER = "-win-x64-"
SETUP_ASSET_SUFFIX = "setup.exe"
PREFERRED_SETUP_MARKER = "-lite-setup.exe"


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int


@dataclass(frozen=True)
class UpdateInfo:
    ok: bool
    current_version: str
    latest_version: str = ""
    update_available: bool = False
    release_url: str = ""
    asset: ReleaseAsset | None = None
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "release_url": self.release_url,
            "asset": None if self.asset is None else self.asset.__dict__,
            "error": self.error,
        }


def parse_version(value: str) -> tuple[int, ...]:
    text = str(value or "")
    match = re.search(r"(?<!\d)(\d+(?:\.\d+){1,3})(?!\d)", text)
    parts = (match.group(1).split(".") if match else re.findall(r"\d+", text))
    return tuple(int(part) for part in parts[:4]) if parts else (0,)


def is_newer_version(latest: str, current: str) -> bool:
    latest_parts = parse_version(latest)
    current_parts = parse_version(current)
    width = max(len(latest_parts), len(current_parts))
    return latest_parts + (0,) * (width - len(latest_parts)) > current_parts + (0,) * (width - len(current_parts))


def normalize_tag(tag: str) -> str:
    return str(tag or "").strip().lstrip("vV")


def choose_setup_asset(assets: list[dict[str, object]]) -> ReleaseAsset | None:
    candidates: list[ReleaseAsset] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if SETUP_ASSET_MARKER not in name or not name.endswith(SETUP_ASSET_SUFFIX) or not url:
            continue
        candidates.append(ReleaseAsset(name=name, url=url, size=int(asset.get("size") or 0)))
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            1 if item.name.endswith(PREFERRED_SETUP_MARKER) else 0,
            0 if "full-test" in item.name else 1,
            item.name,
        ),
    )[-1]


def github_token() -> str:
    return (
        os.getenv("REAGENT_APPROVAL_UPDATE_TOKEN")
        or os.getenv("GITHUB_TOKEN")
        or os.getenv("GH_TOKEN")
        or ""
    ).strip()


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "reagent-approval-bot-updater",
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_latest_release(timeout_seconds: int = 20) -> dict[str, object]:
    repo = app_repository()
    request = urllib.request.Request(
        GITHUB_API.format(repo=repo),
        headers=github_headers(),
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_latest_release_public(timeout_seconds: int = 20) -> dict[str, object]:
    repo = app_repository()
    request = urllib.request.Request(
        GITHUB_LATEST.format(repo=repo),
        headers={"User-Agent": "reagent-approval-bot-updater"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        final_url = response.geturl()
    tag = final_url.rstrip("/").split("/")[-1]
    version = normalize_tag(tag)
    asset_name = f"reagent-approval-bot-{version}-win-x64-lite-setup.exe"
    asset_url = GITHUB_DOWNLOAD.format(repo=repo, tag=tag, asset=asset_name)
    asset_size = 0
    try:
        head = urllib.request.Request(asset_url, method="HEAD", headers={"User-Agent": "reagent-approval-bot-updater"})
        with urllib.request.urlopen(head, timeout=timeout_seconds) as response:
            asset_size = int(response.headers.get("Content-Length") or 0)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        asset_size = 0
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": asset_url,
                "size": asset_size,
            }
        ],
    }


def check_for_update(current_version: str | None = None, timeout_seconds: int = 20) -> UpdateInfo:
    current = normalize_tag(current_version or app_version())
    try:
        release = fetch_latest_release_public(timeout_seconds=timeout_seconds)
    except (urllib.error.URLError, TimeoutError, OSError) as public_error:
        try:
            release = fetch_latest_release(timeout_seconds=timeout_seconds)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as api_error:
            return UpdateInfo(ok=False, current_version=current, error=f"public release check failed: {public_error}; GitHub API failed: {api_error}")

    latest = normalize_tag(str(release.get("tag_name") or release.get("name") or ""))
    asset = choose_setup_asset(list(release.get("assets") or []))
    return UpdateInfo(
        ok=True,
        current_version=current,
        latest_version=latest,
        update_available=bool(latest and is_newer_version(latest, current) and asset),
        release_url=str(release.get("html_url") or ""),
        asset=asset,
        error="" if asset else "No Windows x64 setup asset was found in the latest release.",
    )


def updates_dir() -> Path:
    path = runtime_root() / "data" / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def download_update(asset: ReleaseAsset, timeout_seconds: int = 120) -> Path:
    destination = updates_dir() / asset.name
    tmp = destination.with_suffix(destination.suffix + ".download")
    request = urllib.request.Request(asset.url, headers=github_headers())
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response, tmp.open("wb") as file:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            file.write(chunk)
    tmp.replace(destination)
    return destination


def current_process_is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def installer_launch_copy(installer_path: Path) -> Path:
    source = installer_path.resolve()
    launch_dir = Path(tempfile.gettempdir()) / "ReagentApprovalBotUpdates"
    launch_dir.mkdir(parents=True, exist_ok=True)
    destination = launch_dir / source.name
    if source != destination.resolve():
        tmp = destination.with_suffix(destination.suffix + ".copying")
        shutil.copy2(source, tmp)
        tmp.replace(destination)
    return destination


def launch_installer(installer_path: Path) -> None:
    launch_path = installer_launch_copy(installer_path)
    env = os.environ.copy()
    env["REAGENT_APPROVAL_START_AFTER_INSTALL"] = "1"
    env["REAGENT_APPROVAL_WAIT_FOR_PID"] = str(os.getpid())
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [str(launch_path)],
        cwd=str(launch_path.parent),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
