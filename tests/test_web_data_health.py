from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

import web_app  # noqa: E402


def test_data_health_repair_endpoint_returns_apply_result(monkeypatch) -> None:
    expected = {
        "applied": True,
        "backups": {"memory": "backup.sqlite"},
        "memory": {"disabled_records": 1, "unsafe_disabled": 2},
        "review_queue": {"rebuilt_memory": 3, "blocking_duplicates_resolved": 4},
    }

    monkeypatch.setattr(web_app, "load_settings", lambda: {"paths": {}})
    monkeypatch.setattr(web_app, "apply_data_health_repairs", lambda root_dir, settings=None: expected)

    response = web_app.api_data_health_repair()

    assert json.loads(response.body) == expected
