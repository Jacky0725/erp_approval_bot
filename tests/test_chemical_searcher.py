from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from unittest.mock import patch

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_searcher import ChemicalSearcher  # noqa: E402
from chemical_search_cache import ChemicalSearchCache  # noqa: E402
from approval_flow import ApprovalFlowMixin  # noqa: E402
from web_researcher import ResearchPage  # noqa: E402


NO_LLM_ENV = {
    "LLM_API_KEY": "",
    "SILICONFLOW_API_KEY": "",
    "OPENAI_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
    "DASHSCOPE_API_KEY": "",
    "QIANFAN_API_KEY": "",
    "ARK_API_KEY": "",
    "MOONSHOT_API_KEY": "",
}


class RecordingSearcher(ChemicalSearcher):
    def __init__(self, *args: Any, succeed: bool = True, allow_fallback: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.succeed = succeed
        self.allow_fallback = allow_fallback
        self.queries: list[str] = []

    def _search_chemsrc(
        self,
        name: str,
        cas: str,
        query: str,
        validation_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        self.queries.append(query)
        if not self.succeed:
            return None
        return self._result(name=name, cas=cas, source="Chemsrc", url="https://example.test", raw_text=f"{name} {cas}")

    def _search_chemicalbook(
        self,
        name: str,
        cas: str,
        query: str,
        validation_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        self.queries.append(query)
        return None

    def _fallback_web_research(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if self.allow_fallback:
            return super()._fallback_web_research(*args, **kwargs)
        return None


class ChemicalSearcherTest(unittest.TestCase):
    def setUp(self) -> None:
        ChemicalSearcher._source_failures = {}
        ChemicalSearcher._source_open_until = {}
        ChemicalSearcher._source_failure_reasons = {}
        ChemicalSearcher._source_skip_logged = set()
        ChemicalSearcher._source_half_open = set()
        ChemicalSearcher._source_semaphores = {}

    def test_query_candidates_try_names_after_cas(self) -> None:
        self.assertEqual(
            ChemicalSearcher._query_candidates("123-45-6", "standard", "cleaned", "english"),
            ["123-45-6", "standard", "cleaned", "english"],
        )

    def test_legacy_source_url_is_not_requested_automatically(self) -> None:
        class ManualUrlSearcher(ChemicalSearcher):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.urls: list[str] = []
                self.used_search = False

            def _fetch(self, url: str) -> str:
                self.urls.append(url)
                return "CAS No. 18868-43-4 Name: Molybdenum dioxide Chemical Name: Molybdenum dioxide"

            def _search_chemsrc(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                self.used_search = True
                return None

        searcher = ManualUrlSearcher(root_dir=ROOT_DIR)
        result = searcher.search("二氧化钼")

        self.assertNotIn("https://www.chemsrc.com/cas/18868-43-4_88297.html", searcher.urls)
        self.assertTrue(searcher.used_search)
        self.assertTrue(result["need_manual_review"])
        self.assertIn("chemsrc", result["manual_search_urls"])

    def test_legacy_source_url_is_manual_only_even_when_configured(self) -> None:
        class WrongCasSearcher(ChemicalSearcher):
            def _fetch(self, url: str) -> str:
                return "CAS No. 1317-33-5 Name: Molybdenum trioxide Chemical Name: Molybdenum trioxide"

        searcher = WrongCasSearcher(root_dir=ROOT_DIR)
        result = searcher.search("二氧化钼")

        self.assertTrue(result["need_manual_review"])
        self.assertIn("chemsrc", result["manual_search_urls"])

    def test_search_normalizes_before_query_and_prefers_cleaned_name(self) -> None:
        searcher = RecordingSearcher(root_dir=ROOT_DIR)
        with patch.dict("os.environ", NO_LLM_ENV, clear=False):
            result = searcher.search("工业酒精 75% 500ml", cas="64-17-5", specification="500ml", unit="瓶")

        self.assertEqual(searcher.queries, ["工业酒精", "64-17-5"])
        self.assertEqual(result["query"], "工业酒精")
        self.assertEqual(result["name_normalization"]["standard_name"], "工业酒精")
        self.assertEqual(result["name_normalization"]["english_name"], "")
        self.assertTrue(result["need_manual_review"])
        self.assertTrue(result["is_mixture"])

    def test_search_uses_cas_from_alias_when_cas_is_empty(self) -> None:
        searcher = RecordingSearcher(root_dir=ROOT_DIR)
        result = searcher.search("NaOH 0.1mol/L 分析纯")

        self.assertEqual(searcher.queries, ["氢氧化钠", "1310-73-2"])
        self.assertEqual(result["query"], "氢氧化钠")
        self.assertEqual(result["name_normalization"]["standard_name"], "氢氧化钠")
        self.assertEqual(result["name_normalization"]["english_name"], "sodium hydroxide")
        self.assertEqual(result["name_normalization"]["concentration"], "0.1mol/L")

    def test_missing_cas_uses_enrichment_only_after_name_lookup_fails(self) -> None:
        class EnrichmentSearcher(ChemicalSearcher):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.queries: list[str] = []

            def _search_chemsrc(self, name: str, cas: str, query: str, validation_names=None):  # type: ignore[no-untyped-def]
                self.queries.append(query)
                if query != "ethanol":
                    return None
                result = self._result(
                    name=name,
                    cas="64-17-5",
                    source="PubChem",
                    url="https://pubchem.example/ethanol",
                    raw_text="Ethanol CAS 64-17-5",
                )
                result.update({"relevance_passed": True, "name_similarity": 0.98, "matched_site_name": "Ethanol"})
                return result

            def _search_chemicalbook(self, *args: Any, **kwargs: Any) -> None:
                return None

        class Resolver:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def resolve(self, **kwargs: Any) -> dict[str, Any]:
                return {
                    "attempted": True, "status": "resolved", "english_name": "ethanol",
                    "resolved_standard_name": "乙醇", "candidate_cas": ["64-17-5"],
                    "name_type": "single_compound", "confidence": 0.9, "reason": "fixture",
                }

        settings = {"approval": {"identity_enrichment": {"enabled": True}}, "chemical_search": {"providers": ["chemsrc"], "chemsrc_enabled": True}}
        with tempfile.TemporaryDirectory() as tmp, patch("chemical_searcher.ChemicalIdentityResolver", Resolver):
            result = EnrichmentSearcher(root_dir=Path(tmp), settings=settings).search("乙醇非标准写法")

        self.assertIn("乙醇非标准写法", result["query_plan"]["queries_attempted"])
        self.assertEqual(result["identity_status"], "verified_by_enrichment")
        self.assertEqual(result["candidate_cas"], "64-17-5")
        self.assertFalse(result["need_manual_review"])
        self.assertTrue(result["identity_enrichment"]["attempted"])

    def test_enrichment_candidate_without_external_cas_match_stays_manual_review(self) -> None:
        class MismatchSearcher(ChemicalSearcher):
            def _search_chemsrc(self, name: str, cas: str, query: str, validation_names=None):  # type: ignore[no-untyped-def]
                if query != "ethanol":
                    return None
                result = self._result(name=name, cas="67-56-1", source="PubChem", url="https://pubchem.example/methanol", raw_text="Methanol CAS 67-56-1")
                result.update({"relevance_passed": True, "name_similarity": 0.98, "matched_site_name": "Methanol"})
                return result

            def _search_chemicalbook(self, *args: Any, **kwargs: Any) -> None:
                return None

        class Resolver:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def resolve(self, **kwargs: Any) -> dict[str, Any]:
                return {
                    "attempted": True, "status": "resolved", "english_name": "ethanol",
                    "candidate_cas": ["64-17-5"], "name_type": "single_compound", "confidence": 0.9, "reason": "fixture",
                }

        settings = {"approval": {"identity_enrichment": {"enabled": True}}, "chemical_search": {"providers": ["chemsrc"], "chemsrc_enabled": True}}
        with tempfile.TemporaryDirectory() as tmp, patch("chemical_searcher.ChemicalIdentityResolver", Resolver):
            result = MismatchSearcher(root_dir=Path(tmp), settings=settings).search("乙醇非标准写法")

        self.assertTrue(result["need_manual_review"])
        self.assertEqual(result["identity_status"], "unresolved")

    def test_identity_conflict_source_does_not_block_missing_cas_enrichment(self) -> None:
        class WrongMetalSearcher(ChemicalSearcher):
            def _search_chemsrc(self, name: str, cas: str, query: str, validation_names=None):  # type: ignore[no-untyped-def]
                result = self._result(
                    name=name,
                    cas="13759-92-7",
                    source="Chemsrc",
                    url="https://example.invalid/europium",
                    raw_text="Product Name: Europium(III) chloride hexahydrate CAS Number: 13759-92-7",
                )
                result.update({"relevance_passed": True, "name_similarity": 0.98, "matched_site_name": "Europium(III) chloride hexahydrate"})
                return result

            def _search_chemicalbook(self, *args: Any, **kwargs: Any) -> None:
                return None

        class Resolver:
            calls = 0

            def __init__(self, **kwargs: Any) -> None:
                pass

            def resolve(self, **kwargs: Any) -> dict[str, Any]:
                type(self).calls += 1
                return {"attempted": True, "status": "unresolved", "candidate_cas": [], "english_name": ""}

        settings = {"approval": {"identity_enrichment": {"enabled": True}}, "chemical_search": {"providers": ["chemsrc"], "chemsrc_enabled": True}}
        with tempfile.TemporaryDirectory() as tmp, patch("chemical_searcher.ChemicalIdentityResolver", Resolver):
            result = WrongMetalSearcher(root_dir=Path(tmp), settings=settings).search("氯化铁三水")

        self.assertEqual(Resolver.calls, 1)
        self.assertEqual(result["identity_status"], "unresolved")
        self.assertTrue(result["need_manual_review"])

    def test_trusted_web_result_upgrades_unknown_alias_by_cas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "name_aliases.yaml").write_text(
                """
cas:
  1310-73-2:
    standard_name: 氢氧化钠
    english_name: sodium hydroxide
    aliases:
      - 氢氧化钠
      - 烧碱
      - sodium hydroxide
aliases: {}
abbreviations: {}
""".strip(),
                encoding="utf-8",
            )

            class TrustedAliasSearcher(ChemicalSearcher):
                def _search_chemsrc(
                    self,
                    name: str,
                    cas: str,
                    query: str,
                    validation_names: list[str] | None = None,
                ) -> dict[str, Any] | None:
                    result = self._result(
                        name=name,
                        cas="1310-73-2",
                        source="Chemsrc",
                        url="https://example.test/sodium-hydroxide",
                        raw_text="火碱 氢氧化钠 sodium hydroxide CAS No. 1310-73-2 corrosive",
                    )
                    result.update(
                        {
                            "relevance_passed": True,
                            "passed": True,
                            "matched_site_name": "火碱",
                            "name_similarity": 0.95,
                        }
                    )
                    return result

                def _search_chemicalbook(
                    self,
                    name: str,
                    cas: str,
                    query: str,
                    validation_names: list[str] | None = None,
                ) -> dict[str, Any] | None:
                    return None

            settings = {"paths": {"name_aliases_yaml": "config/name_aliases.yaml"}}
            with patch.dict("os.environ", NO_LLM_ENV, clear=False):
                result = TrustedAliasSearcher(root_dir=root, settings=settings).search("火碱")
            name_result = result["name_normalization"]

            self.assertEqual(name_result["standard_name"], "氢氧化钠")
            self.assertEqual(name_result["cas"], "1310-73-2")
            self.assertGreaterEqual(name_result["confidence"], 0.9)
            self.assertFalse(name_result["need_manual_review"])
            self.assertTrue(name_result["web_verified_alias"])

            candidates_path = config_dir / "name_alias_candidates.xlsx"
            self.assertTrue(candidates_path.exists())
            candidates = pd.read_excel(candidates_path, dtype=str).fillna("")
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates.iloc[0]["alias"], "火碱")
            self.assertEqual(candidates.iloc[0]["standard_name"], "氢氧化钠")
            self.assertEqual(candidates.iloc[0]["cas"], "1310-73-2")
            self.assertEqual(candidates.iloc[0]["status"], "pending")

            with patch.dict("os.environ", NO_LLM_ENV, clear=False):
                TrustedAliasSearcher(root_dir=root, settings=settings).search("火碱")
            candidates = pd.read_excel(candidates_path, dtype=str).fillna("")
            self.assertEqual(len(candidates), 1)

    def test_trusted_cas_result_overrides_non_informative_wrong_standard_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "name_aliases.yaml").write_text(
                "cas: {}\naliases: {}\nabbreviations: {}\n",
                encoding="utf-8",
            )

            class TrustedCasNameSearcher(ChemicalSearcher):
                def _search_chemsrc(
                    self,
                    name: str,
                    cas: str,
                    query: str,
                    validation_names: list[str] | None = None,
                ) -> dict[str, Any] | None:
                    result = self._result(
                        name=name,
                        cas="272-14-0",
                        source="Chemsrc",
                        url="https://example.test/272-14-0",
                        raw_text=(
                            "Name: Thieno[3,2-c]pyridine Chemical Name: Thieno[3,2-c]pyridine "
                            "CAS Number: 272-14-0 Hazard Codes Xn"
                        ),
                    )
                    result.update(
                        {
                            "relevance_passed": True,
                            "passed": True,
                            "matched_site_name": "Thieno[3,2-c]pyridine",
                            "name_similarity": 1.0,
                        }
                    )
                    return result

                def _search_chemicalbook(
                    self,
                    name: str,
                    cas: str,
                    query: str,
                    validation_names: list[str] | None = None,
                ) -> dict[str, Any] | None:
                    return None

            settings = {"paths": {"name_aliases_yaml": "config/name_aliases.yaml"}}
            with patch.dict("os.environ", NO_LLM_ENV, clear=False):
                result = TrustedCasNameSearcher(root_dir=root, settings=settings).search("没写", cas="272-14-0")

        name_result = result["name_normalization"]
        self.assertEqual(name_result["standard_name"], "Thieno[3,2-c]pyridine")
        self.assertEqual(name_result["english_name"], "Thieno[3,2-c]pyridine")
        self.assertEqual(name_result["cas"], "272-14-0")
        self.assertFalse(name_result["need_manual_review"])
        self.assertTrue(name_result["web_verified_alias"])
        self.assertIn("CAS 272-14-0 was verified", name_result["reason"])

    def test_low_confidence_web_result_does_not_upgrade_unknown_alias(self) -> None:
        class LowConfidenceAliasSearcher(ChemicalSearcher):
            def _search_chemsrc(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                result = self._result(
                    name=name,
                    cas="1310-73-2",
                    source="Chemsrc",
                    url="https://example.test/low",
                    raw_text="火碱 CAS No. 1310-73-2",
                )
                result.update(
                    {
                        "source_confidence": 0.7,
                        "relevance_passed": True,
                        "passed": True,
                        "matched_site_name": "火碱",
                        "name_similarity": 0.95,
                    }
                )
                return result

            def _search_chemicalbook(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "name_aliases.yaml").write_text("cas: {}\naliases: {}\nabbreviations: {}\n", encoding="utf-8")
            settings = {"paths": {"name_aliases_yaml": "config/name_aliases.yaml"}}
            with patch.dict("os.environ", NO_LLM_ENV, clear=False):
                result = LowConfidenceAliasSearcher(root_dir=root, settings=settings).search("火碱")
        name_result = result["name_normalization"]

        self.assertEqual(name_result["standard_name"], "火碱")
        self.assertEqual(name_result["confidence"], 0.65)
        self.assertTrue(name_result["need_manual_review"])

    def test_existing_cas_alias_does_not_create_alias_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "name_aliases.yaml").write_text(
                """
cas:
  1310-73-2:
    standard_name: 氢氧化钠
    english_name: sodium hydroxide
    aliases:
      - 氢氧化钠
      - 火碱
aliases: {}
abbreviations: {}
""".strip(),
                encoding="utf-8",
            )

            class ExistingAliasSearcher(ChemicalSearcher):
                def _search_chemsrc(
                    self,
                    name: str,
                    cas: str,
                    query: str,
                    validation_names: list[str] | None = None,
                ) -> dict[str, Any] | None:
                    result = self._result(
                        name=name,
                        cas="1310-73-2",
                        source="Chemsrc",
                        url="https://example.test/sodium-hydroxide",
                        raw_text="火碱 氢氧化钠 CAS No. 1310-73-2",
                    )
                    result.update(
                        {
                            "relevance_passed": True,
                            "passed": True,
                            "matched_site_name": "火碱",
                            "name_similarity": 0.95,
                        }
                    )
                    return result

                def _search_chemicalbook(
                    self,
                    name: str,
                    cas: str,
                    query: str,
                    validation_names: list[str] | None = None,
                ) -> dict[str, Any] | None:
                    return None

            settings = {"paths": {"name_aliases_yaml": "config/name_aliases.yaml"}}
            result = ExistingAliasSearcher(root_dir=root, settings=settings).search("火碱")

            self.assertEqual(result["name_normalization"]["standard_name"], "氢氧化钠")
            self.assertFalse((config_dir / "name_alias_candidates.xlsx").exists())

    def test_trusted_name_match_without_cas_does_not_change_standard_name(self) -> None:
        class NameOnlyAliasSearcher(ChemicalSearcher):
            def _search_chemsrc(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                result = self._result(
                    name=name,
                    cas="",
                    source="Chemsrc",
                    url="https://example.test/name-only",
                    raw_text="火碱 physical and chemical properties",
                )
                result.update(
                    {
                        "relevance_passed": True,
                        "passed": True,
                        "matched_site_name": "火碱",
                        "name_similarity": 0.95,
                    }
                )
                return result

            def _search_chemicalbook(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "name_aliases.yaml").write_text("cas: {}\naliases: {}\nabbreviations: {}\n", encoding="utf-8")
            settings = {"paths": {"name_aliases_yaml": "config/name_aliases.yaml"}}
            with patch.dict("os.environ", NO_LLM_ENV, clear=False):
                result = NameOnlyAliasSearcher(root_dir=root, settings=settings).search("火碱")
        name_result = result["name_normalization"]

        self.assertEqual(name_result["standard_name"], "火碱")
        self.assertGreaterEqual(name_result["confidence"], 0.9)
        self.assertFalse(name_result["need_manual_review"])

    def test_search_failure_returns_manual_review_with_normalization(self) -> None:
        class NoKnowledgeFallbackExtractor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def generate_knowledge_fallback(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
                return {"raw_text": "", "reason": "disabled in test", "confidence": 0.0, "used_llm": False}

        searcher = RecordingSearcher(root_dir=ROOT_DIR, succeed=False)
        with patch("chemical_searcher.LlmExtractor", NoKnowledgeFallbackExtractor):
            result = searcher.search("NaOH 0.1mol/L 分析纯")

        self.assertTrue(result["need_manual_review"])
        self.assertEqual(result["name_normalization"]["standard_name"], "氢氧化钠")
        self.assertIn("1310-73-2", result["raw_text"])

    def test_repeated_identical_search_uses_cache(self) -> None:
        searcher = RecordingSearcher(root_dir=ROOT_DIR, succeed=False)

        first = searcher.search("NaOH 0.1mol/L 分析纯")
        query_count = len(searcher.queries)
        first["need_manual_review"] = False
        second = searcher.search("NaOH 0.1mol/L 分析纯")

        self.assertGreater(query_count, 0)
        self.assertEqual(len(searcher.queries), query_count)
        self.assertTrue(second["need_manual_review"])
        self.assertEqual(second["name_normalization"]["standard_name"], "氢氧化钠")

    def test_persistent_cache_is_reused_across_searcher_instances(self) -> None:
        settings = {
            "chemical_search": {
                "cache": {"enabled": True, "success_ttl_days": 30, "failure_ttl_days": 1},
                "failure_circuit_break_threshold": 5,
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            first_searcher = RecordingSearcher(root_dir=Path(tmp), settings=settings)
            first = first_searcher.search("ethanol", cas="64-17-5")
            query_count = len(first_searcher.queries)
            first["need_manual_review"] = True

            second_searcher = RecordingSearcher(root_dir=Path(tmp), settings=settings)
            second = second_searcher.search("ethanol", cas="64-17-5")

        self.assertGreater(query_count, 0)
        self.assertEqual(second_searcher.queries, [])
        self.assertFalse(second["need_manual_review"])

    def test_compatible_success_cache_reuses_exact_name_after_settings_change(self) -> None:
        """A parser-version change must not discard a recent trusted name result."""
        settings = {
            "chemical_search": {
                "cache": {"enabled": True, "success_ttl_days": 30, "failure_ttl_days": 1},
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            cache = ChemicalSearchCache(root_dir=Path(tmp), settings=settings)
            cache.put(
                {
                    "source": "chemical_search",
                    "normalized_name": "亚甲基蓝三水合物",
                    "cas": "",
                    "search_mode": "legacy_web",
                    "parser_version": "legacy-v1",
                    "settings_version": "legacy-settings",
                },
                {
                    "name": "亚甲基蓝三水合物",
                    "cas": "7220-79-3",
                    "source": "Chemsrc",
                    "url": "https://example.test/829401",
                    "source_confidence": 0.92,
                    "failure_reason": "",
                },
            )

            reused = cache.get_compatible_success(normalized_name="亚甲基蓝三水合物", cas="")

        self.assertIsNotNone(reused)
        assert reused is not None
        self.assertEqual(reused["source"], "Chemsrc")
        self.assertEqual(reused["cas"], "7220-79-3")

    def test_source_circuit_breaker_skips_only_network_failed_source(self) -> None:
        settings = {"chemical_search": {"failure_circuit_break_threshold": 1, "per_source_concurrency": 2}}
        class NetworkFailingSearcher(RecordingSearcher):
            def _search_chemsrc(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                self.queries.append(query)
                self._mark_provider_fetch_failure("timeout")
                return None

        searcher = NetworkFailingSearcher(root_dir=ROOT_DIR, settings=settings, succeed=False)
        ChemicalSearcher._source_failures = {}
        ChemicalSearcher._source_open_until = {}
        ChemicalSearcher._source_failure_reasons = {}
        ChemicalSearcher._source_skip_logged = set()
        ChemicalSearcher._source_semaphores = {}

        first = searcher._run_provider(
            searcher._search_chemsrc,
            name="ethanol",
            cas="64-17-5",
            query="64-17-5",
            validation_names=["ethanol"],
        )

        query_count = len(searcher.queries)
        second = searcher._run_provider(
            searcher._search_chemsrc,
            name="ethanol",
            cas="64-17-5",
            query="64-17-5",
            validation_names=["ethanol"],
        )
        third = searcher._run_provider(
            searcher._search_chemicalbook,
            name="ethanol",
            cas="64-17-5",
            query="64-17-5",
            validation_names=["ethanol"],
        )

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertIsNone(third)
        self.assertEqual(len(searcher.queries), query_count + 1)

    def test_hydrate_notation_adds_supplier_query_variant_without_changing_identity(self) -> None:
        variants = ChemicalSearcher._hydrate_name_query_variants("亚甲基蓝·三水", "亚甲基蓝三水")

        self.assertEqual(variants, ["亚甲基蓝三水合物"])

    def test_no_result_does_not_trip_source_circuit_breaker(self) -> None:
        settings = {"chemical_search": {"failure_circuit_break_threshold": 1, "per_source_concurrency": 2}}
        searcher = RecordingSearcher(root_dir=ROOT_DIR, settings=settings, succeed=False)

        first = searcher._run_provider(
            searcher._search_chemsrc,
            name="unknown",
            cas="",
            query="unknown",
            validation_names=["unknown"],
        )
        second = searcher._run_provider(
            searcher._search_chemsrc,
            name="unknown",
            cas="",
            query="unknown2",
            validation_names=["unknown2"],
        )

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(searcher.queries, ["unknown", "unknown2"])

    def test_budget_exhaustion_without_request_does_not_trip_provider_circuit(self) -> None:
        settings = {"chemical_search": {"failure_circuit_break_threshold": 1}}

        class BudgetExpiredSearcher(RecordingSearcher):
            def _search_chemicalbook(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                self._mark_provider_fetch_failure("lookup_budget_exceeded")
                return None

        searcher = BudgetExpiredSearcher(root_dir=ROOT_DIR, settings=settings)
        result = searcher._run_provider(
            searcher._search_chemicalbook,
            name="ethanol",
            cas="64-17-5",
            query="64-17-5",
            validation_names=["ethanol"],
        )

        self.assertIsNone(result)
        self.assertEqual(ChemicalSearcher._source_failures.get("ChemicalBook", 0), 0)
        self.assertFalse(searcher._source_circuit_open("ChemicalBook"))

    def test_source_circuit_breaker_recovers_after_cooldown(self) -> None:
        settings = {
            "chemical_search": {
                "failure_circuit_break_threshold": 1,
                "failure_circuit_cooldown_seconds": 1,
                "per_source_concurrency": 2,
            }
        }
        searcher = RecordingSearcher(root_dir=ROOT_DIR, settings=settings)
        ChemicalSearcher._source_failures = {"Chemsrc": 1}
        ChemicalSearcher._source_open_until = {"Chemsrc": 1.0}

        result = searcher._run_provider(
            searcher._search_chemsrc,
            name="ethanol",
            cas="64-17-5",
            query="64-17-5",
            validation_names=["ethanol"],
        )

        self.assertIsNotNone(result)
        self.assertEqual(ChemicalSearcher._source_failures.get("Chemsrc"), 0)

    def test_product_or_mixture_without_cas_skips_fallback_web_research(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        name_result = {
            "raw_name": "\u805a\u6c28\u916f\u578b\u9632\u805a\u51dd\u5206\u6563\u5242",
            "cleaned_name": "\u805a\u6c28\u916f\u578b\u9632\u805a\u51dd\u5206\u6563\u5242",
            "standard_name": "\u805a\u6c28\u916f\u578b\u9632\u805a\u51dd\u5206\u6563\u5242",
            "confidence": 0.2,
            "suspected_invalid_name": True,
        }

        self.assertFalse(
            searcher._fallback_web_research_candidate_allowed(
                "\u805a\u6c28\u916f\u578b\u9632\u805a\u51dd\u5206\u6563\u5242",
                "",
                "\u805a\u6c28\u916f\u578b\u9632\u805a\u51dd\u5206\u6563\u5242",
                name_result,
            )
        )

    def test_precise_chemical_name_still_allows_fallback_web_research(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        name_result = {
            "raw_name": "5-\u7532\u57fa-1,3-\u82ef\u4e8c\u915a",
            "cleaned_name": "5-\u7532\u57fa-1,3-\u82ef\u4e8c\u915a",
            "standard_name": "5-\u7532\u57fa-1,3-\u82ef\u4e8c\u915a",
            "confidence": 0.6,
        }

        self.assertTrue(
            searcher._fallback_web_research_candidate_allowed(
                "5-\u7532\u57fa-1,3-\u82ef\u4e8c\u915a",
                "",
                "5-\u7532\u57fa-1,3-\u82ef\u4e8c\u915a",
                name_result,
            )
        )

    def test_relevance_passes_for_similar_name_without_cas(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        relevance = searcher._result_relevance(
            "Product Name: sodium hydroxide Synonyms: caustic soda",
            name="sodium hydroxide",
            cas="",
        )

        self.assertTrue(relevance["relevance_passed"])
        self.assertGreaterEqual(relevance["name_similarity"], 0.82)

    def test_relevance_rejects_unrelated_name_without_cas(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        relevance = searcher._result_relevance(
            "Product Name: glycine hydrochloride Boiling Point 492.4",
            name="sodium hydroxide",
            cas="",
        )

        self.assertFalse(relevance["passed"])

    def test_relevance_rejects_different_metal_even_when_salt_words_are_similar(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        relevance = searcher._result_relevance(
            "Product Name: Europium(III) chloride hexahydrate CAS Number: 13759-92-7",
            name="氯化铁三水",
            cas="",
            validation_names=["氯化铁三水", "Iron(III) chloride hexahydrate"],
        )

        self.assertFalse(relevance["passed"])
        self.assertEqual(relevance["identity_conflict_reason"], "核心元素不一致")

    def test_relevance_rejects_hydrate_count_conflict(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        relevance = searcher._result_relevance(
            "Product Name: Iron(III) chloride hexahydrate CAS Number: 10025-77-1",
            name="氯化铁三水",
            cas="",
            preferred_name="Iron(III) chloride hexahydrate",
            validation_names=["氯化铁三水"],
        )

        self.assertFalse(relevance["passed"])
        self.assertEqual(relevance["identity_conflict_reason"], "水合数不一致")

    def test_relevance_rejects_cas_only_candidate_without_cas(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        relevance = searcher._result_relevance(
            "CAS Number: 473258-60-5 | Molecular Formula: C10H12O2",
            name="甘氨酸",
            cas="",
            preferred_name="473258-60-5",
        )

        self.assertFalse(relevance["passed"])
        self.assertEqual(relevance["name_similarity"], 0.0)

    def test_cas_match_requires_name_evidence_for_informative_name(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        relevance = searcher._result_relevance(
            "CAS Number: 64-17-5 | Molecular Formula: C2H6O | Molecular Weight: 46.07",
            name="氢氧化钠",
            cas="64-17-5",
            validation_names=["氢氧化钠"],
        )

        self.assertFalse(relevance["passed"])
        self.assertTrue(relevance["cas_matched"])
        self.assertTrue(relevance["name_verification_required"])

    def test_cas_match_can_pass_for_non_informative_name(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        relevance = searcher._result_relevance(
            "CAS Number: 64-17-5 | Molecular Formula: C2H6O | Molecular Weight: 46.07",
            name="没写",
            cas="64-17-5",
            validation_names=["没写"],
        )

        self.assertTrue(relevance["passed"])
        self.assertTrue(relevance["cas_matched"])
        self.assertFalse(relevance["name_verification_required"])

    def test_relevance_rejects_detail_when_primary_name_conflicts(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        relevance = searcher._result_relevance(
            "Name: 6-chloro-4-phenylquinazoline Chemical Name: Glycine CAS Number: 4015-28-5",
            name="甘氨酸",
            cas="",
            preferred_name="Glycine",
            validation_names=["甘氨酸", "Glycine"],
        )

        self.assertFalse(relevance["passed"])

    def test_relevance_accepts_detail_when_primary_name_matches(self) -> None:
        searcher = ChemicalSearcher(root_dir=ROOT_DIR)
        relevance = searcher._result_relevance(
            "Name: Glycine Chemical Name: Glycine CAS Number: 56-40-6 Molecular Formula: C2H5NO2",
            name="甘氨酸",
            cas="",
            preferred_name="glycine",
            validation_names=["甘氨酸", "Glycine"],
        )

        self.assertTrue(relevance["passed"])

    def test_chemsrc_row_parser_uses_compound_name_not_cas(self) -> None:
        html = """
        <tr class="rowDat">
          <td><img alt="glycine structure" data-original="x.png"></td>
          <td>
            <a href="https://www.chemsrc.com/en/cas/56-40-6_311698.html">glycine</a>
            <br>
            <a href="https://www.chemsrc.com/en/baike/311698.html">56-40-6</a>
          </td>
        </tr>
        """

        candidates = ChemicalSearcher._chemsrc_search_result_candidates(html, "https://search.chemsrc.com")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "glycine")
        self.assertEqual(candidates[0].url, "https://www.chemsrc.com/en/baike/311698.html")

    def test_best_detail_result_chooses_most_similar_candidate(self) -> None:
        class CandidateSearcher(ChemicalSearcher):
            def _fetch(self, url: str) -> str:
                pages = {
                    "https://example.test/a": "Product Name: glycine hydrochloride Boiling Point 492.4",
                    "https://example.test/b": "Product Name: sodium hydroxide Synonyms: caustic soda",
                }
                return pages[url]

        searcher = CandidateSearcher(root_dir=ROOT_DIR)
        result = searcher._best_detail_result(
            candidates=["https://example.test/a", "https://example.test/b"],
            source="Chemsrc",
            name="sodium hydroxide",
            cas="",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["url"], "https://example.test/b")
        self.assertTrue(result["relevance_passed"])

    def test_unapproved_web_research_is_not_used_after_official_sources_fail(self) -> None:
        class FailingPrimarySearcher(ChemicalSearcher):
            def _search_chemsrc(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                return None

            def _search_chemicalbook(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                return None

        class FakeExtractor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def generate_search_candidates(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
                return {
                    "candidates": ["sodium hydroxide SDS"],
                    "reason": "test",
                    "confidence": 0.8,
                    "used_llm": True,
                }

        class FakeResearcher:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def research(
                self,
                queries: list[str],
                cas: str = "",
                validation_names: list[str] | None = None,
                limit: int = 5,
            ) -> list[ResearchPage]:
                return [
                    ResearchPage(
                        source="PubChem",
                        url="https://pubchem.ncbi.nlm.nih.gov/compound/14798",
                        raw_text="Product Name: sodium hydroxide CAS Number: 1310-73-2 GHS hazard corrosive",
                        source_confidence=0.9,
                        evidence_quality="high",
                        search_query="1310-73-2",
                    )
                ]

        with patch("chemical_searcher.LlmExtractor", FakeExtractor), patch("chemical_searcher.WebResearcher", FakeResearcher):
            result = FailingPrimarySearcher(root_dir=ROOT_DIR).search("NaOH", cas="1310-73-2")

        self.assertTrue(result["need_manual_review"])
        self.assertEqual(result["source"], "")
        self.assertEqual(result["fallback_source"], "")
        self.assertFalse(result["used_llm_search_candidates"])

    def test_low_quality_fallback_result_forces_manual_review(self) -> None:
        class FakeExtractor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def generate_search_candidates(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
                return {"candidates": ["sodium hydroxide"], "used_llm": False}

        class LowQualityResearcher:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def research(
                self,
                queries: list[str],
                cas: str = "",
                validation_names: list[str] | None = None,
                limit: int = 5,
            ) -> list[ResearchPage]:
                return [
                    ResearchPage(
                        source="GuideChem",
                        url="https://example.test/naoh",
                        raw_text="Product Name: sodium hydroxide CAS Number: 1310-73-2",
                        source_confidence=0.6,
                        evidence_quality="low",
                        search_query="1310-73-2",
                    )
                ]

        searcher = RecordingSearcher(root_dir=ROOT_DIR, succeed=False, allow_fallback=True)
        with patch("chemical_searcher.LlmExtractor", FakeExtractor), patch("chemical_searcher.WebResearcher", LowQualityResearcher):
            result = searcher.search("NaOH", cas="1310-73-2")

        self.assertTrue(result["need_manual_review"])
        self.assertEqual(result["fallback_source"], "")
        self.assertEqual(result["retrieval_status"], "not_found")

    def test_llm_does_not_generate_chemical_evidence_when_sources_fail(self) -> None:
        class FakeExtractor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def generate_search_candidates(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
                return {"candidates": ["unknown reagent SDS"], "used_llm": True}

            def generate_knowledge_fallback(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
                return {
                    "raw_text": (
                        "LLM knowledge fallback; no web evidence: likely flammable solvent; "
                        "properties are uncertain and require manual verification."
                    ),
                    "reason": "No trusted web evidence was found.",
                    "confidence": 0.9,
                    "used_llm": True,
                }

        class EmptyResearcher:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def research(
                self,
                queries: list[str],
                cas: str = "",
                validation_names: list[str] | None = None,
                limit: int = 5,
            ) -> list[ResearchPage]:
                return []

        searcher = RecordingSearcher(root_dir=ROOT_DIR, succeed=False, allow_fallback=True)
        with patch("chemical_searcher.LlmExtractor", FakeExtractor), patch("chemical_searcher.WebResearcher", EmptyResearcher):
            result = searcher.search("unknown reagent")

        self.assertTrue(result["need_manual_review"])
        self.assertEqual(result["source"], "")
        self.assertEqual(result["fallback_source"], "")
        self.assertEqual(result["evidence_quality"], "none")
        self.assertEqual(result["source_confidence"], 0.0)
        self.assertFalse(result["used_llm_knowledge_fallback"])

    def test_llm_knowledge_fallback_handles_non_numeric_confidence(self) -> None:
        class FakeExtractor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def generate_search_candidates(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
                return {"candidates": [], "used_llm": False}

            def generate_knowledge_fallback(self, reagent_info: dict[str, Any]) -> dict[str, Any]:
                return {
                    "raw_text": "LLM knowledge fallback; no web evidence: uncertain material.",
                    "reason": "No trusted web evidence was found.",
                    "confidence": "low",
                    "used_llm": True,
                }

        class EmptyResearcher:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def research(
                self,
                queries: list[str],
                cas: str = "",
                validation_names: list[str] | None = None,
                limit: int = 5,
            ) -> list[ResearchPage]:
                return []

        searcher = RecordingSearcher(root_dir=ROOT_DIR, succeed=False, allow_fallback=True)
        with patch("chemical_searcher.LlmExtractor", FakeExtractor), patch("chemical_searcher.WebResearcher", EmptyResearcher):
            result = searcher.search("unknown reagent")

        self.assertTrue(result["need_manual_review"])
        self.assertEqual(result["source_confidence"], 0.0)
        self.assertEqual(result["evidence_quality"], "none")

    def test_nonstandard_selenium_name_gets_manual_review_candidates(self) -> None:
        name_result = {
            "raw_name": "硫酸亚硒",
            "cleaned_name": "硫酸亚硒",
            "standard_name": "硫酸亚硒",
            "english_name": "Selenium(II) sulfate",
            "confidence": 0.6,
            "need_manual_review": True,
            "reason": "low confidence",
        }

        result = ChemicalSearcher(root_dir=ROOT_DIR)._name_result_with_nonstandard_diagnostic(
            name_result,
            name="硫酸亚硒",
            cas="",
        )

        self.assertTrue(result["suspected_invalid_name"])
        self.assertIn("硫酸硒", result["candidate_names"])
        self.assertIn("二硫化硒", result["candidate_names"])
        self.assertTrue(result["need_manual_review"])


    def test_cleaned_name_is_query_priority_and_erp_cas_is_used_for_verification(self) -> None:
        searcher = RecordingSearcher(root_dir=ROOT_DIR)
        result = searcher.search("????", cas="1310-73-2")

        self.assertEqual(searcher.queries, ["????", "1310-73-2"])
        self.assertEqual(result["query"], "????")
        self.assertEqual(result["cas"], "1310-73-2")

    def test_conflicting_erp_cas_uses_trusted_name_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "name_aliases.yaml").write_text(
                """
cas:
  64-17-5:
    standard_name: 乙醇
    english_name: ethanol
    aliases: [乙醇, ethanol]
  1310-73-2:
    standard_name: 氢氧化钠
    english_name: sodium hydroxide
    aliases: [氢氧化钠, 烧碱, sodium hydroxide]
aliases:
  氢氧化钠: 氢氧化钠
  乙醇: 乙醇
abbreviations: {}
""".strip(),
                encoding="utf-8",
            )

            class ConflictingCasSearcher(ChemicalSearcher):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    super().__init__(*args, **kwargs)
                    self.queries: list[tuple[str, str]] = []

                def _search_chemsrc(
                    self,
                    name: str,
                    cas: str,
                    query: str,
                    validation_names: list[str] | None = None,
                ) -> dict[str, Any] | None:
                    self.queries.append((query, cas))
                    if query == "64-17-5":
                        result = self._result(
                            name=name,
                            cas="64-17-5",
                            source="Chemsrc",
                            url="https://example.test/ethanol",
                            raw_text="Product Name: ethanol CAS No. 64-17-5",
                        )
                        result.update(
                            {
                                "relevance_passed": True,
                                "passed": True,
                                "matched_site_name": "ethanol",
                                "name_similarity": 1.0,
                            }
                        )
                        return result
                    if query in {"1310-73-2", "氢氧化钠"}:
                        result = self._result(
                            name=name,
                            cas="1310-73-2",
                            source="Chemsrc",
                            url="https://example.test/sodium-hydroxide",
                            raw_text="Product Name: 氢氧化钠 sodium hydroxide CAS No. 1310-73-2",
                        )
                        result.update(
                            {
                                "relevance_passed": True,
                                "passed": True,
                                "matched_site_name": "氢氧化钠",
                                "name_similarity": 0.95,
                            }
                        )
                        return result
                    return None

                def _search_chemicalbook(
                    self,
                    name: str,
                    cas: str,
                    query: str,
                    validation_names: list[str] | None = None,
                ) -> dict[str, Any] | None:
                    return None

            settings = {"paths": {"name_aliases_yaml": "config/name_aliases.yaml"}}
            searcher = ConflictingCasSearcher(root_dir=root, settings=settings)
            result = searcher.search("氢氧化钠", cas="64-17-5")

        self.assertEqual(result["cas"], "1310-73-2")
        self.assertEqual(result["original_erp_cas"], "64-17-5")
        self.assertEqual(result["candidate_cas"], "1310-73-2")
        self.assertTrue(result["cas_name_conflict"])
        self.assertTrue(result["cas_correction_applied"])
        self.assertFalse(result["need_manual_review"])
        self.assertEqual(result["identity_status"], "conflict")
        self.assertEqual(result["identity_decision_basis"], "name_identity")
        self.assertEqual(result["name_identity"]["cas"], "1310-73-2")
        self.assertEqual(result["cas_identity"]["cas"], "64-17-5")

    def test_cas_authoritative_policy_records_name_conflict_without_blocking(self) -> None:
        class CasAuthoritativeSearcher(ChemicalSearcher):
            def _pubchem_cids(self, query: str) -> list[str]:
                return ["180"] if query == "67-64-1" else []

            def _fetch_json(self, url: str) -> dict[str, Any]:
                if "/property/" in url:
                    return {"PropertyTable": {"Properties": [{
                        "CID": 180,
                        "Title": "Acetone",
                        "IUPACName": "propan-2-one",
                        "MolecularFormula": "C3H6O",
                        "MolecularWeight": "58.08",
                    }]}}
                if "/synonyms/JSON" in url:
                    return {"InformationList": {"Information": [{"Synonym": ["acetone", "67-64-1"]}]}}
                return {"Record": {"Section": []}}

        with tempfile.TemporaryDirectory() as tmp:
            result = CasAuthoritativeSearcher(
                root_dir=Path(tmp),
                settings={"chemical_search": {"cas_authoritative": True}},
            ).search("乙醇", cas="67-64-1")

        self.assertEqual(result["cas"], "67-64-1")
        self.assertEqual(result["identity_status"], "cas_verified")
        self.assertEqual(result["identity_decision_basis"], "cas_identity")
        self.assertTrue(result["cas_name_conflict"])
        self.assertTrue(result["identity_conflict_recorded"])
        self.assertFalse(result["need_manual_review"])
        self.assertFalse(result["cas_correction_applied"])
        self.assertNotIn("corrected_cas", result)
        self.assertEqual(result["query_plan"]["queries_attempted"], ["CAS:67-64-1"])

    def test_cas_checksum_validation_rejects_invalid_erp_cas_for_authoritative_lookup(self) -> None:
        self.assertTrue(ChemicalSearcher._is_valid_cas("67-64-1"))
        self.assertFalse(ChemicalSearcher._is_valid_cas("67-64-2"))
        self.assertFalse(ChemicalSearcher._is_valid_cas("67-641"))

    def test_cas_authoritative_policy_uses_name_fallback_without_cas_correction(self) -> None:
        class NameFallbackSearcher(ChemicalSearcher):
            def _pubchem_cids(self, query: str) -> list[str]:
                return ["702"] if query == "ethanol" else []

            def _fetch_json(self, url: str) -> dict[str, Any]:
                if "/property/" in url:
                    return {"PropertyTable": {"Properties": [{
                        "CID": 702, "Title": "Ethanol", "IUPACName": "ethanol",
                        "MolecularFormula": "C2H6O", "MolecularWeight": "46.07",
                    }]}}
                if "/synonyms/JSON" in url:
                    return {"InformationList": {"Information": [{"Synonym": ["ethanol", "64-17-5"]}]}}
                return {"Record": {"Section": []}}

        with tempfile.TemporaryDirectory() as tmp:
            result = NameFallbackSearcher(
                root_dir=Path(tmp),
                settings={"chemical_search": {"cas_authoritative": True, "providers": ["pubchem"]}},
            ).search("ethanol", cas="67-64-1")

        self.assertEqual(result["identity_status"], "name_verified_pubchem")
        self.assertEqual(result["identity_decision_basis"], "name_fallback_after_cas_unresolved")
        self.assertEqual(result["original_erp_cas"], "67-64-1")
        self.assertEqual(result["candidate_cas"], "64-17-5")
        self.assertEqual(result["cas_lookup_status"], "unresolved_name_fallback")
        self.assertFalse(result["cas_correction_applied"])
        self.assertNotIn("corrected_cas", result)

    def test_pubchem_name_and_cas_converge_with_field_evidence(self) -> None:
        class FixturePubChemSearcher(ChemicalSearcher):
            def _fetch_json(self, url: str) -> dict[str, Any]:
                if "/cids/JSON" in url:
                    return {"IdentifierList": {"CID": [702]}}
                if "/property/" in url:
                    return {"PropertyTable": {"Properties": [{
                        "CID": 702,
                        "Title": "Ethanol",
                        "IUPACName": "ethanol",
                        "MolecularFormula": "C2H6O",
                        "MolecularWeight": "46.07",
                    }]}}
                if "/synonyms/JSON" in url:
                    return {"InformationList": {"Information": [{"Synonym": ["ethanol", "64-17-5"]}]}}
                return {"Record": {"Section": [
                    {
                        "TOCHeading": "Flash Point",
                        "Information": [{"Name": "Flash Point", "Value": {"StringWithMarkup": [{"String": "13 °C"}]}}],
                    },
                    {
                        "TOCHeading": "Boiling Point",
                        "Information": [{"Name": "Boiling Point", "Value": {"StringWithMarkup": [{"String": "78 °C"}]}}],
                    },
                ]}}

        result = FixturePubChemSearcher(root_dir=ROOT_DIR).search("ethanol", cas="64-17-5")

        self.assertEqual(result["identity_status"], "verified")
        self.assertEqual(result["retrieval_status"], "fresh")
        self.assertFalse(result["need_manual_review"])
        self.assertTrue(any(item["field"] == "flash_point" for item in result["evidence_items"]))

    def test_property_enrichment_fetches_pubchem_physical_view_and_selects_field(self) -> None:
        class PropertyFixtureSearcher(ChemicalSearcher):
            def _fetch_json(self, url: str) -> dict[str, Any]:
                self.last_url = url
                return {"Record": {"Section": [{
                    "TOCHeading": "Flash Point",
                    "Information": [{"Name": "Flash Point", "Value": {"StringWithMarkup": [{"String": ">250 °C"}]}}],
                }]}}

        searcher = PropertyFixtureSearcher(root_dir=ROOT_DIR)
        result = searcher._enrich_missing_official_fields({
            "source": "PubChem", "cas": "64-17-5", "pubchem_cid": "702",
            "evidence_items": [], "provider_results": [], "raw_text": "Ethanol",
        })

        self.assertIn("Chemical%20and%20Physical%20Properties", searcher.last_url)
        selected = result["selected_property_evidence"]["flash_point"]
        self.assertEqual(selected["value"], ">250 °C")
        self.assertEqual(selected["status"], "measured")
        self.assertEqual(result["property_enrichment"]["requests"], 2)

    def test_property_evidence_distinguishes_not_applicable_unknown_and_latest_conflict(self) -> None:
        not_applicable = ChemicalSearcher._decorate_property_item({"field": "flash_point", "value": "Not applicable"})
        unavailable = ChemicalSearcher._decorate_property_item({"field": "flash_point", "value": "Not available"})
        self.assertEqual(not_applicable["status"], "not_applicable")
        self.assertEqual(unavailable["status"], "unknown")

        merged = ChemicalSearcher._merge_property_evidence([
            {"field": "flash_point", "value": "13 °C", "source_updated_at": "2025-01-01", "retrieved_at": "2025-01-01T00:00:00+00:00"},
            {"field": "flash_point", "value": "15 °C", "source_updated_at": "2026-01-01", "retrieved_at": "2025-01-01T00:00:00+00:00"},
        ])
        selected = next(item for item in merged if item["selected"])
        self.assertEqual(selected["value"], "15 °C")
        self.assertTrue(selected["conflict"])
        self.assertEqual(selected["selection_basis"], "latest_source_selected")

    def test_property_enrichment_rejects_cross_domain_links_and_overrides_llm_field(self) -> None:
        links = ChemicalSearcher._property_detail_links(
            '<a href="/ChemicalProductProperty_EN_42.htm">Physical properties</a><a href="/MSDS.htm">MSDS</a>',
            "https://www.chemicalbook.com/ChemicalProductProperty_EN_1.htm",
            "ChemicalBook",
        )
        self.assertEqual(links[0], "https://www.chemicalbook.com/ChemicalProductProperty_EN_42.htm")
        self.assertFalse(ChemicalSearcher._is_allowed_property_link(
            "https://example.test/sds", "https://www.chemicalbook.com/ChemicalProductProperty_EN_1.htm", "ChemicalBook",
        ))
        extracted = ApprovalFlowMixin.apply_selected_property_evidence(
            {"flash_point": "25 °C", "flammable": True, "evidence": []},
            {"selected_property_evidence": {
                "flash_point": {"status": "measured", "value": ">250 °C", "source": "PubChem"},
                "flammable": {"status": "unknown", "value": "Not available", "source": "PubChem"},
            }},
        )
        self.assertEqual(extracted["flash_point"], ">250 °C")
        self.assertIsNone(extracted["flammable"])

    def test_chemsrc_chinese_detail_page_is_derived_from_verified_detail_identity(self) -> None:
        url = ChemicalSearcher._chemsrc_chinese_detail_url(
            "https://www.chemsrc.com/en/baike/897661.html",
            "64-17-5",
            "Chemsrc",
        )

        self.assertEqual(url, "https://www.chemsrc.com/cas/64-17-5_897661.html")
        self.assertEqual(
            ChemicalSearcher._chemsrc_chinese_detail_url(
                "https://www.chemsrc.com/en/baike/897661.html",
                "",
                "Chemsrc",
            ),
            "",
        )
        self.assertEqual(
            ChemicalSearcher._chemsrc_chinese_detail_url(
                "https://www.chemicalbook.com/ChemicalProductProperty_EN_1.htm",
                "64-17-5",
                "ChemicalBook",
            ),
            "",
        )

    def test_linked_property_page_uses_current_identity_validation_signature(self) -> None:
        class PropertyLinkSearcher(ChemicalSearcher):
            def _fetch(self, url: str) -> str:
                self.last_url = url
                return "Product Name: Ethanol CAS No. 64-17-5 Flash Point: 18 °C"

        searcher = PropertyLinkSearcher(root_dir=ROOT_DIR)
        items, diagnostic = searcher._linked_property_evidence(
            url="https://www.chemsrc.com/cas/64-17-5_897661.html",
            parent={
                "source": "Chemsrc",
                "url": "https://www.chemsrc.com/en/baike/897661.html",
                "name": "Ethanol",
                "matched_site_name": "Ethanol",
            },
            verified_cas="64-17-5",
        )

        self.assertEqual(searcher.last_url, "https://www.chemsrc.com/cas/64-17-5_897661.html")
        self.assertEqual(diagnostic["status"], "success")
        self.assertTrue(any(item["field"] == "flash_point" for item in items))

    def test_chemsrc_chinese_detail_uses_structured_values_not_flattened_prose(self) -> None:
        page = '''<script type="application/ld+json">{
          "additionalProperty": [
            {"name": "闪点", "value": "8.9±0.0 °C"},
            {"name": "沸点", "value": "72.6±3.0 °C at 760 mmHg"},
            {"name": "危害声明", "value": "H225-H319"}
          ]
        }</script>'''

        items = ChemicalSearcher._chemsrc_chinese_property_evidence(
            page,
            source_url="https://www.chemsrc.com/cas/64-17-5_897661.html",
        )

        values = {(item["field"], item["value"]) for item in items}
        self.assertIn(("flash_point", "8.9±0.0 °C"), values)
        self.assertIn(("boiling_point", "72.6±3.0 °C at 760 mmHg"), values)
        self.assertIn(("ghs_classification", "H225-H319"), values)
        self.assertTrue(all(len(item["value"]) < 80 for item in items))

    def test_cas_only_hit_is_accepted_when_returned_name_exactly_validates_input(self) -> None:
        class CasFallbackSearcher(ChemicalSearcher):
            def _pubchem_cids(self, query: str) -> list[str]:
                return ["702"] if query == "64-17-5" else []

            def _fetch_json(self, url: str) -> dict[str, Any]:
                if "/property/" in url:
                    return {"PropertyTable": {"Properties": [{"Title": "Ethanol", "IUPACName": "ethanol"}]}}
                if "/synonyms/JSON" in url:
                    return {"InformationList": {"Information": [{"Synonym": ["ethanol", "64-17-5"]}]}}
                return {"Record": {}}

            def _enrich_missing_official_fields(self, result: dict[str, Any]) -> dict[str, Any]:
                return result

        result = CasFallbackSearcher(root_dir=ROOT_DIR).search("ethanol", cas="64-17-5")

        self.assertEqual(result["identity_status"], "cas_only")
        self.assertTrue(result["relevance_passed"])
        self.assertFalse(result["need_manual_review"])

    def test_supplier_sds_extracts_mixture_components_and_measured_properties(self) -> None:
        sds = """
SECTION 2: Hazard identification
Highly flammable liquid and vapor
SECTION 3: Composition
Ethanol 64-17-5 70-80%
Water 7732-18-5 20-30%
SECTION 9: Physical and chemical properties
Flash point: 18 °C
Boiling point: 78 °C
SECTION 10: Stability and reactivity
Stable under normal conditions
"""
        result = ChemicalSearcher(root_dir=ROOT_DIR).search(
            "Ethanol disinfectant",
            manufacturer="Example Supplier",
            catalog_number="SDS-001",
            sds_text=sds,
            erp_is_mixture=True,
        )

        self.assertEqual(result["source"], "Supplier SDS")
        self.assertEqual(result["retrieval_status"], "local")
        self.assertEqual(result["identity_status"], "verified")
        self.assertTrue(result["is_mixture"])
        self.assertTrue(result["composition_complete"])
        self.assertEqual({item["cas"] for item in result["mixture_components"]}, {"64-17-5", "7732-18-5"})
        self.assertTrue(any(item["field"] == "flash_point" for item in result["evidence_items"]))

    def test_supplier_sds_with_trade_secret_requires_review(self) -> None:
        sds = """
SECTION 2: Hazard identification
Flammable
SECTION 3: Composition
Proprietary trade secret component
SECTION 9: Physical and chemical properties
Flash point: 25 °C
SECTION 10: Stability and reactivity
Stable
"""
        result = ChemicalSearcher(root_dir=ROOT_DIR).search("Commercial cleaner", sds_text=sds, erp_is_mixture=True)

        self.assertTrue(result["need_manual_review"])
        self.assertFalse(result["composition_complete"])

    def test_http_503_retries_three_times_then_succeeds(self) -> None:
        class Response:
            headers = type("Headers", (), {"get_content_charset": lambda self: "utf-8"})()

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                return b'{"ok": true}'

        url = "https://example.test/retry"
        transient = HTTPError(url, 503, "busy", {}, None)
        searcher = ChemicalSearcher(settings={"chemical_search": {"max_attempts": 3}}, root_dir=ROOT_DIR)
        with patch("chemical_searcher.urlopen", side_effect=[transient, transient, Response()]) as mocked, patch.object(
            searcher, "_retry_wait", return_value=None
        ):
            payload = searcher._fetch(url)

        self.assertEqual(payload, '{"ok": true}')
        self.assertEqual(mocked.call_count, 3)

    def test_sustained_503_returns_unavailable_without_exception_or_log_spam(self) -> None:
        class UnavailableSearcher(ChemicalSearcher):
            def _search_pubchem(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                self._mark_provider_fetch_failure("http_error_503")
                return None

        settings = {"chemical_search": {"failure_circuit_break_threshold": 1, "failure_circuit_cooldown_seconds": 300}}
        with patch("builtins.print") as mocked_print:
            result = UnavailableSearcher(settings=settings, root_dir=ROOT_DIR).search("ethanol", cas="64-17-5")

        messages = [str(call.args[0]) for call in mocked_print.call_args_list if call.args]
        self.assertEqual(result["retrieval_status"], "unavailable")
        self.assertTrue(result["need_manual_review"])
        self.assertEqual(sum("circuit opened" in message for message in messages), 1)
        self.assertFalse(any("Chemical source failure:" in message for message in messages))

    def test_search_many_deduplicates_identical_upstream_work(self) -> None:
        searcher = RecordingSearcher(root_dir=ROOT_DIR)
        results = searcher.search_many([
            {"name": "ethanol", "cas": "64-17-5"},
            {"name": "ethanol", "cas": "64-17-5"},
        ])

        self.assertEqual(len(results), 2)
        self.assertEqual(len(searcher.queries), 2)

    def test_search_many_deduplicates_different_package_metadata(self) -> None:
        searcher = RecordingSearcher(root_dir=ROOT_DIR)
        results = searcher.search_many([
            {"name": "ethanol", "cas": "64-17-5", "规格": "500 mL", "规格单位": "瓶", "包装方式": "箱装"},
            {"name": "ethanol", "cas": "64-17-5", "规格": "2.5 L", "规格单位": "桶", "包装方式": "散装"},
        ])

        self.assertEqual(len(results), 2)
        self.assertEqual(len(searcher.queries), 2)

    def test_search_many_batches_pubchem_properties_by_cid(self) -> None:
        class BatchFixtureSearcher(ChemicalSearcher):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self.urls: list[str] = []

            def _fetch_json(self, url: str) -> dict[str, Any]:
                self.urls.append(url)
                if "/name/ethanol/" in url or "/name/64-17-5/" in url:
                    return {"IdentifierList": {"CID": [702]}}
                if "/name/water/" in url or "/name/7732-18-5/" in url:
                    return {"IdentifierList": {"CID": [962]}}
                if "/property/" in url:
                    cids = [cid for cid in ("702", "962") if cid in url]
                    return {"PropertyTable": {"Properties": [
                        {"CID": int(cid), "Title": "Ethanol" if cid == "702" else "Water", "IUPACName": "ethanol" if cid == "702" else "oxidane"}
                        for cid in cids
                    ]}}
                if "/synonyms/JSON" in url:
                    cids = [cid for cid in ("702", "962") if cid in url]
                    return {"InformationList": {"Information": [
                        {"CID": int(cid), "Synonym": ["ethanol", "64-17-5"] if cid == "702" else ["water", "7732-18-5"]}
                        for cid in cids
                    ]}}
                return {"Record": {}}

            def _enrich_missing_official_fields(self, result: dict[str, Any]) -> dict[str, Any]:
                return result

        searcher = BatchFixtureSearcher(root_dir=ROOT_DIR)
        results = searcher.search_many([
            {"name": "ethanol", "cas": "64-17-5"},
            {"name": "water", "cas": "7732-18-5"},
        ])

        self.assertEqual(len(results), 2)
        self.assertTrue(any("cid/702,962/property/" in url for url in searcher.urls))

    def test_conflicting_erp_cas_without_trusted_name_result_is_not_corrected(self) -> None:
        class LowTrustCorrectionSearcher(ChemicalSearcher):
            def _search_chemsrc(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                if query == "64-17-5":
                    result = self._result(
                        name=name,
                        cas="64-17-5",
                        source="Chemsrc",
                        url="https://example.test/ethanol",
                        raw_text="Product Name: ethanol CAS No. 64-17-5",
                    )
                    result.update(
                        {
                            "relevance_passed": True,
                            "passed": True,
                            "matched_site_name": "ethanol",
                            "name_similarity": 1.0,
                        }
                    )
                    return result
                result = self._result(
                    name=name,
                    cas="1310-73-2",
                    source="GuideChem",
                    url="https://example.test/naoh",
                    raw_text="氢氧化钠 CAS No. 1310-73-2",
                )
                result.update(
                    {
                        "relevance_passed": True,
                        "passed": True,
                        "matched_site_name": "氢氧化钠",
                        "name_similarity": 0.95,
                        "source_confidence": 0.6,
                    }
                )
                return result

            def _search_chemicalbook(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                return None

        result = LowTrustCorrectionSearcher(root_dir=ROOT_DIR).search("氢氧化钠", cas="64-17-5")

        self.assertTrue(result["need_manual_review"])
        self.assertTrue(result["cas_name_conflict"])
        self.assertEqual(result["original_erp_cas"], "64-17-5")
        self.assertNotIn("corrected_cas", result)


    def test_name_lookup_cas_without_erp_cas_is_marked_as_candidate(self) -> None:
        class CandidateCasSearcher(ChemicalSearcher):
            def _search_chemsrc(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                result = self._result(
                    name=name,
                    cas="540-69-2",
                    source="Chemsrc",
                    url="https://example.test/ammonium-formate",
                    raw_text="\u7532\u9178\u94f5 CAS No. 540-69-2",
                )
                result.update(
                    {
                        "relevance_passed": True,
                        "passed": True,
                        "matched_site_name": "\u7532\u9178\u94f5",
                        "name_similarity": 0.95,
                    }
                )
                return result

            def _search_chemicalbook(
                self,
                name: str,
                cas: str,
                query: str,
                validation_names: list[str] | None = None,
            ) -> dict[str, Any] | None:
                return None

        result = CandidateCasSearcher(root_dir=ROOT_DIR).search("\u7532\u9178\u94f5")

        self.assertEqual(result["candidate_cas"], "540-69-2")
        self.assertEqual(result["identity_status"], "cas_missing")
        self.assertTrue(result["need_manual_review"])


if __name__ == "__main__":
    unittest.main()
