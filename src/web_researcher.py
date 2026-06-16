from __future__ import annotations

import html
import json
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.request import Request, urlopen


TRUSTED_DOMAINS = {
    "pubchem.ncbi.nlm.nih.gov": 0.9,
    "www.sigmaaldrich.com": 0.82,
    "www.tcichemicals.com": 0.8,
    "www.aladdin-e.com": 0.78,
    "www.macklin.cn": 0.78,
    "www.thermofisher.com": 0.78,
    "www.fishersci.com": 0.76,
    "www.guidechem.com": 0.72,
    "www.chemicalbook.com": 0.72,
}


@dataclass(frozen=True)
class ResearchPage:
    source: str
    url: str
    raw_text: str
    source_confidence: float
    evidence_quality: str
    search_query: str
    queried_cas: str = ""


@dataclass
class WebResearcher:
    settings: dict[str, Any] | None = None
    timeout_seconds: int = 20

    def research(
        self,
        queries: list[str],
        cas: str = "",
        validation_names: list[str] | None = None,
        limit: int = 5,
    ) -> list[ResearchPage]:
        pages: list[ResearchPage] = []
        seen_urls: set[str] = set()
        clean_queries = self._dedupe([cas, *(queries or [])])

        for query in clean_queries:
            for page in self._pubchem_pages(query=query, cas=cas):
                if page.url not in seen_urls:
                    pages.append(page)
                    seen_urls.add(page.url)
                if len(pages) >= limit:
                    return pages

            for page in self._trusted_web_pages(query=query, cas=cas, validation_names=validation_names or []):
                if page.url not in seen_urls:
                    pages.append(page)
                    seen_urls.add(page.url)
                if len(pages) >= limit:
                    return pages

        return pages

    def _pubchem_pages(self, query: str, cas: str) -> list[ResearchPage]:
        if not query:
            return []
        cid_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{quote_plus(query)}/cids/TXT"
        cid_text = self._fetch(cid_url)
        cid_match = re.search(r"\b\d+\b", cid_text or "")
        if not cid_match:
            return []

        cid = cid_match.group(0)
        property_url = (
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
            f"{cid}/property/IUPACName,MolecularFormula,MolecularWeight,CanonicalSMILES/JSON"
        )
        property_text = self._fetch(property_url)
        view_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON"
        view_text = self._fetch(view_url)

        chunks = [f"PubChem CID: {cid}", property_text[:4000], self._pubchem_view_to_text(view_text)[:8000]]
        raw_text = " ".join(chunk for chunk in chunks if chunk).strip()
        if not raw_text:
            return []

        return [
            ResearchPage(
                source="PubChem",
                url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                raw_text=raw_text,
                source_confidence=0.9,
                evidence_quality=self._evidence_quality(raw_text, 0.9),
                search_query=query,
                queried_cas=cas,
            )
        ]

    def _trusted_web_pages(self, query: str, cas: str, validation_names: list[str]) -> list[ResearchPage]:
        if not query:
            return []
        search_terms = " ".join([query, "SDS MSDS flash point toxicity hazard"]).strip()
        search_url = f"https://duckduckgo.com/html/?q={quote_plus(search_terms)}"
        search_html = self._fetch(search_url)
        if not search_html:
            return []

        candidates = self._duckduckgo_result_urls(search_html, search_url)
        pages: list[ResearchPage] = []
        for url in candidates:
            confidence = self._domain_confidence(url)
            if confidence <= 0:
                continue
            raw_text = self._html_to_text(self._fetch(url))
            if not raw_text:
                continue
            if cas and cas not in raw_text:
                continue
            if not cas and validation_names and not self._contains_any_name(raw_text, validation_names):
                continue
            pages.append(
                ResearchPage(
                    source=self._source_name(url),
                    url=url,
                    raw_text=raw_text[:10000],
                    source_confidence=confidence,
                    evidence_quality=self._evidence_quality(raw_text, confidence),
                    search_query=query,
                    queried_cas=cas,
                )
            )
        return pages

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

    @staticmethod
    def _pubchem_view_to_text(raw_json: str) -> str:
        if not raw_json:
            return ""
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            return raw_json

        snippets: list[str] = []

        def walk(node: Any, heading: str = "") -> None:
            if isinstance(node, dict):
                current_heading = str(node.get("TOCHeading") or node.get("Name") or heading or "")
                for item in node.get("Information", []) or []:
                    values: list[str] = []
                    value = item.get("Value", {})
                    for key in ("StringWithMarkup", "Number", "String"):
                        raw_value = value.get(key)
                        if isinstance(raw_value, list):
                            values.extend(str(entry.get("String", entry)) if isinstance(entry, dict) else str(entry) for entry in raw_value)
                        elif raw_value:
                            values.append(str(raw_value))
                    if values:
                        snippets.append(f"{current_heading}: {'; '.join(values)}")
                for section in node.get("Section", []) or []:
                    walk(section, current_heading)
            elif isinstance(node, list):
                for child in node:
                    walk(child, heading)

        walk(data.get("Record", data))
        return " ".join(snippets)

    @staticmethod
    def _duckduckgo_result_urls(page_html: str, base_url: str, limit: int = 12) -> list[str]:
        urls: list[str] = []
        for match in re.finditer(r'href=["\']([^"\']+)["\'][^>]*class=["\']result__a', page_html, flags=re.I):
            url = html.unescape(match.group(1))
            url = urljoin(base_url, url)
            parsed = urlparse(url)
            if "duckduckgo.com" in parsed.netloc and "uddg=" in parsed.query:
                uddg_match = re.search(r"(?:^|&)uddg=([^&]+)", parsed.query)
                if uddg_match:
                    from urllib.parse import unquote

                    url = unquote(uddg_match.group(1))
            if url.startswith("http") and url not in urls:
                urls.append(url)
            if len(urls) >= limit:
                break
        return urls

    @staticmethod
    def _html_to_text(page_html: str) -> str:
        page_html = re.sub(r"(?is)<script.*?</script>", " ", page_html or "")
        page_html = re.sub(r"(?is)<style.*?</style>", " ", page_html)
        text = re.sub(r"(?s)<[^>]+>", " ", page_html)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _contains_any_name(raw_text: str, names: list[str]) -> bool:
        normalized = re.sub(r"\s+", "", raw_text.lower())
        return any(re.sub(r"\s+", "", name.lower()) in normalized for name in names if len(str(name).strip()) >= 3)

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        output: list[str] = []
        for value in values:
            text = str(value or "").strip()
            if text and text not in output:
                output.append(text)
        return output

    @staticmethod
    def _domain_confidence(url: str) -> float:
        hostname = urlparse(url).hostname or ""
        if hostname in TRUSTED_DOMAINS:
            return TRUSTED_DOMAINS[hostname]
        for domain, confidence in TRUSTED_DOMAINS.items():
            if hostname.endswith("." + domain):
                return confidence
        return 0.0

    @staticmethod
    def _source_name(url: str) -> str:
        hostname = urlparse(url).hostname or ""
        if "pubchem" in hostname:
            return "PubChem"
        if "sigmaaldrich" in hostname:
            return "Sigma-Aldrich"
        if "tcichemicals" in hostname:
            return "TCI"
        if "aladdin" in hostname:
            return "Aladdin"
        if "macklin" in hostname:
            return "Macklin"
        if "thermofisher" in hostname:
            return "Thermo Fisher"
        if "fishersci" in hostname:
            return "Fisher Scientific"
        if "guidechem" in hostname:
            return "GuideChem"
        if "chemicalbook" in hostname:
            return "ChemicalBook"
        return hostname

    @staticmethod
    def _evidence_quality(raw_text: str, source_confidence: float) -> str:
        text = (raw_text or "").lower()
        signal_count = sum(
            1
            for token in (
                "sds",
                "msds",
                "ghs",
                "hazard",
                "flash point",
                "boiling point",
                "ld50",
                "toxicity",
                "flammable",
                "corrosive",
            )
            if token in text
        )
        if source_confidence >= 0.85 and signal_count >= 2:
            return "high"
        if source_confidence >= 0.7 and signal_count >= 1:
            return "medium"
        return "low"
