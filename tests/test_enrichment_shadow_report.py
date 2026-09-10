from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from enrichment_shadow_report import build_report, markdown_report  # noqa: E402


def test_build_report_aggregates_shadow_and_provider_events() -> None:
    report = build_report([
        {"event": "provider", "status": "success", "elapsed_ms": 10},
        {"event": "provider", "status": "unavailable", "elapsed_ms": 30},
        {"event": "llm", "status": "success", "elapsed_ms": 20},
        {"event": "shadow_comparison", "same_category": True, "v2_manual_review": False},
        {"event": "shadow_comparison", "same_category": False, "v2_manual_review": True},
    ])
    assert report["provider_calls"] == 2
    assert report["same_category_count"] == 1
    assert report["v2_manual_review_count"] == 1
    assert "类别一致率：50.0%" in markdown_report(report)
