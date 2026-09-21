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
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urljoin, urlparse
from urllib.request import Request, urlopen

from chemical_search_cache import ChemicalSearchCache
from chemical_identity_resolver import ChemicalIdentityResolver
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
    _response_metadata: ClassVar[dict[str, dict[str, str]]] = {}
    _provider_negative_until: ClassVar[dict[tuple[str, str], float]] = {}
    _identity_enrichment_cache: dict[tuple[str, str, str], dict[str, Any]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _identity_enrichment_calls: int = field(default=0, init=False, repr=False)

    @staticmethod
    def is_trusted_source(source: str) -> bool:
        return str(source or "").strip() in TRUSTED_NAME_VERIFICATION_SOURCES | {"local_memory"}

    def _property_enrichment_config(self) -> dict[str, Any]:
        configured = ((self.settings or {}).get("chemical_search", {}) or {}).get("property_enrichment", {}) or {}
        defaults = {
            "enabled": True,
            "max_requests": 3,
            "budget_seconds": 8,
            "providers": ["pubchem", "nist", "chemicalbook", "chemsrc"],
            "fields": [
                "flash_point", "boiling_point", "toxicity", "ghs_classification", "corrosive",
                "oxidizing", "flammable", "water_reactive", "explosive_risk", "heavy_metal",
            ],
        }
        result = {**defaults, **configured}
        result["enabled"] = bool(result.get("enabled", True))
        try:
            result["max_requests"] = max(1, min(8, int(result.get("max_requests", 3))))
        except (TypeError, ValueError):
            result["max_requests"] = 3
        try:
            result["budget_seconds"] = max(1.0, min(30.0, float(result.get("budget_seconds", 8))))
        except (TypeError, ValueError):
            result["budget_seconds"] = 8.0
        result["providers"] = {str(value).strip().lower() for value in result.get("providers", []) if str(value).strip()}
        result["fields"] = {str(value).strip() for value in result.get("fields", []) if str(value).strip()}
        return result

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
        name_result = normalizer.normalize(raw_name=name, cas=cas)
        supplied_cas = self._extract_cas(str(cas or ""))
        input_cas = supplied_cas if self._is_valid_cas(supplied_cas) else ""
        name_only_result = (
            normalizer.normalize(raw_name=name, cas="")
            if supplied_cas
            else name_result
        )

        query_name_result = name_only_result if input_cas else name_result
        cas_no = input_cas or self._extract_cas(str(query_name_result.get("cas") or ""))
        standard_name = str(query_name_result.get("standard_name") or "").strip()
        cleaned_name = str(query_name_result.get("cleaned_name") or "").strip()
        english_name = str(query_name_result.get("english_name") or "").strip()
        aliases = [str(value).strip() for value in (query_name_result.get("aliases") or []) if str(value).strip()]
        name_result = query_name_result
        queries = self._query_candidates_compat(cas_no, standard_name, cleaned_name, english_name, aliases)
        # Keep the original ERP spelling as a retrieval candidate.  Cleaning is
        # intentionally conservative and can remove a hydration notation such
        # as "·三水" that Chinese supplier indexes retain.  These are query
        # variants only; they do not alter the canonical identity.
        for variant in self._hydrate_name_query_variants(name, cleaned_name):
            if variant and variant not in queries:
                queries.append(variant)
        if name and name not in queries:
            queries.append(name)
        mixture = self._mixture_metadata(name, cleaned_name, "", name_result)
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

        # The original ERP name carries identity details that a normalizer may
        # not preserve (notably hydrate counts).  Keep it in every relevance
        # check so a supplier result cannot silently change that identity.
        validation_names = self._validation_names(name, standard_name, cleaned_name, english_name, aliases)
        persistent_key = self._persistent_cache_key(
            source="chemical_search",
            normalized_name=cleaned_name or standard_name or english_name or name,
            cas=cas_no,
            search_mode="official_api_v2",
            concentration=str(mixture.get("concentration") or ""),
            manufacturer=str(mixture.get("manufacturer") or ""),
            catalog_number=str(mixture.get("catalog_number") or ""),
        )
        # A syntactically valid ERP CAS is an explicit business identifier.  When
        # PubChem resolves it, use that identity before considering a supplier
        # name match.  This avoids silently replacing ERP's CAS with a similarly
        # named but different substance.  A name mismatch remains visible in the
        # audit payload, but is not a review or write gate under the configured
        # CAS-authoritative policy.
        if input_cas and self._cas_authoritative():
            self._thread_state.lookup_deadline = time.monotonic() + self._lookup_budget_seconds()
            cas_candidate = self._run_provider(
                self._search_pubchem,
                name=input_cas,
                cas=input_cas,
                query=input_cas,
                validation_names=validation_names,
            )
            if cas_candidate and self._same_cas(
                input_cas,
                self._extract_cas(str(cas_candidate.get("candidate_cas") or cas_candidate.get("cas") or "")),
            ):
                cas_candidate = self._finalize_cas_authoritative_result(
                    cas_candidate,
                    input_cas=input_cas,
                    raw_name=name,
                    name_result=name_result,
                    name_queries=queries,
                    mixture=mixture,
                )
                return self._remember_result(cache_key, cas_candidate, persistent_key)
        persistent_result = self._persistent_cache().get(persistent_key)
        persistent_failure_result: dict[str, Any] | None = None
        if persistent_result is not None:
            try:
                persistent_confidence = float(persistent_result.get("source_confidence") or 0.0)
            except (TypeError, ValueError):
                persistent_confidence = 0.0
            has_trusted_success = bool(
                str(persistent_result.get("url") or "").strip()
                and str(persistent_result.get("failure_reason") or "").strip() == ""
                and persistent_confidence >= 0.7
            )
            if has_trusted_success and str(persistent_result.get("raw_text") or "").strip():
                cached_relevance = self._result_relevance(
                    str(persistent_result["raw_text"]),
                    name=name,
                    cas=input_cas,
                    preferred_name=str(persistent_result.get("matched_site_name") or persistent_result.get("name") or ""),
                    validation_names=validation_names,
                )
                if not cached_relevance.get("passed"):
                    has_trusted_success = False
                    persistent_result["identity_conflict_reason"] = str(
                        cached_relevance.get("identity_conflict_reason") or "缓存来源名称不一致"
                    )
                    print("Ignoring cached chemical result because current identity validation rejected it.")
            if has_trusted_success:
                self._record_cache_metric("persistent", "hit")
                persistent_result["retrieval_status"] = "cache"
                print("Chemical lookup result: source=cache retrieval=cache")
                return self._remember_result(cache_key, persistent_result)
            # Do not let a short-lived negative cache entry hide a still-valid
            # compatible success saved under a prior parser/query revision.
            persistent_failure_result = persistent_result
        self._record_cache_metric("persistent", "miss")

        compatible_result = None
        compatible_names = list(dict.fromkeys([
            cleaned_name, standard_name, english_name, *aliases, *queries, name,
        ]))
        for compatible_name in compatible_names:
            compatible_result = self._persistent_cache().get_compatible_success(
                normalized_name=compatible_name,
                # ERP CAS is the identity constraint.  A CAS inferred by the
                # normalizer is only a name candidate and must not hide a
                # compatible no-CAS cache entry.
                cas=input_cas,
                max_age_days=int(((self.settings or {}).get("chemical_search", {}).get("cache", {}) or {}).get("success_ttl_days", 30) or 30),
            )
            if compatible_result is not None:
                break
        if compatible_result is not None:
            cached_raw_text = str(compatible_result.get("raw_text") or "")
            if cached_raw_text:
                cached_relevance = self._result_relevance(
                    cached_raw_text,
                    name=name,
                    cas=input_cas,
                    preferred_name=str(compatible_result.get("matched_site_name") or compatible_result.get("name") or ""),
                    validation_names=validation_names,
                )
                if not cached_relevance.get("passed"):
                    print("Ignoring compatible cache result because current identity validation rejected it.")
                    compatible_result = None
        if compatible_result is not None:
            compatible_result["retrieval_status"] = "compatible_cache"
            compatible_result["name_normalization"] = name_result
            compatible_result["query"] = cleaned_name or standard_name or english_name or name
            compatible_result["query_plan"] = {
                "cleaned_name": cleaned_name,
                "candidate_names": queries,
                "erp_cas": input_cas,
                "queries_attempted": ["compatible_success_cache"],
            }
            candidate_cas = self._extract_cas(str(compatible_result.get("candidate_cas") or compatible_result.get("cas") or ""))
            if not input_cas:
                # A name match is useful evidence, but never supplies an ERP CAS
                # implicitly.  This also covers older cache rows that predate the
                # candidate_cas field.
                if candidate_cas:
                    compatible_result["candidate_cas"] = candidate_cas
                verified_pubchem = (
                    str(compatible_result.get("source") or "") == "PubChem"
                    and str(compatible_result.get("pubchem_cid") or "")
                    and float(compatible_result.get("name_similarity") or 0) >= 0.9
                    and compatible_result.get("relevance_passed", False)
                )
                compatible_result["identity_status"] = "name_verified_pubchem" if verified_pubchem else "cas_missing"
                compatible_result["identity_decision_basis"] = "name_identity"
                compatible_result["need_manual_review"] = not verified_pubchem
            compatible_result.update(mixture)
            print("Chemical lookup result: source=compatible_cache retrieval=compatible_cache")
            return self._remember_result(cache_key, compatible_result, persistent_key)

        if persistent_failure_result is not None:
            self._record_cache_metric("persistent", "negative_hit")
            persistent_failure_result["retrieval_status"] = "negative_cache"
            enrichment = persistent_failure_result.get("identity_enrichment")
            if not isinstance(enrichment, dict):
                enrichment = self._resolve_missing_cas_identity(
                    raw_name=name,
                    input_cas=input_cas,
                    cleaned_name=cleaned_name,
                    standard_name=standard_name,
                    mixture=mixture,
                )
            enriched_result = self._search_enriched_identity(
                enrichment=enrichment,
                raw_name=name,
                name_result=name_result,
                mixture=mixture,
                failed_queries=["negative_cache"],
            )
            if enriched_result is not None:
                return self._remember_result(cache_key, enriched_result, persistent_key)
            persistent_failure_result["identity_enrichment"] = enrichment
            print("Chemical lookup result: source=cache retrieval=negative_cache")
            return self._remember_result(cache_key, persistent_failure_result, persistent_key)

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
            validation_names = self._validation_names(
                name,
                standard_name,
                cleaned_name,
                english_name,
                [*aliases, query],
            )
            result: dict[str, Any] | None = None
            for provider in self._provider_chain():
                candidate = self._run_provider(
                    provider,
                    name=query,
                    cas="" if name_queries else cas_no,
                    query=query,
                    validation_names=validation_names,
                )
                if not candidate:
                    continue
                candidate = self._reconcile_provider_identity(
                    candidate,
                    input_cas=input_cas,
                    query=query,
                    validation_names=validation_names,
                )
                if not candidate.get("relevance_passed", False):
                    reason = str(candidate.get("identity_conflict_reason") or "名称相关性不足")
                    print(f"Chemical source rejected by identity validation: {reason}")
                    continue
                result = candidate
                if result:
                    break
            if result:
                resolve_cas_identity()
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
                    elif self._cas_authoritative():
                        # This path is reached only after the direct PubChem CAS
                        # lookup did not resolve. Retain ERP CAS and use the
                        # verified name result as a documented fallback.
                        result.update({
                            "candidate_cas": name_candidate_cas,
                            "original_erp_cas": input_cas,
                            "cas_lookup_status": "unresolved_name_fallback",
                            "identity_status": "name_only",
                            "identity_decision_basis": "name_fallback_after_cas_unresolved",
                            "cas_correction_candidate": False,
                            "cas_correction_applied": False,
                        })
                        result.pop("corrected_cas", None)
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
                if input_cas and not self._cas_authoritative() and self._should_attempt_cas_correction(
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
                elif (
                    self._cas_authoritative()
                    and result.get("identity_status") == "name_only"
                    and str(result.get("source") or "") == "PubChem"
                    and str(result.get("pubchem_cid") or "")
                    and float(result.get("name_similarity") or 0.0) >= 0.9
                    and result.get("relevance_passed", False)
                ):
                    result["identity_status"] = "name_verified_pubchem"
                    result["need_manual_review"] = False
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
                if not input_cas:
                    candidate_cas = self._extract_cas(
                        str(result.get("candidate_cas") or result.get("cas") or "")
                    )
                    if candidate_cas:
                        # Preserve the completed name-normalization result, then make
                        # the missing ERP CAS explicit for review and downstream writes.
                        result["candidate_cas"] = candidate_cas
                        verified_pubchem = (
                            str(result.get("source") or "") == "PubChem"
                            and str(result.get("pubchem_cid") or "")
                            and float(result.get("name_similarity") or 0) >= 0.9
                            and result.get("relevance_passed", False)
                            and not mixture["is_mixture"]
                        )
                        result["identity_status"] = "name_verified_pubchem" if verified_pubchem else "cas_missing"
                        result["identity_decision_basis"] = "name_identity"
                        result["need_manual_review"] = not verified_pubchem
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

        enrichment = self._resolve_missing_cas_identity(
            raw_name=name,
            input_cas=input_cas,
            cleaned_name=cleaned_name,
            standard_name=standard_name,
            mixture=mixture,
        )
        enriched_result = self._search_enriched_identity(
            enrichment=enrichment,
            raw_name=name,
            name_result=name_result,
            mixture=mixture,
            failed_queries=failed_queries,
        )
        if enriched_result is not None:
            return self._remember_result(cache_key, enriched_result, persistent_key)

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
        manual["identity_enrichment"] = enrichment
        manual["manual_search_urls"] = self._manual_search_urls(search_name, cas_no)
        print(
            "Chemical lookup result: "
            f"source=none retrieval={manual['retrieval_status']} identity=unresolved mixture={str(mixture['is_mixture']).lower()}"
        )
        return self._remember_result(cache_key, manual, None if unavailable else persistent_key)

    def _resolve_missing_cas_identity(
        self,
        *,
        raw_name: str,
        input_cas: str,
        cleaned_name: str,
        standard_name: str,
        mixture: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve a candidate identity once per name during this run.

        This is deliberately called only after ordinary name lookup has failed.
        It therefore cannot add latency to known, well-indexed reagents.
        """
        if input_cas or mixture.get("is_mixture") or not str(raw_name or "").strip():
            return {"attempted": False, "status": "not_applicable", "reason": "已有 CAS、混合物或空名称不启用身份补全。"}
        key = (
            str(raw_name or "").strip().casefold(),
            str(cleaned_name or "").strip().casefold(),
            str(standard_name or "").strip().casefold(),
        )
        cached = self._identity_enrichment_cache.get(key)
        if cached is not None:
            self._record_cache_metric("identity_enrichment", "hit")
            return copy.deepcopy(cached)
        config = ((self.settings or {}).get("approval", {}) or {}).get("identity_enrichment", {}) or {}
        try:
            max_calls = max(1, min(100, int(config.get("max_calls_per_batch", 20))))
        except (TypeError, ValueError):
            max_calls = 20
        if self._identity_enrichment_calls >= max_calls:
            return {
                "attempted": False,
                "status": "budget_exhausted",
                "reason": f"本批身份补全已达到 {max_calls} 条上限。",
            }
        self._record_cache_metric("identity_enrichment", "miss")
        self._identity_enrichment_calls += 1
        resolved = ChemicalIdentityResolver(
            settings=self.settings,
            metrics=self.metrics,
            root_dir=Path(self.root_dir) if self.root_dir is not None else None,
        ).resolve(
            raw_name=raw_name,
            cleaned_name=cleaned_name,
            standard_name=standard_name,
        )
        print(
            "Chemical identity enrichment: "
            f"status={resolved.get('status') or 'unknown'} "
            f"english_candidate={str(bool(resolved.get('english_name'))).lower()} "
            f"cas_candidates={len(resolved.get('candidate_cas') or [])} "
            f"elapsed_ms={int(resolved.get('elapsed_ms') or 0)}"
        )
        self._identity_enrichment_cache[key] = copy.deepcopy(resolved)
        return resolved

    def _search_enriched_identity(
        self,
        *,
        enrichment: dict[str, Any],
        raw_name: str,
        name_result: dict[str, Any],
        mixture: dict[str, Any],
        failed_queries: list[str],
    ) -> dict[str, Any] | None:
        """Verify an LLM search candidate before allowing it into V1.

        A model-supplied CAS never becomes authoritative here.  The returned
        result exists only when a configured external source independently
        links an enriched English name or candidate CAS to that same CAS.
        """
        if str(enrichment.get("status") or "") != "resolved":
            return None
        english_name = str(enrichment.get("english_name") or "").strip()
        candidates = [
            candidate for candidate in (
                self._extract_cas(str(value or ""))
                for value in (enrichment.get("candidate_cas") or [])
            )
            if candidate
        ]
        if not english_name:
            return None
        candidates = list(dict.fromkeys(candidates))[:3]
        validation_names = self._validation_names(
            english_name,
            str(name_result.get("standard_name") or ""),
            str(name_result.get("cleaned_name") or ""),
            english_name,
            [raw_name, str(enrichment.get("resolved_standard_name") or "")],
        )
        # One English-name lookup is cheap and often returns the canonical CAS.
        # Only if it cannot confirm a model candidate do we spend one additional
        # lookup per candidate CAS.
        query_specs = [(english_name, "")]
        query_specs.extend((candidate, candidate) for candidate in candidates)
        for query, query_cas in query_specs:
            for provider in self._provider_chain():
                result = self._run_provider(
                    provider,
                    name=query,
                    cas=query_cas,
                    query=query,
                    validation_names=validation_names,
                )
                if not result:
                    continue
                result = self._reconcile_provider_identity(
                    result,
                    input_cas="",
                    query=query,
                    validation_names=validation_names,
                )
                verified_cas = self._extract_cas(str(result.get("candidate_cas") or result.get("cas") or ""))
                if not verified_cas or (candidates and verified_cas not in candidates):
                    continue
                if not self._trusted_name_verification(result):
                    continue
                result.update({
                    "cas": verified_cas,
                    "candidate_cas": verified_cas,
                    "identity_status": "verified_by_enrichment",
                    "identity_decision_basis": "enriched_name_and_external_source",
                    "need_manual_review": False,
                    "name_normalization": name_result,
                    "identity_enrichment": dict(enrichment),
                    "query": query,
                    "query_plan": {
                        "cleaned_name": str(name_result.get("cleaned_name") or ""),
                        "candidate_names": [english_name],
                        "erp_cas": "",
                        "queries_attempted": [*failed_queries, query],
                        "identity_enrichment_used": True,
                    },
                    "name_identity": self._identity_snapshot(result, query_kind="enriched_identity"),
                    "cas_identity": self._identity_snapshot(result, query_kind="enriched_cas"),
                })
                result.update(mixture)
                print(
                    "Chemical identity enrichment verified: "
                    f"source={result.get('source') or 'none'} candidate_cas={verified_cas}"
                )
                return result
        return None

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

    def _cas_authoritative(self) -> bool:
        """Whether a PubChem-resolved, checksum-valid ERP CAS is authoritative."""
        settings = (self.settings or {}).get("chemical_search", {}) or {}
        return bool(settings.get("cas_authoritative", False))

    def _finalize_cas_authoritative_result(
        self,
        result: dict[str, Any],
        *,
        input_cas: str,
        raw_name: str,
        name_result: dict[str, Any],
        name_queries: list[str],
        mixture: dict[str, Any],
    ) -> dict[str, Any]:
        """Mark a PubChem CAS lookup as the formal identity without mutating ERP CAS."""
        finalized = self._enrich_missing_official_fields(dict(result))
        matched_name = str(finalized.get("matched_site_name") or finalized.get("name") or "").strip()
        name_similarity = self._normalize_confidence(finalized.get("name_similarity"))
        name_is_informative = not self._non_informative_name_text(
            raw_name,
            name_result.get("cleaned_name", ""),
            name_result.get("standard_name", ""),
        )
        name_conflict = bool(name_is_informative and name_similarity < 0.82)
        finalized.update({
            "cas": input_cas,
            "candidate_cas": input_cas,
            "original_erp_cas": input_cas,
            "identity_status": "cas_verified",
            "identity_decision_basis": "cas_identity",
            "relevance_passed": True,
            "need_manual_review": False,
            "cas_matched": True,
            "cas_name_conflict": name_conflict,
            "identity_conflict_recorded": name_conflict,
            "name_identity": {
                "query_kind": "name",
                "name": raw_name,
                "cas": "",
                "source": "ERP / 名称标准化",
                "url": "",
                "status": "not_queried",
                "match_score": 0.0,
            },
            "cas_identity": self._identity_snapshot(finalized, query_kind="cas"),
            "name_normalization": name_result,
            "query": input_cas,
            "query_plan": {
                "cleaned_name": str(name_result.get("cleaned_name") or ""),
                "candidate_names": name_queries,
                "erp_cas": input_cas,
                "queries_attempted": [f"CAS:{input_cas}"],
                "cas_authoritative": True,
            },
        })
        # CAS is not corrected from a name result under this policy.  Preserve
        # both values for traceability when the recorded name does not agree.
        finalized.pop("corrected_cas", None)
        finalized["cas_correction_candidate"] = False
        finalized["cas_correction_applied"] = False
        if name_conflict:
            finalized["identity_conflict_reason"] = (
                f"ERP 名称“{raw_name}”与 PubChem CAS {input_cas} 对应物质“{matched_name or '-'}”不一致；"
                "按已验证 CAS 作为正式物化性质和 ERP 写入依据，冲突仅记录审计。"
            )
            finalized["failure_reason"] = self._append_reason(
                str(finalized.get("failure_reason") or ""),
                finalized["identity_conflict_reason"],
            )
        finalized.update(mixture)
        if mixture.get("is_mixture"):
            finalized["need_manual_review"] = True
            finalized["failure_reason"] = self._append_reason(
                str(finalized.get("failure_reason") or ""),
                "混合物或商品试剂需要使用已审核 SDS 及组分最高风险等级确认。",
            )
        return finalized

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
            manufacturer = str(reagent.get("manufacturer") or reagent.get("供应商") or reagent.get("生产厂家") or "")
            catalog_number = str(reagent.get("catalog_number") or reagent.get("货号") or "")
            sds_text = str(reagent.get("sds_text") or reagent.get("SDS文本") or "")
            erp_is_mixture = bool(reagent.get("is_mixture") or reagent.get("是否混合物"))
            key = (
                *self._cache_key(name, cas),
                manufacturer.strip().lower(),
                catalog_number.strip().lower(),
                hashlib.sha256(sds_text.encode("utf-8")).hexdigest()[:16] if sds_text else "",
            )
            ordered_keys.append(key)
            payloads.setdefault(key, {
                "reagent_name": name, "cas": cas,
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
            normalized = normalizer.normalize(name, cas=cas)
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
    ) -> tuple[str, str]:
        # Keep legacy optional parameters for callers, but package/size metadata
        # must not split chemical lookup or model work.
        return (
            str(reagent_name or "").strip().lower(),
            str(cas or "").strip().lower(),
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

    @staticmethod
    def _identity_elements(value: str) -> set[str]:
        """Return a small, conservative set of element identities in a name.

        Similarity scoring is useful for aliases, but words such as
        ``chloride`` and ``hexahydrate`` must never make iron and europium look
        like the same chemical entity.  This list intentionally covers the
        elements that routinely occur in the project's inorganic reagents; an
        absent element is treated as unknown rather than a mismatch.
        """
        text = str(value or "").casefold()
        aliases = {
            "aluminium": ("铝", "aluminum", "aluminium"),
            "barium": ("钡", "barium"),
            "calcium": ("钙", "calcium"),
            "chromium": ("铬", "chromium"),
            "cobalt": ("钴", "cobalt"),
            "copper": ("铜", "copper"),
            "europium": ("铕", "europium"),
            "iron": ("铁", "iron", "ferric", "ferrous"),
            "lead": ("铅", "lead"),
            "magnesium": ("镁", "magnesium"),
            "manganese": ("锰", "manganese"),
            "mercury": ("汞", "mercury"),
            "nickel": ("镍", "nickel"),
            "potassium": ("钾", "potassium"),
            "sodium": ("钠", "sodium"),
            "strontium": ("锶", "strontium"),
            "zinc": ("锌", "zinc"),
        }
        return {
            element
            for element, markers in aliases.items()
            if any(marker in text for marker in markers)
        }

    @staticmethod
    def _hydrate_counts(value: str) -> set[int]:
        text = str(value or "").casefold()
        chinese_numbers = {
            "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        }
        counts: set[int] = set()
        for match in re.finditer(r"([一二三四五六七八九十]|\d{1,2})水(?:合物?)?", text):
            token = match.group(1)
            counts.add(chinese_numbers.get(token, int(token) if token.isdigit() else 0))
        english_counts = {
            "mono": 1, "di": 2, "tri": 3, "tetra": 4, "penta": 5,
            "hexa": 6, "hepta": 7, "octa": 8, "nona": 9, "deca": 10,
        }
        for prefix, count in english_counts.items():
            if f"{prefix}hydrate" in text:
                counts.add(count)
        return {count for count in counts if count > 0}

    @classmethod
    def _identity_conflict_reason(cls, names: list[str], matched_name: str) -> str:
        expected_elements: set[str] = set()
        hydrate_counts: set[int] = set()
        for name in names:
            expected_elements.update(cls._identity_elements(name))
            hydrate_counts.update(cls._hydrate_counts(name))
        matched_elements = cls._identity_elements(matched_name)
        matched_hydrates = cls._hydrate_counts(matched_name)
        if expected_elements and matched_elements and expected_elements.isdisjoint(matched_elements):
            return "核心元素不一致"
        if len(hydrate_counts) > 1:
            return "输入名称与标准化名称的水合数不一致"
        if hydrate_counts and matched_hydrates and hydrate_counts.isdisjoint(matched_hydrates):
            return "水合数不一致"
        return ""

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

    @staticmethod
    def _hydrate_name_query_variants(raw_name: str, cleaned_name: str = "") -> list[str]:
        """Return safe Chinese hydrate spellings for supplier-name lookup.

        The result is deliberately a query expansion, never a canonical-name or
        CAS correction.  For example, `亚甲基蓝·三水` gains
        `亚甲基蓝三水合物` while the ERP source text is kept intact.
        """
        values: list[str] = []
        raw = str(raw_name or "").strip()
        match = re.match(r"^(.*?)[·・•]([一二三四五六七八九十百千万0-9]+)水(?:合物)?$", raw)
        if match and match.group(1).strip():
            values.append(f"{match.group(1).strip()}{match.group(2)}水合物")
        return [value for value in dict.fromkeys(values) if value and value != cleaned_name]

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
        raw_text = str(reconciled.get("raw_text") or "")
        # Providers may have ranked a result before the full name-normalization
        # context was available.  Re-run relevance whenever text exists so a
        # provider's optimistic flag cannot bypass element/hydrate checks.
        if raw_text:
            relevance = self._result_relevance(
                raw_text,
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
            if not reconciled.get("matched_site_name"):
                reconciled["matched_site_name"] = str(relevance.get("matched_site_name") or "")
            if relevance.get("identity_conflict_reason"):
                reconciled["identity_conflict_reason"] = str(relevance["identity_conflict_reason"])
                reconciled["need_manual_review"] = True
                reconciled["failure_reason"] = self._append_reason(
                    str(reconciled.get("failure_reason") or ""),
                    f"外部来源身份校验失败：{relevance['identity_conflict_reason']}。",
                )

        current_status = str(reconciled.get("identity_status") or "unresolved")
        if input_cas and candidate_cas and not self._same_cas(input_cas, candidate_cas):
            if self._cas_authoritative() and not self._same_cas(query, input_cas):
                # CAS resolution already failed. This is the configured name
                # fallback: preserve the ERP CAS and keep the discovered CAS as
                # evidence only, without creating a correction or review gate.
                name_relevance = self._result_relevance(
                    raw_text,
                    name=query,
                    cas="",
                    preferred_name=str(reconciled.get("matched_site_name") or reconciled.get("name") or ""),
                    validation_names=validation_names,
                )
                reconciled.update({
                    "candidate_cas": candidate_cas,
                    "original_erp_cas": input_cas,
                    "cas_lookup_status": "unresolved_name_fallback",
                    "identity_decision_basis": "name_fallback_after_cas_unresolved",
                    "identity_status": "name_only",
                    "relevance_passed": bool(name_relevance.get("relevance_passed")),
                    "name_similarity": max(
                        self._normalize_confidence(reconciled.get("name_similarity")),
                        self._normalize_confidence(name_relevance.get("name_similarity")),
                    ),
                    "need_manual_review": not bool(name_relevance.get("relevance_passed")),
                    "cas_correction_candidate": False,
                    "cas_correction_applied": False,
                })
                reconciled.pop("corrected_cas", None)
                return reconciled
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
        cas_authoritative_query = bool(cas_only_query and self._cas_authoritative())
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
            # In CAS-authoritative mode, an unresolved CAS permits a verified
            # name fallback but never a silent CAS correction.
            identity_status = "name_only" if (not cas or self._cas_authoritative()) else "conflict"
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
        if (
            identity_status == "cas_only"
            and name_similarity < 0.82
            and not cas_authoritative_query
            and not self._non_informative_name_text(*validation_names)
        ):
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
        cas_only_name_validated = identity_status == "cas_only" and (
            name_similarity >= 0.9 or cas_authoritative_query
        )
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
        providers = config["providers"]

        # The initial PubChem request reads Safety and Hazards. A separate
        # physical-properties view is only requested when a relevant field is missing.
        pubchem_cid = str(enriched.get("pubchem_cid") or "").strip()
        if pubchem_cid and "pubchem" in providers and self._enrichment_request_allowed(state):
            items, diagnostic = self._pubchem_physical_evidence(pubchem_cid, state)
            evidence.extend(items)
            diagnostics.append(diagnostic)

        missing = self._missing_property_fields(evidence, config["fields"])
        if "boiling_point" in missing and "nist" in providers and self._enrichment_request_allowed(state):
            self._enrichment_request_started(state)
            items, diagnostic = self._nist_evidence(verified_cas)
            evidence.extend(items)
            diagnostics.append(diagnostic)

        # Supplier sites are only followed from the already identity-verified
        # detail page. No new site search or browser interaction is introduced.
        source_key = str(enriched.get("source") or "").strip().lower()
        if source_key in providers:
            for url in enriched.get("property_detail_links") or []:
                if not self._enrichment_request_allowed(state):
                    break
                self._enrichment_request_started(state)
                items, diagnostic = self._linked_property_evidence(
                    url=str(url), parent=enriched, verified_cas=verified_cas,
                )
                evidence.extend(items)
                diagnostics.append(diagnostic)
                if not self._missing_property_fields(evidence, config["fields"]):
                    break

        merged = self._merge_property_evidence(evidence)
        selected = {
            str(item.get("field") or ""): item
            for item in merged
            if item.get("selected") and str(item.get("field") or "") in config["fields"]
        }
        enriched["evidence_items"] = merged
        enriched["selected_property_evidence"] = selected
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
        items = self._chemsrc_chinese_property_evidence(
            raw_html,
            source_url=url,
            source_updated_at=source_updated_at,
        )
        if not items:
            items = self._property_evidence_from_text(raw_text, source=source, source_url=url, source_updated_at=source_updated_at)
        return items, {"provider": f"{source} property enrichment", "status": "success" if items else "not_found", "attempts": 1, "elapsed_ms": int((time.monotonic() - started) * 1000)}

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
            ranked = sorted(values, key=cls._property_evidence_sort_key, reverse=True)
            for position, item in enumerate(ranked):
                item["conflict"] = conflict
                item["selected"] = position == 0
                if conflict:
                    item["selection_basis"] = "latest_source_selected"
                output.append(item)
        return output

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
                    r'href="([^"]*/ProductChemicalPropertiesCB[^\"?#]*_(?:CN|EN)\.htm)"',
                    r'href="([^"]*/ProductChemicalPropertiesCB[^\"?#]*\.htm)"',
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
                    response_headers = response.headers
                    last_modified = response_headers.get("Last-Modified") if hasattr(response_headers, "get") else ""
                    self._response_metadata[response_key] = {
                        "last_modified": self._normalize_http_date(str(last_modified or "")),
                        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
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

    @staticmethod
    def _normalize_http_date(value: str) -> str:
        if not value:
            return ""
        try:
            return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, IndexError):
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
                result["property_detail_links"] = self._property_detail_links(raw_html, candidate.url, source)
                chinese_detail_url = self._chemsrc_chinese_detail_url(
                    candidate.url,
                    page_cas or cas,
                    source,
                )
                if chinese_detail_url:
                    # The English baike page often exposes only the SDS link. Its
                    # Chinese sibling has server-rendered property sections, so use
                    # it first while retaining the existing request/time limits.
                    result["property_detail_links"] = [
                        chinese_detail_url,
                        *[link for link in result["property_detail_links"] if link != chinese_detail_url],
                    ]
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
    def _property_detail_links(page_html: str, base_url: str, source: str) -> list[str]:
        if str(source or "").strip().lower() not in {"chemicalbook", "chemsrc"}:
            return []
        links: list[str] = []
        for match in re.finditer(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page_html or ""):
            href, label_html = match.groups()
            label = ChemicalSearcher._html_to_text(label_html).lower()
            if not any(token in label for token in ("msds", "sds", "physical", "property", "物理", "物化", "安全数据", "毒性", "生态")):
                continue
            url = urljoin(base_url, html.unescape(href))
            if url not in links:
                links.append(url)
            if len(links) >= 3:
                break
        return links

    @staticmethod
    def _chemsrc_chinese_detail_url(detail_url: str, cas: str, source: str) -> str:
        """Derive the same-record Chinese Chemsrc detail page from a verified baike URL."""
        if str(source or "").strip().lower() != "chemsrc":
            return ""
        normalized_cas = ChemicalSearcher._extract_cas(str(cas or ""))
        match = re.search(r"/en/baike/(\d+)\.html(?:$|[?#])", str(detail_url or ""), flags=re.I)
        if not normalized_cas or not match:
            return ""
        return f"https://www.chemsrc.com/cas/{normalized_cas}_{match.group(1)}.html"

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

    @staticmethod
    def _is_valid_cas(value: str) -> bool:
        """Validate a CAS Registry Number's layout and checksum before trusting ERP input."""
        match = re.fullmatch(r"(\d{2,7})-(\d{2})-(\d)", str(value or "").strip())
        if not match:
            return False
        body = f"{match.group(1)}{match.group(2)}"
        check_digit = sum(int(digit) * (index + 1) for index, digit in enumerate(reversed(body))) % 10
        return check_digit == int(match.group(3))

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
            identity_conflict_reason = self._identity_conflict_reason([name, *names_for_validation], best_name)
            relevance_passed = bool((name_is_non_informative or best_score >= 0.82) and not identity_conflict_reason)
            return {
                "relevance_passed": relevance_passed,
                "matched_site_name": best_name,
                "name_similarity": 1.0 if name_is_non_informative else round(best_score, 3),
                "passed": relevance_passed,
                "cas_matched": True,
                "name_verification_required": not name_is_non_informative,
                "identity_conflict_reason": identity_conflict_reason,
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

        # A zero similarity score must not discard the provider title: it can
        # still prove a hard conflict such as iron versus europium.
        identity_name = best_name or self._clean_site_name(preferred_name)
        identity_conflict_reason = self._identity_conflict_reason([name, *names_for_validation], identity_name)
        relevance_passed = best_score >= 0.82 and not identity_conflict_reason
        if primary_names and primary_score < 0.82:
            relevance_passed = False
        return {
            "relevance_passed": relevance_passed,
            "matched_site_name": best_name or identity_name,
            "name_similarity": round(best_score, 3),
            "passed": relevance_passed,
            "identity_conflict_reason": identity_conflict_reason,
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
