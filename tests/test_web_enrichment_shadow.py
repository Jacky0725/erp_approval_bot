from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

import web_app  # noqa: E402


def test_enrichment_shadow_endpoint_includes_operating_mode(monkeypatch) -> None:
    monkeypatch.setattr(web_app, "load_settings", lambda: {
        "enrichment_v2": {"shadow_mode": True, "enabled": False},
        "enrichment_metrics": {"jsonl_path": "data/logs/not-present.jsonl"},
    })
    response = web_app.api_enrichment_shadow()
    body = response.body.decode("utf-8")
    assert '"shadow_mode":true' in body
    assert '"enabled":false' in body
