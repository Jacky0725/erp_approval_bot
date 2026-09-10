from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chinese_alias_backfill import ChineseAliasBackfill  # noqa: E402
from chemical_searcher import ChemicalSearcher  # noqa: E402


class FakeSearcher:
    def __init__(self, **_: object) -> None:
        self._search_chemsrc = self.chemsrc
        self._search_chemicalbook = self.chemicalbook

    @staticmethod
    def _validation_names(*values: object) -> list[str]:
        return [str(value) for value in values if isinstance(value, str) and value]

    @staticmethod
    def chemsrc(**_: object) -> dict[str, object] | None:
        return None

    @staticmethod
    def chemicalbook(**_: object) -> dict[str, object] | None:
        return None

    def _run_provider(self, provider: object, **_: object) -> dict[str, object] | None:
        if provider is self._search_chemsrc:
            return {"relevance_passed": True, "cas": "64-17-5", "matched_site_name": "Ethanol", "source": "Chemsrc", "url": "https://example.test", "name_similarity": 0.94}
        return None


class ChineseAliasBackfillTest(unittest.TestCase):
    def test_collects_only_a_verified_candidate_for_single_substance_identity(self) -> None:
        service = ChineseAliasBackfill(root_dir=ROOT_DIR, searcher_factory=FakeSearcher)
        candidates = service.collect([{"试剂名称": "乙醇别名", "CAS号": "64-17-5"}])

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].alias, "乙醇别名")
        self.assertEqual(candidates[0].source, "Chemsrc")

    def test_skips_mixture_products(self) -> None:
        service = ChineseAliasBackfill(root_dir=ROOT_DIR, searcher_factory=FakeSearcher)
        self.assertEqual(service.collect([{"试剂名称": "乙醇溶液", "CAS号": "64-17-5"}]), [])

    def test_chemicalbook_uses_chinese_search_before_english_fallback(self) -> None:
        class Searcher(ChemicalSearcher):
            def __init__(self) -> None:
                super().__init__(root_dir=ROOT_DIR)
                self.urls: list[str] = []

            def _fetch(self, url: str, headers: object = None) -> str:
                self.urls.append(url)
                return ""

        searcher = Searcher()
        self.assertIsNone(searcher._search_chemicalbook("乙醇", "", "乙醇"))
        self.assertIn("Search.aspx", searcher.urls[0])
        self.assertIn("Search_EN.aspx", searcher.urls[1])
