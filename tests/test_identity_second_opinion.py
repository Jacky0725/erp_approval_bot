from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chemical_sources.base import ProviderDiagnostic, ProviderEvidence
from chemical_sources.pubchem import PubChemAdapter
from enrichment_v2 import EnrichmentV2
from llm_extractor import LlmExtractor
from reagent_identity import IdentityRecord
from rule_engine import RuleEngine


def _identity(cas: str = "64-17-5") -> IdentityRecord:
    return IdentityRecord("乙醇", "乙醇", "乙醇", "Ethanol", cas, "", (), "single_substance", "name_only", 0.6, "test")


def test_pubchem_keeps_name_and_cas_candidates_separate() -> None:
    adapter = PubChemAdapter(max_workers=1)
    with patch.object(
        adapter,
        "_get_json",
        side_effect=[
            ({"IdentifierList": {"CID": [702]}}, ""),
            ({"IdentifierList": {"CID": [180]}}, ""),
        ],
    ):
        result = adapter.resolve_identity_pair(_identity())
    assert result["status"] == "conflict"
    assert result["name_candidate"]["cid"] == "702"
    assert result["cas_candidate"]["cid"] == "180"


def test_identity_second_opinion_is_advisory_and_labels_model_knowledge() -> None:
    payload = {
        "name_identity_opinion": "名称可能对应乙醇。",
        "cas_identity_opinion": "CAS 对应另一物质。",
        "identity_opinion": "冲突",
        "candidate_category": "易燃类",
        "physicochemical_summary_cn": "可能为易燃液体。",
        "reason_cn": "依据名称和通用化学知识。",
        "matched_rule_summary_cn": "涉及易燃规则。",
        "uncertainties_cn": ["需要人工确认身份"],
        "evidence_basis": "模型知识",
        "advisory_confidence": 0.9,
    }
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload, ensure_ascii=False)))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)))
    extractor = LlmExtractor(settings={})
    with patch.object(extractor, "_has_api_key", return_value=True), patch.object(extractor, "_client", return_value=client):
        result = extractor.generate_identity_second_opinion({"allowed_categories": ["易燃类"], "rules_fingerprint": "r1"})
    assert result["used_llm"] is True
    assert result["advisory_only"] is True
    assert result["must_manual_review"] is True
    assert result["evidence_basis"] == "模型知识"
    assert result["advisory_confidence"] <= 0.65


class _PairSource:
    name = "PubChem"

    def resolve_many(self, identities):
        identity = replace(identities[0], status="name_only", provider_ids=(("pubchem_cid", "702"),))
        return [ProviderEvidence(identity, "PubChem", "https://example.test/702", {"pubchem_cid": "702"}, diagnostic=ProviderDiagnostic("PubChem", "success", 1))]

    def resolve_identity_pair(self, identity):
        return {
            "status": "cas_missing",
            "name_candidate": {"source": "name", "query": "乙醇", "cid": "702", "name": "Ethanol", "url": "https://example.test/702"},
            "cas_candidate": None,
            "name_candidates": [{"source": "name", "query": "乙醇", "cid": "702", "name": "Ethanol", "url": "https://example.test/702"}],
            "cas_candidates": [],
        }

    def fetch_evidence_many(self, identities):
        return [ProviderEvidence(item, "PubChem", "https://example.test/702", {"pubchem_cid": "702", "name": "Ethanol", "flammable": "Flammable liquid"}, diagnostic=ProviderDiagnostic("PubChem", "success", 1)) for item in identities]


class _Opinion:
    def generate_identity_second_opinion(self, info):
        return {"used_llm": True, "advisory_only": True, "must_manual_review": True, "identity_opinion": "CAS缺失", "candidate_category": "易燃类", "evidence_basis": "模型知识"}


def test_v2_forces_manual_review_when_cas_missing() -> None:
    service = EnrichmentV2(
        root_dir=ROOT,
        source=_PairSource(),
        llm_extractor=_Opinion(),
        settings={"enrichment_v2": {"enabled": True, "shadow_mode": False}, "approval": {"llm_identity_review": {"enabled": True}}},
    )
    engine = RuleEngine.from_structured_excel(ROOT / "config" / "rules_structured.xlsx")
    result = service.evaluate({"试剂名称": "乙醇", "CAS号": ""}, engine)
    assert result["identity_resolution"]["status"] == "cas_missing"
    assert result["classification"]["need_manual_review"] is True
    assert result["llm_second_opinion"]["advisory_only"] is True


def test_verified_v2_identity_does_not_mark_llm_advisory() -> None:
    class VerifiedSource(_PairSource):
        def resolve_identity_pair(self, identity):
            return {"status": "verified", "name_candidate": {"cid": "702", "name": "Ethanol"}, "cas_candidate": {"cid": "702", "name": "Ethanol"}, "name_candidates": [], "cas_candidates": []}

    service = EnrichmentV2(
        root_dir=ROOT,
        source=VerifiedSource(),
        llm_extractor=_Opinion(),
        settings={"enrichment_v2": {"enabled": True, "shadow_mode": False}, "approval": {"llm_identity_review": {"enabled": True}}},
    )
    engine = RuleEngine.from_structured_excel(ROOT / "config" / "rules_structured.xlsx")
    result = service.evaluate({"试剂名称": "乙醇", "CAS号": "64-17-5"}, engine)
    assert result["identity_resolution"]["status"] == "verified"
    assert result["llm_second_opinion"]["used_llm"] is False
