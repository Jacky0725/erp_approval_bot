from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_sources.comptox import CompToxAdapter  # noqa: E402
from chemical_sources.nist import NistWebBookAdapter  # noqa: E402
from reagent_identity import IdentityRecord  # noqa: E402


def identity() -> IdentityRecord:
    return IdentityRecord("乙醇", "乙醇", "乙醇", "Ethanol", "64-17-5", "", (), "single_substance", "verified", 0.99, "test")


class SupplementarySourceTest(unittest.TestCase):
    def test_comptox_reports_missing_credentials_without_network(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = CompToxAdapter().fetch_evidence_many([identity()])[0]
        self.assertEqual(result.diagnostic.status, "not_configured")
        self.assertEqual(result.diagnostic.failure_kind, "disabled_missing_credentials")

    def test_nist_extracts_only_labelled_temperature_fields(self) -> None:
        adapter = NistWebBookAdapter()
        html = "<h2>Boiling point</h2> 78.4 °C <h2>Flash point</h2> 13 °C"

        class Response:
            headers = type("Headers", (), {"get_content_charset": lambda self: "utf-8"})()
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return html.encode()

        with patch("chemical_sources.nist.urlopen", return_value=Response()):
            result = adapter.fetch_evidence_many([identity()])[0]
        self.assertEqual(result.fields["boiling_point"], "78.4 °C")
        self.assertEqual(result.fields["flash_point"], "13 °C")
