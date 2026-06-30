from __future__ import annotations

import os
import shutil
from pathlib import Path


def source_root() -> Path:
    configured = os.getenv("REAGENT_APPROVAL_SOURCE_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[1]


def runtime_root() -> Path:
    configured = os.getenv("REAGENT_APPROVAL_RUNTIME_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    return source_root()


def ensure_runtime_layout() -> None:
    source = source_root()
    runtime = runtime_root()
    (runtime / "data" / "logs").mkdir(parents=True, exist_ok=True)
    (runtime / "config").mkdir(parents=True, exist_ok=True)

    for relative in (
        "config/settings.yaml",
        "config/name_aliases.yaml",
        "config/rules.xlsx",
        "config/rules_structured.xlsx",
        ".env.example",
    ):
        src = source / relative
        dst = runtime / relative
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
