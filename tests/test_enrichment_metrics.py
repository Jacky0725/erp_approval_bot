from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from enrichment_metrics import EnrichmentMetrics, build_confirmed_benchmark, compare_benchmark  # noqa: E402


class EnrichmentMetricsTest(unittest.TestCase):
    def test_records_sanitized_structured_events_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.jsonl"
            metrics = EnrichmentMetrics(enabled=True, output_path=path, run_id="run-1")
            metrics.record_cache(layer="persistent", outcome="hit")
            metrics.record_provider(provider="PubChem", status="success", elapsed_ms=12, attempts=1)
            metrics.record_llm(operation="extract_properties", status="success", elapsed_ms=34)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([row["event"] for row in rows], ["cache", "provider", "llm"])
        self.assertEqual(rows[1]["provider"], "PubChem")
        self.assertEqual(metrics.summary()["provider_calls"], 1)
        self.assertEqual(metrics.summary()["llm_calls"], 1)

    def test_builds_confirmed_deduplicated_benchmark(self) -> None:
        cases = build_confirmed_benchmark(
            [
                {"status": "confirmed", "chemical_name": "乙醇", "cas": "64-17-5", "manual_result": "易燃类"},
                {"status": "confirmed", "chemical_name": "乙醇", "cas": "64-17-5", "manual_result": "易燃类"},
                {"status": "pending", "chemical_name": "丙酮", "manual_result": "易燃类"},
            ]
        )
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["expected_category"], "易燃类")

    def test_compares_predictions_without_mutating_cases(self) -> None:
        cases = [{"case_id": "a", "expected_category": "普通类"}, {"case_id": "b", "expected_category": "易燃类"}]
        result = compare_benchmark(
            cases,
            lambda case: {"final_category": "普通类", "need_manual_review": case["case_id"] == "b"},
        )
        self.assertEqual(result["case_count"], 2)
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(len(result["mismatches"]), 1)
