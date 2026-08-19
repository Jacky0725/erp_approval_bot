from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_searcher import ChemicalSearcher  # noqa: E402
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
        ChemicalSearcher._source_semaphores = {}

    def test_query_candidates_try_names_after_cas(self) -> None:
        self.assertEqual(
            ChemicalSearcher._query_candidates("123-45-6", "standard", "cleaned", "english"),
            ["123-45-6", "standard", "cleaned", "english"],
        )

    def test_manual_verified_source_url_is_used_before_search(self) -> None:
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

        self.assertEqual(searcher.urls, ["https://www.chemsrc.com/cas/18868-43-4_88297.html"])
        self.assertFalse(searcher.used_search)
        self.assertFalse(result["need_manual_review"])
        self.assertEqual(result["cas"], "18868-43-4")
        self.assertEqual(result["url"], "https://www.chemsrc.com/cas/18868-43-4_88297.html")

    def test_manual_verified_source_url_rejects_wrong_cas(self) -> None:
        class WrongCasSearcher(ChemicalSearcher):
            def _fetch(self, url: str) -> str:
                return "CAS No. 1317-33-5 Name: Molybdenum trioxide Chemical Name: Molybdenum trioxide"

        searcher = WrongCasSearcher(root_dir=ROOT_DIR)
        result = searcher.search("二氧化钼")

        self.assertTrue(result["need_manual_review"])
        self.assertIn("人工确认 URL", result["raw_text"])

    def test_search_normalizes_before_query_and_prefers_cas(self) -> None:
        searcher = RecordingSearcher(root_dir=ROOT_DIR)
        result = searcher.search("工业酒精 75% 500ml", cas="64-17-5", specification="500ml", unit="瓶")

        self.assertEqual(searcher.queries, ["64-17-5"])
        self.assertEqual(result["query"], "64-17-5")
        self.assertEqual(result["name_normalization"]["standard_name"], "乙醇")
        self.assertEqual(result["name_normalization"]["english_name"], "ethanol")
        self.assertFalse(result["need_manual_review"])

    def test_search_uses_cas_from_alias_when_cas_is_empty(self) -> None:
        searcher = RecordingSearcher(root_dir=ROOT_DIR)
        result = searcher.search("NaOH 0.1mol/L 分析纯")

        self.assertEqual(searcher.queries, ["1310-73-2"])
        self.assertEqual(result["query"], "1310-73-2")
        self.assertEqual(result["name_normalization"]["standard_name"], "氢氧化钠")
        self.assertEqual(result["name_normalization"]["english_name"], "sodium hydroxide")
        self.assertEqual(result["name_normalization"]["concentration"], "0.1mol/L")

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

    def test_source_circuit_breaker_skips_only_failed_source(self) -> None:
        settings = {"chemical_search": {"failure_circuit_break_threshold": 1, "per_source_concurrency": 2}}
        searcher = RecordingSearcher(root_dir=ROOT_DIR, settings=settings, succeed=False)
        ChemicalSearcher._source_failures = {}
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

    def test_fallback_web_research_is_used_after_primary_sources_fail(self) -> None:
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

        self.assertFalse(result["need_manual_review"])
        self.assertEqual(result["source"], "PubChem")
        self.assertEqual(result["fallback_source"], "PubChem")
        self.assertEqual(result["source_confidence"], 0.9)
        self.assertEqual(result["evidence_quality"], "high")
        self.assertTrue(result["used_llm_search_candidates"])

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
        self.assertEqual(result["fallback_source"], "GuideChem")
        self.assertIn("low", result["failure_reason"])

    def test_llm_knowledge_fallback_is_used_when_no_web_evidence_exists(self) -> None:
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
        self.assertEqual(result["source"], "LLM knowledge fallback")
        self.assertEqual(result["fallback_source"], "LLM knowledge fallback")
        self.assertEqual(result["evidence_quality"], "llm_knowledge_low")
        self.assertLessEqual(result["source_confidence"], 0.65)
        self.assertTrue(result["used_llm_knowledge_fallback"])
        self.assertIn("no trusted web evidence", result["failure_reason"])

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
        self.assertEqual(result["evidence_quality"], "llm_knowledge_low")

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


    def test_erp_cas_is_query_priority_even_when_name_matches_different_alias(self) -> None:
        searcher = RecordingSearcher(root_dir=ROOT_DIR)
        result = searcher.search("????", cas="1310-73-2")

        self.assertEqual(searcher.queries, ["1310-73-2"])
        self.assertEqual(result["query"], "1310-73-2")
        self.assertEqual(result["cas"], "1310-73-2")

    def test_conflicting_erp_cas_is_corrected_by_trusted_name_search(self) -> None:
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
        self.assertEqual(result["corrected_cas"], "1310-73-2")
        self.assertTrue(result["cas_name_conflict"])
        self.assertTrue(result["cas_correction_applied"])
        self.assertEqual(result["name_normalization"]["cas"], "1310-73-2")
        self.assertEqual(result["name_normalization"]["original_erp_cas"], "64-17-5")
        self.assertIn(("64-17-5", "64-17-5"), searcher.queries)
        self.assertIn(("1310-73-2", ""), searcher.queries)

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


if __name__ == "__main__":
    unittest.main()
