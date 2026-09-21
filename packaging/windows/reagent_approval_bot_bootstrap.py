from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


APP_NAME = "ReagentApprovalBot.exe"


def resolve_target(install_root: Path, pointer_name: str) -> Path | None:
    try:
        pointer = json.loads((install_root / pointer_name).read_text(encoding="utf-8"))
        version = str(pointer.get("version") or "")
        path = (install_root / str(pointer.get("path") or "")).resolve()
        path.relative_to((install_root / "versions").resolve())
        if path.name != version:
            return None
        executable = path / APP_NAME
        return executable if executable.is_file() else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def main() -> int:
    install_root = Path(sys.executable).resolve().parent
    for pointer in ("current.json", "previous.json"):
        target = resolve_target(install_root, pointer)
        if target is None:
            continue
        subprocess.Popen([str(target), *sys.argv[1:]], cwd=str(target.parent), close_fds=True)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
