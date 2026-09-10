from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_sources.base import ProviderDiagnostic, ProviderEvidence  # noqa: E402
from enrichment_v2 import EnrichmentV2  # noqa: E402
from reagent_identity import IdentityRecord  # noqa: E402
from rule_engine import RuleEngine  # noqa: E402


class BatchSource:
    name = "BatchSource"

    def __init__(self) -> None:
        self.resolve_calls = 0
        self.fetch_calls = 0

    def resolve_many(self, identities):
        self.resolve_calls += 1
        return [ProviderEvidence(replace(identity, status="verified", provider_ids=(("pubchem_cid", str(index + 1)),)), "PubChem", "", {"pubchem_cid": str(index + 1)}, diagnostic=ProviderDiagnostic("PubChem", "success", 1)) for index, identity in enumerate(identities)]

    def fetch_evidence_many(self, identities):
        self.fetch_calls += 1
        return [ProviderEvidence(identity, "PubChem", "", {"flash_point": "13 C"}, diagnostic=ProviderDiagnostic("PubChem", "success", 1)) for identity in identities]


def test_evaluate_many_resolves_and_fetches_once_for_a_batch() -> None:
    source = BatchSource()
    service = EnrichmentV2(root_dir=ROOT_DIR, source=source)
    engine = RuleEngine.from_structured_excel(ROOT_DIR / "config" / "rules_structured.xlsx")
    results = service.evaluate_many([{"试剂名称": "乙醇"}, {"试剂名称": "丙酮"}], engine)
    assert len(results) == 2
    assert source.resolve_calls == 1
    assert source.fetch_calls == 1
