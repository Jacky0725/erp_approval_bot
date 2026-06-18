from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_searcher import ChemicalSearcher  # noqa: E402
from web_researcher import ResearchPage  # noqa: E402


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

    def test_search_failure_returns_manual_review_with_normalization(self) -> None:
        searcher = RecordingSearcher(root_dir=ROOT_DIR, succeed=False)
        result = searcher.search("NaOH 0.1mol/L 分析纯")

        self.assertTrue(result["need_manual_review"])
        self.assertEqual(result["name_normalization"]["standard_name"], "氢氧化钠")
        self.assertIn("1310-73-2", result["raw_text"])

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


if __name__ == "__main__":
    unittest.main()
