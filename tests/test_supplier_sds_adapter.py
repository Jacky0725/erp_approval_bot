from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_sources.supplier_sds import SupplierSdsAdapter  # noqa: E402
from evidence_resolver import EvidenceResolver  # noqa: E402
from reagent_identity import IdentityRecord  # noqa: E402


class SupplierSdsAdapterTest(unittest.TestCase):
    def test_extracts_labeled_properties_and_hazards(self) -> None:
        identity = IdentityRecord("乙醇", "乙醇", "乙醇", "Ethanol", "64-17-5", "", (), "single_substance", "verified", 0.99, "test")
        evidence = SupplierSdsAdapter().extract(
            identity,
            {"SDS文本": "第2部分：危险性概述\n易燃液体。\n第9部分：理化特性\n闪点：13 °C\n沸点：78.4 °C"},
        )

        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.source, "Supplier SDS")
        self.assertEqual(evidence.fields["flash_point"], "13 °C")
        properties = EvidenceResolver().resolve(EvidenceResolver().normalize_legacy_items([
            {"field": key, "value": value, "source": evidence.source} for key, value in evidence.fields.items()
        ]))
        self.assertEqual(properties.fields["flash_point"].value, 13.0)
        self.assertTrue(properties.fields["flammable"].value)

    def test_returns_none_without_local_sds(self) -> None:
        identity = IdentityRecord("乙醇", "乙醇", "乙醇", "Ethanol", "64-17-5", "", (), "single_substance", "verified", 0.99, "test")
        self.assertIsNone(SupplierSdsAdapter().extract(identity, {}))
