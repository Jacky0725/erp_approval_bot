from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_identity_resolver import ChemicalIdentityResolver  # noqa: E402


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class ChemicalIdentityResolverTest(unittest.TestCase):
    def settings(self) -> dict:
        return {
            "approval": {
                "identity_enrichment": {
                    "enabled": True,
                    "base_url": "https://example.test/v1",
                    "api_key_env": "DASHSCOPE_API_KEY",
                    "model": "qwen3.7-flash",
                    "timeout_seconds": 10,
                }
            }
        }

    def test_resolves_structured_candidate_without_sending_cas(self) -> None:
        captured: dict = {}

        def requester(request, timeout):  # type: ignore[no-untyped-def]
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return _Response({"choices": [{"message": {"content": json.dumps({
                "standard_name": "乙醇",
                "english_name": "Ethanol",
                "candidate_cas": ["64-17-5", "not-a-cas"],
                "name_type": "single_compound",
                "confidence": 0.9,
                "reason": "名称是乙醇的常见非标准写法",
            })}}]})

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False):
            result = ChemicalIdentityResolver(settings=self.settings(), requester=requester).resolve(
                raw_name="无水酒精试剂", cleaned_name="酒精", standard_name=""
            )

        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")
        user_input = json.loads(captured["body"]["messages"][1]["content"])
        self.assertNotIn("cas", user_input)
        self.assertEqual(captured["body"]["response_format"]["type"], "json_schema")
        self.assertFalse(captured["body"]["enable_thinking"])
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["english_name"], "Ethanol")
        self.assertEqual(result["candidate_cas"], ["64-17-5"])

    def test_product_name_is_skipped_without_request(self) -> None:
        def requester(*args, **kwargs):  # type: ignore[no-untyped-def]
            self.fail("product names must not call the resolver")

        result = ChemicalIdentityResolver(settings=self.settings(), requester=requester).resolve(
            raw_name="未知清洗剂 Lot#L2107277", cleaned_name="未知清洗剂", standard_name=""
        )

        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["attempted"])
        self.assertEqual(result["candidate_cas"], [])

    def test_invalid_schema_response_fails_closed(self) -> None:
        def requester(*args, **kwargs):  # type: ignore[no-untyped-def]
            return _Response({"choices": [{"message": {"content": "not json"}}]})

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False):
            result = ChemicalIdentityResolver(settings=self.settings(), requester=requester).resolve(raw_name="乙醇别名")

        self.assertEqual(result["status"], "failure")
        self.assertTrue(result["attempted"])
        self.assertEqual(result["candidate_cas"], [])

    def test_daily_limit_reserves_attempts_without_storing_reagent_content(self) -> None:
        def requester(*args, **kwargs):  # type: ignore[no-untyped-def]
            return _Response({"choices": [{"message": {"content": json.dumps({
                "standard_name": "乙醇", "english_name": "Ethanol", "candidate_cas": ["64-17-5"],
                "name_type": "single_compound", "confidence": 0.9, "reason": "fixture",
            })}}]})

        settings = self.settings()
        settings["approval"]["identity_enrichment"]["max_calls_per_day"] = 1
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False):
            resolver = ChemicalIdentityResolver(settings=settings, requester=requester, root_dir=Path(tmp))
            first = resolver.resolve(raw_name="乙醇别名")
            second = resolver.resolve(raw_name="另一种乙醇别名")
            usage = (Path(tmp) / "data" / "logs" / "identity_enrichment_usage.json").read_text(encoding="utf-8")

        self.assertEqual(first["status"], "resolved")
        self.assertEqual(second["status"], "budget_exhausted")
        self.assertNotIn("乙醇", usage)


if __name__ == "__main__":
    unittest.main()
