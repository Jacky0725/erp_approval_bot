from __future__ import annotations

import sys
import unittest
from unittest.mock import patch


ROOT_DIR = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_sources.pubchem import PubChemAdapter  # noqa: E402
from reagent_identity import IdentityRecord  # noqa: E402


def identity(*, cas: str = "64-17-5", name: str = "乙醇") -> IdentityRecord:
    return IdentityRecord(name, name, name, "Ethanol", cas, "", (), "single_substance", "name_only", 0.6, "test")


class PubChemAdapterTest(unittest.TestCase):
    def test_resolve_many_verifies_matching_name_and_cas(self) -> None:
        adapter = PubChemAdapter(max_workers=1)
        responses = [({"IdentifierList": {"CID": [702]}}, ""), ({"IdentifierList": {"CID": [702]}}, "")]
        with patch.object(adapter, "_get_json", side_effect=responses):
            result = adapter.resolve_many([identity()])[0]

        self.assertEqual(result.identity.status, "verified")
        self.assertEqual(result.identity.cas, "64-17-5")
        self.assertEqual(result.identity.provider_id("pubchem_cid"), "702")

    def test_resolve_many_reports_identity_conflict(self) -> None:
        adapter = PubChemAdapter(max_workers=1)
        responses = [({"IdentifierList": {"CID": [1]}}, ""), ({"IdentifierList": {"CID": [2]}}, "")]
        with patch.object(adapter, "_get_json", side_effect=responses):
            result = adapter.resolve_many([identity()])[0]

        self.assertEqual(result.diagnostic.status, "conflict")

    def test_fetches_descriptor_properties_in_one_batch_request(self) -> None:
        adapter = PubChemAdapter()
        records = [
            IdentityRecord("乙醇", "乙醇", "乙醇", "Ethanol", "64-17-5", "", (), "single_substance", "verified", 0.99, "test", provider_ids=(("pubchem_cid", "702"),)),
            IdentityRecord("丙酮", "丙酮", "丙酮", "Acetone", "67-64-1", "", (), "single_substance", "verified", 0.99, "test", provider_ids=(("pubchem_cid", "180"),)),
        ]
        payload = {
            "PropertyTable": {
                "Properties": [
                    {"CID": 702, "Title": "Ethanol", "MolecularFormula": "C2H6O"},
                    {"CID": 180, "Title": "Acetone", "MolecularFormula": "C3H6O"},
                ]
            }
        }
        with patch.object(adapter, "_get_json", return_value=(payload, "")) as get_json:
            results = adapter.fetch_evidence_many(records)

        self.assertEqual(get_json.call_count, 1)
        self.assertEqual(results[0].fields["name"], "Ethanol")
        self.assertEqual(results[1].fields["molecular_formula"], "C3H6O")

    def test_extracts_hazard_fields_when_enabled(self) -> None:
        adapter = PubChemAdapter(include_hazard_details=True)
        payload = {
            "Record": {
                "Section": [
                    {
                        "TOCHeading": "Safety and Hazards",
                        "Information": [
                            {"Name": "Flammability", "Value": {"String": "Flammable liquid"}},
                            {"Name": "Flash Point", "Value": {"String": "13 °C"}},
                        ],
                    }
                ]
            }
        }
        with patch.object(adapter, "_get_json", return_value=(payload, "")):
            fields, failure = adapter._hazard_fields("702")

        self.assertEqual(failure, "")
        self.assertEqual(fields["flammable"], "Flammable liquid")
        self.assertEqual(fields["flash_point"], "13 °C")


def replace_identity(record: IdentityRecord) -> IdentityRecord:
    return record
