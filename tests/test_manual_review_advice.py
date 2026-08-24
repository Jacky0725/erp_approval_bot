from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from approval_flow import ApprovalFlowMixin  # noqa: E402
from llm_extractor import LlmExtractor  # noqa: E402
from review_queue import migrate_pending_review_reasons, review_display_summary  # noqa: E402


class _Bot(ApprovalFlowMixin):
    settings: dict[str, object] = {"approval": {"enable_llm_manual_review_advice": True}}


class ManualReviewAdviceTest(unittest.TestCase):
    def test_unified_advice_caps_confidence_and_filters_unknown_category(self) -> None:
        payload = {
            "candidate_category": "未配置类别",
            "physicochemical_summary_cn": "可能具有腐蚀性，但缺少可靠物性数据。",
            "reason_cn": "根据通用化学知识只能给出保守判断。",
            "matched_rule_summary_cn": "未可靠命中规则。",
            "uncertainties_cn": ["试剂浓度未知"],
            "identity_confidence": 0.8,
            "advisory_confidence": 0.95,
            "evidence_basis": "模型知识",
        }
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
        )
        extractor = LlmExtractor(settings={})
        with patch.object(extractor, "_has_api_key", return_value=True), patch.object(
            extractor, "_client", return_value=client
        ):
            result = extractor.generate_manual_review_advice(
                {
                    "allowed_categories": ["普通类", "腐蚀性"],
                    "has_trusted_web_evidence": False,
                    "rules_fingerprint": "abc123",
                }
            )

        self.assertEqual(result["candidate_category"], "")
        self.assertEqual(result["advisory_confidence"], 0.65)
        self.assertTrue(result["advisory_only"])
        self.assertTrue(result["must_manual_review"])
        self.assertIn("腐蚀", result["physicochemical_summary_cn"])

    def test_missing_api_key_returns_safe_chinese_message(self) -> None:
        extractor = LlmExtractor(settings={})
        with patch.object(extractor, "_has_api_key", return_value=False):
            result = extractor.generate_manual_review_advice({})

        self.assertFalse(result["used_llm"])
        self.assertIn("未配置", result["reason_cn"])
        self.assertNotIn("Error code", result["reason_cn"])

    def test_flow_keeps_llm_candidate_out_of_formal_classification(self) -> None:
        class FakeExtractor:
            advice_calls = 0

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def extract_properties(self, raw_text: str, name: str = "", cas: str = "") -> dict[str, object]:
                self.assert_no_web_llm_call(raw_text)
                return {"suggested_categories": [], "evidence": [], "confidence": 0.0}

            @staticmethod
            def assert_no_web_llm_call(raw_text: str) -> None:
                if raw_text:
                    raise AssertionError("search failure text must not be treated as web evidence")

            def generate_manual_review_advice(self, reagent_info: dict[str, object]) -> dict[str, object]:
                type(self).advice_calls += 1
                return {
                    "candidate_category": "强反应性",
                    "physicochemical_summary_cn": "可能具有较强反应性。",
                    "reason_cn": "按规则提供第二意见。",
                    "matched_rule_summary_cn": "命中强反应性候选规则。",
                    "uncertainties_cn": ["缺少 SDS"],
                    "advisory_confidence": 0.6,
                    "evidence_basis": "模型知识",
                    "advisory_only": True,
                    "must_manual_review": True,
                    "used_llm": True,
                    "model": "test-model",
                    "rules_fingerprint": reagent_info.get("rules_fingerprint", ""),
                }

        class FakeRuleEngine:
            rules: list[object] = []
            priority: list[str] = []

            def classify(self, reagent_info: dict[str, object]) -> dict[str, object]:
                return {
                    "final_category": "",
                    "matched_categories": [],
                    "reason": "证据不足，需要人工复核。",
                    "confidence": 0.0,
                    "need_manual_review": True,
                }

        item = {
            "index": 1,
            "progress": "1/1",
            "reagent": {"试剂名称": "测试试剂", "CAS号": "", "规格": "", "规格单位": ""},
            "search_result": {
                "name": "测试试剂",
                "source": "",
                "raw_text": "no trusted web evidence",
                "need_manual_review": True,
                "source_confidence": 0.0,
                "evidence_quality": "none",
                "failure_reason": "no trusted web evidence",
                "name_normalization": {},
            },
            "name_result": {},
            "search_name": "测试试剂",
            "search_cas": "",
        }
        bot = _Bot()
        with patch.dict("os.environ", {"ENABLE_LLM_MANUAL_REVIEW_ADVICE": "true"}, clear=False), patch(
            "approval_flow.LlmExtractor", FakeExtractor
        ):
            extracted, classification = bot.extract_and_classify_worker(item, FakeRuleEngine())  # type: ignore[arg-type]

        self.assertEqual(FakeExtractor.advice_calls, 1)
        self.assertEqual(classification["final_category"], "")
        self.assertEqual(classification["matched_categories"], [])
        self.assertEqual(extracted.get("suggested_categories"), [])
        self.assertEqual(item["search_result"]["llm_advisory_category"], "强反应性")
        self.assertTrue(item["search_result"]["llm_advisory_only"])

    def test_advisory_suggestion_is_never_writable(self) -> None:
        bot = _Bot()
        suggestions = [
            {
                "最终建议类别": "普通类",
                "置信度": 1.0,
                "需人工复核": False,
                "LLM辅助意见仅供复核": True,
            }
        ]
        self.assertEqual(bot.high_confidence_write_candidates(suggestions), [])

    def test_review_display_never_preselects_llm_advice(self) -> None:
        result = review_display_summary(
            evidence_fields={
                "evidence_source_type": "llm_manual_review_advice",
                "llm_advisory_only": True,
                "llm_advisory_category": "腐蚀性",
                "llm_advisory_reason_cn": "可能具有腐蚀性，需人工核验。",
                "llm_advisory_confidence": 0.65,
                "llm_advisory_evidence_basis": "模型知识",
            }
        )
        self.assertEqual(result["display_suggestion"], "LLM第二意见：腐蚀性")
        self.assertFalse(result["allow_suggestion_preselect"])

    def test_pending_reason_migration_backs_up_and_preserves_completed_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review_queue.xlsx"
            pd.DataFrame(
                [
                    {"status": "pending", "reason": "No trusted web evidence was found"},
                    {"status": "confirmed", "reason": "needs manual review"},
                ]
            ).to_excel(path, index=False)

            self.assertTrue(migrate_pending_review_reasons(path))
            migrated = pd.read_excel(path, dtype=str).fillna("")
            self.assertIn("未找到可信网页证据", migrated.loc[0, "reason"])
            self.assertEqual(migrated.loc[0, "reason_raw"], "No trusted web evidence was found")
            self.assertEqual(migrated.loc[1, "reason"], "needs manual review")
            self.assertEqual(len(list(path.parent.glob("review_queue.before_chinese_remarks.*.xlsx"))), 1)
            self.assertFalse(migrate_pending_review_reasons(path))


if __name__ == "__main__":
    unittest.main()
