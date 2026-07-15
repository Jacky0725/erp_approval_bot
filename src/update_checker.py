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
import textwrap
import urllib.error
import urllib.request

from app_info import app_repository, app_version
from runtime_paths import runtime_root


GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_LATEST = "https://github.com/{repo}/releases/latest"
GITHUB_DOWNLOAD = "https://github.com/{repo}/releases/download/{tag}/{asset}"
SETUP_ASSET_MARKER = "-win-x64-"
SETUP_ASSET_SUFFIX = "setup.exe"
PORTABLE_ASSET_SUFFIX = "portable.zip"
PREFERRED_SETUP_MARKER = "-lite-setup.exe"
PREFERRED_PORTABLE_MARKER = "-lite-portable.zip"


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


def choose_portable_asset(assets: list[dict[str, object]]) -> ReleaseAsset | None:
    candidates: list[ReleaseAsset] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if SETUP_ASSET_MARKER not in name or not name.endswith(PORTABLE_ASSET_SUFFIX) or not url:
            continue
        candidates.append(ReleaseAsset(name=name, url=url, size=int(asset.get("size") or 0)))
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            1 if item.name.endswith(PREFERRED_PORTABLE_MARKER) else 0,
            0 if "full-portable" in item.name else 1,
            item.name,
        ),
    )[-1]


def choose_update_asset(assets: list[dict[str, object]]) -> ReleaseAsset | None:
    return choose_portable_asset(assets) or choose_setup_asset(assets)


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
    asset_name = f"reagent-approval-bot-{version}-win-x64-lite-portable.zip"
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
    asset = choose_update_asset(list(release.get("assets") or []))
    return UpdateInfo(
        ok=True,
        current_version=current,
        latest_version=latest,
        update_available=bool(latest and is_newer_version(latest, current) and asset),
        release_url=str(release.get("html_url") or ""),
        asset=asset,
        error="" if asset else "No Windows x64 portable or setup asset was found in the latest release.",
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


def update_launch_copy(update_path: Path) -> Path:
    source = update_path.resolve()
    launch_dir = Path(tempfile.gettempdir()) / "ReagentApprovalBotUpdates"
    launch_dir.mkdir(parents=True, exist_ok=True)
    destination = launch_dir / source.name
    if source != destination.resolve():
        tmp = destination.with_suffix(destination.suffix + ".copying")
        shutil.copy2(source, tmp)
        tmp.replace(destination)
    return destination


def installer_launch_copy(installer_path: Path) -> Path:
    return update_launch_copy(installer_path)


def portable_launch_copy(portable_path: Path) -> Path:
    return update_launch_copy(portable_path)


def launch_portable_updater(portable_zip: Path) -> Path:
    launch_path = portable_launch_copy(portable_zip)
    install_dir = Path(sys.executable).resolve().parent
    exe_path = Path(sys.executable).resolve()
    log_path = runtime_root() / "data" / "logs" / "update.log"
    script_path = launch_path.parent / "apply_reagent_update.ps1"
    script = portable_update_script(
        zip_path=launch_path,
        install_dir=install_dir,
        exe_path=exe_path,
        current_pid=os.getpid(),
        log_path=log_path,
    )
    script_path.write_text(script, encoding="utf-8")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        cwd=str(launch_path.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    return script_path


def ps_quote(path: Path | str) -> str:
    return str(path).replace("'", "''")


def portable_update_script(zip_path: Path, install_dir: Path, exe_path: Path, current_pid: int, log_path: Path) -> str:
    return textwrap.dedent(
        f"""
        $ErrorActionPreference = "Stop"
        $ZipPath = '{ps_quote(zip_path)}'
        $InstallDir = '{ps_quote(install_dir)}'
        $ExePath = '{ps_quote(exe_path)}'
        $CurrentPid = {int(current_pid)}
        $LogPath = '{ps_quote(log_path)}'
        $LogDir = Split-Path -Parent $LogPath
        New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        function Write-UpdateLog([string]$Message) {{
            $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
            Add-Content -Path $LogPath -Value "$stamp $Message" -Encoding UTF8
        }}
        try {{
            Write-UpdateLog "Updater started. Zip=$ZipPath InstallDir=$InstallDir"
            if (!(Test-Path -LiteralPath $ZipPath)) {{ throw "Update zip not found: $ZipPath" }}
            $deadline = (Get-Date).AddSeconds(90)
            while ((Get-Date) -lt $deadline) {{
                $proc = Get-Process -Id $CurrentPid -ErrorAction SilentlyContinue
                if ($null -eq $proc) {{ break }}
                Start-Sleep -Milliseconds 500
            }}
            if (Get-Process -Id $CurrentPid -ErrorAction SilentlyContinue) {{
                throw "Current application did not exit within 90 seconds. Close it and retry update."
            }}

            $WorkRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("ReagentApprovalBotUpdate_" + [Guid]::NewGuid().ToString("N"))
            $ExtractDir = Join-Path $WorkRoot "extract"
            New-Item -ItemType Directory -Force -Path $ExtractDir | Out-Null
            Write-UpdateLog "Extracting update zip..."
            Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractDir -Force
            $PayloadExe = Get-ChildItem -LiteralPath $ExtractDir -Recurse -Filter "ReagentApprovalBot.exe" | Select-Object -First 1
            if ($null -eq $PayloadExe) {{ throw "ReagentApprovalBot.exe was not found in update zip." }}
            $PayloadRoot = $PayloadExe.Directory.FullName

            Write-UpdateLog "Replacing application files..."
            New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
            Get-ChildItem -LiteralPath $InstallDir -Force | Where-Object {{
                $_.Name -notin @("data", ".env")
            }} | Remove-Item -Recurse -Force
            Copy-Item -LiteralPath (Join-Path $PayloadRoot "*") -Destination $InstallDir -Recurse -Force

            Write-UpdateLog "Starting updated application..."
            Start-Process -FilePath $ExePath -WorkingDirectory $InstallDir
            Write-UpdateLog "Update finished."
            Remove-Item -LiteralPath $WorkRoot -Recurse -Force -ErrorAction SilentlyContinue
        }} catch {{
            Write-UpdateLog ("Update failed: " + $_.Exception.Message)
            Add-Type -AssemblyName PresentationFramework -ErrorAction SilentlyContinue
            [System.Windows.MessageBox]::Show(("Reagent Approval Bot update failed.`n" + $_.Exception.Message + "`n`nLog: " + $LogPath), "Update failed", "OK", "Error") | Out-Null
            exit 1
        }}
        """
    ).strip() + "\n"


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


def launch_update_package(update_path: Path) -> Path | None:
    if update_path.name.endswith(PORTABLE_ASSET_SUFFIX):
        return launch_portable_updater(update_path)
    launch_installer(update_path)
    return None
