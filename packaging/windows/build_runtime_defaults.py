"""Generate defaults for a new runtime, without editing the operator's configuration."""

from __future__ import annotations

from pathlib import Path
import sys

import yaml


def write_defaults(source: Path, destination: Path) -> None:
    settings = yaml.safe_load(source.read_text(encoding="utf-8"))
    settings.setdefault("workbuddy", {}).update(
        executable="", project_name="", desktop_enabled=False, write_enabled=False,
        batch_dir="data/workbuddy", auto_recover=True, prompt_mode="full",
    )
    settings["workbuddy"].pop("batch_size_canary", None)
    settings.setdefault("app", {}).update(dry_run=True)
    settings.setdefault("approval", {}).update(write_mode="disabled", erp_write_backend="web_ui", auto_pass=False)
    settings.setdefault("scheduler", {}).update(enabled=False, approval_write_mode="disabled", auto_pass=False)
    settings.setdefault("erp_api", {}).update(base_url="", enabled_for_write=False, discovery={})
    settings.setdefault("notification", {}).setdefault("dingtalk", {})["enabled"] = False
    settings.setdefault("dingtalk_control", {}).update(admin_user_ids=[], management_group_ids=[])
    settings.setdefault("dingtalk_stream", {}).update(
        enabled=False, corp_id="", agent_id="", robot_code="", open_conversation_id="",
    )
    settings.setdefault("dingtalk_chat", {})["enabled"] = False
    settings.setdefault("memory_sync", {})["enabled"] = False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(settings, allow_unicode=True, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    write_defaults(Path(sys.argv[1]), Path(sys.argv[2]))
