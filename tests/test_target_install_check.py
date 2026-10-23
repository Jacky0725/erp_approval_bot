from __future__ import annotations

import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_target_install.py"
SPEC = importlib.util.spec_from_file_location("check_target_install", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_installed_layout_check_is_read_only_and_rejects_pointer_escape(tmp_path: Path) -> None:
    install = tmp_path / "Programs" / "ReagentApprovalBot"
    runtime = tmp_path / "ReagentApprovalBot"
    version_dir = install / "versions" / "0.1.20"
    version_dir.mkdir(parents=True)
    (runtime / "data").mkdir(parents=True)
    (version_dir / "ReagentApprovalBot.exe").touch()
    (install / "ReagentApprovalBotBootstrap.exe").touch()
    (install / "ReagentApprovalBotUpdater.exe").touch()
    pointer = install / "current.json"
    pointer.write_text(json.dumps({"version": "0.1.20", "path": "versions/0.1.20"}), encoding="utf-8")
    assert MODULE.inspect_install(install, runtime)["passed"] is True
    pointer.write_text(json.dumps({"version": "0.1.20", "path": "../../outside"}), encoding="utf-8")
    assert MODULE.inspect_install(install, runtime)["checks"]["current_pointer_within_versions"] is False
