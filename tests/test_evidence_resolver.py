from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from evidence_resolver import EvidenceResolver  # noqa: E402


class EvidenceResolverTest(unittest.TestCase):

    def test_boolean_negation_is_field_specific(self) -> None:
        resolver = EvidenceResolver()
        corrosive = resolver.resolve(resolver.normalize_legacy_items([
            {"field": "corrosive", "value": "Non-flammable but corrosive", "source": "PubChem"},
        ]))
        self.assertTrue(corrosive.fields["corrosive"].value)

        negative = resolver.resolve(resolver.normalize_legacy_items([
            {"field": "corrosive", "value": "Not corrosive", "source": "PubChem"},
            {"field": "oxidizing", "value": "Non-oxidizing", "source": "PubChem"},
        ]))
        self.assertFalse(negative.fields["corrosive"].value)
        self.assertFalse(negative.fields["oxidizing"].value)
    def setUp(self) -> None:
        self.resolver = EvidenceResolver()

    def test_normalizes_temperature_and_hazard_tristate(self) -> None:
        items = self.resolver.normalize_legacy_items(
            [
                {"field": "flash_point", "value": "55 F", "source": "PubChem"},
                {"field": "flammable", "value": "Flammable liquid", "source": "PubChem"},
                {"field": "oxidizing", "value": "no data", "source": "PubChem"},
            ]
        )
        result = self.resolver.resolve(items)
        self.assertAlmostEqual(result.fields["flash_point"].value, 12.778, places=3)
        self.assertTrue(result.fields["flammable"].value)
        self.assertEqual(result.fields["oxidizing"].status, "unknown")

    def test_preserves_conflicting_measurements(self) -> None:
        items = self.resolver.normalize_legacy_items(
            [
                {"field": "flash_point", "value": "10 C", "source": "Supplier SDS"},
                {"field": "flash_point", "value": "30 C", "source": "PubChem"},
            ]
        )
        result = self.resolver.resolve(items)
        self.assertEqual(result.fields["flash_point"].status, "conflict")
        self.assertIn("flash_point", result.conflicts)

    def test_rule_input_keeps_unknown_distinct_from_false(self) -> None:
        items = self.resolver.normalize_legacy_items(
            [{"field": "flammable", "value": "non-flammable", "source": "Supplier SDS"}]
        )
        result = self.resolver.resolve(items)
        rule_input = result.to_rule_input(name="water")
        self.assertIs(rule_input["flammable"], False)
        self.assertIsNone(rule_input["oxidizing"])
