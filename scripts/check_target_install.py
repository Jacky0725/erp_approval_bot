from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def inspect_install(install_dir: Path, runtime_dir: Path) -> dict[str, object]:
    root = install_dir.resolve()
    versions = root / "versions"
    pointer_path = root / "current.json"
    checks: dict[str, bool] = {
        "bootstrap_present": (root / "ReagentApprovalBotBootstrap.exe").is_file(),
        "updater_present": (root / "ReagentApprovalBotUpdater.exe").is_file(),
        "current_pointer_present": pointer_path.is_file(),
        "runtime_separate_from_install": runtime_dir.resolve() != root,
        "runtime_data_present": (runtime_dir / "data").is_dir(),
    }
    version = ""
    if pointer_path.is_file():
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            version = str(pointer.get("version") or "")
            target = (root / str(pointer.get("path") or "")).resolve()
            checks["current_pointer_within_versions"] = target.parent == versions.resolve() and target.name == version
            checks["current_executable_present"] = checks["current_pointer_within_versions"] and (
                target / "ReagentApprovalBot.exe"
            ).is_file()
        except (OSError, ValueError, TypeError):
            checks["current_pointer_within_versions"] = False
            checks["current_executable_present"] = False
    else:
        checks["current_pointer_within_versions"] = False
        checks["current_executable_present"] = False
    return {"version": version, "checks": checks, "passed": all(checks.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only installed layout check; never prints configuration values.")
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    parser.add_argument("--install-dir", type=Path, default=local / "Programs" / "ReagentApprovalBot")
    parser.add_argument("--runtime-dir", type=Path, default=local / "ReagentApprovalBot")
    args = parser.parse_args()
    result = inspect_install(args.install_dir, args.runtime_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
