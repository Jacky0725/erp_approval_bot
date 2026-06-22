from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from llm_providers import (  # noqa: E402
    get_llm_provider,
    parse_models_response,
    provider_base_url,
    provider_default_model,
    provider_options,
)


class LlmProvidersTest(unittest.TestCase):
    def test_provider_options_include_common_openai_compatible_platforms(self) -> None:
        provider_ids = {item["id"] for item in provider_options()}

        self.assertIn("siliconflow", provider_ids)
        self.assertIn("deepseek", provider_ids)
        self.assertIn("aliyun_bailian", provider_ids)
        self.assertIn("openai", provider_ids)
        self.assertIn("openai_compatible", provider_ids)
        self.assertIn("ollama", provider_ids)

    def test_provider_base_url_uses_default_unless_configured(self) -> None:
        self.assertEqual(provider_base_url("siliconflow", ""), "https://api.siliconflow.cn/v1")
        self.assertEqual(provider_base_url("openai_compatible", "https://example.test/v1"), "https://example.test/v1")

    def test_default_model_comes_from_provider(self) -> None:
        self.assertEqual(provider_default_model("deepseek"), "deepseek-chat")
        self.assertEqual(get_llm_provider("missing").id, "siliconflow")

    def test_parse_openai_models_response(self) -> None:
        models = parse_models_response({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]})

        self.assertEqual(models, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
