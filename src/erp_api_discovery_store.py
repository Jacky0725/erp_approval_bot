"""Persistence and fail-closed settings promotion for ERP API discovery."""
from __future__ import annotations

import copy
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml


class ErpApiConfigurator:
    """Persist discovery evidence and atomically promote fail-closed settings."""

    _lock = threading.Lock()

    def __init__(
        self,
        root_dir: Path,
        settings_path: Path | None = None,
        *,
        compact_event: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.root_dir = Path(root_dir)
        self.settings_path = settings_path or self.root_dir / "config" / "settings.yaml"
        self._compact_event = compact_event

    def _compact(self, event: dict[str, Any]) -> dict[str, Any]:
        if "analysis" in event:
            return event
        if self._compact_event is not None:
            return self._compact_event(event)
        from erp_api_discovery import compact_discovery_event

        return compact_discovery_event(event)

    def save_candidate(self, candidate: Any, events: list[dict[str, Any]]) -> Path:
        settings = self._load_settings()
        discovery = ((settings.get("erp_api") or {}).get("discovery") or {})
        configured = discovery.get("candidate_path") or "data/logs/erp_api_candidate.yaml"
        path = self.root_dir / str(configured)
        payload = {
            "status": candidate.status,
            "confidence": candidate.confidence,
            "config": candidate.config,
            "evidence": candidate.evidence,
            "missing": candidate.missing,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "events": [self._compact(event) for event in events],
        }
        self._atomic_yaml(path, payload)
        return path

    def load_events(self) -> list[dict[str, Any]]:
        settings = self._load_settings()
        discovery = ((settings.get("erp_api") or {}).get("discovery") or {})
        configured = discovery.get("candidate_path") or "data/logs/erp_api_candidate.yaml"
        path = self.root_dir / str(configured)
        if not path.exists():
            return []
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return []
        events = payload.get("events") or []
        return [event for event in events if isinstance(event, dict)][-500:]

    def promote_pending(self, candidate: Any) -> bool:
        if candidate.status != "candidate" or candidate.confidence < 0.9 or candidate.missing:
            return False
        with self._lock:
            settings = self._load_settings()
            erp_api = settings.setdefault("erp_api", {})
            discovery = erp_api.setdefault("discovery", {})
            if str(discovery.get("status") or "") in {"pending_canary", "verified", "active"}:
                return False
            self._backup_settings(discovery)
            discovery["previous_config"] = copy.deepcopy(
                {key: value for key, value in erp_api.items() if key != "discovery"}
            )
            for key, value in candidate.config.items():
                if key == "field_mapping":
                    merged = dict(erp_api.get(key) or {})
                    merged.update(value or {})
                    erp_api[key] = merged
                else:
                    erp_api[key] = copy.deepcopy(value)
            erp_api["enabled_for_write"] = False
            discovery.update({
                "status": "pending_canary",
                "confidence": candidate.confidence,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            self._atomic_yaml(self.settings_path, settings)
        return True

    def activate(self) -> None:
        self._set_activation("verified", True, "")

    def reject(self, reason: str) -> None:
        with self._lock:
            settings = self._load_settings()
            erp_api = settings.setdefault("erp_api", {})
            discovery = erp_api.setdefault("discovery", {})
            previous = discovery.get("previous_config")
            if isinstance(previous, dict):
                for key in list(erp_api):
                    if key != "discovery":
                        erp_api.pop(key, None)
                erp_api.update(copy.deepcopy(previous))
            erp_api["enabled_for_write"] = False
            discovery.update({
                "status": "rejected",
                "last_result": reason,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            erp_api["discovery"] = discovery
            self._atomic_yaml(self.settings_path, settings)

    def _set_activation(self, status: str, enabled: bool, reason: str) -> None:
        with self._lock:
            settings = self._load_settings()
            erp_api = settings.setdefault("erp_api", {})
            erp_api["enabled_for_write"] = enabled
            discovery = erp_api.setdefault("discovery", {})
            discovery.update({
                "status": status,
                "last_result": reason,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            })
            self._atomic_yaml(self.settings_path, settings)

    def _backup_settings(self, discovery: dict[str, Any]) -> Path:
        configured = discovery.get("backup_dir") or "data/logs/erp_api_config_backups"
        backup_dir = self.root_dir / str(configured)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"settings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
        if self.settings_path.exists():
            backup.write_bytes(self.settings_path.read_bytes())
        return backup

    def _load_settings(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return {}
        return yaml.safe_load(self.settings_path.read_text(encoding="utf-8")) or {}

    @staticmethod
    def _atomic_yaml(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as file:
                yaml.safe_dump(payload, file, allow_unicode=True, sort_keys=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
