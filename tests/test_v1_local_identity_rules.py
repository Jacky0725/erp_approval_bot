from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from approval_flow import ApprovalFlowMixin
from chemical_searcher import ChemicalSearcher
from rule_engine import RuleEngine


class _Bot(ApprovalFlowMixin):
    def __init__(self) -> None:
        self.settings = {"approval": {"pipeline_version": "v1"}}


class V1LocalIdentityRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = RuleEngine.from_structured_excel(ROOT / "config" / "rules_structured.xlsx")

    def test_elemental_heavy_metal_forms_are_local_and_compounds_are_not(self) -> None:
        for name in ("镍粉200目", "镍粒", "镍片", "铅箔", "钴丝", "银块"):
            with self.subTest(name=name):
                result = self.engine.explicit_heavy_metal_element_rule({"reagent_name": name})
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result["final_category"], "重金属类")
                self.assertEqual(result["matched_rule_ids"], ["BUS-HMT-ELEMENT-FORM-001"])
        for name in ("硫酸镍", "氧化镍", "镍合金", "氯化钴溶液"):
            with self.subTest(compound=name):
                self.assertIsNone(self.engine.explicit_heavy_metal_element_rule({"reagent_name": name}))

    def test_primary_component_strips_only_dilution_and_form(self) -> None:
        self.assertEqual(
            _Bot.v1_primary_component_profile("稀释10倍石墨烯量子点"),
            {"raw_name": "稀释10倍石墨烯量子点", "primary_component": "石墨烯量子点", "modifiers": "稀释10倍"},
        )
        self.assertEqual(
            _Bot.v1_primary_component_profile("石墨烯量子点分散液"),
            {"raw_name": "石墨烯量子点分散液", "primary_component": "石墨烯量子点", "modifiers": "分散液"},
        )
        self.assertIsNone(_Bot.v1_primary_component_profile("石墨烯量子点乙醇分散液"))

    def test_elemental_heavy_metal_generates_v1_direct_write_suggestion(self) -> None:
        bot = _Bot()
        with patch.dict("os.environ", {"APPROVAL_PIPELINE_VERSION": "v1"}):
            suggestion = bot.direct_business_rule_suggestion({"试剂名称": "镍粉200目", "CAS号": ""}, self.engine)
        self.assertIsNotNone(suggestion)
        assert suggestion is not None
        self.assertEqual(suggestion["最终建议类别"], "重金属类")
        self.assertFalse(suggestion["需人工复核"])
        self.assertEqual(suggestion["身份验证状态"], "v1_local_elemental_heavy_metal")
        self.assertEqual(suggestion["查询来源"], "v1_local_rule")

    def test_primary_component_classification_uses_graphene_but_keeps_known_hazard(self) -> None:
        bot = _Bot()
        unresolved = {"identity_status": "unresolved", "need_manual_review": True}
        classification, result = bot.v1_primary_component_classification(
            {"试剂名称": "稀释10倍石墨烯量子点"},
            unresolved,
            {},
            {"final_category": "", "matched_categories": [], "need_manual_review": True},
            self.engine,
        )
        self.assertEqual(classification["final_category"], "普通类")
        self.assertFalse(classification["need_manual_review"])
        self.assertEqual(result["identity_status"], "v1_local_primary_component")
        self.assertEqual(result["primary_component"], "石墨烯量子点")

        hazardous = {"final_category": "刺激性", "matched_categories": ["刺激性"], "need_manual_review": False}
        unchanged, guarded = bot.v1_primary_component_classification(
            {"试剂名称": "稀释10倍石墨烯量子点"},
            unresolved,
            {"corrosive": True},
            hazardous,
            self.engine,
        )
        self.assertEqual(unchanged["final_category"], "刺激性")
        self.assertTrue(guarded["primary_component_hazard_overridden"])

    def test_detail_cas_must_be_labeled_and_unique(self) -> None:
        valid = '<h1>Nickel</h1><div>CAS No.: 7440-02-0</div>'
        wrong_page = '<h1>Graphene quantum dots</h1><aside>Recommended CAS: 18162-48-6</aside>'
        self.assertEqual(
            ChemicalSearcher._detail_page_bound_cas(valid, "https://www.chemicalbook.com/x", "ChemicalBook"),
            "7440-02-0",
        )
        self.assertEqual(
            ChemicalSearcher._detail_page_bound_cas(wrong_page, "https://www.chemicalbook.com/x", "ChemicalBook"),
            "",
        )

    def test_web_candidate_requires_pubchem_secondary_confirmation(self) -> None:
        searcher = ChemicalSearcher(settings={})
        searcher._run_provider = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "source": "PubChem", "cas": "64-17-5", "candidate_cas": "64-17-5", "url": "https://pubchem.example/ethanol"
        }
        result = searcher._confirm_candidate_cas_for_memory(
            {
                "source": "ChemicalBook", "candidate_cas": "64-17-5", "cas": "64-17-5",
                "url": "https://chemicalbook.example/ethanol", "candidate_cas_page_bound": True,
            },
            validation_names=["乙醇", "ethanol"],
        )
        self.assertTrue(result["candidate_cas_eligible_for_memory"])
        self.assertEqual(result["candidate_cas_secondary_confirmation_source"], "PubChem")


if __name__ == "__main__":
    unittest.main()
