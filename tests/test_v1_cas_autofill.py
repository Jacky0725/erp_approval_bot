from __future__ import annotations

import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from approval_flow import ApprovalFlowMixin
from rule_engine import RuleEngine


class Bot(ApprovalFlowMixin):
    def __init__(self) -> None:
        self.settings = {
            "approval": {
                "pipeline_version": "v1",
                "v1": {"candidate_cas_memory": {"enabled": True}},
            }
        }


class V1CandidateCasPolicyTest(unittest.TestCase):
    def test_source_verified_candidate_enters_identity_memory_independent_of_erp_cas(self) -> None:
        bot = Bot()
        source = {
            "candidate_cas_source_verified": True,
            "candidate_cas_eligible_for_memory": True,
            "identity_status": "name_verified",
            "source": "Chemsrc",
            "relevance_passed": True,
        }
        self.assertTrue(bot.v1_candidate_cas_memory_eligible(
            erp_cas="", candidate_cas="64-17-5", search_result=source,
        ))
        self.assertTrue(bot.v1_candidate_cas_memory_eligible(
            erp_cas="not-a-cas", candidate_cas="64-17-5", search_result=source,
        ))
        self.assertTrue(bot.v1_candidate_cas_memory_eligible(
            erp_cas="67-64-1", candidate_cas="64-17-5", search_result=source,
        ))

    def test_web_candidate_must_have_page_bound_and_secondary_confirmation_when_configured(self) -> None:
        bot = Bot()
        bot.settings["approval"]["v1"]["candidate_cas_memory"].update({
            "require_page_bound_web_cas": True,
            "require_secondary_confirmation_for_web_sources": True,
        })
        source = {
            "candidate_cas_source_verified": True,
            "candidate_cas_eligible_for_memory": True,
            "identity_status": "name_verified",
            "source": "ChemicalBook",
            "relevance_passed": True,
        }
        self.assertFalse(bot.v1_candidate_cas_memory_eligible(
            erp_cas="", candidate_cas="64-17-5", search_result=source,
        ))
        source["candidate_cas_page_bound"] = True
        source["candidate_cas_secondary_confirmation_source"] = "PubChem"
        self.assertTrue(bot.v1_candidate_cas_memory_eligible(
            erp_cas="", candidate_cas="64-17-5", search_result=source,
        ))

    def test_verified_candidate_cas_is_written_to_identity_memory_without_erp_update(self) -> None:
        bot = Bot()
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict("os.environ", {"APPROVAL_PIPELINE_VERSION": "v1"}):
            bot.root_dir = Path(temp_dir)
            suggestion = {
                "序号": "1", "试剂名称": "四甲基氢氧化铵溶液", "清洗后名称": "四甲基氢氧化铵溶液",
                "标准化名称": "四甲基氢氧化铵", "网页候选CAS": "75-59-2",
                "候选CAS可写入记忆库": True, "候选CAS确认来源": "PubChem",
            }
            bot.remember_verified_v1_candidate_identities([suggestion])
            from reagent_memory import ReagentMemory
            remembered = ReagentMemory.from_settings(bot.settings, Path(temp_dir)).lookup_verified_identity(
                raw_name="四甲基氢氧化铵溶液", standard_name="四甲基氢氧化铵"
            )
        self.assertIsNotNone(remembered)
        self.assertEqual(remembered["cas"], "75-59-2")
        self.assertEqual(suggestion["候选CAS记忆库状态"], "已写入")
        self.assertNotIn("CAS号", suggestion)

    def test_unverified_candidate_cas_is_not_exposed_in_approval_suggestion(self) -> None:
        bot = Bot()
        engine = RuleEngine.from_structured_excel(ROOT / "config" / "rules_structured.xlsx")
        row = bot._approval_suggestion_row(
            {"序号": "1", "试剂名称": "商品名", "CAS号": ""},
            {"cleaned_name": "商品名", "standard_name": "商品名"},
            {
                "source": "ChemicalBook", "candidate_cas": "17256-27-8",
                "candidate_cas_source_verified": False,
                "candidate_cas_eligible_for_memory": False,
                "candidate_cas_validation_status": "rejected",
                "candidate_cas_validation_reason": "详情页实体不匹配",
                "identity_status": "unresolved",
            },
            {},
            {"final_category": "", "matched_categories": [], "need_manual_review": True},
        )
        self.assertEqual(row["网页候选CAS"], "")
        self.assertEqual(row["候选CAS校验状态"], "rejected")

    def test_llm_only_candidate_is_not_eligible(self) -> None:
        bot = Bot()
        self.assertFalse(bot.v1_candidate_cas_memory_eligible(
            erp_cas="",
            candidate_cas="64-17-5",
            search_result={
                "candidate_cas_source_verified": False,
                "candidate_cas_eligible_for_memory": False,
                "identity_status": "verified_by_enrichment",
                "source": "LLM knowledge fallback",
                "relevance_passed": True,
            },
        ))


if __name__ == "__main__":
    unittest.main()
