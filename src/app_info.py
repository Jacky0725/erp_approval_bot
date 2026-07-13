from __future__ import annotations

import os
from pathlib import Path

from runtime_paths import source_root


DEFAULT_VERSION = "0.1.4"


def app_version() -> str:
    configured = os.getenv("REAGENT_APPROVAL_VERSION", "").strip()
    if configured:
        return configured
    version_file = source_root() / "VERSION"
    if version_file.exists():
        value = version_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    return DEFAULT_VERSION


def app_repository() -> str:
    return os.getenv("REAGENT_APPROVAL_GITHUB_REPO", "Jacky0725/erp_approval_bot").strip()
