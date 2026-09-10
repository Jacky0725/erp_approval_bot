from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from evidence_resolver import EvidenceResolver  # noqa: E402
from rule_engine import RuleEngine  # noqa: E402


class RuleEngineResolvedPropertiesTest(unittest.TestCase):
    def test_adds_versioned_trace_and_requires_verified_identity(self) -> None:
        engine = RuleEngine.from_structured_excel(ROOT_DIR / "config" / "rules_structured.xlsx")
        properties = EvidenceResolver().resolve(
            EvidenceResolver().normalize_legacy_items(
                [{"field": "flammable", "value": "Flammable liquid", "source": "Supplier SDS"}]
            )
        )
        result = engine.classify_resolved_properties(
            properties,
            name="ethanol",
            identity_status="name_only",
        )
        self.assertTrue(result["need_manual_review"])
        self.assertTrue(result["decision_trace"]["rule_version"].startswith("sha256:"))
        self.assertIn("identity_status:name_only", result["decision_trace"]["warnings"])

    def test_marks_evidence_conflicts_for_review(self) -> None:
        engine = RuleEngine.from_structured_excel(ROOT_DIR / "config" / "rules_structured.xlsx")
        resolver = EvidenceResolver()
        properties = resolver.resolve(
            resolver.normalize_legacy_items(
                [
                    {"field": "flash_point", "value": "10 C", "source": "Supplier SDS"},
                    {"field": "flash_point", "value": "30 C", "source": "PubChem"},
                ]
            )
        )
        result = engine.classify_resolved_properties(properties, name="test", identity_status="verified")
        self.assertTrue(result["need_manual_review"])
        self.assertIn("evidence_conflict:flash_point", result["decision_trace"]["warnings"])
