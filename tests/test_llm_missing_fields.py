from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from llm_extractor import LlmExtractor  # noqa: E402


class LlmMissingFieldsTest(unittest.TestCase):
    def test_discards_values_without_verifiable_evidence_span(self) -> None:
        extractor = LlmExtractor(settings={})
        with patch.object(extractor, "extract_properties", return_value={"flammable": True, "confidence": 0.9, "evidence": ["invented statement"]}):
            result = extractor.extract_missing_fields("Flash point: 13 °C", {"flammable"})
        self.assertIsNone(result["flammable"])

    def test_retains_only_requested_fields_with_source_span(self) -> None:
        extractor = LlmExtractor(settings={})
        with patch.object(extractor, "extract_properties", return_value={"flammable": True, "flash_point": "13 °C", "confidence": 0.9, "evidence": ["Flammable liquid"]}):
            result = extractor.extract_missing_fields("Flammable liquid. Flash point: 13 °C", {"flammable"})
        self.assertTrue(result["flammable"])
        self.assertEqual(result["flash_point"], "")
