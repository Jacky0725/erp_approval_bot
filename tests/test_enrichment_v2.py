from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_sources.base import ProviderDiagnostic, ProviderEvidence  # noqa: E402
from enrichment_v2 import EnrichmentV2  # noqa: E402
from reagent_identity import IdentityRecord  # noqa: E402
from rule_engine import RuleEngine  # noqa: E402


class FakeSource:
    name = "Fake"

    def resolve_many(self, identities: list[IdentityRecord]) -> list[ProviderEvidence]:
        identity = replace(identities[0], status="verified", confidence=0.99, provider_ids=(("pubchem_cid", "702"),))
        return [ProviderEvidence(identity, "PubChem", "https://example.test/702", {"pubchem_cid": "702"}, diagnostic=ProviderDiagnostic("PubChem", "success", 2))]

    def fetch_evidence_many(self, identities: list[IdentityRecord]) -> list[ProviderEvidence]:
        return [ProviderEvidence(identities[0], "Supplier SDS", "https://example.test/sds", {"flammable": "Flammable liquid"}, diagnostic=ProviderDiagnostic("Supplier SDS", "success", 3))]


class EnrichmentV2Test(unittest.TestCase):
    def test_evaluates_as_shadow_only_by_default(self) -> None:
        service = EnrichmentV2(root_dir=ROOT_DIR, source=FakeSource())
        engine = RuleEngine.from_structured_excel(ROOT_DIR / "config" / "rules_structured.xlsx")
        result = service.evaluate({"试剂名称": "乙醇", "CAS号": "64-17-5"}, engine)

        self.assertTrue(result["shadow_only"])
        self.assertEqual(result["identity"]["status"], "verified")
        self.assertTrue(result["properties"]["flammable"]["value"])
        self.assertIn("decision_trace", result["classification"])

    def test_compares_v2_with_legacy_result(self) -> None:
        service = EnrichmentV2(root_dir=ROOT_DIR, source=FakeSource())
        comparison = service.compare_legacy(
            {"最终建议类别": "普通类", "需人工复核": False},
            {"identity": {"status": "verified"}, "classification": {"final_category": "易燃类", "need_manual_review": True}},
        )
        self.assertFalse(comparison["same_category"])
        self.assertTrue(comparison["v2_manual_review"])
