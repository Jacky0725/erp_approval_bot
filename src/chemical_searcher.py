from __future__ import annotations

import html
import re
import socket
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen

from llm_extractor import LlmExtractor
from name_normalizer import NameNormalizer
from web_researcher import ResearchPage, WebResearcher


HAZARD_KEYWORDS = [
    "易燃",
    "可燃",
    "爆炸",
    "易爆",
    "氧化",
    "腐蚀",
    "刺激",
    "有毒",
    "剧毒",
    "高毒",
    "致癌",
    "急性毒性",
    "皮肤腐蚀",
    "眼刺激",
    "吸入有害",
    "危险",
    "flammable",
    "combustible",
    "explosive",
    "oxidizer",
    "oxidizing",
    "corrosive",
    "irritant",
    "toxic",
    "poison",
    "carcinogen",
    "hazard",
    "harmful",
]


@dataclass(frozen=True)
class SearchCandidate:
    url: str
    title: str = ""


@dataclass
class ChemicalSearcher:
    settings: dict[str, Any] | None = None
    root_dir: Any | None = None
    timeout_seconds: int = 20

    def search(
        self,
        reagent_name: str,
        cas: str = "",
        specification: str = "",
        unit: str = "",
    ) -> dict[str, Any]:
        name = reagent_name.strip()
        normalizer = NameNormalizer(settings=self.settings, root_dir=self.root_dir)
        name_result = normalizer.normalize(raw_name=name, cas=cas, specification=specification, unit=unit)

        input_cas = self._extract_cas(str(cas or ""))
        cas_no = input_cas or self._extract_cas(str(name_result.get("cas") or ""))
        standard_name = str(name_result.get("standard_name") or "").strip()
        cleaned_name = str(name_result.get("cleaned_name") or "").strip()
        english_name = str(name_result.get("english_name") or "").strip()
        source_url = str(name_result.get("source_url") or name_result.get("chemsrc_url") or "").strip()
        if input_cas and source_url and input_cas not in source_url:
            source_url = ""
        aliases = [str(value).strip() for value in (name_result.get("aliases") or []) if str(value).strip()]
        queries = self._query_candidates(cas_no, standard_name, cleaned_name, english_name)

        validation_names = self._validation_names(standard_name or name, standard_name, cleaned_name, english_name, aliases)
        if source_url:
            result = self._detail_result_from_url(
                url=source_url,
                source="Chemsrc",
                name=standard_name or english_name or name,
                cas=cas_no,
                validation_names=validation_names,
            )
            if result:
                result["name_normalization"] = name_result
                result["query"] = cas_no or standard_name or source_url
                return result
            return self._manual_result(
                name=standard_name or english_name or name,
                cas=cas_no,
                reason=f"人工确认 URL 抓取失败或 CAS/名称校验未通过: {source_url}",
                name_normalization=name_result,
            )

        if not queries:
            return self._manual_result(
                name=name,
                cas=cas_no,
                reason="试剂名称和 CAS 号均为空，名称标准化后也无可查询名称。",
                name_normalization=name_result,
            )

        search_name = standard_name or cleaned_name or english_name or name
        failed_queries: list[str] = []
        for query in queries:
            validation_names = self._validation_names(query, standard_name, cleaned_name, english_name, aliases)
            for provider in (self._search_chemsrc, self._search_chemicalbook):
                result = provider(name=query, cas=cas_no, query=query, validation_names=validation_names)
                if result:
                    result["name_normalization"] = name_result
                    result["query"] = query
                    return result
            failed_queries.append(query)

        fallback_result = self._fallback_web_research(
            reagent_name=name,
            cas=cas_no,
            search_name=search_name,
            name_result=name_result,
            failed_queries=failed_queries,
            validation_names=self._validation_names(search_name, standard_name, cleaned_name, english_name, aliases),
        )
        if fallback_result:
            return fallback_result

        name_result = self._name_result_with_nonstandard_diagnostic(name_result, name=name, cas=cas_no)
        nonstandard_reason = str(name_result.get("suspected_invalid_reason") or "").strip()
        reason = f"Chemsrc 和 ChemicalBook 均查询失败或无有效结果。查询关键词: {', '.join(failed_queries)}"
        if nonstandard_reason:
            reason = f"{reason}；{nonstandard_reason}"

        return self._manual_result(
            name=search_name or name,
            cas=cas_no,
            reason=reason,
            name_normalization=name_result,
        )

    def _name_result_with_nonstandard_diagnostic(
        self,
        name_result: dict[str, Any],
        name: str,
        cas: str,
    ) -> dict[str, Any]:
        result = dict(name_result or {})
        if cas:
            return result

        try:
            confidence = float(result.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence >= 0.8:
            return result

        candidates = self._nonstandard_name_candidates(result, name)
        if not candidates:
            return result

        result["candidate_names"] = candidates
        result["suspected_invalid_name"] = True
        result["need_manual_review"] = True
        result["suspected_invalid_reason"] = (
            "疑似非标准试剂名或 ERP 录入错误；无 CAS 且主站/托底查询均未取得可信页面。"
            f"建议人工核对是否应为：{', '.join(candidates)}。"
        )
        reason = str(result.get("reason") or "").strip()
        if result["suspected_invalid_reason"] not in reason:
            result["reason"] = f"{reason} {result['suspected_invalid_reason']}".strip()
        return result

    @staticmethod
    def _nonstandard_name_candidates(name_result: dict[str, Any], name: str) -> list[str]:
        texts = [
            name,
            name_result.get("raw_name", ""),
            name_result.get("cleaned_name", ""),
            name_result.get("standard_name", ""),
            name_result.get("english_name", ""),
        ]
        normalized = " ".join(str(value or "") for value in texts).lower()
        candidates: list[str] = []

        if "硫酸亚硒" in normalized or "selenium(ii) sulfate" in normalized:
            candidates.extend(["硫酸硒", "二硫化硒", "亚硒酸盐", "硒酸盐"])

        return list(dict.fromkeys(candidates))

    def _detail_result_from_url(
        self,
        url: str,
        source: str,
        name: str,
        cas: str,
        validation_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        raw_html = self._fetch(url)
        raw_text = self._html_to_text(raw_html)
        if not raw_text:
            return None

        relevance = self._result_relevance(
            raw_text,
            name=name,
            cas=cas,
            preferred_name="",
            validation_names=validation_names or [name],
        )
        if not relevance.get("passed"):
            return None

        result = self._result(
            name=name,
            cas=cas or self._extract_cas(raw_text),
            source=source,
            url=url,
            raw_text=raw_text,
        )
        result.update(relevance)
        return result

    def _fallback_web_research(
        self,
        reagent_name: str,
        cas: str,
        search_name: str,
        name_result: dict[str, Any],
        failed_queries: list[str],
        validation_names: list[str],
    ) -> dict[str, Any] | None:
        llm_candidates = LlmExtractor(settings=self.settings).generate_search_candidates(
            {
                "raw_name": reagent_name,
                "name": reagent_name,
                "cas": cas,
                "standard_name": name_result.get("standard_name", ""),
                "cleaned_name": name_result.get("cleaned_name", ""),
                "english_name": name_result.get("english_name", ""),
                "aliases": name_result.get("aliases", []),
                "concentration": name_result.get("concentration", ""),
            }
        )
        candidate_queries = self._dedupe_strings(
            [*failed_queries, *self._normalize_string_list(llm_candidates.get("candidates"))]
        )
        pages = WebResearcher(settings=self.settings, timeout_seconds=self.timeout_seconds).research(
            queries=candidate_queries,
            cas=cas,
            validation_names=validation_names,
            limit=6,
        )
        if not pages:
            return None

        best_page: ResearchPage | None = None
        best_relevance: dict[str, Any] = {}
        best_score = -1.0
        for page in pages:
            relevance = self._result_relevance(
                page.raw_text,
                name=search_name or reagent_name,
                cas=cas,
                preferred_name="",
                validation_names=validation_names,
            )
            evidence_weight = {"high": 0.2, "medium": 0.1, "low": 0.0}.get(page.evidence_quality, 0.0)
            score = (
                float(relevance.get("name_similarity", 0.0))
                + float(page.source_confidence)
                + evidence_weight
            )
            if relevance.get("passed") and score > best_score:
                best_page = page
                best_relevance = relevance
                best_score = score

        if not best_page:
            return None

        result = self._result(
            name=search_name or reagent_name,
            cas=cas or self._extract_cas(best_page.raw_text),
            source=best_page.source,
            url=best_page.url,
            raw_text=best_page.raw_text,
        )
        result.update(best_relevance)
        result["name_normalization"] = name_result
        result["query"] = best_page.search_query
        result["fallback_source"] = best_page.source
        result["fallback_url"] = best_page.url
        result["source_confidence"] = round(float(best_page.source_confidence), 3)
        result["evidence_quality"] = best_page.evidence_quality
        result["used_llm_search_candidates"] = bool(llm_candidates.get("used_llm"))
        result["llm_search_candidates"] = candidate_queries[:20]
        result["failure_reason"] = ""
        if best_page.source_confidence < 0.7 or best_page.evidence_quality == "low":
            result["need_manual_review"] = True
            result["failure_reason"] = (
                "Fallback research found a related page, but source confidence or evidence quality is low."
            )
        return result

    @staticmethod
    def _query_candidates(cas: str, standard_name: str, cleaned_name: str, english_name: str = "") -> list[str]:
        candidates: list[str] = []
        for value in (
            cas,
            standard_name if not cas else "",
            cleaned_name if not cas else "",
            english_name if not cas else "",
        ):
            value = str(value or "").strip()
            if value and value not in candidates:
                candidates.append(value)
        return candidates

    @staticmethod
    def _validation_names(
        query: str,
        standard_name: str,
        cleaned_name: str,
        english_name: str,
        aliases: list[str],
    ) -> list[str]:
        names: list[str] = []
        for value in (query, standard_name, cleaned_name, english_name, *aliases):
            value = str(value or "").strip()
            if value and value not in names:
                names.append(value)
        return names

    def _search_chemsrc(
        self,
        name: str,
        cas: str,
        query: str,
        validation_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        search_url = f"https://search.chemsrc.com/api/en/search?keyword={quote_plus(query)}"
        search_html = self._fetch(search_url)
        if not search_html:
            return None

        candidates = self._chemsrc_search_result_candidates(search_html, search_url)
        return self._best_detail_result(
            candidates=candidates,
            source="Chemsrc",
            name=name,
            cas=cas,
            validation_names=validation_names,
        )

    def _search_chemicalbook(
        self,
        name: str,
        cas: str,
        query: str,
        validation_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        search_url = f"https://www.chemicalbook.com/Search_EN.aspx?keyword={quote_plus(query)}"
        search_html = self._fetch(search_url)
        if not search_html:
            return None

        candidates = self._search_result_candidates(
            search_html,
            base_url=search_url,
            link_patterns=[
                r'href="([^"]*ChemicalProductProperty_EN_[^"]+\.htm)"',
                r'href="([^"]*/CASEN_[^"]+\.htm)"',
            ],
        )
        return self._best_detail_result(
            candidates=candidates,
            source="ChemicalBook",
            name=name,
            cas=cas,
            validation_names=validation_names,
        )

    def _fetch(self, url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                data = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return data.decode(charset, errors="ignore")
        except (HTTPError, URLError, TimeoutError, socket.timeout, OSError):
            return ""

    @classmethod
    def _chemsrc_search_result_candidates(cls, page_html: str, base_url: str, limit: int = 8) -> list[SearchCandidate]:
        candidates: list[SearchCandidate] = []
        rows = re.findall(r'(?is)<tr[^>]*class=["\'][^"\']*rowDat[^"\']*["\'][^>]*>(.*?)</tr>', page_html)
        for row in rows:
            baike_match = re.search(r'href=["\']([^"\']*/en/baike/\d+\.html)["\']', row, flags=re.I)
            if not baike_match:
                continue
            url = urljoin(base_url, html.unescape(baike_match.group(1)))
            title = cls._chemsrc_title_from_row(row, url)
            if url and not any(candidate.url == url for candidate in candidates):
                candidates.append(SearchCandidate(url=url, title=title))
            if len(candidates) >= limit:
                return candidates

        if candidates:
            return candidates

        return cls._search_result_candidates(
            page_html,
            base_url=base_url,
            link_patterns=[
                r'href="([^"]*/en/baike/\d+\.html)"',
                r'href="([^"]*/en/cas/[^"]+)"',
            ],
            limit=limit,
        )

    @classmethod
    def _chemsrc_title_from_row(cls, row_html: str, url: str) -> str:
        alt_match = re.search(r'alt=["\']([^"\']+?)\s+structure["\']', row_html, flags=re.I)
        if alt_match:
            return cls._html_to_text(alt_match.group(1))[:160]

        anchors = re.findall(r'(?is)<a[^>]*href=["\'][^"\']+["\'][^>]*>(.*?)</a>', row_html)
        for anchor in anchors:
            text = cls._html_to_text(anchor)
            if text and not cls._looks_like_cas(text) and "MSDS" not in text.upper():
                return text[:160]

        return url.rsplit("/", 1)[-1]

    @staticmethod
    def _first_matching_url(page_html: str, base_url: str, patterns: list[str]) -> str:
        candidates = ChemicalSearcher._search_result_candidates(page_html, base_url, patterns, limit=1)
        return candidates[0].url if candidates else ""

    @staticmethod
    def _matching_urls(page_html: str, base_url: str, patterns: list[str], limit: int = 8) -> list[str]:
        return [candidate.url for candidate in ChemicalSearcher._search_result_candidates(page_html, base_url, patterns, limit)]

    @staticmethod
    def _search_result_candidates(
        page_html: str,
        base_url: str,
        link_patterns: list[str],
        limit: int = 8,
    ) -> list[SearchCandidate]:
        candidates: list[SearchCandidate] = []
        for pattern in link_patterns:
            for match in re.finditer(pattern, page_html, flags=re.I):
                url = urljoin(base_url, html.unescape(match.group(1)))
                if any(candidate.url == url for candidate in candidates):
                    continue
                context_start = max(0, match.start() - 300)
                context_end = min(len(page_html), match.end() + 500)
                title = ChemicalSearcher._candidate_title_from_html(page_html[context_start:context_end], url)
                candidates.append(SearchCandidate(url=url, title=title))
                if len(candidates) >= limit:
                    return candidates
        return candidates

    @staticmethod
    def _candidate_title_from_html(fragment: str, url: str) -> str:
        title_patterns = [
            r'title=["\']([^"\']{2,160})["\']',
            r'<a[^>]*>(.*?)</a>',
            r'<h[1-4][^>]*>(.*?)</h[1-4]>',
        ]
        for pattern in title_patterns:
            match = re.search(pattern, fragment, flags=re.I | re.S)
            if match:
                text = ChemicalSearcher._html_to_text(match.group(1))
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    return text[:160]
        return url.rsplit("/", 1)[-1]

    def _best_detail_result(
        self,
        candidates: list[SearchCandidate] | list[str],
        source: str,
        name: str,
        cas: str,
        validation_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        normalized_candidates = [
            candidate if isinstance(candidate, SearchCandidate) else SearchCandidate(url=str(candidate), title="")
            for candidate in candidates
        ]
        names_for_validation = validation_names or [name]
        if not cas:
            normalized_candidates = self._rank_candidates_by_title(normalized_candidates, names_for_validation)

        best_result: dict[str, Any] | None = None
        best_score = -1.0
        for candidate in normalized_candidates:
            raw_html = self._fetch(candidate.url)
            raw_text = self._html_to_text(raw_html)
            if not raw_text:
                continue

            relevance = self._result_relevance(
                raw_text,
                name=name,
                cas=cas,
                preferred_name=candidate.title,
                validation_names=names_for_validation,
            )
            score = float(relevance.get("name_similarity", 0.0))
            if relevance.get("passed") and score > best_score:
                result = self._result(
                    name=name,
                    cas=cas or self._extract_cas(raw_text),
                    source=source,
                    url=candidate.url,
                    raw_text=raw_text,
                )
                result.update(relevance)
                best_result = result
                best_score = score

            if cas and relevance.get("relevance_passed"):
                return best_result

        return best_result

    def _rank_candidates_by_title(self, candidates: list[SearchCandidate], names: list[str]) -> list[SearchCandidate]:
        targets = [self._normalize_for_similarity(name) for name in names if name]
        return sorted(
            candidates,
            key=lambda candidate: max(
                [self._similarity(target, self._normalize_for_similarity(candidate.title)) for target in targets]
                or [0.0]
            ),
            reverse=True,
        )

    @staticmethod
    def _html_to_text(page_html: str) -> str:
        page_html = re.sub(r"(?is)<script.*?</script>", " ", page_html)
        page_html = re.sub(r"(?is)<style.*?</style>", " ", page_html)
        text = re.sub(r"(?s)<[^>]+>", " ", page_html)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _extract_cas(raw_text: str) -> str:
        match = re.search(r"\b\d{2,7}-\d{2}-\d\b", raw_text)
        return match.group(0) if match else ""

    def _result_relevance(
        self,
        raw_text: str,
        name: str,
        cas: str,
        preferred_name: str = "",
        validation_names: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_text = self._normalize_for_similarity(raw_text)
        if cas and cas.lower() in normalized_text:
            return {
                "relevance_passed": True,
                "matched_site_name": preferred_name,
                "name_similarity": 1.0,
                "passed": True,
            }

        names_for_validation = validation_names or [name]
        candidates = self._candidate_names(raw_text)
        if preferred_name:
            candidates.insert(0, preferred_name)

        best_name = ""
        best_score = 0.0
        for validation_name in names_for_validation:
            target = self._normalize_for_similarity(validation_name)
            if not target:
                continue
            for candidate in candidates:
                normalized_candidate = self._normalize_for_similarity(candidate)
                if not normalized_candidate or self._looks_like_cas(candidate):
                    continue
                score = self._similarity(target, normalized_candidate)
                if score > best_score:
                    best_name = candidate
                    best_score = score

            if len(target) >= 2 and target in normalized_text:
                best_name = best_name or validation_name
                best_score = max(best_score, 0.9)

        primary_names = self._primary_names(raw_text)
        primary_score = 0.0
        for validation_name in names_for_validation:
            target = self._normalize_for_similarity(validation_name)
            for primary_name in primary_names:
                score = self._similarity(target, self._normalize_for_similarity(primary_name))
                primary_score = max(primary_score, score)

        relevance_passed = best_score >= 0.82
        if primary_names and primary_score < 0.82:
            relevance_passed = False
        return {
            "relevance_passed": relevance_passed,
            "matched_site_name": best_name,
            "name_similarity": round(best_score, 3),
            "passed": relevance_passed,
        }

    @classmethod
    def _candidate_names(cls, raw_text: str) -> list[str]:
        candidates: list[str] = []
        patterns = [
            r"(?i)(?:product name|chemical name|english name|iupac name|synonyms?|name)\s*[:：]\s*([^|;,，。；\n]{2,120})",
            r"(?:中文名|中文名称|化学名称|英文名|英文名称|别名|同义词)\s*[:：]\s*([^|;,，。；\n]{2,120})",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, raw_text):
                value = re.sub(r"\s+", " ", match.group(1)).strip()
                if value and value not in candidates:
                    candidates.append(value)

        for chunk in re.split(r"[|;；。,\n]", raw_text[:800]):
            value = re.sub(r"\s+", " ", chunk).strip()
            if (
                2 <= len(value) <= 120
                and value not in candidates
                and not cls._looks_like_cas(value)
                and not re.search(r"https?://|copyright|login|search", value, flags=re.I)
            ):
                candidates.append(value)
        return candidates[:20]

    @staticmethod
    def _primary_names(raw_text: str) -> list[str]:
        names: list[str] = []
        patterns = [
            r"(?<!Product )Name:\s*(.{2,120}?)\s+(?:Chemical Name|CAS Number|Molecular Formula|Molecular Weight):",
            r"中文名[:：]\s*(.{2,120}?)\s+(?:英文名|CAS|分子式|分子量)[:：]",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, raw_text, flags=re.I):
                value = re.sub(r"\s+", " ", match.group(1)).strip()
                if value and value not in names and not ChemicalSearcher._looks_like_cas(value):
                    names.append(value)
        return names[:5]

    @staticmethod
    def _normalize_for_similarity(value: str) -> str:
        return re.sub(r"[\s,，.。;；:：()（）\[\]【】'\"\\\-_]+", "", (value or "").lower())

    @staticmethod
    def _looks_like_cas(value: str) -> bool:
        return bool(re.fullmatch(r"\s*\d{2,7}-\d{2}-\d\s*", value or ""))

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        if left in right or right in left:
            return min(1.0, min(len(left), len(right)) / max(len(left), len(right)) + 0.15)
        return SequenceMatcher(None, left, right).ratio()

    @staticmethod
    def _dedupe_strings(values: list[Any]) -> list[str]:
        output: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if text and text not in output:
                output.append(text)
        return output

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            values = re.split(r"[,;锛涳紱\n]+", value)
        elif isinstance(value, list):
            values = value
        else:
            values = [value]
        return [str(item).strip() for item in values if str(item).strip()]

    @staticmethod
    def _hazard_keywords(raw_text: str) -> list[str]:
        normalized = raw_text.lower()
        found = []
        for keyword in HAZARD_KEYWORDS:
            if keyword.lower() in normalized and keyword not in found:
                found.append(keyword)
        return found

    def _result(self, name: str, cas: str, source: str, url: str, raw_text: str) -> dict[str, Any]:
        return {
            "name": name,
            "cas": cas,
            "source": source,
            "url": url,
            "raw_text": raw_text[:8000],
            "hazard_keywords": self._hazard_keywords(raw_text),
            "need_manual_review": False,
            "name_normalization": {},
            "query": "",
            "matched_site_name": "",
            "name_similarity": 0.0,
            "relevance_passed": False,
            "source_confidence": self._source_confidence(source),
            "evidence_quality": "high" if source in {"Chemsrc", "ChemicalBook"} else "medium",
            "failure_reason": "",
            "fallback_source": "",
            "fallback_url": "",
            "used_llm_search_candidates": False,
            "llm_search_candidates": [],
        }

    @staticmethod
    def _source_confidence(source: str) -> float:
        if source == "Chemsrc":
            return 0.92
        if source == "ChemicalBook":
            return 0.86
        if source == "PubChem":
            return 0.9
        return 0.7 if source else 0.0

    @staticmethod
    def _manual_result(
        name: str,
        cas: str,
        reason: str,
        name_normalization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "cas": cas,
            "source": "",
            "url": "",
            "raw_text": reason,
            "hazard_keywords": [],
            "need_manual_review": True,
            "name_normalization": name_normalization or {},
            "query": "",
            "matched_site_name": "",
            "name_similarity": 0.0,
            "relevance_passed": False,
            "source_confidence": 0.0,
            "evidence_quality": "none",
            "failure_reason": reason,
            "fallback_source": "",
            "fallback_url": "",
            "used_llm_search_candidates": False,
            "llm_search_candidates": [],
        }


def search_chemical_info(
    reagent_name: str,
    cas: str = "",
    specification: str = "",
    unit: str = "",
    settings: dict[str, Any] | None = None,
    root_dir: Any | None = None,
) -> dict[str, Any]:
    return ChemicalSearcher(settings=settings, root_dir=root_dir).search(
        reagent_name=reagent_name,
        cas=cas,
        specification=specification,
        unit=unit,
    )
