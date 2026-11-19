"""Windows desktop shortcut entry point, run with pythonw.exe."""

import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
ADDRESS = ("127.0.0.1", 8000)
URL = f"http://{ADDRESS[0]}:{ADDRESS[1]}"


def health() -> dict:
    try:
        with urlopen(f"{URL}/api/health", timeout=0.5) as response:
            data = json.load(response)
        if isinstance(data, dict) and data.get("ok"):
            return data
    except (OSError, ValueError):
        pass
    return {}


def is_our_server(status: dict, root: Path) -> bool:
    return bool(status.get("install_root") and Path(status["install_root"]).resolve() == root.resolve())


def start(root: Path, interpreter: Path) -> None:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        port_in_use = probe.connect_ex(ADDRESS) == 0

    process = None
    if not port_in_use:
        python = interpreter.with_name("python.exe")
        if not python.is_file():
            raise RuntimeError(f"Python interpreter not found: {python}")
        logs = root / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "web_ui_server.log").open("ab") as output, (logs / "web_ui_server.err.log").open("ab") as errors:
            process = subprocess.Popen(
                [str(python), "-m", "web_server_control", "--host", ADDRESS[0], "--port", str(ADDRESS[1])],
                cwd=root / "src",
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        (logs / "web_ui_server.pid").write_text(str(process.pid), encoding="ascii")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = health()
        if is_our_server(status, root):
            os.startfile(URL)
            return
        if status or (process is not None and process.poll() is not None):
            break
        time.sleep(0.25)
    if port_in_use:
        raise RuntimeError(f"Port {ADDRESS[1]} is in use, but the project Web UI could not be verified.")
    raise RuntimeError(f"Web UI startup failed. See {root / 'data' / 'logs' / 'web_ui_server.err.log'}")


def main() -> None:
    try:
        start(ROOT, Path(sys.executable))
    except (OSError, RuntimeError) as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), "Reagent Approval Bot", 0x10)


if __name__ == "__main__":
    main()
