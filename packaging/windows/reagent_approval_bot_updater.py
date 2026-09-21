from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def _module_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2] / "src"


sys.path.insert(0, str(_module_dir()))
from secure_update import (  # noqa: E402
    UPDATE_STATE,
    atomic_json,
    rollback_current,
    safe_extract_zip,
    sha256_file,
    stage_version,
    switch_current,
)


def wait_for_exit(pid: int, timeout: int = 90) -> None:
    if pid <= 0:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.5)
    raise TimeoutError("The previous application did not exit in time.")


def healthy(version: str, timeout: int = 60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for port in range(8000, 8050):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                runtime = payload.get("runtime") or {}
                if runtime.get("app_version") == version:
                    return True
            except Exception:
                continue
        time.sleep(1)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--core", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--core-sha256", required=True)
    parser.add_argument("--browser")
    parser.add_argument("--browser-sha256")
    parser.add_argument("--browser-revision", default="")
    parser.add_argument("--wait-pid", type=int, default=0)
    args = parser.parse_args()
    root = Path(args.install_root).resolve()
    core = Path(args.core).resolve()
    try:
        UPDATE_STATE.set("verifying")
        if sha256_file(core).lower() != args.core_sha256.lower():
            raise ValueError("Core archive checksum changed after download.")
        if args.browser:
            browser = Path(args.browser).resolve()
            if not args.browser_sha256 or sha256_file(browser).lower() != args.browser_sha256.lower():
                raise ValueError("Browser archive checksum changed after download.")
            browser_dir = root / "shared" / "browsers" / args.browser_revision
            if not browser_dir.exists():
                UPDATE_STATE.set("staging")
                safe_extract_zip(browser, browser_dir)
            atomic_json(root / "browser-current.json", {"revision": args.browser_revision, "path": f"shared/browsers/{args.browser_revision}"})
        UPDATE_STATE.set("waiting_exit")
        wait_for_exit(args.wait_pid)
        UPDATE_STATE.set("staging")
        stage_version(core, root, args.version)
        UPDATE_STATE.set("switching")
        switch_current(root, args.version, args.browser_revision)
        bootstrap = root / "ReagentApprovalBot.exe"
        if not bootstrap.is_file():
            raise FileNotFoundError("Stable launcher was not installed.")
        subprocess.Popen([str(bootstrap)], cwd=str(root), close_fds=True)
        UPDATE_STATE.set("health_check")
        if not healthy(args.version):
            UPDATE_STATE.set("rollback", "Health check failed; restoring previous version.")
            if rollback_current(root):
                subprocess.Popen([str(bootstrap)], cwd=str(root), close_fds=True)
            raise RuntimeError("Updated version failed its health check and was rolled back.")
        UPDATE_STATE.set("succeeded")
        return 0
    except Exception as error:  # noqa: BLE001
        UPDATE_STATE.set("failed", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
