"""Property and hazard evidence enrichment mixin for ChemicalSearcher."""
from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, quote_plus, urljoin, urlparse

from name_normalizer import NameNormalizer


class ChemicalEvidenceEnrichmentMixin:
    def _enrich_missing_official_fields(self, result: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(result)
        config = self._property_enrichment_config()
        evidence = self._decorate_property_evidence(list(enriched.get("evidence_items") or []))
        verified_cas = self._extract_cas(str(enriched.get("cas") or ""))
        if not config["enabled"] or not verified_cas:
            return enriched
        missing = self._missing_property_fields(evidence, config["fields"])
        if not missing:
            merged = self._merge_property_evidence(evidence)
            enriched["evidence_items"] = merged
            enriched["selected_property_evidence"] = {
                str(item.get("field") or ""): item
                for item in merged
                if item.get("selected") and str(item.get("field") or "") in config["fields"]
            }
            enriched["selected_toxicity_evidence"] = [
                item for item in merged if item.get("field") == "toxicity" and item.get("selected")
            ]
            enriched["property_enrichment"] = {
                "enabled": True,
                "requests": 0,
                "elapsed_ms": 0,
                "missing_fields": [],
                "diagnostics": [],
            }
            return enriched

        started = time.monotonic()
        state = {"requests": 0, "max_requests": config["max_requests"], "deadline": started + config["budget_seconds"]}
        diagnostics: list[dict[str, Any]] = []
        providers = list(config["providers"])
        source_key = str(enriched.get("source") or "").strip().lower()

        # PubChem's identity response contains Safety and Hazards; its physical
        # property view is a distinct API heading and must still be queried
        # before moving on to NIST.
        pubchem_cid = str(enriched.get("pubchem_cid") or "").strip()
        if source_key == "pubchem" and pubchem_cid and self._enrichment_request_allowed(state):
            items, diagnostic = self._pubchem_physical_evidence(pubchem_cid, state)
            evidence.extend(items)
            diagnostics.append(diagnostic)

        # First consume the verified source's same-record detail links.
        for url in list(dict.fromkeys(str(value).split("#", 1)[0] for value in enriched.get("property_detail_links") or [])):
            if not self._enrichment_request_allowed(state):
                break
            self._enrichment_request_started(state)
            items, diagnostic = self._linked_property_evidence(url=url, parent=enriched, verified_cas=verified_cas)
            evidence.extend(items)
            diagnostics.append(diagnostic)

        # Fill only still-missing fields, in the configured source order after
        # the identity source.  A source that already supplied a field prevents
        # later network calls for that field.
        start_index = providers.index(source_key) + 1 if source_key in providers else 0
        for provider_key in providers[start_index:]:
            missing = self._missing_property_fields(evidence, config["fields"])
            if not missing or not self._enrichment_request_allowed(state):
                break
            if provider_key == "nist":
                if "boiling_point" not in missing:
                    continue
                self._enrichment_request_started(state)
                items, diagnostic = self._nist_evidence(verified_cas)
                evidence.extend(items)
                diagnostics.append(diagnostic)
                continue
            provider = {
                "chemsrc": self._search_chemsrc,
                "chemicalbook": self._search_chemicalbook,
                "pubchem": self._search_pubchem,
            }.get(provider_key)
            if provider is None:
                continue
            self._enrichment_request_started(state)
            supplemental = self._run_provider(
                provider,
                name=verified_cas,
                cas=verified_cas,
                query=verified_cas,
                validation_names=[],
            )
            diagnostic = dict(getattr(self._thread_state, "last_provider_result", {}) or {})
            if diagnostic:
                diagnostic["provider"] = f"{diagnostic.get('provider')} property enrichment"
                diagnostics.append(diagnostic)
            if not supplemental:
                continue
            supplemental_items = self._decorate_property_evidence(list(supplemental.get("evidence_items") or []))
            evidence.extend(
                item for item in supplemental_items
                if str(item.get("field") or "") not in config["fields"]
                or str(item.get("field") or "") in missing
            )
            if supplemental.get("chemsrc_sections") and not enriched.get("chemsrc_sections"):
                enriched["chemsrc_sections"] = supplemental["chemsrc_sections"]
            for link in list(dict.fromkeys(str(value).split("#", 1)[0] for value in supplemental.get("property_detail_links") or [])):
                if not self._enrichment_request_allowed(state):
                    break
                self._enrichment_request_started(state)
                items, linked_diagnostic = self._linked_property_evidence(
                    url=link, parent=supplemental, verified_cas=verified_cas,
                )
                evidence.extend(
                    item for item in items
                    if str(item.get("field") or "") not in config["fields"]
                    or str(item.get("field") or "") in missing
                )
                diagnostics.append(linked_diagnostic)
            pubchem_cid = str(supplemental.get("pubchem_cid") or "").strip()
            if provider_key == "pubchem" and pubchem_cid and self._enrichment_request_allowed(state):
                items, physical_diagnostic = self._pubchem_physical_evidence(pubchem_cid, state)
                evidence.extend(
                    item for item in items
                    if str(item.get("field") or "") not in config["fields"]
                    or str(item.get("field") or "") in missing
                )
                diagnostics.append(physical_diagnostic)

        merged = self._merge_property_evidence(evidence)
        selected = {
            str(item.get("field") or ""): item
            for item in merged
            if item.get("selected") and str(item.get("field") or "") in config["fields"]
        }
        enriched["evidence_items"] = merged
        enriched["selected_property_evidence"] = selected
        enriched["selected_toxicity_evidence"] = [
            item for item in merged if item.get("field") == "toxicity" and item.get("selected")
        ]
        enriched["property_enrichment"] = {
            "enabled": True,
            "requests": state["requests"],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "missing_fields": sorted(self._missing_property_fields(merged, config["fields"])),
            "diagnostics": diagnostics,
        }
        enriched.setdefault("provider_results", []).extend(diagnostics)
        selected_text = self._selected_property_text(selected)
        if selected_text:
            enriched["raw_text"] = f"{str(enriched.get('raw_text') or '')}\n\nVerified property evidence:\n{selected_text}".strip()[:12000]
        print(
            "Property enrichment: "
            f"source={enriched.get('source') or 'none'} requests={state['requests']} "
            f"missing={','.join(enriched['property_enrichment']['missing_fields']) or 'none'}"
        )
        return enriched

    @staticmethod
    def _enrichment_request_allowed(state: dict[str, Any]) -> bool:
        return int(state["requests"]) < int(state["max_requests"]) and time.monotonic() < float(state["deadline"])

    @staticmethod
    def _enrichment_request_started(state: dict[str, Any]) -> None:
        state["requests"] = int(state["requests"]) + 1

    @staticmethod
    def _missing_property_fields(evidence: list[dict[str, Any]], expected: set[str]) -> set[str]:
        known = {
            str(item.get("field") or "") for item in evidence
            if str(item.get("status") or "") in {"measured", "present", "absent", "not_applicable"}
        }
        return expected - known

    @classmethod
    def _decorate_property_evidence(cls, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [cls._decorate_property_item(item) for item in evidence if isinstance(item, dict)]

    @classmethod
    def _decorate_property_item(cls, item: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(item)
        value = str(decorated.get("value") or "").strip()
        field_name = str(decorated.get("field") or "").strip()
        if not decorated.get("status"):
            lowered = value.lower()
            if re.search(r"\bnot\s+(?:applicable|available)\b|不适用|暂无数据|未提供", lowered):
                decorated["status"] = "not_applicable" if re.search(r"\bnot\s+applicable\b|不适用", lowered) else "unknown"
            elif field_name in {"flash_point", "boiling_point", "toxicity"}:
                decorated["status"] = "measured" if re.search(r"[-+]?\d", value) else "unknown"
            elif field_name in {"corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal"}:
                negative = re.search(r"\bnot\s+(?:a[n ]+)?(?:flammable|oxidizing|corrosive|explosive)\b|不易燃|无腐蚀|非氧化", lowered)
                decorated["status"] = "absent" if negative else "present" if value else "unknown"
            else:
                decorated["status"] = "measured" if value else "unknown"
        decorated.setdefault("retrieved_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        decorated.setdefault("source_type", "static_web")
        decorated.setdefault("source_updated_at", "")
        return decorated

    def _pubchem_physical_evidence(self, cid: str, state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        started = time.monotonic()
        self._enrichment_request_started(state)
        heading = quote("Chemical and Physical Properties", safe="")
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON?heading={heading}"
        data = self._fetch_json(url)
        items, _ = self._pubchem_evidence(data, cid, source_url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=Chemical-and-Physical-Properties")
        return self._decorate_property_evidence(items), {
            "provider": "PubChem property enrichment",
            "status": "success" if items else "not_found",
            "attempts": 1,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    def _linked_property_evidence(self, *, url: str, parent: dict[str, Any], verified_cas: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        started = time.monotonic()
        source = str(parent.get("source") or "").strip()
        if not self._is_allowed_property_link(url, str(parent.get("url") or ""), source):
            return [], {"provider": f"{source} property enrichment", "status": "skipped_untrusted_link", "attempts": 0, "elapsed_ms": 0}
        raw_html = self._fetch(url)
        raw_text = self._html_to_text(raw_html)
        if not raw_text or not self._property_page_identity_matches(raw_text, parent, verified_cas):
            return [], {"provider": f"{source} property enrichment", "status": "identity_rejected", "attempts": 1, "elapsed_ms": int((time.monotonic() - started) * 1000)}
        source_updated_at = self._page_updated_at(raw_text) or self._response_metadata_for(url).get("last_modified", "")
        items: list[dict[str, Any]] = []
        if source.lower() == "chemsrc":
            items, sections = self._chemsrc_full_page_evidence(
                raw_html,
                source_url=url.split("#", 1)[0],
                source_updated_at=source_updated_at,
            )
            if sections:
                parent["chemsrc_sections"] = sections
        if not items:
            items = self._chemsrc_chinese_property_evidence(
                raw_html,
                source_url=url,
                source_updated_at=source_updated_at,
            )
        if not items:
            items = self._property_evidence_from_text(raw_text, source=source, source_url=url, source_updated_at=source_updated_at)
        return items, {"provider": f"{source} property enrichment", "status": "success" if items else "not_found", "attempts": 1, "elapsed_ms": int((time.monotonic() - started) * 1000)}

    @classmethod
    def _chemsrc_full_page_evidence(
        cls,
        page_html: str,
        *,
        source_url: str,
        source_updated_at: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Parse the server-rendered Chemsrc detail document once.

        `#wuHuaDiv`, `#MSDSDiv`, and `#duXinDiv` are anchors in one HTML
        document.  The raw section text is retained for audit while selected
        rows are also emitted as normal field evidence.
        """
        parsed_url = urlparse(str(source_url or ""))
        if not (parsed_url.hostname and parsed_url.hostname.lower().endswith("chemsrc.com") and "/cas/" in parsed_url.path.lower()):
            return [], {}

        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        physical_html = cls._html_section_by_id(page_html, "wuHuaDiv")
        msds_html = cls._html_section_by_id(page_html, "MSDSDiv") or cls._html_section_by_id(page_html, "MSDSTbl")
        toxicity_html = cls._html_section_by_id(page_html, "duXinDiv") or cls._html_section_by_id(page_html, "duXinTbl")
        safety_html = cls._html_section_by_id(page_html, "anQuanDiv") or cls._section_containing_heading(page_html, ("安全信息", "安全"))

        physical_rows = cls._html_table_rows(physical_html)
        physical_text = cls._html_to_text(physical_html)
        msds_text = cls._html_to_text(msds_html)
        toxicity_text = cls._html_to_text(toxicity_html)
        safety_text = cls._html_to_text(safety_html)
        msds_sections = cls._split_msds_sections(msds_text)
        sections: dict[str, Any] = {
            "physicochemical": {"rows": physical_rows, "raw_text": physical_text},
            "msds": {"sections": msds_sections, "raw_text": msds_text},
            "toxicology_ecology": {"raw_text": toxicity_text},
            "safety": {"raw_text": safety_text},
            "source_url": source_url.split("#", 1)[0],
            "retrieved_at": retrieved_at,
        }

        items: list[dict[str, Any]] = []
        field_labels = {
            "闪点": "flash_point", "沸点": "boiling_point", "熔点": "melting_point",
            "密度": "density", "分子式": "molecular_formula", "分子量": "molecular_weight",
            "精确质量": "exact_mass", "蒸汽压": "vapor_pressure", "蒸气压": "vapor_pressure",
            "折射率": "refractive_index", "外观": "appearance", "外观性状": "appearance",
            "溶解性": "solubility", "稳定性": "stability", "储存条件": "storage_conditions",
            "logp": "logp", "psa": "psa", "inchikey": "inchikey", "smiles": "smiles",
        }

        def add(field_name: str, value: str, section: str, *, unit: str = "", extra: dict[str, Any] | None = None) -> None:
            value = re.sub(r"\s+", " ", str(value or "")).strip()
            if not value:
                return
            record: dict[str, Any] = {
                "field": field_name,
                "value": value[:1200],
                "raw_value": value[:1200],
                "normalized_value": value[:1200],
                "unit": unit,
                "source": "Chemsrc",
                "source_url": source_url.split("#", 1)[0],
                "source_section": section,
                "source_updated_at": source_updated_at,
                "source_type": "static_dom_page",
                "retrieved_at": retrieved_at,
            }
            if extra:
                record.update(extra)
            items.append(cls._decorate_property_item(record))

        for label, value in physical_rows.items():
            normalized_label = re.sub(r"[\s:：]", "", label).lower()
            field_name = next((field for token, field in field_labels.items() if token in normalized_label), "")
            if field_name:
                add(field_name, value, f"wuHuaDiv/{label}")

        # Section 9 may repeat physical values with different test conditions.
        section_nine = msds_sections.get("9", "")
        for label, field_name in (("闪点", "flash_point"), ("沸点", "boiling_point"), ("熔点", "melting_point")):
            for match in re.finditer(rf"{label}\s*[:：]?\s*([^；;。]{{1,100}})", section_nine):
                add(field_name, match.group(1), f"MSDS 第9部分/{label}")

        # Preserve all 16 MSDS sections as traceable evidence.  Only sections
        # relevant to rules are additionally mapped to typed fields.
        for number, text_value in msds_sections.items():
            add("msds_section", text_value, f"MSDS 第{number}部分", extra={"section_number": number})
        for number, field_name in (("5", "explosive_risk"), ("10", "reactivity"), ("11", "toxicity"), ("12", "ecotoxicity"), ("14", "transport_hazard")):
            if msds_sections.get(number):
                add(field_name, msds_sections[number], f"MSDS 第{number}部分")

        endpoints = cls._toxicity_endpoints(toxicity_text)
        sections["toxicology_ecology"]["endpoints"] = endpoints
        for endpoint in endpoints:
            add("toxicity", endpoint["raw"], "duXinDiv/毒理生态", extra=endpoint)

        safety_values = cls._safety_values(safety_text)
        sections["safety"].update(safety_values)
        for key, value in safety_values.items():
            if value:
                add("ghs_classification" if key not in {"un_number", "packing_group"} else "transport_hazard", str(value), f"安全/{key}")

        # JSON-LD is supplementary: it can add values but never suppresses DOM.
        items.extend(cls._chemsrc_chinese_property_evidence(
            page_html,
            source_url=source_url,
            source_updated_at=source_updated_at,
        ))
        present_fields = {str(item.get("field") or "") for item in items}
        for field_name in (
            "flash_point", "boiling_point", "toxicity", "ghs_classification",
            "corrosive", "oxidizing", "flammable", "water_reactive", "explosive_risk",
        ):
            if field_name in present_fields:
                continue
            items.append({
                "field": field_name,
                "value": "",
                "raw_value": "",
                "normalized_value": "",
                "unit": "",
                "status": "unknown",
                "source": "Chemsrc",
                "source_url": source_url.split("#", 1)[0],
                "source_section": "field_absent_from_page",
                "source_updated_at": source_updated_at,
                "source_type": "static_dom_page",
                "retrieved_at": retrieved_at,
            })
        return cls._merge_property_evidence(items), sections

    @staticmethod
    def _html_section_by_id(page_html: str, section_id: str) -> str:
        match = re.search(rf"(?is)<(?P<tag>[a-z0-9]+)[^>]*\bid=[\"']{re.escape(section_id)}[\"'][^>]*>", page_html or "")
        if not match:
            return ""
        start = match.start()
        # Chemsrc's top-level content blocks are sibling containers.  Stopping
        # at the next known id avoids swallowing navigation and product prose.
        if match.group("tag").lower() == "table":
            closing = re.search(r"(?is)</table\s*>", (page_html or "")[match.end():])
            end = match.end() + closing.end() if closing else len(page_html or "")
        else:
            next_match = re.search(
                r"(?is)<div[^>]*\bid=[\"'](?:wuHuaDiv|MSDSDiv|duXinDiv|anQuanDiv)[\"'][^>]*>",
                (page_html or "")[match.end():],
            )
            end = match.end() + next_match.start() if next_match else len(page_html or "")
        return (page_html or "")[start:end]

    @staticmethod
    def _section_containing_heading(page_html: str, headings: tuple[str, ...]) -> str:
        for heading in headings:
            hit = (page_html or "").find(heading)
            if hit >= 0:
                start = max(0, (page_html or "").rfind("<div", 0, hit))
                return (page_html or "")[start:start + 20000]
        return ""

    @classmethod
    def _html_table_rows(cls, section_html: str) -> dict[str, str]:
        rows: dict[str, str] = {}
        for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", section_html or ""):
            cells = [cls._html_to_text(cell) for cell in re.findall(r"(?is)<(?:th|td)[^>]*>(.*?)</(?:th|td)>", row)]
            cells = [cell for cell in cells if cell]
            if len(cells) >= 2:
                rows[cells[0].strip()] = " ".join(cells[1:]).strip()
        return rows

    @staticmethod
    def _split_msds_sections(text_value: str) -> dict[str, str]:
        numeral_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
                       "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15, "十六": 16}
        pattern = re.compile(r"第\s*([一二三四五六七八九十]{1,3}|\d{1,2})\s*部分\s*[:：]?", re.I)
        matches = list(pattern.finditer(text_value or ""))
        output: dict[str, str] = {}
        for index, match in enumerate(matches):
            token = match.group(1)
            number = int(token) if token.isdigit() else numeral_map.get(token)
            if not number or not 1 <= number <= 16:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text_value or "")
            output[str(number)] = re.sub(r"\s+", " ", (text_value or "")[match.end():end]).strip()
        return output

    @staticmethod
    def _toxicity_endpoints(text_value: str) -> list[dict[str, Any]]:
        endpoints: list[dict[str, Any]] = []
        pattern = re.compile(
            r"\b(LD50|LC50|EC50|IC50)\b\s*[:：]?\s*([<>≤≥~]?\s*\d+(?:\.\d+)?(?:\s*[~-]\s*\d+(?:\.\d+)?)?)\s*"
            r"(mg/kg|mg/L|g/L|g/kg|ppm|ppb)(?:\s*（([^）]{0,120})）|\s*\(([^)]{0,120})\))?",
            re.I,
        )
        for match in pattern.finditer(text_value or ""):
            context = (match.group(4) or match.group(5) or "").strip()
            route = next((value for token, value in (("口服", "oral"), ("经口", "oral"), ("吸入", "inhalation"), ("经皮", "dermal"), ("静脉", "intravenous")) if token in context), "unknown")
            duration = ""
            duration_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(h|hr|小时|d|天)\b", context, re.I)
            if duration_match:
                duration = "".join(duration_match.groups())
            endpoints.append({
                "endpoint": match.group(1).upper(),
                "route": route,
                "value_text": re.sub(r"\s+", "", match.group(2)),
                "unit_dimension": match.group(3).lower(),
                "exposure_duration": duration,
                "medium": context,
                "raw": re.sub(r"\s+", " ", match.group(0)).strip(),
            })
        return endpoints

    @staticmethod
    def _safety_values(text_value: str) -> dict[str, Any]:
        text_value = re.sub(r"\s+", " ", text_value or "").strip()
        h_statements = sorted(set(re.findall(r"\bH\d{3}[A-Za-z]?\b", text_value)))
        p_statements = sorted(set(re.findall(r"\bP\d{3}(?:\+P\d{3})*\b", text_value)))
        un_match = re.search(r"\bUN\s*(\d{4})\b", text_value, re.I)
        packing = re.search(r"(?:包装等级|Packing\s+Group)\s*[:：]?\s*([I]{1,3}|[123])\b", text_value, re.I)
        signal = next((word for word in ("危险", "警告", "Danger", "Warning") if word.lower() in text_value.lower()), "")
        return {
            "signal_word": signal,
            "hazard_statements": h_statements,
            "precautionary_statements": p_statements,
            "un_number": un_match.group(1) if un_match else "",
            "packing_group": packing.group(1) if packing else "",
        }

    @classmethod
    def _chemsrc_chinese_property_evidence(
        cls,
        page_html: str,
        *,
        source_url: str,
        source_updated_at: str = "",
    ) -> list[dict[str, Any]]:
        """Extract bounded values from Chemsrc's Chinese JSON-LD property block.

        The rendered page repeats navigation and product prose after each label;
        parsing its flattened text can turn a flash-point value into a paragraph.
        JSON-LD carries the same static values as name/value pairs.
        """
        parsed_url = urlparse(str(source_url or ""))
        if not (
            parsed_url.hostname
            and parsed_url.hostname.lower().endswith("chemsrc.com")
            and "/cas/" in parsed_url.path.lower()
            and not parsed_url.path.lower().startswith("/en/")
        ):
            return []

        fields = {
            "闪点": "flash_point",
            "沸点": "boiling_point",
            "急性毒性": "toxicity",
            "符号": "ghs_classification",
            "信号词": "ghs_classification",
            "危害声明": "ghs_classification",
        }
        records: list[dict[str, Any]] = []
        for raw_json in re.findall(
            r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            page_html or "",
        ):
            try:
                payload = json.loads(html.unescape(raw_json).strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            nodes = payload if isinstance(payload, list) else [payload]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                for property_value in node.get("additionalProperty") or []:
                    if not isinstance(property_value, dict):
                        continue
                    field_name = fields.get(str(property_value.get("name") or "").strip())
                    value = str(property_value.get("value") or "").strip()
                    if not field_name or not value:
                        continue
                    records.append(cls._decorate_property_item({
                        "field": field_name,
                        "value": re.sub(r"\s+", " ", cls._html_to_text(value)).strip()[:240],
                        "unit": "",
                        "source": "Chemsrc",
                        "source_url": source_url,
                        "source_section": "Chemsrc Chinese structured properties",
                        "source_updated_at": source_updated_at,
                        "source_type": "static_structured_page",
                        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }))
        return records

    @staticmethod
    def _selected_property_text(selected: dict[str, dict[str, Any]]) -> str:
        return "\n".join(
            f"{field}: {item.get('value') or item.get('status')}; source={item.get('source') or '-'}"
            for field, item in sorted(selected.items())
        )[:4000]

    @staticmethod
    def _is_allowed_property_link(url: str, parent_url: str, source: str) -> bool:
        candidate = urlparse(str(url or ""))
        parent = urlparse(str(parent_url or ""))
        allowed_domains = {
            "chemicalbook": "chemicalbook.com",
            "chemsrc": "chemsrc.com",
        }
        domain = allowed_domains.get(str(source or "").strip().lower())
        if not domain or candidate.scheme != "https" or not candidate.hostname:
            return False
        if not candidate.hostname.lower().endswith(domain):
            return False
        if parent.hostname and not parent.hostname.lower().endswith(domain):
            return False
        path = candidate.path.lower()
        return bool(path and not path.endswith((".pdf", ".zip", ".doc", ".docx")))

    def _property_page_identity_matches(self, raw_text: str, parent: dict[str, Any], verified_cas: str) -> bool:
        page_cas = self._extract_cas(raw_text)
        if page_cas and not self._same_cas(page_cas, verified_cas):
            return False
        validation_names = self._validation_names(
            str(parent.get("name") or ""),
            str(parent.get("matched_site_name") or ""),
            "",
            "",
            [],
        )
        relevance = self._result_relevance(
            raw_text,
            name=str(parent.get("name") or ""),
            cas=verified_cas,
            preferred_name=str(parent.get("matched_site_name") or ""),
            validation_names=validation_names,
        )
        # Ancillary pages frequently omit the name but are allowed only when
        # reached from the verified same-site detail page and contain no CAS conflict.
        return bool(relevance.get("passed") or not page_cas)

    @staticmethod
    def _page_updated_at(raw_text: str) -> str:
        match = re.search(
            r"(?:last\s+updated|updated(?:\s+on)?|更新时间|更新日期)\s*[:：]?\s*"
            r"(\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)",
            str(raw_text or ""),
            re.I,
        )
        if not match:
            return ""
        value = re.sub(r"年|月", "-", match.group(1)).replace("日", "")
        return value.replace("/", "-").replace(".", "-")

    @classmethod
    def _response_metadata_for(cls, url: str) -> dict[str, str]:
        prefix = f"{url}|"
        for key, value in cls._response_metadata.items():
            if key.startswith(prefix):
                return dict(value)
        return {}

    @classmethod
    def _property_evidence_from_text(
        cls,
        raw_text: str,
        *,
        source: str,
        source_url: str,
        source_updated_at: str = "",
    ) -> list[dict[str, Any]]:
        text = re.sub(r"\s+", " ", raw_text or " ").strip()
        labels = {
            "flash_point": ("flash point", "闪点"),
            "boiling_point": ("boiling point", "沸点"),
            "toxicity": ("ld50", "lc50", "toxicity", "急性毒性", "毒性"),
            "corrosive": ("corrosive", "corrosion", "腐蚀性", "皮肤腐蚀"),
            "oxidizing": ("oxidizing", "oxidizer", "氧化性", "氧化剂"),
            "flammable": ("flammable", "combustible", "易燃", "可燃"),
            "water_reactive": ("water reactive", "reacts with water", "遇水反应"),
            "explosive_risk": ("explosive", "explosion", "爆炸", "易爆"),
            "heavy_metal": ("heavy metal", "重金属"),
        }
        items: list[dict[str, Any]] = []
        for field_name, aliases in labels.items():
            pattern = "|".join(re.escape(alias) for alias in aliases)
            match = re.search(rf"(?i)(?:{pattern})\s*[:：]?\s*([^|;；。]{{1,300}})", text)
            if not match:
                continue
            value = match.group(1).strip()
            items.append(cls._decorate_property_item({
                "field": field_name,
                "value": value,
                "unit": "",
                "source": source,
                "source_url": source_url,
                "source_section": "static property or SDS page",
                "source_updated_at": source_updated_at,
                "source_type": "static_linked_page",
                "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }))
        return items

    @classmethod
    def _merge_property_evidence(cls, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in cls._decorate_property_evidence(evidence):
            field_name = str(item.get("field") or "").strip()
            if not field_name:
                continue
            grouped.setdefault(field_name, []).append(item)
        output: list[dict[str, Any]] = []
        for field_name, values in grouped.items():
            unique_values = {re.sub(r"\s+", " ", str(item.get("value") or "").strip().lower()) for item in values}
            conflict = len(unique_values - {""}) > 1
            if field_name == "flash_point":
                comparable: list[tuple[float, dict[str, Any]]] = []
                for item in values:
                    parsed = cls._temperature_lower_bound_c(str(item.get("value") or ""))
                    if parsed is not None:
                        number, comparator = parsed
                        item["normalized_value"] = number
                        item["unit"] = "°C"
                        item["comparison_operator"] = comparator
                        comparable.append((number, item))
                selected_item = min(comparable, key=lambda pair: pair[0])[1] if comparable else None
                ranked = sorted(values, key=cls._property_evidence_sort_key, reverse=True)
                for position, item in enumerate(ranked):
                    item["conflict"] = conflict
                    item["selected"] = item is selected_item if selected_item is not None else position == 0
                    item["selection_basis"] = "lowest_comparable_flash_point" if selected_item is not None else "latest_source_selected"
                    output.append(item)
                continue
            if field_name == "toxicity":
                cls._mark_toxicity_group_minima(values)
                ranked = sorted(values, key=cls._property_evidence_sort_key, reverse=True)
            else:
                ranked = sorted(values, key=cls._property_evidence_sort_key, reverse=True)
            for position, item in enumerate(ranked):
                item["conflict"] = conflict
                if field_name == "toxicity" and item.get("toxicity_group_comparable"):
                    item["selected"] = bool(item.get("group_selected"))
                else:
                    item["selected"] = position == 0
                if conflict:
                    item.setdefault("selection_basis", "latest_source_selected")
                output.append(item)
        return output

    @staticmethod
    def _temperature_lower_bound_c(value: str) -> tuple[float, str] | None:
        text_value = str(value or "").replace("−", "-").replace("–", "-")
        match = re.search(
            r"([<>≤≥~]?)\s*(-?\d+(?:\.\d+)?)"
            r"(?:\s*(?:~|-|至)\s*(-?\d+(?:\.\d+)?))?"
            r"(?:\s*(?:±|\+/-)\s*\d+(?:\.\d+)?)?\s*°?\s*([CFK])\b",
            text_value,
            re.I,
        )
        if not match:
            return None
        comparator = match.group(1) or "="
        values = [float(match.group(2))]
        if match.group(3) is not None:
            values.append(float(match.group(3)))
        number = min(values)
        unit = match.group(4).upper()
        if unit == "F":
            number = (number - 32.0) * 5.0 / 9.0
        elif unit == "K":
            number -= 273.15
        return round(number, 6), comparator

    @classmethod
    def _mark_toxicity_group_minima(cls, values: list[dict[str, Any]]) -> None:
        groups: dict[tuple[str, str, str, str, str], list[tuple[float, dict[str, Any]]]] = {}
        for item in values:
            endpoint = str(item.get("endpoint") or "").upper()
            route = str(item.get("route") or "unknown").lower()
            unit = str(item.get("unit_dimension") or item.get("unit") or "").lower().replace(" ", "")
            duration = str(item.get("exposure_duration") or "").lower()
            medium = str(item.get("medium") or "").lower()
            value_text = str(item.get("value_text") or item.get("value") or "")
            number_match = re.search(r"[-+]?\d+(?:\.\d+)?", value_text)
            if not endpoint or not unit or not number_match:
                continue
            number = float(number_match.group(0))
            # Convert only within dimensions that are mechanically compatible.
            if unit == "g/kg":
                number *= 1000.0
                unit = "mg/kg"
            elif unit == "g/l":
                number *= 1000.0
                unit = "mg/l"
            key = (endpoint, route, unit, duration, medium)
            item.update({
                "toxicity_group": "|".join(key),
                "toxicity_group_comparable": True,
                "normalized_value": number,
                "unit": unit,
            })
            groups.setdefault(key, []).append((number, item))
        for entries in groups.values():
            selected = min(entries, key=lambda pair: pair[0])[1]
            for _number, item in entries:
                item["group_selected"] = item is selected
                item["selection_basis"] = "lowest_value_within_same_toxicity_endpoint_route_unit_duration_medium"

    @staticmethod
    def _property_evidence_sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
        def timestamp(value: Any) -> float:
            raw = str(value or "").strip()
            if not raw:
                return 0.0
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                try:
                    return datetime.fromisoformat(f"{raw}T00:00:00+00:00").timestamp()
                except ValueError:
                    return 0.0
        return (
            timestamp(item.get("source_updated_at")),
            timestamp(item.get("http_last_modified")),
            str(item.get("retrieved_at") or ""),
        )

    def _comptox_evidence(self, cas: str, api_key: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
        started = time.monotonic()
        base_url = str(
            ((self.settings or {}).get("chemical_search", {}) or {}).get(
                "comptox_base_url", "https://api-ccte.epa.gov"
            )
        ).rstrip("/")
        search_url = f"{base_url}/chemical/search/equal/{quote(cas, safe='')}"
        payload = self._fetch_json_with_headers(search_url, {"x-api-key": api_key})
        records = payload if isinstance(payload, list) else payload.get("results", []) if isinstance(payload, dict) else []
        record = records[0] if records and isinstance(records[0], dict) else {}
        dtxsid = str(record.get("dtxsid") or record.get("dtxSid") or "").strip()
        detail = record
        detail_url = search_url
        if dtxsid:
            detail_url = f"{base_url}/chemical/detail/search/by-dtxsid/{quote(dtxsid, safe='')}"
            detail_payload = self._fetch_json_with_headers(detail_url, {"x-api-key": api_key})
            if isinstance(detail_payload, dict):
                detail = detail_payload
        items: list[dict[str, str]] = []
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for key, field_name in (
            ("toxicityValue", "toxicity"),
            ("hazard", "ghs_classification"),
            ("humanHealthHazard", "toxicity"),
        ):
            value = detail.get(key) if isinstance(detail, dict) else None
            if value not in (None, "", [], {}):
                items.append({
                    "field": field_name,
                    "value": json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value,
                    "unit": "",
                    "source": "EPA CompTox",
                    "source_url": detail_url,
                    "source_section": key,
                    "retrieved_at": retrieved_at,
                })
        status = "success" if items else ("unavailable" if self._failure_counts_toward_circuit(self._provider_failure_kind()) else "not_found")
        return items, {
            "provider": "EPA CompTox",
            "status": status,
            "attempts": int(getattr(self._thread_state, "fetch_attempts", 0) or 0),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    def _nist_evidence(self, cas: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
        started = time.monotonic()
        nist_id = re.sub(r"\D", "", cas)
        url = f"https://webbook.nist.gov/cgi/cbook.cgi?ID=C{nist_id}&Units=SI&Mask=4"
        page = self._html_to_text(self._fetch(url))
        items: list[dict[str, str]] = []
        match = re.search(
            r"(?:Tboil|Boiling point|Normal boiling point)\s*[:=]?\s*([<>≤≥~]?\s*[-+]?\d+(?:\.\d+)?\s*(?:K|°?C))",
            page,
            re.I,
        )
        if match:
            items.append({
                "field": "boiling_point",
                "value": re.sub(r"\s+", " ", match.group(1)).strip(),
                "unit": "",
                "source": "NIST",
                "source_url": url,
                "source_section": "Phase change data",
                "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
        status = "success" if items else ("unavailable" if self._failure_counts_toward_circuit(self._provider_failure_kind()) else "not_found")
        return items, {
            "provider": "NIST",
            "status": status,
            "attempts": int(getattr(self._thread_state, "fetch_attempts", 0) or 0),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    def _pubchem_cids(self, query: str) -> list[str]:
        query = str(query or "").strip()
        if not query:
            return []
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{quote(query, safe='')}/cids/JSON"
        data = self._fetch_json(url)
        identifiers = (data.get("IdentifierList") or {}).get("CID") or [] if data else []
        return [str(value) for value in identifiers[:5]]

    def _fetch_json(self, url: str) -> dict[str, Any]:
        raw = self._fetch(url)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            self._mark_provider_fetch_failure("invalid_json")
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _fetch_json_with_headers(self, url: str, headers: dict[str, str]) -> Any:
        raw = self._fetch(url, headers=headers)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self._mark_provider_fetch_failure("invalid_json")
            return {}

    def _pubchem_identity_issue(
        self,
        *,
        name: str,
        cas: str,
        query: str,
        status: str,
        reason: str,
        cid: str = "",
        matched_name: str = "",
    ) -> dict[str, Any]:
        url = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}" if cid else ""
        result = self._result(name=name, cas=cas, source="PubChem", url=url, raw_text=reason)
        result.update({
            "identity_status": status,
            "retrieval_status": "partial",
            "pubchem_cid": cid,
            "matched_site_name": matched_name,
            "relevance_passed": False,
            "need_manual_review": True,
            "failure_reason": reason,
            "evidence_items": [],
            "manual_search_urls": self._manual_search_urls(name, cas),
        })
        return result

    @classmethod
    def _pubchem_evidence(
        cls,
        data: dict[str, Any],
        cid: str,
        source_url: str | None = None,
    ) -> tuple[list[dict[str, str]], str]:
        items: list[dict[str, str]] = []
        snippets: list[str] = []
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        source_url = source_url or f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=Safety-and-Hazards"

        def values(node: Any) -> list[str]:
            output: list[str] = []
            if isinstance(node, dict):
                if "String" in node and isinstance(node.get("String"), str):
                    output.append(str(node["String"]))
                for key in ("StringWithMarkup", "Number", "Value"):
                    value = node.get(key)
                    if value is not None:
                        output.extend(values(value))
            elif isinstance(node, list):
                for child in node:
                    output.extend(values(child))
            elif isinstance(node, (str, int, float)):
                output.append(str(node))
            return output

        def walk(node: Any, heading: str = "") -> None:
            if len(items) >= 100:
                return
            if isinstance(node, dict):
                current = str(node.get("TOCHeading") or node.get("Name") or heading or "").strip()
                for information in node.get("Information", []) or []:
                    heading_text = str(information.get("Name") or current or "Safety and Hazards").strip()
                    for value in cls._dedupe_strings(values(information.get("Value") or {})):
                        cleaned = re.sub(r"\s+", " ", value).strip()
                        if not cleaned:
                            continue
                        lowered = heading_text.lower()
                        field_name = "hazard_information"
                        for token, field in (
                            ("flash point", "flash_point"),
                            ("boiling point", "boiling_point"),
                            ("tox", "toxicity"),
                            ("ghs", "ghs_classification"),
                            ("corros", "corrosive"),
                            ("oxid", "oxidizing"),
                            ("explos", "explosive_risk"),
                            ("flamm", "flammable"),
                        ):
                            if token in lowered:
                                field_name = field
                                break
                        items.append({
                            "field": field_name,
                            "value": cleaned[:600],
                            "unit": "",
                            "source": "PubChem",
                            "source_url": source_url,
                            "source_section": heading_text,
                            "retrieved_at": retrieved_at,
                        })
                        snippets.append(f"{heading_text}: {cleaned}")
                        if len(items) >= 100:
                            return
                for section in node.get("Section", []) or []:
                    walk(section, current)
            elif isinstance(node, list):
                for child in node:
                    walk(child, heading)

        walk((data or {}).get("Record") or {})
        return items, " ".join(snippets)[:10000]

    @classmethod
    def _best_name_match(cls, expected: list[str], candidates: list[str]) -> tuple[str, float]:
        best_name = ""
        best_score = 0.0
        for expected_name in expected:
            left = cls._normalize_for_similarity(expected_name)
            if not left:
                continue
            for candidate in candidates:
                right = cls._normalize_for_similarity(candidate)
                if not right or cls._looks_like_cas(candidate):
                    continue
                score = cls._similarity(left, right)
                if score > best_score:
                    best_name, best_score = candidate, score
        return best_name, best_score

