from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from approval_flow import ApprovalFlowMixin  # noqa: E402


class Bot(ApprovalFlowMixin):
    def __init__(self) -> None:
        self.root_dir = ROOT_DIR
        self.settings = {"enrichment_v2": {"shadow_mode": True}}


class ApprovalFlowShadowV2Test(unittest.TestCase):
    def test_shadow_evaluation_records_comparison_without_mutating_suggestion(self) -> None:
        class FakeService:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def evaluate(self, *_args: object) -> dict[str, object]:
                return {"identity": {"status": "verified"}, "classification": {"final_category": "易燃类", "need_manual_review": False}}

            def compare_legacy(self, *_args: object) -> dict[str, object]:
                return {"same_category": False, "identity_status": "verified"}

        bot = Bot()
        suggestion = {"最终建议类别": "普通类"}
        with patch("approval_flow.EnrichmentV2", FakeService):
            bot.run_enrichment_v2_shadow({"试剂名称": "乙醇"}, object(), suggestion)  # type: ignore[arg-type]

        self.assertEqual(suggestion["最终建议类别"], "普通类")
        events = bot.enrichment_metrics().snapshot()
        self.assertEqual(events[-1]["event"], "shadow_comparison")
        self.assertFalse(events[-1]["same_category"])
