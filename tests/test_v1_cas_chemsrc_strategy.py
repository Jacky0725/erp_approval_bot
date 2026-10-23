from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from chemical_searcher import ChemicalSearcher  # noqa: E402
from chemical_search_cache import ChemicalSearchCache  # noqa: E402
from name_normalizer import NameNormalizer  # noqa: E402


def _source_result(searcher: ChemicalSearcher, source: str, cas: str, name: str) -> dict[str, Any]:
    result = searcher._result(
        name=name,
        cas=cas,
        source=source,
        url=f"https://example.test/{source.lower()}",
        raw_text=f"{name} CAS {cas}",
    )
    result.update({"candidate_cas": cas, "matched_site_name": name, "relevance_passed": False})
    return result


def test_valid_cas_uses_chemsrc_first_and_ignores_name_mismatch() -> None:
    class Searcher(ChemicalSearcher):
        calls: list[str] = []

        def _search_chemsrc(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append("chemsrc")
            return _source_result(self, "Chemsrc", "67-64-1", "Acetone")

        def _search_chemicalbook(self, **kwargs: Any) -> dict[str, Any] | None:
            self.calls.append("chemicalbook")
            return None

        def _search_pubchem(self, **kwargs: Any) -> dict[str, Any] | None:
            self.calls.append("pubchem")
            return None

        def _enrich_missing_official_fields(self, result: dict[str, Any]) -> dict[str, Any]:
            return result

    with tempfile.TemporaryDirectory() as tmp:
        searcher = Searcher(
            root_dir=Path(tmp),
            settings={"chemical_search": {"cas_authoritative": True, "providers": ["chemsrc", "chemicalbook", "pubchem"]}},
        )
        searcher.calls = []
        result = searcher.search("乙醇", cas="67-64-1")

    assert searcher.calls == ["chemsrc"]
    assert result["identity_status"] == "cas_verified"
    assert result["cas_name_conflict"] is False
    assert result["need_manual_review"] is False


def test_chemsrc_full_page_parser_keeps_sections_and_selects_lowest_flash_point() -> None:
    page = """
    <div id="wuHuaDiv"><table>
      <tr><th>闪点</th><td>8.9 °C</td></tr><tr><th>沸点</th><td>78 °C</td></tr>
    </table></div>
    <div id="MSDSDiv"><table id="MSDSTbl"><tr><td>
      第一部分：化学品名称 乙醇 第二部分：危险性概述 易燃
      第九部分：闪点：12 °C；沸点：78.3 °C。 第十部分：避免强氧化剂。
      第十一部分：LD50: 7060 mg/kg（大鼠经口）。
      第十二部分：LC50: 13 g/L（96h 鱼）。
      第十三部分：废弃处置 第十四部分：UN 1170 第十五部分：法规 第十六部分：其他
    </td></tr></table></div>
    <div id="duXinDiv"><table id="duXinTbl"><tr><td>
      LD50: 7060mg/kg（大鼠经口）；LD50: 7430mg/kg（兔经皮）；LC50: 20000ppm（大鼠吸入，10h）
    </td></tr></table></div>
    <div id="anQuanDiv">信号词：危险 H225 P210 UN 1170 包装等级 II</div>
    """
    items, sections = ChemicalSearcher._chemsrc_full_page_evidence(
        page,
        source_url="https://www.chemsrc.com/cas/64-17-5_897661.html#MSDSDiv",
    )

    flashes = [item for item in items if item.get("field") == "flash_point"]
    selected = next(item for item in flashes if item.get("selected"))
    assert selected["normalized_value"] == 8.9
    assert selected["selection_basis"] == "lowest_comparable_flash_point"
    assert {item["value"] for item in flashes} >= {"8.9 °C", "12 °C"}
    assert sections["physicochemical"]["rows"]["闪点"] == "8.9 °C"
    assert "9" in sections["msds"]["sections"]
    assert len(sections["toxicology_ecology"]["endpoints"]) == 3
    assert sections["safety"]["un_number"] == "1170"


def test_transient_cas_failure_never_becomes_name_fallback() -> None:
    class Searcher(ChemicalSearcher):
        def _search_chemsrc(self, **kwargs: Any) -> None:
            self._mark_provider_fetch_failure("http_error_503")
            return None

        def _search_chemicalbook(self, **kwargs: Any) -> None:
            return None

        def _search_pubchem(self, **kwargs: Any) -> None:
            return None

    ChemicalSearcher._source_failures = {}
    ChemicalSearcher._source_open_until = {}
    ChemicalSearcher._source_failure_reasons = {}
    ChemicalSearcher._provider_negative_until = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = Searcher(
                root_dir=Path(tmp),
                settings={"chemical_search": {"cas_authoritative": True, "providers": ["chemsrc", "chemicalbook", "pubchem"]}},
            ).search("乙醇", cas="64-17-5")
    finally:
        ChemicalSearcher._source_failures = {}
        ChemicalSearcher._source_open_until = {}
        ChemicalSearcher._source_failure_reasons = {}
        ChemicalSearcher._provider_negative_until = {}

    assert result["identity_status"] == "unresolved"
    assert result["lookup_status"] == "unavailable"
    assert result["need_manual_review"] is True
    assert result["provider_results"][0]["failure_kind"] == "http_error_503"


def test_invalid_erp_cas_is_not_used_as_a_query_key() -> None:
    class Searcher(ChemicalSearcher):
        queries: list[str] = []

        def _search_chemsrc(self, **kwargs: Any) -> dict[str, Any]:
            self.queries.append(kwargs["query"])
            return _source_result(self, "Chemsrc", "64-17-5", "乙醇")

        def _enrich_missing_official_fields(self, result: dict[str, Any]) -> dict[str, Any]:
            return result

    with tempfile.TemporaryDirectory() as tmp:
        searcher = Searcher(
            root_dir=Path(tmp),
            settings={"chemical_search": {"cas_authoritative": True, "providers": ["chemsrc"]}},
        )
        searcher.queries = []
        result = searcher.search("乙醇", cas="64-17-6")

    assert "64-17-6" not in searcher.queries
    assert result["identity_status"] == "name_verified"
    assert result["cas_validation_status"] == "invalid"


def test_cas_input_normalizes_unicode_hyphens_and_missing_markers() -> None:
    assert ChemicalSearcher._extract_cas("7704‑34‑9") == "7704-34-9"
    assert ChemicalSearcher._is_valid_cas("7704‑34‑9")

    class NoResult(ChemicalSearcher):
        def _search_chemsrc(self, **kwargs: Any) -> None:
            return None

    result = NoResult(
        settings={"chemical_search": {"cas_authoritative": True, "providers": ["chemsrc"]}},
    ).search("未知试剂", cas="-")
    assert result["cas_validation_status"] == "missing"


def test_toxicity_values_are_only_minimized_inside_comparable_groups() -> None:
    evidence = [
        {"field": "toxicity", "value": "LD50 10 mg/kg oral", "endpoint": "LD50", "route": "oral", "unit_dimension": "mg/kg", "value_text": "10"},
        {"field": "toxicity", "value": "LD50 5 mg/kg oral", "endpoint": "LD50", "route": "oral", "unit_dimension": "mg/kg", "value_text": "5"},
        {"field": "toxicity", "value": "LD50 2 mg/kg dermal", "endpoint": "LD50", "route": "dermal", "unit_dimension": "mg/kg", "value_text": "2"},
        {"field": "toxicity", "value": "LC50 1 ppm inhalation", "endpoint": "LC50", "route": "inhalation", "unit_dimension": "ppm", "value_text": "1"},
    ]
    merged = ChemicalSearcher._merge_property_evidence(evidence)
    selected = {item["value"] for item in merged if item.get("selected")}
    assert "LD50 5 mg/kg oral" in selected
    assert "LD50 2 mg/kg dermal" in selected
    assert "LC50 1 ppm inhalation" in selected
    assert "LD50 10 mg/kg oral" not in selected


def test_flash_point_uncertainty_is_not_mistaken_for_a_range() -> None:
    assert ChemicalSearcher._temperature_lower_bound_c("8.9±0.0 °C") == (8.9, "=")
    assert ChemicalSearcher._temperature_lower_bound_c("12~15 °C") == (12.0, "=")
    assert ChemicalSearcher._temperature_lower_bound_c("<32 °F") == (0.0, "<")


def test_name_cleaning_preserves_salt_hydrate_and_anhydrous_identity() -> None:
    normalizer = NameNormalizer(enable_llm=False, aliases_path=Path("__missing__.yaml"))
    assert "无水" in normalizer.normalize("无水硫酸铜 AR")["cleaned_name"]
    assert "五水合物" in normalizer.normalize("硫酸铜五水合物GR")["cleaned_name"]
    assert "溶液" in normalizer.normalize("氢氧化钾滴定溶液 1mol/L")["cleaned_name"]


def test_chemsrc_english_cas_url_maps_to_single_chinese_detail_document() -> None:
    assert ChemicalSearcher._chemsrc_chinese_detail_url(
        "https://www.chemsrc.com/en/cas/64-17-5_897661.html#MSDSDiv",
        "64-17-5",
        "Chemsrc",
    ) == "https://www.chemsrc.com/cas/64-17-5_897661.html"


def test_chemsrc_detail_mapping_cache_is_separate_from_result_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        settings = {"chemical_search": {"cache": {"enabled": True, "path": "mapping.sqlite"}}}
        cache = ChemicalSearchCache(Path(tmp), settings)
        cache.put_detail_mapping(
            "chemsrc", "64-17-5",
            {"url": "https://www.chemsrc.com/cas/64-17-5_897661.html", "cas": "64-17-5"},
            ttl_days=180,
        )
        mapping = cache.get_detail_mapping("chemsrc", "64-17-5")
    assert mapping is not None
    assert mapping["cas"] == "64-17-5"
