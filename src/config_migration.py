from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile

import yaml


TARGET_SCHEMA_VERSION = 2


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _safe_env(text: str) -> str:
    forced = {
        "APP_DRY_RUN": "true",
        "AUTO_PASS": "false",
        "APPROVAL_WRITE_MODE": "disabled",
        "PROCESS_ALL_TODOS": "false",
    }
    lines = text.splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if key in forced:
            output.append(f"{key}={forced[key]}")
            seen.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in forced.items() if key not in seen)
    return "\n".join(output).rstrip() + "\n"


def migrate_security_defaults(runtime_root: Path) -> bool:
    marker = runtime_root / "data" / ".security-migration-v2"
    if marker.exists():
        return False
    settings_path = runtime_root / "config" / "settings.yaml"
    env_path = runtime_root / ".env"
    if not settings_path.exists() and not env_path.exists():
        return False

    backup = runtime_root / "data" / "backups" / f"security-v2-{datetime.now():%Y%m%d-%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        shutil.copy2(settings_path, backup / "settings.yaml")
        settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        settings["config_schema_version"] = TARGET_SCHEMA_VERSION
        settings.setdefault("app", {})["dry_run"] = True
        approval = settings.setdefault("approval", {})
        approval["write_mode"] = "disabled"
        approval["erp_write_backend"] = "web_ui"
        scheduler = settings.setdefault("scheduler", {})
        scheduler["enabled"] = False
        scheduler["approval_write_mode"] = "disabled"
        scheduler["auto_pass"] = False
        _atomic_write(settings_path, yaml.safe_dump(settings, allow_unicode=True, sort_keys=False))
    if env_path.exists():
        shutil.copy2(env_path, backup / ".env")
        _atomic_write(env_path, _safe_env(env_path.read_text(encoding="utf-8")))
    marker.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(marker, f"migrated_at={datetime.now().isoformat(timespec='seconds')}\nbackup={backup}\n")
    return True
