from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request


def run_server(*, host: str, port: int, app_dir: str | None = None) -> None:
    import uvicorn

    if app_dir and app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    from web_app import app

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info", log_config=None))
    app.state.request_server_shutdown = lambda: setattr(server, "should_exit", True)
    try:
        server.run()
    finally:
        app.state.request_server_shutdown = None


def _port_is_free(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return False
    except OSError:
        return True


def run_restart_helper(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart-after", action="store_true", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args(argv)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def record(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as output:
            output.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _port_is_free(args.host, args.port):
            break
        time.sleep(0.25)
    else:
        record("Restart failed: old Web UI did not release its port within 30 seconds.")
        return 1

    command = json.loads(args.command)
    try:
        with log_path.open("a", encoding="utf-8") as output:
            environment = {**os.environ, "REAGENT_APPROVAL_RESTART_STRICT_PORT": "true"}
            process = subprocess.Popen(command, cwd=args.cwd, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=output, close_fds=True, env=environment,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(120):
            if process.poll() is not None:
                record(f"Restart failed: new Web UI exited with code {process.returncode}.")
                return 1
            try:
                with urllib.request.urlopen(f"http://{args.host}:{args.port}/api/health", timeout=0.5) as response:
                    if response.status == 200 and json.load(response).get("process_id") == process.pid:
                        record("Restart completed: new Web UI health check passed.")
                        return 0
            except OSError:
                pass
            time.sleep(0.25)
        record("Restart failed: new Web UI did not pass its health check within 30 seconds.")
        return 1
    except (OSError, ValueError) as error:
        record(f"Restart failed: {error}")
        return 1


if __name__ == "__main__":
    if "--restart-after" in sys.argv:
        raise SystemExit(run_restart_helper(sys.argv[1:]))
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("WEB_UI_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_UI_PORT", "8000")))
    args = parser.parse_args()
    os.environ["WEB_UI_HOST"], os.environ["WEB_UI_PORT"] = args.host, str(args.port)
    run_server(host=args.host, port=args.port)
