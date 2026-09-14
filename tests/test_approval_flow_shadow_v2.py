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

    def test_production_evaluation_replaces_suggestion_and_keeps_review_gate(self) -> None:
        suggestion = {"最终建议类别": "普通类", "需人工复核": False, "置信度": 0.9}
        evaluation = {
            "identity": {"status": "conflict"},
            "evidence": [{"field": "flash_point", "raw_value": "10 C"}],
            "provider_diagnostics": [{"provider": "PubChem", "status": "conflict"}],
            "classification": {
                "final_category": "易燃液体",
                "matched_categories": ["易燃液体"],
                "reason": "evidence conflict",
                "confidence": 0.4,
                "need_manual_review": True,
            },
        }
        ApprovalFlowMixin.apply_enrichment_v2_evaluation_to_suggestion(suggestion, evaluation)
        self.assertEqual(suggestion["最终建议类别"], "易燃液体")
        self.assertTrue(suggestion["需人工复核"])
        self.assertTrue(suggestion["V2正式判定"])

    def test_unknown_production_evaluation_without_name_evidence_requires_review(self) -> None:
        suggestion = {"最终建议类别": "普通类", "需人工复核": False, "置信度": 0.9}
        evaluation = {
            "identity": {"status": "unresolved"},
            "classification": {
                "final_category": "未知类",
                "matched_categories": ["未知类"],
                "reason": "no verified identity",
                "confidence": 0.0,
                "need_manual_review": True,
            },
        }

        ApprovalFlowMixin.apply_enrichment_v2_evaluation_to_suggestion(suggestion, evaluation)

        self.assertEqual(suggestion["最终建议类别"], "未知类")
        self.assertTrue(suggestion["需人工复核"])
        self.assertEqual(suggestion["未知类判定状态"], "证据不足，需人工确认")
