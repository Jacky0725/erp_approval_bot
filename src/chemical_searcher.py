from __future__ import annotations

import html
import copy
import hashlib
import json
import os
import random
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urljoin, urlparse
from urllib.request import Request, urlopen

from chemical_search_cache import ChemicalSearchCache
from enrichment_metrics import EnrichmentMetrics
from llm_extractor import LlmExtractor
from name_normalizer import NameNormalizer
from web_researcher import ResearchPage, WebResearcher


TRUSTED_NAME_VERIFICATION_SOURCES = {
    "PubChem", "EPA CompTox", "NIST", "Supplier SDS",
    # Historical/manual evidence remains readable, but these sources are no longer queried automatically.
    "Chemsrc", "ChemicalBook",
}

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
    metrics: EnrichmentMetrics | None = None
    _search_cache: dict[tuple[Any, ...], dict[str, Any]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _public_request_count: int = field(default=0, init=False, repr=False)
    _provider_metrics: dict[str, dict[str, float]] = field(default_factory=dict, init=False, repr=False)
    _source_semaphores: ClassVar[dict[str, threading.BoundedSemaphore]] = {}
    _source_failures: ClassVar[dict[str, int]] = {}
    _source_open_until: ClassVar[dict[str, float]] = {}
    _source_failure_reasons: ClassVar[dict[str, str]] = {}
    _source_skip_logged: ClassVar[set[str]] = set()
    _source_half_open: ClassVar[set[str]] = set()
    _thread_state: ClassVar[threading.local] = threading.local()
    _source_lock: ClassVar[threading.RLock] = threading.RLock()
    _host_last_request: ClassVar[dict[str, float]] = {}
    _response_cache: ClassVar[dict[str, str]] = {}
    _provider_negative_until: ClassVar[dict[tuple[str, str], float]] = {}

    @staticmethod
    def is_trusted_source(source: str) -> bool:
        return str(source or "").strip() in TRUSTED_NAME_VERIFICATION_SOURCES | {"local_memory"}

    def search(
        self,
        reagent_name: str,
        cas: str = "",
        specification: str = "",
        unit: str = "",
        manufacturer: str = "",
        catalog_number: str = "",
        sds_text: str = "",
        erp_is_mixture: bool = False,
    ) -> dict[str, Any]:
        cache_key = (
            *self._cache_key(reagent_name, cas, specification, unit),
            str(manufacturer or "").strip().lower(),
            str(catalog_number or "").strip().lower(),
            hashlib.sha256(str(sds_text or "").encode("utf-8")).hexdigest()[:16] if sds_text else "",
        )
        if cache_key in self._search_cache:
            self._record_cache_metric("memory", "hit")
            return copy.deepcopy(self._search_cache[cache_key])

        name = reagent_name.strip()
        normalizer = NameNormalizer(settings=self.settings, root_dir=self.root_dir)
        name_result = normalizer.normalize(raw_name=name, cas=cas, specification=specification, unit=unit)
        name_only_result = (
            normalizer.normalize(raw_name=name, cas="", specification=specification, unit=unit)
            if self._extract_cas(str(cas or ""))
            else name_result
        )

        input_cas = self._extract_cas(str(cas or ""))
        query_name_result = name_only_result if input_cas else name_result
        cas_no = input_cas or self._extract_cas(str(query_name_result.get("cas") or ""))
        standard_name = str(query_name_result.get("standard_name") or "").strip()
        cleaned_name = str(query_name_result.get("cleaned_name") or "").strip()
        english_name = str(query_name_result.get("english_name") or "").strip()
        aliases = [str(value).strip() for value in (query_name_result.get("aliases") or []) if str(value).strip()]
        name_result = query_name_result
        queries = self._query_candidates_compat(cas_no, standard_name, cleaned_name, english_name, aliases)
        mixture = self._mixture_metadata(name, cleaned_name, specification, name_result)
        mixture["manufacturer"] = str(manufacturer or "").strip()
        mixture["catalog_number"] = str(catalog_number or "").strip()
        mixture["is_mixture"] = bool(mixture["is_mixture"] or erp_is_mixture)

        if str(sds_text or "").strip():
            sds_result = self._supplier_sds_result(
                name=name,
                cas=cas_no,
                raw_text=str(sds_text),
                manufacturer=manufacturer,
                catalog_number=catalog_number,
                name_normalization=name_result,
            )
            sds_result.update({key: value for key, value in mixture.items() if key not in {"mixture_components"}})
            sds_result["is_mixture"] = bool(
                sds_result.get("is_mixture") or mixture.get("is_mixture") or len(sds_result.get("mixture_components") or []) > 1
            )
            return self._remember_result(cache_key, sds_result)

        validation_names = self._validation_names(standard_name or name, standard_name, cleaned_name, english_name, aliases)
        persistent_key = self._persistent_cache_key(
            source="chemical_search",
            normalized_name=cleaned_name or standard_name or english_name or name,
            cas=cas_no,
            search_mode="official_api_v2",
            concentration=str(mixture.get("concentration") or ""),
            manufacturer=str(mixture.get("manufacturer") or ""),
            catalog_number=str(mixture.get("catalog_number") or ""),
        )
        persistent_result = self._persistent_cache().get(persistent_key)
        if persistent_result is not None:
            self._record_cache_metric("persistent", "hit")
            persistent_result["retrieval_status"] = "cache"
            print("Chemical lookup result: source=cache retrieval=cache")
            return self._remember_result(cache_key, persistent_result)
        self._record_cache_metric("persistent", "miss")

        if not queries:
            return self._remember_result(cache_key, self._manual_result(
                name=name,
                cas=cas_no,
                reason="试剂名称和 CAS 号均为空，名称标准化后也无可查询名称。",
                name_normalization=name_result,
            ), persistent_key)

        search_name = cleaned_name or standard_name or english_name or name
        self._thread_state.lookup_deadline = time.monotonic() + self._lookup_budget_seconds()
        failed_queries: list[str] = []
        name_queries = [item for item in queries if item != cas_no][: self._max_name_queries()]
        cas_identity_result: dict[str, Any] | None = None
        def resolve_cas_identity() -> dict[str, Any] | None:
            nonlocal cas_identity_result
            if cas_identity_result is not None or not cas_no:
                return cas_identity_result
            cas_validation_names = self._validation_names(
                cas_no, standard_name, cleaned_name, english_name, aliases
            )
            for provider in self._provider_chain():
                candidate = self._run_provider(
                    provider,
                    name=cas_no,
                    cas=cas_no,
                    query=cas_no,
                    validation_names=cas_validation_names,
                )
                if candidate:
                    cas_identity_result = self._reconcile_provider_identity(
                        candidate,
                        input_cas=cas_no,
                        query=cas_no,
                        validation_names=cas_validation_names,
                    )
                    break
            return cas_identity_result

        execution_queries = name_queries
        for query in execution_queries:
            validation_names = self._validation_names(query, standard_name, cleaned_name, english_name, aliases)
            result: dict[str, Any] | None = None
            for provider in self._provider_chain():
                result = self._run_provider(
                    provider,
                    name=query,
                    cas="" if name_queries else cas_no,
                    query=query,
                    validation_names=validation_names,
                )
                if result:
                    break
            if result:
                resolve_cas_identity()
                result = self._reconcile_provider_identity(
                    result,
                    input_cas=input_cas,
                    query=query,
                    validation_names=validation_names,
                )
                name_candidate_cas = self._extract_cas(
                    str(result.get("candidate_cas") or result.get("cas") or "")
                )
                cas_candidate_cas = self._extract_cas(
                    str((cas_identity_result or {}).get("candidate_cas") or (cas_identity_result or {}).get("cas") or "")
                )
                if input_cas and name_candidate_cas:
                    if self._same_cas(input_cas, name_candidate_cas):
                        result["identity_status"] = "verified"
                        result["identity_decision_basis"] = "name_and_cas"
                    else:
                        trusted_name = self._trusted_name_verification(result)
                        result.update({
                            "identity_status": "conflict",
                            "identity_decision_basis": "name_identity",
                            "original_erp_cas": input_cas,
                            "cas_name_conflict": True,
                            "cas_correction_candidate": trusted_name,
                            "cas_correction_applied": trusted_name,
                            "need_manual_review": not trusted_name,
                        })
                        if trusted_name:
                            result["corrected_cas"] = name_candidate_cas
                        else:
                            result.pop("corrected_cas", None)
                elif input_cas and cas_candidate_cas and self._same_cas(input_cas, cas_candidate_cas):
                    result["cas"] = cas_candidate_cas
                    result["identity_status"] = "name_only"
                if result.get("identity_status") == "verified":
                    result = self._enrich_missing_official_fields(result)
                if input_cas and self._should_attempt_cas_correction(
                    result,
                    original_name=name,
                    input_cas=input_cas,
                    name_result=name_result,
                    name_only_result=name_only_result,
                ):
                    name_correction = self._search_by_name_for_corrected_cas(
                        original_name=name,
                        original_cas=input_cas,
                        specification=specification,
                        unit=unit,
                        name_result=name_only_result,
                        normalizer=normalizer,
                    )
                    if name_correction:
                        result = name_correction
                    corrected_cas = self._extract_cas(
                        str(result.get("corrected_cas") or name_only_result.get("cas") or result.get("cas") or "")
                    )
                    result["original_erp_cas"] = input_cas
                    result["cas_name_conflict"] = True
                    trusted_name = self._trusted_name_verification(result)
                    if trusted_name and corrected_cas:
                        result["corrected_cas"] = corrected_cas
                    else:
                        result.pop("corrected_cas", None)
                    result["cas_correction_candidate"] = bool(corrected_cas and trusted_name)
                    result["cas_correction_applied"] = bool(corrected_cas and trusted_name)
                    result["identity_decision_basis"] = "name_identity"
                    result["identity_status"] = "conflict"
                    result["need_manual_review"] = not trusted_name
                    result["cas_correction_reason"] = (
                        f"ERP CAS {input_cas} 与可信名称结果不一致；本次按名称身份作为物化特性判定依据，"
                        f"按名称身份将 CAS 修正为 {corrected_cas or '-'}。"
                    )
                    result["failure_reason"] = self._append_reason(
                        str(result.get("failure_reason") or ""),
                        result["cas_correction_reason"],
                    )
                result["name_normalization"] = name_result
                result["query"] = query
                result["query_plan"] = {
                    "cleaned_name": cleaned_name,
                    "candidate_names": name_queries,
                    "erp_cas": input_cas,
                    "queries_attempted": [*failed_queries, query],
                }
                result["name_identity"] = self._identity_snapshot(result, query_kind="name")
                result["cas_identity"] = self._identity_snapshot(cas_identity_result, query_kind="cas")
                result.update(mixture)
                if mixture["is_mixture"]:
                    result["need_manual_review"] = True
                    result["failure_reason"] = self._append_reason(
                        str(result.get("failure_reason") or ""),
                        "混合物或商品试剂需要使用已审核 SDS 及组分最高风险等级确认。",
                    )
                if result.get("identity_status") in {"ambiguous", "unresolved"}:
                    result["need_manual_review"] = True
                elif result.get("identity_status") == "conflict" and result.get("identity_decision_basis") == "name_identity":
                    result["need_manual_review"] = not self._trusted_name_verification(result)
                elif self._trusted_name_verification(result):
                    name_result = self._verified_name_normalization(
                        original_name=name,
                        specification=specification,
                        unit=unit,
                        name_result=name_result,
                        search_result=result,
                        normalizer=normalizer,
                    )
                    result["name_normalization"] = name_result
                    self._record_verified_alias_candidate(name_result, result, normalizer)
                print(
                    "Chemical lookup result: "
                    f"source={result.get('source') or 'none'} retrieval={result.get('retrieval_status') or 'fresh'} "
                    f"identity={result.get('identity_status') or 'unresolved'} mixture={str(mixture['is_mixture']).lower()}"
                )
                return self._remember_result(cache_key, result, persistent_key)
            failed_queries.append(query)

        resolve_cas_identity()
        if cas_identity_result and cas_identity_result.get("relevance_passed"):
            cas_identity_result = dict(cas_identity_result)
            cas_identity_result["identity_status"] = "cas_only"
            cas_identity_result["identity_decision_basis"] = "cas_identity"
            matched_identity_name = str(cas_identity_result.get("matched_site_name") or cas_identity_result.get("name") or "").strip()
            cas_name_validated = (
                float(cas_identity_result.get("name_similarity") or 0.0) >= 0.9
                and not self._extract_cas(matched_identity_name)
            )
            cas_identity_result["need_manual_review"] = not cas_name_validated
            if not cas_name_validated:
                cas_identity_result["failure_reason"] = self._append_reason(
                    str(cas_identity_result.get("failure_reason") or ""),
                    "CAS 可解析，但名称身份未能独立确认；按名称优先策略转人工复核。",
                )
            cas_identity_result["name_identity"] = {"query_kind": "name", "status": "unresolved"}
            cas_identity_result["cas_identity"] = self._identity_snapshot(cas_identity_result, query_kind="cas")
            cas_identity_result["name_normalization"] = name_result
            cas_identity_result["query_plan"] = {
                "cleaned_name": cleaned_name,
                "candidate_names": name_queries,
                "erp_cas": input_cas,
                "queries_attempted": [input_cas, *failed_queries],
            }
            cas_identity_result.update(mixture)
            return self._remember_result(cache_key, cas_identity_result, persistent_key)

        name_result = self._name_result_with_nonstandard_diagnostic(name_result, name=name, cas=cas_no)
        nonstandard_reason = str(name_result.get("suspected_invalid_reason") or "").strip()
        failure_kind = self._provider_failure_kind()
        unavailable = self._failure_counts_toward_circuit(failure_kind)
        if unavailable:
            search_settings = (self.settings or {}).get("chemical_search", {}) or {}
            try:
                stale_days = max(1, int(search_settings.get("stale_if_error_days", 180)))
            except (TypeError, ValueError):
                stale_days = 180
            stale_result = self._persistent_cache().get_stale(persistent_key, max_age_days=stale_days)
            if stale_result is not None:
                stale_result["query_plan"] = {
                    "cleaned_name": cleaned_name,
                    "candidate_names": [item for item in queries if item != cas_no][: self._max_name_queries()],
                    "erp_cas": input_cas,
                    "queries_attempted": failed_queries,
                }
                stale_result.update(mixture)
                print("Chemical search degraded: served stale verified cache after official source outage.")
                return self._remember_result(cache_key, stale_result)
        attempted = [*failed_queries, *([f"CAS:{cas_no}"] if cas_no else [])]
        reason = (
            f"官方化学数据源暂不可用。查询关键词: {', '.join(attempted)}"
            if unavailable
            else f"官方化学数据源未找到可信结果。查询关键词: {', '.join(attempted)}"
        )
        if nonstandard_reason:
            reason = f"{reason}；{nonstandard_reason}"

        manual = self._manual_result(
            name=search_name or name,
            cas=cas_no,
            reason=reason,
            name_normalization=name_result,
        )
        manual.update(mixture)
        manual["identity_status"] = "unresolved"
        manual["retrieval_status"] = "unavailable" if unavailable else "not_found"
        manual["query_plan"] = {
            "cleaned_name": cleaned_name,
            "candidate_names": [item for item in queries if item != cas_no][: self._max_name_queries()],
            "erp_cas": input_cas,
            "queries_attempted": attempted,
        }
        manual["manual_search_urls"] = self._manual_search_urls(search_name, cas_no)
        print(
            "Chemical lookup result: "
            f"source=none retrieval={manual['retrieval_status']} identity=unresolved mixture={str(mixture['is_mixture']).lower()}"
        )
        return self._remember_result(cache_key, manual, None if unavailable else persistent_key)

    @staticmethod
    def _identity_snapshot(result: dict[str, Any] | None, *, query_kind: str) -> dict[str, Any]:
        if not result:
            return {"query_kind": query_kind, "status": "unresolved"}
        return {
            "query_kind": query_kind,
            "name": result.get("matched_site_name") or result.get("name") or "",
            "cas": result.get("candidate_cas") or result.get("cas") or "",
            "source": result.get("source") or "",
            "url": result.get("url") or "",
            "status": result.get("identity_status") or "unresolved",
            "match_score": result.get("name_similarity") or result.get("source_confidence") or 0.0,
        }

    def _provider_chain(self) -> list[Any]:
        """Select the configured historical Chinese-name chain or PubChem fallback."""
        cls = type(self)
        legacy_overridden = (
            cls._search_chemsrc is not ChemicalSearcher._search_chemsrc
            or cls._search_chemicalbook is not ChemicalSearcher._search_chemicalbook
        )
        pubchem_overridden = cls._search_pubchem is not ChemicalSearcher._search_pubchem
        if legacy_overridden and not pubchem_overridden:
            return [self._search_chemsrc, self._search_chemicalbook]
        search_settings = (self.settings or {}).get("chemical_search", {}) or {}
        configured = search_settings.get("providers") or ["chemsrc", "chemicalbook", "pubchem", "nist"]
        provider_map = {
            "chemsrc": ("chemsrc_enabled", self._search_chemsrc),
            "chemicalbook": ("chemicalbook_enabled", self._search_chemicalbook),
            "pubchem": ("pubchem_enabled", self._search_pubchem),
            "nist": ("nist_enabled", self._search_nist),
        }
        providers: list[Any] = []
        for item in configured:
            key = str(item or "").strip().lower()
            if key not in provider_map:
                continue
            enabled_key, provider = provider_map[key]
            if bool(search_settings.get(enabled_key, key == "pubchem")):
                providers.append(provider)
        if providers:
            return providers
        return [self._search_pubchem]

    def _manual_verified_source_result(
        self,
        *,
        name: str,
        cas: str,
        name_result: dict[str, Any],
        validation_names: list[str],
    ) -> dict[str, Any] | None:
        source_url = str(name_result.get("source_url") or "").strip()
        if not source_url:
            return None
        try:
            raw_text = self._fetch(source_url)
        except Exception as error:  # noqa: BLE001 - surface configured URL problems without falling through silently.
            return self._manual_result(
                name=name,
                cas=cas,
                reason=f"人工确认 URL 读取失败：{error}",
                name_normalization=name_result,
            )
        url_cas = self._extract_cas(raw_text)
        if cas and url_cas and not self._same_cas(cas, url_cas):
            return self._manual_result(
                name=name,
                cas=cas,
                reason=f"人工确认 URL 返回 CAS {url_cas}，与本地标准 CAS {cas} 不一致。",
                name_normalization=name_result,
            )
        relevance = self._result_relevance(
            raw_text,
            name=str(name_result.get("standard_name") or name),
            cas=cas,
            preferred_name=str(name_result.get("standard_name") or ""),
            validation_names=validation_names,
        )
        if not relevance.get("passed"):
            return self._manual_result(
                name=name,
                cas=cas,
                reason="人工确认 URL 内容与当前试剂名称或 CAS 未通过一致性校验。",
                name_normalization=name_result,
            )
        result = self._result(
            name=str(name_result.get("standard_name") or name),
            cas=cas or url_cas,
            source="manual_verified_url",
            url=source_url,
            raw_text=raw_text,
        )
        result.update(relevance)
        result["name_normalization"] = name_result
        result["query"] = source_url
        result["identity_status"] = "verified" if (cas or url_cas) else "name_only"
        result["retrieval_status"] = "manual_verified_url"
        result["source_confidence"] = 0.92
        result["evidence_quality"] = "high"
        result["need_manual_review"] = False
        return result

    def search_many(self, reagents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Search unique top-level identities concurrently while preserving input order."""
        completed: dict[tuple[Any, ...], dict[str, Any]] = {}
        ordered_keys: list[tuple[Any, ...]] = []
        payloads: dict[tuple[Any, ...], dict[str, Any]] = {}
        started = time.monotonic()
        search_settings = (self.settings or {}).get("chemical_search", {}) or {}
        try:
            batch_budget = max(1.0, float(search_settings.get("batch_budget_seconds", 180)))
        except (TypeError, ValueError):
            batch_budget = 180.0
        self._prefetch_pubchem_batch(reagents, deadline=started + batch_budget)
        for item in reagents:
            reagent = item.get("reagent") if isinstance(item.get("reagent"), dict) else item
            name = str(reagent.get("reagent_name") or reagent.get("试剂名称") or reagent.get("name") or "").strip()
            cas = str(reagent.get("cas") or reagent.get("CAS号") or reagent.get("cas_no") or "").strip()
            specification = str(reagent.get("specification") or reagent.get("规格") or "")
            unit = str(reagent.get("unit") or reagent.get("规格单位") or "")
            manufacturer = str(reagent.get("manufacturer") or reagent.get("供应商") or reagent.get("生产厂家") or "")
            catalog_number = str(reagent.get("catalog_number") or reagent.get("货号") or "")
            sds_text = str(reagent.get("sds_text") or reagent.get("SDS文本") or "")
            erp_is_mixture = bool(reagent.get("is_mixture") or reagent.get("是否混合物"))
            key = (
                *self._cache_key(name, cas, specification, unit),
                manufacturer.strip().lower(),
                catalog_number.strip().lower(),
                hashlib.sha256(sds_text.encode("utf-8")).hexdigest()[:16] if sds_text else "",
            )
            ordered_keys.append(key)
            payloads.setdefault(key, {
                "reagent_name": name, "cas": cas, "specification": specification, "unit": unit,
                "manufacturer": manufacturer, "catalog_number": catalog_number,
                "sds_text": sds_text, "erp_is_mixture": erp_is_mixture,
            })
        try:
            workers = max(1, min(8, int(search_settings.get("batch_workers", 3))))
        except (TypeError, ValueError):
            workers = 3
        def run_one(payload: dict[str, Any]) -> dict[str, Any]:
            if time.monotonic() - started >= batch_budget:
                result = self._manual_result(
                    name=payload["reagent_name"], cas=self._extract_cas(payload["cas"]),
                    reason=f"批量查询达到 {batch_budget:g} 秒预算，剩余项目已转人工复核。",
                )
                result["retrieval_status"] = "unavailable"
                return result
            return self.search(**payload)
        with ThreadPoolExecutor(max_workers=min(workers, max(1, len(payloads))), thread_name_prefix="chemical-search") as pool:
            future_keys = {pool.submit(run_one, payload): key for key, payload in payloads.items()}
            for future in as_completed(future_keys):
                key = future_keys[future]
                try:
                    completed[key] = future.result()
                except Exception as error:
                    payload = payloads[key]
                    completed[key] = self._manual_result(
                        name=payload["reagent_name"], cas=self._extract_cas(payload["cas"]),
                        reason=f"并行查询失败，已安全转人工复核：{type(error).__name__}",
                    )
        for key, result in list(completed.items()):
            component_results: list[dict[str, Any]] = []
            for component in result.get("mixture_components") or []:
                component_cas = str(component.get("cas") or "").strip()
                component_name = str(component.get("name") or component_cas).strip()
                if not component_cas:
                    continue
                component_key = (*self._cache_key(component_name, component_cas), "", "", "")
                if component_key not in completed:
                    completed[component_key] = self.search(component_name, cas=component_cas)
                component_result = copy.deepcopy(completed[component_key])
                component_result["concentration"] = str(component.get("concentration") or "")
                component_results.append(component_result)
            if component_results:
                result["component_results"] = component_results
        results = [copy.deepcopy(completed[key]) for key in ordered_keys]
        provider_parts: list[str] = []
        for provider, metrics in sorted(self._provider_metrics.items()):
            calls = int(metrics.get("calls", 0))
            successes = int(metrics.get("successes", 0))
            average_ms = int(metrics.get("elapsed_ms", 0) / calls) if calls else 0
            provider_parts.append(f"{provider}:success={successes}/{calls},avg_ms={average_ms}")
        print(
            f"Chemical batch metrics: public_requests={self._public_request_count} "
            f"providers={';'.join(provider_parts) or 'none'}"
        )
        return results

    def _prefetch_pubchem_batch(self, reagents: list[dict[str, Any]], *, deadline: float) -> None:
        """Resolve identities once and warm per-CID responses from PUG REST batch calls."""
        cls = type(self)
        if (
            cls._search_pubchem is not ChemicalSearcher._search_pubchem
            or cls._search_chemsrc is not ChemicalSearcher._search_chemsrc
            or cls._search_chemicalbook is not ChemicalSearcher._search_chemicalbook
        ):
            return
        configured = ((self.settings or {}).get("chemical_search", {}) or {}).get("providers")
        provider_order = [
            str(value or "").strip().lower()
            for value in (configured if isinstance(configured, list) else ["pubchem"])
        ]
        search_settings = (self.settings or {}).get("chemical_search", {}) or {}
        prefetch_enabled = bool(search_settings.get("pubchem_batch_prefetch", provider_order[:1] == ["pubchem"]))
        if "pubchem" not in provider_order or not prefetch_enabled:
            return

        self._thread_state.lookup_deadline = deadline
        normalizer = NameNormalizer(settings=self.settings, root_dir=self.root_dir)
        cids: list[str] = []
        seen_identities: set[tuple[str, str]] = set()
        for item in reagents:
            if time.monotonic() >= deadline:
                break
            reagent = item.get("reagent") if isinstance(item.get("reagent"), dict) else item
            sds_text = str(reagent.get("sds_text") or reagent.get("SDS文本") or "").strip()
            if sds_text:
                continue
            name = str(reagent.get("reagent_name") or reagent.get("试剂名称") or reagent.get("name") or "").strip()
            cas = self._extract_cas(str(reagent.get("cas") or reagent.get("CAS号") or reagent.get("cas_no") or ""))
            specification = str(reagent.get("specification") or reagent.get("规格") or "")
            unit = str(reagent.get("unit") or reagent.get("规格单位") or "")
            normalized = normalizer.normalize(name, cas=cas, specification=specification, unit=unit)
            resolved_cas = cas or self._extract_cas(str(normalized.get("cas") or ""))
            persistent_key = self._persistent_cache_key(
                source="chemical_search",
                normalized_name=str(
                    normalized.get("cleaned_name")
                    or normalized.get("standard_name")
                    or normalized.get("english_name")
                    or name
                ),
                cas=resolved_cas,
                search_mode="official_api_v2",
                concentration=str(normalized.get("concentration") or ""),
                manufacturer=str(reagent.get("manufacturer") or reagent.get("供应商") or reagent.get("生产厂家") or ""),
                catalog_number=str(reagent.get("catalog_number") or reagent.get("货号") or ""),
            )
            if self._persistent_cache().get(persistent_key) is not None:
                continue
            query_candidates = self._query_candidates_compat(
                resolved_cas,
                str(normalized.get("standard_name") or ""),
                str(normalized.get("cleaned_name") or ""),
                str(normalized.get("english_name") or ""),
                [str(value) for value in normalized.get("aliases", []) or []],
            )
            query = next((value for value in query_candidates if value != resolved_cas), resolved_cas)
            identity_key = (query.lower(), resolved_cas.lower())
            if identity_key in seen_identities:
                continue
            seen_identities.add(identity_key)
            name_cids = [] if query == resolved_cas else self._pubchem_cids(query)
            cas_cids = self._pubchem_cids(resolved_cas) if resolved_cas else []
            common = [cid for cid in name_cids if cid in set(cas_cids)] if name_cids and cas_cids else []
            chosen = common[0] if common else (cas_cids[0] if cas_cids else name_cids[0] if name_cids else "")
            if chosen and chosen not in cids:
                cids.append(chosen)

        for offset in range(0, len(cids), 50):
            if time.monotonic() >= deadline:
                break
            chunk = cids[offset:offset + 50]
            cid_list = ",".join(chunk)
            property_batch_url = (
                "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
                f"{cid_list}/property/Title,IUPACName,MolecularFormula,MolecularWeight,CanonicalSMILES/JSON"
            )
            property_data = self._fetch_json(property_batch_url)
            for prop in ((property_data.get("PropertyTable") or {}).get("Properties") or []):
                cid = str(prop.get("CID") or "")
                if not cid:
                    continue
                individual_url = (
                    "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
                    f"{cid}/property/Title,IUPACName,MolecularFormula,MolecularWeight,CanonicalSMILES/JSON"
                )
                self._response_cache[f"{individual_url}|"] = json.dumps(
                    {"PropertyTable": {"Properties": [prop]}}, ensure_ascii=False
                )

            synonym_batch_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid_list}/synonyms/JSON"
            synonym_data = self._fetch_json(synonym_batch_url)
            for info in ((synonym_data.get("InformationList") or {}).get("Information") or []):
                cid = str(info.get("CID") or "")
                if not cid:
                    continue
                individual_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/synonyms/JSON"
                self._response_cache[f"{individual_url}|"] = json.dumps(
                    {"InformationList": {"Information": [info]}}, ensure_ascii=False
                )

    @staticmethod
    def _cache_key(
        reagent_name: str,
        cas: str = "",
        specification: str = "",
        unit: str = "",
    ) -> tuple[str, str, str, str]:
        return (
            str(reagent_name or "").strip().lower(),
            str(cas or "").strip().lower(),
            str(specification or "").strip().lower(),
            str(unit or "").strip().lower(),
        )

    def _remember_result(
        self,
        cache_key: tuple[Any, ...],
        result: dict[str, Any],
        persistent_key: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self._search_cache[cache_key] = copy.deepcopy(result)
        if persistent_key is not None:
            self._persistent_cache().put(persistent_key, result)
        return copy.deepcopy(result)

    def _persistent_cache(self) -> ChemicalSearchCache:
        root = self.root_dir if self.root_dir is not None else "."
        return ChemicalSearchCache(root_dir=Path(root), settings=self.settings)

    def _persistent_cache_key(
        self,
        *,
        source: str,
        normalized_name: str,
        cas: str,
        search_mode: str,
        concentration: str = "",
        manufacturer: str = "",
        catalog_number: str = "",
    ) -> dict[str, str]:
        return {
            "source": source,
            "normalized_name": str(normalized_name or "").strip().lower(),
            "cas": str(cas or "").strip().lower(),
            "concentration": str(concentration or "").strip().lower(),
            "manufacturer": str(manufacturer or "").strip().lower(),
            "catalog_number": str(catalog_number or "").strip().lower(),
            "search_mode": search_mode,
            "parser_version": "official-v2-sds-v1",
            "settings_version": ChemicalSearchCache.settings_version(self.settings),
        }

    def _search_by_name_for_corrected_cas(
        self,
        *,
        original_name: str,
        original_cas: str,
        specification: str,
        unit: str,
        name_result: dict[str, Any],
        normalizer: NameNormalizer,
    ) -> dict[str, Any] | None:
        standard_name = str(name_result.get("standard_name") or "").strip()
        cleaned_name = str(name_result.get("cleaned_name") or "").strip()
        english_name = str(name_result.get("english_name") or "").strip()
        aliases = [str(value).strip() for value in (name_result.get("aliases") or []) if str(value).strip()]
        name_based_cas = self._extract_cas(str(name_result.get("cas") or ""))
        queries = self._query_candidates_compat(name_based_cas, standard_name, cleaned_name or original_name, english_name)
        if original_name and original_name not in queries:
            queries.append(original_name)
        for query in queries:
            validation_names = self._validation_names(query, standard_name, cleaned_name, english_name, aliases)
            for provider in (self._search_chemsrc, self._search_chemicalbook):
                result = self._run_provider(provider, name=query, cas="", query=query, validation_names=validation_names)
                if not result or not self._trusted_name_verification(result):
                    continue
                corrected_cas = self._extract_cas(str(result.get("cas") or ""))
                if not corrected_cas:
                    corrected_cas = self._extract_cas(str(result.get("raw_text") or ""))
                if not corrected_cas or self._same_cas(corrected_cas, original_cas):
                    continue
                corrected_name_result = self._verified_name_normalization(
                    original_name=original_name,
                    specification=specification,
                    unit=unit,
                    name_result=name_result,
                    search_result={**result, "cas": corrected_cas},
                    normalizer=normalizer,
                )
                corrected_name_result["cas"] = corrected_cas
                corrected_name_result["original_erp_cas"] = original_cas
                corrected_name_result["corrected_cas"] = corrected_cas
                corrected_name_result["cas_name_conflict"] = True
                corrected_name_result["cas_correction_candidate"] = True
                corrected_name_result["identity_decision_basis"] = "name_identity"
                corrected_name_result["cas_correction_applied"] = False
                corrected_name_result["cas_correction_reason"] = (
                    f"ERP CAS {original_cas} conflicts with reagent name; trusted name evidence suggests "
                    f"CAS {corrected_cas}. Human confirmation is required before memory promotion."
                )
                corrected_name_result["cas_correction_source"] = result.get("source", "")
                corrected_name_result["cas_correction_url"] = result.get("url", "")

                result = dict(result)
                result["cas"] = corrected_cas
                result["name_normalization"] = corrected_name_result
                result["query"] = query
                result["original_erp_cas"] = original_cas
                result["corrected_cas"] = corrected_cas
                result["cas_name_conflict"] = True
                result["cas_correction_candidate"] = True
                result["identity_decision_basis"] = "name_identity"
                result["cas_correction_applied"] = False
                result["cas_correction_reason"] = corrected_name_result["cas_correction_reason"]
                result["cas_correction_source"] = result.get("source", "")
                result["cas_correction_url"] = result.get("url", "")
                self._record_verified_alias_candidate(corrected_name_result, result, normalizer)
                return result
        return None

    def _cas_name_conflict(
        self,
        search_result: dict[str, Any],
        original_name: str,
        name_result: dict[str, Any],
    ) -> bool:
        if not search_result.get("relevance_passed", False):
            return False
        if self._non_informative_name_text(
            original_name,
            name_result.get("raw_name", ""),
            name_result.get("cleaned_name", ""),
            name_result.get("standard_name", ""),
        ):
            return False
        site_names = [
            self._site_verified_name(search_result),
            str(search_result.get("matched_site_name") or "").strip(),
            *self._primary_names(str(search_result.get("raw_text") or "")),
        ]
        site_names = [self._clean_site_name(value) for value in site_names if self._clean_site_name(value)]
        if not site_names:
            return False
        validation_names = self._validation_names(
            original_name,
            str(name_result.get("standard_name") or ""),
            str(name_result.get("cleaned_name") or ""),
            str(name_result.get("english_name") or ""),
            [str(value).strip() for value in (name_result.get("aliases") or []) if str(value).strip()],
        )
        best = 0.0
        for validation_name in validation_names:
            target = self._normalize_for_similarity(validation_name)
            if not target:
                continue
            for site_name in site_names:
                best = max(best, self._similarity(target, self._normalize_for_similarity(site_name)))
        return best < 0.82

    def _should_attempt_cas_correction(
        self,
        search_result: dict[str, Any],
        *,
        original_name: str,
        input_cas: str,
        name_result: dict[str, Any],
        name_only_result: dict[str, Any],
    ) -> bool:
        if not input_cas:
            return False
        if self._non_informative_name_text(
            original_name,
            name_result.get("raw_name", ""),
            name_result.get("cleaned_name", ""),
            name_result.get("standard_name", ""),
        ):
            return False
        name_only_cas = self._extract_cas(str(name_only_result.get("cas") or ""))
        if not name_only_cas or self._same_cas(name_only_cas, input_cas):
            return False
        if self._normalize_confidence(name_only_result.get("confidence")) < 0.75:
            return False
        return self._cas_name_conflict(search_result, original_name, name_only_result)

    @staticmethod
    def _same_cas(left: str, right: str) -> bool:
        return str(left or "").strip().lower() == str(right or "").strip().lower()

    def _verified_name_normalization(
        self,
        *,
        original_name: str,
        specification: str,
        unit: str,
        name_result: dict[str, Any],
        search_result: dict[str, Any],
        normalizer: NameNormalizer,
    ) -> dict[str, Any]:
        current = dict(name_result or {})
        if not self._trusted_name_verification(search_result):
            return current

        source = str(search_result.get("source") or "").strip()
        source_confidence = self._normalize_confidence(search_result.get("source_confidence"))
        verified_cas = self._extract_cas(str(search_result.get("cas") or ""))
        if not verified_cas:
            verified_cas = self._extract_cas(str(search_result.get("raw_text") or ""))
        site_name = self._site_verified_name(search_result)
        current_confidence = self._normalize_confidence(current.get("confidence"))
        current_has_verified_cas = bool(self._extract_cas(str(current.get("cas") or "")))
        current_is_invalid_name = bool(current.get("suspected_invalid_name")) or bool(
            self._non_informative_name_text(
                current.get("raw_name", ""),
                current.get("cleaned_name", ""),
                current.get("standard_name", ""),
            )
        )
        if (
            current_confidence >= 0.8
            and not current.get("need_manual_review", True)
            and (not verified_cas or current_has_verified_cas)
            and not current_is_invalid_name
        ):
            return current

        if verified_cas:
            cas_result = normalizer.normalize(
                raw_name=original_name,
                cas=verified_cas,
                specification=specification,
                unit=unit,
            )
            cas_standard = str(cas_result.get("standard_name") or "").strip()
            cas_result_invalid = bool(cas_result.get("suspected_invalid_name")) or self._non_informative_name_text(
                cas_result.get("raw_name", ""),
                cas_result.get("cleaned_name", ""),
                cas_standard,
            )
            if cas_standard and not cas_result.get("need_manual_review", True) and not cas_result_invalid:
                upgraded = dict(cas_result)
                upgraded["confidence"] = max(
                    self._normalize_confidence(upgraded.get("confidence")),
                    source_confidence,
                    current_confidence,
                )
                upgraded["need_manual_review"] = False
                upgraded["reason"] = self._append_reason(
                    str(upgraded.get("reason") or current.get("reason") or ""),
                    f"Verified by {source} result; CAS-based local standard name was confirmed by web evidence.",
                )
                upgraded["web_verified_alias"] = True
                upgraded["web_verification_source"] = source
                upgraded["web_verification_url"] = str(search_result.get("url") or "")
                return upgraded

        if verified_cas and site_name:
            upgraded = dict(current)
            upgraded["standard_name"] = site_name
            upgraded["english_name"] = site_name if self._looks_english_name(site_name) else str(upgraded.get("english_name") or "")
            upgraded["cas"] = verified_cas
            upgraded["confidence"] = max(current_confidence, source_confidence, 0.9)
            upgraded["need_manual_review"] = False
            upgraded["reason"] = self._append_reason(
                str(upgraded.get("reason") or ""),
                f"CAS {verified_cas} was verified by {source}; standard name was aligned to the trusted website name.",
            )
            upgraded["web_verified_alias"] = True
            upgraded["web_verification_source"] = source
            upgraded["web_verification_url"] = str(search_result.get("url") or "")
            return upgraded

        if (
            current_confidence >= 0.8
            and not current.get("need_manual_review", True)
            and (not verified_cas or bool(self._extract_cas(str(current.get("cas") or ""))))
        ):
            return current
        if verified_cas:
            cas_result = normalizer.normalize(
                raw_name=original_name,
                cas=verified_cas,
                specification=specification,
                unit=unit,
            )
            if not cas_result.get("need_manual_review", True):
                upgraded = dict(cas_result)
                upgraded["confidence"] = max(
                    self._normalize_confidence(upgraded.get("confidence")),
                    source_confidence,
                    current_confidence,
                )
                upgraded["need_manual_review"] = False
                upgraded["reason"] = self._append_reason(
                    str(upgraded.get("reason") or current.get("reason") or ""),
                    f"Verified by {source} result; upgraded name normalization from web evidence.",
                )
                upgraded["web_verified_alias"] = True
                upgraded["web_verification_source"] = source
                upgraded["web_verification_url"] = str(search_result.get("url") or "")
                return upgraded

        name_similarity = self._normalize_confidence(search_result.get("name_similarity"))
        if name_similarity >= 0.9:
            upgraded = dict(current)
            upgraded["confidence"] = max(current_confidence, min(source_confidence, name_similarity))
            upgraded["need_manual_review"] = self._normalize_confidence(upgraded.get("confidence")) < 0.8
            upgraded["reason"] = self._append_reason(
                str(upgraded.get("reason") or ""),
                f"Verified by {source} name match; confidence upgraded from web evidence.",
            )
            upgraded["web_verified_alias"] = True
            upgraded["web_verification_source"] = source
            upgraded["web_verification_url"] = str(search_result.get("url") or "")
            return upgraded

        return current

    def _trusted_name_verification(self, search_result: dict[str, Any]) -> bool:
        source = str(search_result.get("source") or "").strip()
        return bool(
            source in TRUSTED_NAME_VERIFICATION_SOURCES
            and not search_result.get("need_manual_review", True)
            and search_result.get("relevance_passed", False)
            and self._normalize_confidence(search_result.get("source_confidence")) >= 0.86
        )

    @classmethod
    def _site_verified_name(cls, search_result: dict[str, Any]) -> str:
        candidates = [
            str(search_result.get("matched_site_name") or "").strip(),
            *cls._primary_names(str(search_result.get("raw_text") or "")),
        ]
        for candidate in candidates:
            name = cls._clean_site_name(candidate)
            if name:
                return name
        return ""

    @staticmethod
    def _clean_site_name(value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" -|,;，；")
        if not text or ChemicalSearcher._looks_like_cas(text):
            return ""
        reject_fragments = (
            "cas#",
            "cas no",
            "cas number",
            "search",
            "login",
            "copyright",
            "molecular formula",
            "molecular weight",
        )
        lowered = text.lower()
        if any(fragment in lowered for fragment in reject_fragments):
            return ""
        if len(text) > 120:
            text = text[:120].strip()
        return text

    @staticmethod
    def _looks_english_name(value: str) -> bool:
        return bool(re.search(r"[A-Za-z]", value or "")) and not re.search(r"[\u4e00-\u9fff]", value or "")

    @staticmethod
    def _non_informative_name_text(*values: Any) -> bool:
        text = re.sub(r"\s+", "", " ".join(str(value or "") for value in values)).lower()
        return any(
            token in text
            for token in (
                "没写",
                "未写",
                "未填写",
                "未填",
                "空白",
                "无名称",
            )
        ) or bool(re.fullmatch(r"[\?\ufffd�]+", text))

    def _record_verified_alias_candidate(
        self,
        name_result: dict[str, Any],
        search_result: dict[str, Any],
        normalizer: NameNormalizer,
    ) -> None:
        if not name_result.get("web_verified_alias"):
            return
        alias = str(name_result.get("cleaned_name") or name_result.get("raw_name") or "").strip()
        standard_name = str(name_result.get("standard_name") or "").strip()
        cas = self._extract_cas(str(name_result.get("cas") or search_result.get("cas") or ""))
        if not alias or not standard_name or alias == standard_name:
            return
        if self._alias_already_configured(alias, str(name_result.get("raw_name") or ""), normalizer):
            return

        try:
            import pandas as pd
        except ImportError:
            return

        columns = [
            "timestamp",
            "alias",
            "standard_name",
            "cas",
            "source",
            "source_url",
            "confidence",
            "evidence",
            "status",
            "reviewer",
            "reviewed_at",
        ]
        path = self._name_alias_candidates_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            candidates = pd.read_excel(path, dtype=str).fillna("")
        else:
            candidates = pd.DataFrame(columns=columns)
        candidates = candidates.reindex(columns=columns).fillna("")
        duplicate = (
            (candidates["alias"].astype(str).str.strip() == alias)
            & (candidates["standard_name"].astype(str).str.strip() == standard_name)
            & (candidates["cas"].astype(str).str.strip() == cas)
            & (candidates["status"].astype(str).str.strip().str.lower().isin({"pending", ""}))
        )
        if not candidates.empty and bool(duplicate.any()):
            return

        row = {
            "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
            "alias": alias,
            "standard_name": standard_name,
            "cas": cas,
            "source": str(search_result.get("source") or ""),
            "source_url": str(search_result.get("url") or search_result.get("fallback_url") or ""),
            "confidence": str(name_result.get("confidence") or ""),
            "evidence": str(search_result.get("matched_site_name") or search_result.get("query") or ""),
            "status": "pending",
            "reviewer": "",
            "reviewed_at": "",
        }
        candidates = pd.concat([candidates, pd.DataFrame([row])], ignore_index=True)
        candidates.reindex(columns=columns).to_excel(path, index=False)

    def _name_alias_candidates_path(self) -> Path:
        root = Path(self.root_dir) if self.root_dir is not None else Path(".")
        paths = (self.settings or {}).get("paths", {}) or {}
        return root / paths.get("name_alias_candidates_excel", "config/name_alias_candidates.xlsx")

    @staticmethod
    def _alias_already_configured(alias: str, raw_name: str, normalizer: NameNormalizer) -> bool:
        if normalizer._lookup_alias(alias, raw_name):
            return True
        alias_key = normalizer._alias_key(alias)
        for cas_info in (normalizer.alias_data.get("cas") or {}).values():
            if not isinstance(cas_info, dict):
                continue
            for configured_alias in normalizer._string_list(cas_info.get("aliases")):
                if normalizer._alias_key(configured_alias) == alias_key:
                    return True
        return False

    @staticmethod
    def _append_reason(reason: str, addition: str) -> str:
        reason = str(reason or "").strip()
        addition = str(addition or "").strip()
        if not reason:
            return addition
        if addition in reason:
            return reason
        return f"{reason} {addition}"

    def _run_provider(
        self,
        provider: Any,
        *,
        name: str,
        cas: str,
        query: str,
        validation_names: list[str] | None,
    ) -> dict[str, Any] | None:
        source = {
            "_search_pubchem": "PubChem",
            "_search_chemsrc": "Chemsrc",
            "_search_chemicalbook": "ChemicalBook",
            "_search_nist": "NIST",
        }.get(getattr(provider, "__name__", ""), getattr(provider, "__name__", "chemical_source"))
        negative_key = (source, str(query or cas or name).strip().casefold())
        with self._source_lock:
            negative_until = self._provider_negative_until.get(negative_key, 0.0)
            if negative_until > time.monotonic():
                return None
            self._provider_negative_until.pop(negative_key, None)
        circuit_open, remaining_seconds = self._source_circuit_status(source)
        if circuit_open:
            self._log_source_circuit_skip(source, remaining_seconds)
            return None
        self._begin_provider_attempt()
        started = time.monotonic()
        original_deadline = getattr(self._thread_state, "lookup_deadline", None)
        provider_deadline = time.monotonic() + self._provider_budget_seconds(source)
        if original_deadline is not None:
            provider_deadline = min(float(original_deadline), provider_deadline)
        self._thread_state.lookup_deadline = provider_deadline
        try:
            with self._source_slot(source):
                result = provider(name=name, cas=cas, query=query, validation_names=validation_names)
        finally:
            if original_deadline is None:
                try:
                    delattr(self._thread_state, "lookup_deadline")
                except AttributeError:
                    pass
            else:
                self._thread_state.lookup_deadline = original_deadline
        elapsed_ms = int((time.monotonic() - started) * 1000)
        failure_kind = self._provider_failure_kind()
        provider_status = "success" if result else ("unavailable" if self._failure_counts_toward_circuit(failure_kind) else "not_found")
        provider_result = {
            "provider": source,
            "status": provider_status,
            "attempts": int(getattr(self._thread_state, "fetch_attempts", 0) or 0),
            "elapsed_ms": elapsed_ms,
        }
        if self.metrics is not None:
            self.metrics.record_provider(
                provider=source,
                status=provider_status,
                elapsed_ms=elapsed_ms,
                attempts=provider_result["attempts"],
                failure_kind=failure_kind,
            )
        metrics = self._provider_metrics.setdefault(source, {"calls": 0.0, "successes": 0.0, "elapsed_ms": 0.0})
        metrics["calls"] += 1
        metrics["successes"] += 1 if result else 0
        metrics["elapsed_ms"] += elapsed_ms
        attempts = int(provider_result["attempts"])
        if result:
            with self._source_lock:
                self._provider_negative_until.pop(negative_key, None)
            self._record_source_success(source)
            result.setdefault("provider_results", []).append(provider_result)
        elif failure_kind == "lookup_budget_exceeded" and attempts == 0:
            # A previous provider may consume the per-identity deadline. Do
            # not open this provider's circuit when it was never contacted.
            print(f"Chemical source skipped before request: {source} reason=lookup_budget_exceeded")
        else:
            if self._failure_counts_toward_circuit(failure_kind):
                with self._source_lock:
                    self._provider_negative_until[negative_key] = time.monotonic() + self._provider_negative_cache_seconds()
            self._record_source_failure(source, failure_kind)
        self._thread_state.last_provider_result = provider_result
        return result

    def _provider_budget_seconds(self, source: str) -> float:
        search_settings = (self.settings or {}).get("chemical_search", {}) or {}
        configured = search_settings.get("provider_budget_seconds") or {}
        defaults = {"Chemsrc": 6.0, "ChemicalBook": 3.0, "PubChem": 6.0, "NIST": 4.0}
        try:
            return max(1.0, float(configured.get(source.lower(), configured.get(source, defaults.get(source, 5.0)))))
        except (TypeError, ValueError):
            return defaults.get(source, 5.0)

    def _provider_negative_cache_seconds(self) -> float:
        search_settings = (self.settings or {}).get("chemical_search", {}) or {}
        try:
            return max(1.0, float(search_settings.get("provider_negative_cache_seconds", 60)))
        except (TypeError, ValueError):
            return 60.0

    def _record_cache_metric(self, layer: str, outcome: str) -> None:
        if self.metrics is not None:
            self.metrics.record_cache(layer=layer, outcome=outcome)

    @contextmanager
    def _source_slot(self, source: str) -> Any:
        semaphore = self._source_semaphore(source)
        semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

    def _source_semaphore(self, source: str) -> threading.BoundedSemaphore:
        limit = self._per_source_concurrency()
        with self._source_lock:
            semaphore = self._source_semaphores.get(source)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(limit)
                self._source_semaphores[source] = semaphore
            return semaphore

    def _per_source_concurrency(self) -> int:
        settings = (self.settings or {}).get("chemical_search", {}) or {}
        try:
            return max(1, min(8, int(settings.get("per_source_concurrency", 2))))
        except (TypeError, ValueError):
            return 2

    def _failure_threshold(self) -> int:
        settings = (self.settings or {}).get("chemical_search", {}) or {}
        try:
            return max(1, int(settings.get("failure_circuit_break_threshold", 5)))
        except (TypeError, ValueError):
            return 5

    def _circuit_cooldown_seconds(self) -> float:
        settings = (self.settings or {}).get("chemical_search", {}) or {}
        try:
            return max(1.0, float(settings.get("failure_circuit_cooldown_seconds", 600)))
        except (TypeError, ValueError):
            return 600.0

    def _source_circuit_open(self, source: str) -> bool:
        open_status, _remaining = self._source_circuit_status(source)
        return open_status

    def _source_circuit_status(self, source: str) -> tuple[bool, float]:
        with self._source_lock:
            now = time.monotonic()
            open_until = self._source_open_until.get(source, 0.0)
            if open_until > now:
                return True, open_until - now
            if open_until:
                if source in self._source_half_open:
                    return True, 1.0
                self._source_open_until.pop(source, None)
                self._source_half_open.add(source)
                self._source_failures[source] = max(0, self._failure_threshold() - 1)
                self._source_skip_logged.discard(source)
                print(f"Chemical source circuit half-open probe: {source}")
                return False, 0.0

            failure_count = self._source_failures.get(source, 0)
            if failure_count >= self._failure_threshold():
                cooldown = self._circuit_cooldown_seconds()
                self._source_open_until[source] = now + cooldown
                self._source_skip_logged.discard(source)
                reason = self._source_failure_reasons.get(source, "network_error")
                print(
                    "Chemical source circuit opened: "
                    f"{source} reason={reason} failures={failure_count}/{self._failure_threshold()} "
                    f"cooldown_seconds={int(cooldown)}"
                )
                return True, cooldown
            return False, 0.0

    def _record_source_success(self, source: str) -> None:
        with self._source_lock:
            self._source_failures[source] = 0
            self._source_open_until.pop(source, None)
            self._source_failure_reasons.pop(source, None)
            self._source_skip_logged.discard(source)
            self._source_half_open.discard(source)
        print(f"Chemical source success: {source}")

    def _record_source_failure(self, source: str, failure_kind: str = "") -> None:
        failure_kind = str(failure_kind or "no_result_or_relevance").strip()
        if not self._failure_counts_toward_circuit(failure_kind):
            with self._source_lock:
                if source in self._source_half_open:
                    self._source_half_open.discard(source)
                    self._source_failures[source] = 0
                    self._source_failure_reasons.pop(source, None)
            print(f"Chemical source no trusted result: {source} reason={failure_kind}; circuit counter unchanged")
            return
        opened = False
        with self._source_lock:
            self._source_failures[source] = self._source_failures.get(source, 0) + 1
            self._source_failure_reasons[source] = failure_kind
            failure_count = self._source_failures[source]
            if failure_count >= self._failure_threshold():
                self._source_open_until[source] = time.monotonic() + self._circuit_cooldown_seconds()
                self._source_half_open.discard(source)
                if source not in self._source_skip_logged:
                    self._source_skip_logged.add(source)
                    opened = True
        if opened:
            print(
                "Chemical source circuit opened: "
                f"{source} reason={failure_kind} failures={failure_count}/{self._failure_threshold()} "
                f"cooldown_seconds={int(self._circuit_cooldown_seconds())}"
            )

    def _log_source_circuit_skip(self, source: str, remaining_seconds: float) -> None:
        with self._source_lock:
            if source in self._source_skip_logged:
                return
            self._source_skip_logged.add(source)
            failure_count = self._source_failures.get(source, 0)
            reason = self._source_failure_reasons.get(source, "network_error")
        print(
            "Skipping chemical source while circuit is open: "
            f"{source} reason={reason} failures={failure_count}/{self._failure_threshold()} "
            f"remaining_seconds={int(max(0.0, remaining_seconds))}"
        )

    def _begin_provider_attempt(self) -> None:
        self._thread_state.provider_fetch_failure_kind = ""
        self._thread_state.fetch_attempts = 0
        self._thread_state.last_provider_result = {}

    def _mark_provider_fetch_failure(self, failure_kind: str) -> None:
        current = str(getattr(self._thread_state, "provider_fetch_failure_kind", "") or "")
        if not current:
            self._thread_state.provider_fetch_failure_kind = failure_kind

    def _provider_failure_kind(self) -> str:
        return str(getattr(self._thread_state, "provider_fetch_failure_kind", "") or "no_result_or_relevance")

    @staticmethod
    def _failure_counts_toward_circuit(failure_kind: str) -> bool:
        normalized = str(failure_kind or "").lower()
        if any(token in normalized for token in ("timeout", "budget", "url_error", "network_error", "socket", "os_error")):
            return True
        return any(f"http_error_{status}" in normalized for status in (429, 500, 502, 503, 504))

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
        if not self._fallback_web_research_enabled():
            return None
        if not self._fallback_web_research_candidate_allowed(reagent_name, cas, search_name, name_result):
            return None

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
        pages = WebResearcher(settings=self.settings, timeout_seconds=self._fallback_web_research_timeout_seconds()).research(
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

    def _llm_knowledge_fallback(
        self,
        reagent_name: str,
        cas: str,
        search_name: str,
        name_result: dict[str, Any],
        failed_queries: list[str],
        reason: str,
    ) -> dict[str, Any] | None:
        search_settings = ((self.settings or {}).get("chemical_search") or {})
        enabled = str(
            os.getenv("ENABLE_LLM_KNOWLEDGE_FALLBACK")
            or search_settings.get("enable_llm_knowledge_fallback", True)
        ).strip().lower()
        if enabled in {"0", "false", "no", "off"}:
            return None
        extractor = LlmExtractor(settings=self.settings)
        legacy_enabled = str(os.getenv("ENABLE_LEGACY_LLM_KNOWLEDGE_FALLBACK") or "").strip().lower()
        if hasattr(extractor, "generate_manual_review_advice") and legacy_enabled not in {"1", "true", "yes", "on"}:
            # The approval flow now generates one consolidated second opinion after
            # deterministic classification. Avoid an earlier duplicate LLM call here.
            return None
        fallback = extractor.generate_knowledge_fallback(
            {
                "raw_name": reagent_name,
                "name": reagent_name,
                "cas": cas,
                "standard_name": name_result.get("standard_name", ""),
                "cleaned_name": name_result.get("cleaned_name", ""),
                "english_name": name_result.get("english_name", ""),
                "aliases": name_result.get("aliases", []),
                "failed_queries": failed_queries,
                "no_web_evidence_reason": reason,
            }
        )
        raw_text = str(fallback.get("raw_text") or "").strip()
        if not raw_text:
            return None

        result = self._result(
            name=search_name or reagent_name,
            cas=cas,
            source="LLM knowledge fallback",
            url="",
            raw_text=raw_text,
        )
        result["need_manual_review"] = True
        result["name_normalization"] = name_result
        result["query"] = search_name or reagent_name or cas
        result["relevance_passed"] = False
        result["source_confidence"] = min(self._normalize_confidence(fallback.get("confidence")), 0.65)
        result["evidence_quality"] = "llm_knowledge_low"
        result["failure_reason"] = (
            f"{reason}; no trusted web evidence was found, so low-confidence LLM knowledge fallback was used. "
            f"{fallback.get('reason') or ''}"
        ).strip()
        result["fallback_source"] = "LLM knowledge fallback"
        result["fallback_url"] = ""
        result["used_llm_knowledge_fallback"] = True
        return result

    def _fallback_web_research_candidate_allowed(
        self,
        reagent_name: str,
        cas: str,
        search_name: str,
        name_result: dict[str, Any],
    ) -> bool:
        if self._extract_cas(cas):
            return True
        if self._looks_like_product_or_mixture_name(reagent_name, search_name, name_result):
            return False
        confidence = self._normalize_confidence(name_result.get("confidence"))
        normalized_name = str(
            name_result.get("standard_name")
            or name_result.get("cleaned_name")
            or search_name
            or reagent_name
            or ""
        ).strip()
        if not normalized_name:
            return False
        if bool(name_result.get("suspected_invalid_name")) and confidence < 0.8:
            return False
        return confidence >= 0.72 or self._looks_like_precise_chemical_name(normalized_name)

    @classmethod
    def _looks_like_product_or_mixture_name(
        cls,
        reagent_name: str,
        search_name: str,
        name_result: dict[str, Any],
    ) -> bool:
        text = " ".join(
            str(value or "")
            for value in (
                reagent_name,
                search_name,
                name_result.get("raw_name", ""),
                name_result.get("cleaned_name", ""),
                name_result.get("standard_name", ""),
            )
        )
        normalized = re.sub(r"[\s\-_()/\\\[\]{}（）]+", "", text.lower())
        return any(
            token in normalized
            for token in (
                "分散剂",
                "润湿剂",
                "润滑液",
                "机油",
                "油品",
                "树脂",
                "聚合物",
                "共聚物",
                "色浆",
                "涂料",
                "胶水",
                "清洗剂",
                "添加剂",
                "助剂",
                "耗材",
                "roll",
                "pvdf",
            )
        )

    @staticmethod
    def _looks_like_precise_chemical_name(value: str) -> bool:
        text = str(value or "").strip()
        if len(text) < 3:
            return False
        if re.search(r"\d[,，-]", text) or re.search(r"[αβγa-z]-", text.lower()):
            return True
        return bool(re.search(r"(酸|醇|酮|醛|酯|醚|胺|酚|盐|吡啶|苯|萘|咪唑|吲哚|噻唑)", text))

    def _fallback_web_research_enabled(self) -> bool:
        search_settings = ((self.settings or {}).get("chemical_search") or {})
        enabled = str(
            os.getenv("ENABLE_FALLBACK_WEB_RESEARCH")
            or search_settings.get("enable_fallback_web_research", True)
        ).strip().lower()
        return enabled not in {"0", "false", "no", "off"}

    def _fallback_web_research_timeout_seconds(self) -> int:
        search_settings = ((self.settings or {}).get("chemical_search") or {})
        try:
            value = int(search_settings.get("fallback_timeout_seconds", 8))
        except (TypeError, ValueError):
            value = 8
        return max(3, min(self.timeout_seconds, value))

    @staticmethod
    def _query_candidates(
        cas: str,
        standard_name: str,
        cleaned_name: str,
        english_name: str = "",
        aliases: list[str] | None = None,
    ) -> list[str]:
        """Build CAS-first queries while retaining names for conflict repair."""
        candidates: list[str] = []
        for value in (cas, standard_name, cleaned_name, english_name, *(aliases or [])):
            value = str(value or "").strip()
            if value and value not in candidates:
                candidates.append(value)
        return candidates

    def _query_candidates_compat(
        self,
        cas: str,
        standard_name: str,
        cleaned_name: str,
        english_name: str = "",
        aliases: list[str] | None = None,
    ) -> list[str]:
        try:
            return self._query_candidates(cas, standard_name, cleaned_name, english_name, aliases)
        except TypeError as error:
            if "_query_candidates" not in str(error):
                raise
            return self._query_candidates(cas, standard_name, cleaned_name, english_name)

    def _max_name_queries(self) -> int:
        settings = (self.settings or {}).get("chemical_search", {}) or {}
        try:
            return max(1, min(8, int(settings.get("max_name_queries", 4))))
        except (TypeError, ValueError):
            return 4

    @classmethod
    def _mixture_metadata(
        cls,
        raw_name: str,
        cleaned_name: str,
        specification: str,
        name_result: dict[str, Any],
    ) -> dict[str, Any]:
        concentration = str(name_result.get("concentration") or "").strip()
        text = re.sub(r"\s+", "", f"{raw_name} {cleaned_name} {specification}").lower()
        mixture_tokens = (
            "混合物", "混合液", "溶液", "分散剂", "清洗剂", "润湿剂", "润滑液",
            "油品", "机油", "树脂", "涂料", "色浆", "胶水", "添加剂", "助剂",
            "mixture", "solution", "blend", "dispersion",
        )
        is_mixture = any(token in text for token in mixture_tokens)
        if concentration and not cls._looks_like_precise_chemical_name(cleaned_name):
            is_mixture = True
        return {
            "is_mixture": is_mixture,
            "concentration": concentration,
            "product_name": raw_name if is_mixture else "",
            "manufacturer": "",
            "catalog_number": "",
            "mixture_components": [],
            "mixture_risk_categories": [],
        }

    def _supplier_sds_result(
        self,
        *,
        name: str,
        cas: str,
        raw_text: str,
        manufacturer: str,
        catalog_number: str,
        name_normalization: dict[str, Any],
    ) -> dict[str, Any]:
        """Create field-level evidence from an already supplied/approved SDS.

        This parser is intentionally deterministic.  It extracts evidence only; it
        never invents properties or decides the final business risk category.
        """
        text = re.sub(r"\r\n?", "\n", str(raw_text or "")).strip()
        sections = self._sds_sections(text)
        components = self._sds_components(sections.get("3", ""))
        incomplete = bool(
            re.search(r"保密|商业秘密|proprietary|trade\s+secret|unknown|未披露", sections.get("3", ""), re.I)
            or not components
        )
        source_label = "Supplier SDS"
        source_url = ""
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        evidence_items: list[dict[str, str]] = []
        section_fields = {
            "2": "ghs_classification",
            "3": "composition",
            "9": "physical_properties",
            "10": "stability_reactivity",
        }
        for number, field_name in section_fields.items():
            section_text = re.sub(r"\s+", " ", sections.get(number, "")).strip()
            if not section_text:
                continue
            evidence_items.append({
                "field": field_name,
                "value": section_text[:2000],
                "unit": "",
                "source": source_label,
                "source_url": source_url,
                "source_section": f"SDS Section {number}",
                "retrieved_at": retrieved_at,
            })
        for field_name, labels in {
            "flash_point": ("闪点", "flash point"),
            "boiling_point": ("沸点", "boiling point", "initial boiling point"),
        }.items():
            value = self._sds_labeled_value(sections.get("9", ""), labels)
            if value:
                evidence_items.append({
                    "field": field_name,
                    "value": value,
                    "unit": "",
                    "source": source_label,
                    "source_url": source_url,
                    "source_section": "SDS Section 9",
                    "retrieved_at": retrieved_at,
                })

        result = self._result(name=name, cas=cas, source=source_label, url=source_url, raw_text=text)
        result.update({
            "identity_status": "verified" if (manufacturer and catalog_number) else "name_only",
            "retrieval_status": "local",
            "relevance_passed": True,
            "need_manual_review": incomplete,
            "failure_reason": "SDS 组成不完整或含保密组分，按已知最高风险输出并转人工复核。" if incomplete else "",
            "name_normalization": name_normalization,
            "is_mixture": len(components) > 1,
            "manufacturer": str(manufacturer or "").strip(),
            "catalog_number": str(catalog_number or "").strip(),
            "mixture_components": components,
            "composition_complete": not incomplete,
            "evidence_items": evidence_items,
            "query_plan": {
                "cleaned_name": str(name_normalization.get("cleaned_name") or ""),
                "candidate_names": [],
                "erp_cas": cas,
                "queries_attempted": ["approved_supplier_sds"],
            },
        })
        return result

    @staticmethod
    def _sds_sections(text: str) -> dict[str, str]:
        pattern = re.compile(
            r"(?im)^\s*(?:SECTION\s*)?(?:第\s*)?(?P<number>0?[1-9]|1[0-6])\s*(?:部分|节|SECTION)?\s*[:：.、-]?\s*"
        )
        matches = list(pattern.finditer(text or ""))
        sections: dict[str, str] = {}
        for index, match in enumerate(matches):
            number = str(int(match.group("number")))
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            sections[number] = text[match.end():end].strip()
        return sections

    @classmethod
    def _sds_components(cls, section: str) -> list[dict[str, str]]:
        components: list[dict[str, str]] = []
        for line in str(section or "").splitlines():
            cas = cls._extract_cas(line)
            if not cas:
                continue
            concentration_match = re.search(
                r"(?<!\d)(?:[<>≤≥~约]?\s*\d+(?:\.\d+)?\s*(?:[-–~至]\s*\d+(?:\.\d+)?)?\s*%)(?!\w)",
                line,
            )
            before_cas = line[: line.find(cas)].strip(" \t|,，;；:-")
            name = re.sub(r"\s{2,}|\t+|[|,，;；]+$", " ", before_cas).strip()
            components.append({
                "name": name,
                "cas": cas,
                "concentration": concentration_match.group(0).strip() if concentration_match else "",
            })
        deduped: dict[str, dict[str, str]] = {}
        for component in components:
            deduped.setdefault(component["cas"], component)
        return list(deduped.values())

    @staticmethod
    def _sds_labeled_value(section: str, labels: tuple[str, ...]) -> str:
        for line in str(section or "").splitlines():
            if any(label.lower() in line.lower() for label in labels):
                parts = re.split(r"[:：]", line, maxsplit=1)
                return (parts[1] if len(parts) > 1 else line).strip()[:300]
        return ""

    def _reconcile_provider_identity(
        self,
        result: dict[str, Any],
        *,
        input_cas: str,
        query: str,
        validation_names: list[str],
    ) -> dict[str, Any]:
        """Enforce the name/CAS convergence contract for every provider."""
        reconciled = dict(result)
        candidate_cas = self._extract_cas(str(reconciled.get("cas") or reconciled.get("raw_text") or ""))
        if not reconciled.get("relevance_passed"):
            relevance = self._result_relevance(
                str(reconciled.get("raw_text") or ""),
                name=query,
                cas=input_cas,
                preferred_name=str(reconciled.get("matched_site_name") or reconciled.get("name") or ""),
                validation_names=validation_names,
            )
            reconciled["relevance_passed"] = bool(relevance.get("relevance_passed"))
            reconciled["name_similarity"] = max(
                self._normalize_confidence(reconciled.get("name_similarity")),
                self._normalize_confidence(relevance.get("name_similarity")),
            )
            reconciled["matched_site_name"] = str(
                reconciled.get("matched_site_name") or relevance.get("matched_site_name") or ""
            )

        current_status = str(reconciled.get("identity_status") or "unresolved")
        if input_cas and candidate_cas and not self._same_cas(input_cas, candidate_cas):
            trusted_name = self._trusted_name_verification(reconciled)
            reconciled.update({
                "cas": candidate_cas,
                "candidate_cas": candidate_cas,
                "original_erp_cas": input_cas,
                "cas_name_conflict": True,
                "cas_correction_candidate": trusted_name,
                "cas_correction_applied": trusted_name,
                "identity_decision_basis": "name_identity",
                "identity_status": "conflict",
                "need_manual_review": not trusted_name,
                "failure_reason": self._append_reason(
                    str(reconciled.get("failure_reason") or ""),
                    f"ERP CAS {input_cas} 与名称来源 CAS {candidate_cas} 冲突；按名称身份采用 CAS {candidate_cas}。",
                ),
            })
            if trusted_name:
                reconciled["corrected_cas"] = candidate_cas
            else:
                reconciled.pop("corrected_cas", None)
            return reconciled
        if current_status == "unresolved":
            if input_cas and candidate_cas and self._same_cas(input_cas, candidate_cas):
                reconciled["identity_status"] = "verified"
            elif reconciled.get("relevance_passed"):
                reconciled["identity_status"] = "name_only"
        return reconciled

    @staticmethod
    def _manual_search_urls(name: str, cas: str) -> dict[str, str]:
        query = cas or name
        encoded = quote_plus(query)
        return {
            "chemsrc": f"https://www.chemsrc.com/en/searchResult/{encoded}/" if query else "",
            "chemicalbook": f"https://www.chemicalbook.com/Search_EN.aspx?keyword={encoded}" if query else "",
        }

    @staticmethod
    def _normalize_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, confidence))

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

    def _search_pubchem(
        self,
        name: str,
        cas: str,
        query: str,
        validation_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        validation_names = validation_names or [name]
        cas_only_query = bool(cas and self._same_cas(query, cas))
        name_cids = [] if cas_only_query else self._pubchem_cids(query)
        cas_cids = self._pubchem_cids(cas) if cas else []

        identity_status = "unresolved"
        chosen_cid = ""
        if name_cids and cas_cids:
            common = [cid for cid in name_cids if cid in set(cas_cids)]
            if common:
                chosen_cid = common[0]
                identity_status = "verified"
            else:
                return self._pubchem_identity_issue(
                    name=name,
                    cas=cas,
                    query=query,
                    status="conflict",
                    reason=f"清洗名称解析到 PubChem CID {name_cids[:3]}，但 ERP CAS 解析到 {cas_cids[:3]}，两者不一致。",
                )
        elif name_cids:
            if len(name_cids) > 1 and not cas:
                return self._pubchem_identity_issue(
                    name=name,
                    cas=cas,
                    query=query,
                    status="ambiguous",
                    reason=f"清洗名称在 PubChem 命中多个候选 CID：{name_cids[:5]}。",
                )
            chosen_cid = name_cids[0]
            identity_status = "name_only" if not cas else "conflict"
        elif cas_cids:
            chosen_cid = cas_cids[0]
            identity_status = "cas_only"
        else:
            return None

        property_url = (
            "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
            f"{chosen_cid}/property/Title,IUPACName,MolecularFormula,MolecularWeight,CanonicalSMILES/JSON"
        )
        property_data = self._fetch_json(property_url)
        properties = ((property_data.get("PropertyTable") or {}).get("Properties") or []) if property_data else []
        if not properties:
            return None
        prop = properties[0]

        synonyms_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{chosen_cid}/synonyms/JSON"
        synonym_data = self._fetch_json(synonyms_url)
        info = ((synonym_data.get("InformationList") or {}).get("Information") or []) if synonym_data else []
        synonyms = [str(value).strip() for value in ((info[0].get("Synonym") or []) if info else []) if str(value).strip()]
        returned_cas_values = self._dedupe_strings(
            self._extract_cas(value) for value in synonyms if self._extract_cas(value)
        )
        resolved_cas = cas if cas and cas in returned_cas_values else (returned_cas_values[0] if returned_cas_values else cas)

        matched_name, name_similarity = self._best_name_match(
            validation_names,
            [str(prop.get("Title") or ""), str(prop.get("IUPACName") or ""), *synonyms[:80]],
        )
        if identity_status == "cas_only" and name_similarity < 0.82 and not self._non_informative_name_text(*validation_names):
            identity_status = "conflict"
        if identity_status == "conflict":
            return self._pubchem_identity_issue(
                name=name,
                cas=cas,
                query=query,
                status="conflict",
                reason="ERP CAS 可在 PubChem 中解析，但返回名称与清洗后的试剂名不一致。",
                cid=chosen_cid,
                matched_name=matched_name,
            )

        view_url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{chosen_cid}/JSON?heading=Safety%20and%20Hazards"
        view_data = self._fetch_json(view_url)
        evidence_items, annotation_text = self._pubchem_evidence(view_data, chosen_cid)
        base_url = f"https://pubchem.ncbi.nlm.nih.gov/compound/{chosen_cid}"
        base_fields = (
            ("name", prop.get("Title")),
            ("iupac_name", prop.get("IUPACName")),
            ("molecular_formula", prop.get("MolecularFormula")),
            ("molecular_weight", prop.get("MolecularWeight")),
            ("canonical_smiles", prop.get("ConnectivitySMILES") or prop.get("CanonicalSMILES")),
        )
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for field_name, value in base_fields:
            if value not in (None, ""):
                evidence_items.insert(0, {
                    "field": field_name,
                    "value": str(value),
                    "unit": "",
                    "source": "PubChem",
                    "source_url": base_url,
                    "source_section": "Computed Descriptors",
                    "retrieved_at": retrieved_at,
                })

        raw_parts = [
            f"Product Name: {prop.get('Title') or name}",
            f"IUPAC Name: {prop.get('IUPACName') or ''}",
            f"CAS Number: {resolved_cas}",
            f"PubChem CID: {chosen_cid}",
            f"Molecular Formula: {prop.get('MolecularFormula') or ''}",
            f"Molecular Weight: {prop.get('MolecularWeight') or ''}",
            f"Synonyms: {'; '.join(synonyms[:20])}",
            annotation_text,
        ]
        raw_text = " ".join(part for part in raw_parts if part and not part.endswith(": ")).strip()
        result = self._result(
            name=str(prop.get("Title") or name),
            cas=resolved_cas,
            source="PubChem",
            url=base_url,
            raw_text=raw_text,
        )
        cas_only_name_validated = identity_status == "cas_only" and name_similarity >= 0.9
        result.update({
            "identity_status": identity_status,
            "retrieval_status": "fresh" if view_data else "partial",
            "pubchem_cid": chosen_cid,
            "matched_site_name": matched_name or str(prop.get("Title") or ""),
            "name_similarity": round(name_similarity, 3),
            "relevance_passed": (
                identity_status in {"verified", "name_only"} and name_similarity >= 0.82
            ) or cas_only_name_validated,
            "cas_matched": identity_status in {"verified", "cas_only"},
            "evidence_items": evidence_items,
            "manual_search_urls": self._manual_search_urls(str(prop.get("Title") or name), resolved_cas),
        })
        result["need_manual_review"] = not result["relevance_passed"]
        if (identity_status == "name_only" and name_similarity >= 0.9) or cas_only_name_validated:
            result["need_manual_review"] = False
        return result

    def _search_nist(
        self,
        name: str,
        cas: str,
        query: str,
        validation_names: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch labelled NIST WebBook properties for a CAS-resolved identity."""
        cas_no = self._extract_cas(cas or query)
        if not cas_no:
            return None
        url = f"https://webbook.nist.gov/cgi/cbook.cgi?ID=C{cas_no.replace('-', '')}&Mask=4"
        try:
            raw_text = self._fetch(url)
        except Exception:
            return None
        if cas_no.replace("-", "") not in raw_text.replace("-", "") and "NIST Chemistry WebBook" not in raw_text:
            return None
        result = self._result(name=name or query, cas=cas_no, source="NIST", url=url, raw_text=raw_text)
        result.update(
            {
                "relevance_passed": True,
                "passed": True,
                "matched_site_name": name or query,
                "name_similarity": 1.0 if name else 0.0,
                "identity_status": "cas_only",
                "source_confidence": 0.92,
                "evidence_quality": "high",
                "need_manual_review": False,
            }
        )
        return result

    def _enrich_missing_official_fields(self, result: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(result)
        evidence = list(enriched.get("evidence_items") or [])
        present = {str(item.get("field") or "") for item in evidence}
        verified_cas = self._extract_cas(str(enriched.get("cas") or ""))
        if not verified_cas:
            return enriched
        configured = ((self.settings or {}).get("chemical_search", {}) or {}).get("providers")
        providers = {
            str(value or "").strip().lower()
            for value in (configured if isinstance(configured, list) else ["pubchem"])
        }

        api_key = str(os.getenv("EPA_COMPTOX_API_KEY") or "").strip()
        if "comptox" in providers and api_key and not ({"toxicity", "ghs_classification"} & present):
            comptox_items, diagnostic = self._comptox_evidence(verified_cas, api_key)
            evidence.extend(comptox_items)
            enriched.setdefault("provider_results", []).append(diagnostic)
            present.update(str(item.get("field") or "") for item in comptox_items)

        if "nist" in providers and "boiling_point" not in present:
            nist_items, diagnostic = self._nist_evidence(verified_cas)
            evidence.extend(nist_items)
            enriched.setdefault("provider_results", []).append(diagnostic)

        enriched["evidence_items"] = evidence
        return enriched

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
    def _pubchem_evidence(cls, data: dict[str, Any], cid: str) -> tuple[list[dict[str, str]], str]:
        items: list[dict[str, str]] = []
        snippets: list[str] = []
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        source_url = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}#section=Safety-and-Hazards"

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
        # The Chinese endpoint indexes Chinese product names and aliases more
        # reliably.  Keep the English endpoint as a compatibility fallback for
        # bilingual ERP data and for site-side index gaps.
        contains_chinese = bool(re.search(r"[\u3400-\u9fff]", query))
        search_urls = (
            [
                f"https://www.chemicalbook.com/Search.aspx?keyword={quote_plus(query)}",
                f"https://www.chemicalbook.com/Search_EN.aspx?keyword={quote_plus(query)}",
            ]
            if contains_chinese
            else [f"https://www.chemicalbook.com/Search_EN.aspx?keyword={quote_plus(query)}"]
        )
        for search_url in search_urls:
            search_html = self._fetch(search_url)
            if not search_html:
                continue
            candidates = self._search_result_candidates(
                search_html,
                base_url=search_url,
                link_patterns=[
                    r'href="([^"]*ChemicalProductProperty_(?:CN|EN)_[^"]+\.htm)"',
                    r'href="([^"]*/CAS(?:CN|EN)_[^"]+\.htm)"',
                ],
            )
            result = self._best_detail_result(
                candidates=candidates,
                source="ChemicalBook",
                name=name,
                cas=cas,
                validation_names=validation_names,
            )
            if result:
                return result
        return None

    def _fetch(self, url: str, headers: dict[str, str] | None = None) -> str:
        response_key = f"{url}|{','.join(sorted((headers or {}).keys()))}"
        if response_key in self._response_cache:
            return self._response_cache[response_key]
        request_headers = {
            "User-Agent": "reagent-approval-bot/2.0 (chemical-data; contact=local-operator)",
            "Accept": "application/json,text/html;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        request_headers.update(headers or {})
        request = Request(
            url,
            headers=request_headers,
        )
        max_attempts = self._http_max_attempts()
        transient_codes = {429, 500, 502, 503, 504}
        for attempt in range(1, max_attempts + 1):
            remaining = self._remaining_lookup_budget()
            if remaining <= 0:
                self._mark_provider_fetch_failure("lookup_budget_exceeded")
                return ""
            self._thread_state.fetch_attempts = int(getattr(self._thread_state, "fetch_attempts", 0) or 0) + 1
            self._wait_for_host_rate_limit(url)
            try:
                timeout = max(0.2, min(self._http_timeout_seconds(), self._remaining_lookup_budget()))
                self._public_request_count += 1
                with urlopen(request, timeout=timeout) as response:
                    data = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    text = data.decode(charset, errors="ignore")
                    self._response_cache[response_key] = text
                    return text
            except HTTPError as error:
                failure = f"http_error_{error.code}"
                if error.code not in transient_codes or attempt >= max_attempts:
                    self._mark_provider_fetch_failure(failure if error.code != 404 else "no_result")
                    return ""
                retry_after = str(error.headers.get("Retry-After") or "").strip() if error.headers else ""
                self._retry_wait(attempt, retry_after)
            except (TimeoutError, socket.timeout):
                if attempt >= max_attempts:
                    self._mark_provider_fetch_failure("timeout")
                    return ""
                self._retry_wait(attempt)
            except URLError:
                if attempt >= max_attempts:
                    self._mark_provider_fetch_failure("url_error")
                    return ""
                self._retry_wait(attempt)
            except OSError:
                if attempt >= max_attempts:
                    self._mark_provider_fetch_failure("network_error")
                    return ""
                self._retry_wait(attempt)
        return ""

    def _http_max_attempts(self) -> int:
        settings = (self.settings or {}).get("chemical_search", {}) or {}
        try:
            return max(1, min(5, int(settings.get("max_attempts", 3))))
        except (TypeError, ValueError):
            return 3

    def _http_timeout_seconds(self) -> float:
        settings = (self.settings or {}).get("chemical_search", {}) or {}
        try:
            configured = settings.get("request_timeout_seconds", settings.get("read_timeout_seconds", 15))
            return max(1.0, min(float(self.timeout_seconds), float(configured)))
        except (TypeError, ValueError):
            return min(float(self.timeout_seconds), 15.0)

    def _lookup_budget_seconds(self) -> float:
        settings = (self.settings or {}).get("chemical_search", {}) or {}
        try:
            return max(1.0, float(settings.get("lookup_budget_seconds", 30)))
        except (TypeError, ValueError):
            return 30.0

    def _remaining_lookup_budget(self) -> float:
        deadline = float(getattr(self._thread_state, "lookup_deadline", 0.0) or 0.0)
        if not deadline:
            return self._lookup_budget_seconds()
        return max(0.0, deadline - time.monotonic())

    def _requests_per_second(self) -> float:
        settings = (self.settings or {}).get("chemical_search", {}) or {}
        try:
            return max(0.2, min(5.0, float(settings.get("requests_per_second", 2))))
        except (TypeError, ValueError):
            return 2.0

    def _wait_for_host_rate_limit(self, url: str) -> None:
        host = (urlparse(url).hostname or "unknown").lower()
        interval = 1.0 / self._requests_per_second()
        with self._source_lock:
            now = time.monotonic()
            wait_seconds = max(0.0, self._host_last_request.get(host, 0.0) + interval - now)
            self._host_last_request[host] = now + wait_seconds
        if wait_seconds:
            time.sleep(wait_seconds)

    def _retry_wait(self, attempt: int, retry_after: str = "") -> None:
        try:
            wait_seconds = float(retry_after)
        except (TypeError, ValueError):
            wait_seconds = min(8.0, float(2 ** (attempt - 1))) + random.uniform(0.0, 0.25)
        time.sleep(max(0.0, min(30.0, wait_seconds, self._remaining_lookup_budget())))

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
                page_cas = self._extract_cas(raw_text)
                result = self._result(
                    name=name,
                    cas=page_cas or cas,
                    source=source,
                    url=candidate.url,
                    raw_text=raw_text,
                )
                if cas:
                    result["query_cas"] = cas
                if page_cas:
                    result["candidate_cas"] = page_cas
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
        names_for_validation = validation_names or [name]
        normalized_cas = self._normalize_for_similarity(cas)
        if normalized_cas and normalized_cas in normalized_text:
            site_name = self._clean_site_name(preferred_name)
            primary_names = self._primary_names(raw_text)
            candidates = self._candidate_names(raw_text)
            if preferred_name:
                candidates.insert(0, preferred_name)
            candidates = [*primary_names, *candidates]
            if not site_name:
                site_name = self._clean_site_name(primary_names[0] if primary_names else "")
            best_name = site_name
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
                        best_name = self._clean_site_name(candidate) or candidate
                        best_score = score
                if len(target) >= 2 and target in normalized_text:
                    best_name = best_name or validation_name
                    best_score = max(best_score, 0.9)

            name_is_non_informative = self._non_informative_name_text(name, *names_for_validation)
            relevance_passed = bool(name_is_non_informative or best_score >= 0.82)
            return {
                "relevance_passed": relevance_passed,
                "matched_site_name": best_name,
                "name_similarity": 1.0 if name_is_non_informative else round(best_score, 3),
                "passed": relevance_passed,
                "cas_matched": True,
                "name_verification_required": not name_is_non_informative,
            }

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
            "evidence_quality": "high" if source in TRUSTED_NAME_VERIFICATION_SOURCES else "medium",
            "failure_reason": "",
            "fallback_source": "",
            "fallback_url": "",
            "used_llm_search_candidates": False,
            "llm_search_candidates": [],
            "used_llm_knowledge_fallback": False,
            "identity_status": "unresolved",
            "retrieval_status": "fresh",
            "evidence_items": [],
            "provider_results": [],
            "manual_search_urls": self._manual_search_urls(name, cas),
        }

    @staticmethod
    def _source_confidence(source: str) -> float:
        if source == "Chemsrc":
            return 0.92
        if source == "ChemicalBook":
            return 0.86
        if source == "PubChem":
            return 0.9
        if source == "EPA CompTox":
            return 0.92
        if source == "NIST":
            return 0.94
        if source == "Supplier SDS":
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
            "used_llm_knowledge_fallback": False,
            "identity_status": "unresolved",
            "retrieval_status": "not_found",
            "evidence_items": [],
            "provider_results": [],
            "manual_search_urls": ChemicalSearcher._manual_search_urls(name, cas),
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
