from __future__ import annotations

import os
from pathlib import Path
import json
import runpy
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser


class TeeLogWriter:
    def __init__(self, log_path: Path, original: object | None = None) -> None:
        self.log_path = log_path
        self.original = original
        self._file = log_path.open("a", encoding="utf-8", buffering=1)

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._file.write(text)
        self._file.flush()
        if self.original and hasattr(self.original, "write"):
            try:
                self.original.write(text)
                self.original.flush()
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        self._file.flush()
        if self.original and hasattr(self.original, "flush"):
            try:
                self.original.flush()
            except Exception:
                pass


def bundled_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[2]


def app_root() -> Path:
    root = bundled_root()
    packaged_app = root / "app"
    return packaged_app if packaged_app.exists() else root


def configure_runtime() -> Path:
    app = app_root()
    executable_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else app
    runtime = default_runtime_root(executable_dir) if getattr(sys, "frozen", False) else app
    src = app / "src"
    sys.path.insert(0, str(src))
    os.environ.setdefault("REAGENT_APPROVAL_SOURCE_ROOT", str(app))
    os.environ.setdefault("REAGENT_APPROVAL_RUNTIME_ROOT", str(runtime))
    migrate_legacy_runtime(executable_dir, runtime)
    os.chdir(executable_dir)
    browser_root = bundled_root() / "ms-playwright"
    if browser_root.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))
        has_full_chromium = any(browser_root.glob("chromium-*"))
        if not has_full_chromium:
            os.environ.setdefault("REAGENT_APPROVAL_HEADLESS_ONLY", "true")
    log_dir = runtime / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    redirect_process_output(log_dir / "launcher.log")
    return runtime


def default_runtime_root(executable_dir: Path) -> Path:
    configured = os.getenv("REAGENT_APPROVAL_RUNTIME_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "ReagentApprovalBot"
    return executable_dir


def migrate_legacy_runtime(executable_dir: Path, runtime: Path) -> None:
    if executable_dir == runtime:
        return
    runtime.mkdir(parents=True, exist_ok=True)
    legacy_items = (
        ".env",
        "data",
        "config/settings.yaml",
        "config/name_aliases.yaml",
    )
    for relative in legacy_items:
        source = executable_dir / relative
        destination = runtime / relative
        if not source.exists() or destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)


def redirect_process_output(log_path: Path) -> None:
    if os.getenv("REAGENT_APPROVAL_LAUNCHER_LOG_REDIRECTED") == "1":
        return
    os.environ["REAGENT_APPROVAL_LAUNCHER_LOG_REDIRECTED"] = "1"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as file:
        file.write(f"\n===== launcher start {stamp} =====\n")
    sys.stdout = TeeLogWriter(log_path, getattr(sys, "__stdout__", None))  # type: ignore[assignment]
    sys.stderr = TeeLogWriter(log_path, getattr(sys, "__stderr__", None))  # type: ignore[assignment]


def port_is_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def is_reagent_web_ui(host: str, port: int) -> bool:
    request = urllib.request.Request(
        f"http://{host}:{port}/api/status",
        headers={"User-Agent": "reagent-approval-bot-launcher"},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return False
    runtime = payload.get("runtime") if isinstance(payload, dict) else None
    if isinstance(runtime, dict) and runtime.get("app_version"):
        return True
    return isinstance(payload, dict) and bool(payload.get("version"))


def resolve_web_ui_port(host: str, preferred_port: int, scan_count: int = 50) -> tuple[int, bool]:
    for port in range(preferred_port, preferred_port + scan_count):
        if not port_is_open(host, port):
            return port, False
        if is_reagent_web_ui(host, port):
            return port, True
    raise RuntimeError(f"No available Web UI port found from {preferred_port} to {preferred_port + scan_count - 1}.")


def open_browser_later(url: str) -> None:
    time.sleep(1.5)
    webbrowser.open(url)


def worker_requested() -> bool:
    return "-m" in sys.argv and len(sys.argv) > sys.argv.index("-m") + 1 and sys.argv[sys.argv.index("-m") + 1] == "automation_worker"


def run_worker_if_requested() -> int | None:
    if "-m" not in sys.argv:
        return None
    module_index = sys.argv.index("-m")
    if len(sys.argv) <= module_index + 1 or sys.argv[module_index + 1] != "automation_worker":
        return None
    sys.argv = ["automation_worker", *sys.argv[module_index + 2 :]]
    try:
        runpy.run_module("automation_worker", run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 1


def main() -> int:
    runtime = configure_runtime()
    app = app_root()
    log_path = runtime / "data" / "logs" / "launcher.log"
    is_worker = worker_requested()
    try:
        worker_return_code = run_worker_if_requested()
        if worker_return_code is not None:
            return worker_return_code

        import uvicorn

        host = os.getenv("WEB_UI_HOST", "127.0.0.1")
        preferred_port = int(os.getenv("WEB_UI_PORT", "8000"))
        port, existing_web_ui = resolve_web_ui_port(host, preferred_port)
        url = f"http://{host}:{port}/"
        if existing_web_ui:
            webbrowser.open(url)
            return 0

        if port != preferred_port:
            os.environ["WEB_UI_PORT"] = str(port)
            print(f"Preferred Web UI port {preferred_port} is occupied; using {port}.")

        threading.Thread(target=open_browser_later, args=(url,), daemon=True).start()
        uvicorn.run(
            "web_app:app",
            app_dir=str(app / "src"),
            host=host,
            port=port,
            log_level="info",
            log_config=None,
        )
        return 0
    except Exception:  # noqa: BLE001 - write crash details for desktop users
        with log_path.open("a", encoding="utf-8") as file:
            file.write(traceback.format_exc())
        if is_worker:
            traceback.print_exc()
            return 1
        try:
            webbrowser.open(str(log_path))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
