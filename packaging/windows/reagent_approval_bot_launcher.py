from __future__ import annotations

import os
from pathlib import Path
import runpy
import socket
import sys
import threading
import time
import traceback
import webbrowser


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
    runtime = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else app
    src = app / "src"
    sys.path.insert(0, str(src))
    os.environ.setdefault("REAGENT_APPROVAL_SOURCE_ROOT", str(app))
    os.environ.setdefault("REAGENT_APPROVAL_RUNTIME_ROOT", str(runtime))
    os.chdir(runtime)
    browser_root = bundled_root() / "ms-playwright"
    if browser_root.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_root))
    (runtime / "data" / "logs").mkdir(parents=True, exist_ok=True)
    return runtime


def port_is_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def open_browser_later(url: str) -> None:
    time.sleep(1.5)
    webbrowser.open(url)


def run_worker_if_requested() -> bool:
    if "-m" not in sys.argv:
        return False
    module_index = sys.argv.index("-m")
    if len(sys.argv) <= module_index + 1 or sys.argv[module_index + 1] != "automation_worker":
        return False
    sys.argv = ["automation_worker", *sys.argv[module_index + 2 :]]
    runpy.run_module("automation_worker", run_name="__main__")
    return True


def main() -> int:
    runtime = configure_runtime()
    app = app_root()
    log_path = runtime / "data" / "logs" / "launcher.log"
    try:
        if run_worker_if_requested():
            return 0

        import uvicorn

        host = os.getenv("WEB_UI_HOST", "127.0.0.1")
        port = int(os.getenv("WEB_UI_PORT", "8000"))
        url = f"http://{host}:{port}/"
        if port_is_open(host, port):
            webbrowser.open(url)
            return 0

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
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            webbrowser.open(str(log_path))
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
