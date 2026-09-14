from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Iterable

from chemical_sources.base import ChemicalSourceAdapter, ProviderEvidence
from chemical_sources.pubchem import PubChemAdapter
from chemical_sources.supplier_sds import SupplierSdsAdapter
from chemical_sources.nist import NistWebBookAdapter
from chemical_sources.comptox import CompToxAdapter
from evidence_resolver import EvidenceResolver
from reagent_identity import IdentityRecord, ReagentIdentityResolver
from rule_engine import RuleEngine
from llm_extractor import LlmExtractor


@dataclass
class EnrichmentV2:
    """Evidence-first enrichment implementation used in shadow mode initially."""

    settings: dict[str, Any] | None = None
    root_dir: Path | None = None
    source: ChemicalSourceAdapter | None = None
    supplier_sds_source: SupplierSdsAdapter | None = None
    supplementary_sources: list[Any] | None = None
    llm_extractor: LlmExtractor | None = None

    def __post_init__(self) -> None:
        self.root_dir = self.root_dir or Path(__file__).resolve().parents[1]
        config = (self.settings or {}).get("enrichment_v2", {}) or {}
        provider_config = (config.get("providers", {}) or {}).get("pubchem", {}) or {}
        self.identity_resolver = ReagentIdentityResolver(settings=self.settings, root_dir=self.root_dir)
        self.evidence_resolver = EvidenceResolver()
        llm_config = (config.get("llm", {}) or {})
        self.llm_enabled = bool(llm_config.get("enabled", False))
        self.llm_extractor = self.llm_extractor or LlmExtractor(settings=self.settings)
        self._identity_opinion_cache: dict[str, dict[str, Any]] = {}
        self.source = self.source or PubChemAdapter(
            timeout_seconds=float(provider_config.get("read_timeout_seconds", 10)),
            max_workers=int(provider_config.get("max_concurrency", 2)),
            include_hazard_details=bool(provider_config.get("include_hazard_details", False)),
        )
        self.supplier_sds_source = self.supplier_sds_source or SupplierSdsAdapter()
        if self.supplementary_sources is None:
            providers = (config.get("providers", {}) or {})
            supplements: list[Any] = []
            nist = providers.get("nist", {}) or {}
            if nist.get("enabled", False):
                supplements.append(NistWebBookAdapter(timeout_seconds=float(nist.get("read_timeout_seconds", 8))))
            comptox = providers.get("comptox", {}) or {}
            if comptox.get("enabled", False):
                supplements.append(CompToxAdapter(api_key_env=str(comptox.get("api_key_env", "EPA_CTX_API_KEY"))))
            self.supplementary_sources = supplements

    @property
    def enabled(self) -> bool:
        config = (self.settings or {}).get("enrichment_v2", {}) or {}
        return bool(config.get("enabled", False))

    @property
    def shadow_mode(self) -> bool:
        config = (self.settings or {}).get("enrichment_v2", {}) or {}
        return bool(config.get("shadow_mode", True))

    def evaluate(self, reagent: dict[str, Any], rule_engine: RuleEngine) -> dict[str, Any]:
        erp_cas = str(reagent.get("CAS号") or reagent.get("cas") or "").strip()
        identity = self.identity_resolver.resolve(
            str(reagent.get("试剂名称") or reagent.get("reagent_name") or reagent.get("name") or ""),
            cas=str(reagent.get("CAS号") or reagent.get("cas") or ""),
            specification=str(reagent.get("规格") or reagent.get("specification") or ""),
            unit=str(reagent.get("规格单位") or reagent.get("unit") or ""),
        )
        sds_evidence = self.supplier_sds_source.extract(identity, reagent)
        resolved_evidence = self.source.resolve_many([identity])[0]
        provider_evidence: list[ProviderEvidence] = [item for item in (sds_evidence, resolved_evidence) if item is not None]
        resolved_identity = resolved_evidence.identity
        identity_resolution = self._resolve_identity_resolution(identity, resolved_evidence, erp_cas)
        candidate_evidence = self._hydrate_identity_candidates(identity, identity_resolution)
        provider_evidence.extend(candidate_evidence)
        if (
            resolved_evidence.diagnostic
            and resolved_evidence.diagnostic.status in {"verified", "name_only", "success"}
            and resolved_identity.provider_id("pubchem_cid")
        ):
            provider_evidence.extend(self.source.fetch_evidence_many([resolved_identity]))
        for source in self.supplementary_sources or []:
            provider_evidence.extend(source.fetch_evidence_many([resolved_identity]))

        legacy_items = [
            {"field": field, "value": value, "source": evidence.source, "source_url": evidence.source_url}
            for evidence in provider_evidence
            for field, value in evidence.fields.items()
            if field not in {"pubchem_cid", "detail"}
        ]
        normalized_evidence = self.evidence_resolver.normalize_legacy_items(legacy_items)
        properties = self.evidence_resolver.resolve(normalized_evidence)
        if self.llm_enabled and properties.missing_decision_fields():
            trusted_text = "\n".join(
                str(evidence.fields[field])
                for evidence in provider_evidence
                if evidence.source != "LLM"
                for field in evidence.fields
                if field not in {"detail", "pubchem_cid"}
            )[:12000]
            if trusted_text.strip():
                llm_fields = self.llm_extractor.extract_missing_fields(
                    trusted_text,
                    properties.missing_decision_fields(),
                    name=resolved_identity.standard_name_cn or resolved_identity.cleaned_base_name,
                    cas=resolved_identity.cas,
                )
                llm_items = [
                    {"field": field, "value": value, "source": "LLM", "evidence_span": " | ".join(llm_fields.get("evidence") or [])}
                    for field, value in llm_fields.items()
                    if field in properties.missing_decision_fields() and value not in (None, "", [])
                ]
                if llm_items:
                    normalized_evidence.extend(self.evidence_resolver.normalize_legacy_items(llm_items))
                    properties = self.evidence_resolver.resolve(normalized_evidence)
        identity_status = str(identity_resolution.get("status") or resolved_identity.status)
        classification = rule_engine.classify_resolved_properties(
            properties,
            name=resolved_identity.standard_name_cn or resolved_identity.cleaned_base_name,
            cas=resolved_identity.cas,
            identity_status=identity_status,
        )
        self._force_identity_manual_review(classification, identity_resolution)
        llm_second_opinion = self._identity_second_opinion(
            reagent,
            identity,
            identity_resolution,
            provider_evidence,
            properties,
            classification,
            rule_engine,
        )
        return {
            "identity": resolved_identity.to_dict(),
            "identity_resolution": identity_resolution,
            "properties": {
                field: {"value": value.value, "unit": value.unit, "status": value.status, "evidence_refs": list(value.evidence_refs)}
                for field, value in properties.fields.items()
            },
            "evidence": [item.to_dict() for item in normalized_evidence],
            "provider_diagnostics": [
                {
                    "provider": evidence.diagnostic.provider,
                    "status": evidence.diagnostic.status,
                    "elapsed_ms": evidence.diagnostic.elapsed_ms,
                    "failure_kind": evidence.diagnostic.failure_kind,
                }
                for evidence in provider_evidence
                if evidence.diagnostic is not None
            ],
            "classification": classification,
            "llm_second_opinion": llm_second_opinion,
            "shadow_only": self.shadow_mode or not self.enabled,
        }

    def _resolve_identity_resolution(
        self,
        identity: IdentityRecord,
        resolved_evidence: ProviderEvidence,
        erp_cas: str,
    ) -> dict[str, Any]:
        resolver = getattr(self.source, "resolve_identity_pair", None)
        if callable(resolver):
            try:
                result = dict(resolver(identity) or {})
            except Exception as error:  # identity diagnostics must not block review
                result = {"status": "unresolved", "diagnostics": {"error": str(error)}}
        else:
            diagnostic = resolved_evidence.diagnostic
            status = "cas_missing" if not erp_cas else (
                "conflict" if diagnostic and diagnostic.status == "conflict" else
                ("verified" if str(resolved_evidence.identity.status) == "verified" else "unresolved")
            )
            result = {"status": status, "diagnostics": {}}
        result["erp_cas"] = erp_cas
        result.setdefault("name_identity", result.pop("name_candidate", None))
        result.setdefault("cas_identity", result.pop("cas_candidate", None))
        if not erp_cas:
            result["status"] = "cas_missing"
        diagnostic_status = resolved_evidence.diagnostic.status if resolved_evidence.diagnostic else ""
        if result.get("status") == "verified" and diagnostic_status:
            if diagnostic_status == "conflict":
                result["status"] = "conflict"
        result.setdefault("name_candidates", [])
        result.setdefault("cas_candidates", [])
        result.setdefault("diagnostics", {})
        result["identity_key"] = "|".join(
            [str(identity.standard_name_cn or identity.cleaned_base_name or identity.raw_name), erp_cas]
        )
        return result

    def _hydrate_identity_candidates(
        self,
        identity: IdentityRecord,
        resolution: dict[str, Any],
    ) -> list[ProviderEvidence]:
        candidates = [*resolution.get("name_candidates", []), *resolution.get("cas_candidates", [])]
        records: list[IdentityRecord] = []
        seen: set[str] = set()
        for candidate in candidates:
            cid = str(candidate.get("cid") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            records.append(replace(identity, status="name_only", provider_ids=(("pubchem_cid", cid),)))
        if not records or not hasattr(self.source, "fetch_evidence_many"):
            return []
        try:
            fetched = self.source.fetch_evidence_many(records)
        except Exception:
            return []
        by_cid = {str(item.identity.provider_id("pubchem_cid")): item for item in fetched if item is not None}
        for candidate in candidates:
            item = by_cid.get(str(candidate.get("cid") or ""))
            if item is not None:
                candidate["name"] = str(item.fields.get("name") or item.fields.get("iupac_name") or "")
                candidate["fields"] = {key: value for key, value in item.fields.items() if key != "pubchem_cid"}
                candidate["source_url"] = item.source_url
        for key in ("name_identity", "cas_identity"):
            selected = resolution.get(key)
            if isinstance(selected, dict):
                match = next((item for item in candidates if item.get("cid") == selected.get("cid")), None)
                if match:
                    selected.update(match)
        return fetched

    @staticmethod
    def _force_identity_manual_review(classification: dict[str, Any], resolution: dict[str, Any]) -> None:
        status = str(resolution.get("status") or "unresolved")
        if status in {"cas_missing", "conflict", "ambiguous", "unresolved"}:
            classification["need_manual_review"] = True
            warning = f"identity_status:{status}"
            trace = classification.setdefault("decision_trace", {})
            warnings = trace.setdefault("warnings", [])
            if warning not in warnings:
                warnings.append(warning)
            classification["reason"] = (
                f"{classification.get('reason') or ''} 身份状态为 {status}，必须人工复核。"
            ).strip()

    def _identity_second_opinion(
        self,
        reagent: dict[str, Any],
        identity: IdentityRecord,
        resolution: dict[str, Any],
        provider_evidence: list[ProviderEvidence],
        properties: Any,
        classification: dict[str, Any],
        rule_engine: RuleEngine,
    ) -> dict[str, Any]:
        approval = (self.settings or {}).get("approval", {}) or {}
        config = approval.get("llm_identity_review", {}) or {}
        status = str(resolution.get("status") or "unresolved")
        trigger = (status == "cas_missing" and config.get("trigger_on_cas_missing", True)) or (
            status == "conflict" and config.get("trigger_on_name_cas_conflict", True)
        )
        base = {"enabled": False, "used_llm": False, "advisory_only": True, "must_manual_review": True, "trigger_status": status}
        if not config.get("enabled", True) or not trigger or not getattr(self.llm_extractor, "generate_identity_second_opinion", None):
            return base
        cache_key = f"{resolution.get('identity_key', '')}|{status}"
        if cache_key in self._identity_opinion_cache:
            return dict(self._identity_opinion_cache[cache_key])
        evidence = [item.to_dict() for item in self.evidence_resolver.normalize_legacy_items([
            {"field": field, "value": value, "source": item.source, "source_url": item.source_url}
            for item in provider_evidence
            for field, value in item.fields.items()
            if field not in {"detail", "pubchem_cid"}
        ])]
        rule_summary = self._rule_summary(rule_engine)
        rules_fingerprint = __import__("hashlib").sha256(json.dumps(rule_summary, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        opinion = self.llm_extractor.generate_identity_second_opinion({
            "raw_name": reagent.get("试剂名称") or reagent.get("reagent_name") or reagent.get("name") or "",
            "standard_name": identity.standard_name_cn,
            "cleaned_name": identity.cleaned_base_name,
            "english_name": identity.standard_name_en,
            "aliases": list(identity.aliases),
            "cas": resolution.get("erp_cas", ""),
            "name_identity": resolution.get("name_identity"),
            "cas_identity": resolution.get("cas_identity"),
            "identity_resolution": resolution,
            "evidence": evidence,
            "properties": {key: value.value for key, value in properties.fields.items()},
            "classification": classification,
            "rule_summary": rule_summary,
            "allowed_categories": list(getattr(rule_engine, "priority", []) or []),
            "rules_fingerprint": rules_fingerprint,
            "allow_model_knowledge": bool(config.get("allow_model_knowledge", True)),
        })
        self._identity_opinion_cache[cache_key] = dict(opinion)
        return opinion

    @staticmethod
    def _rule_summary(rule_engine: RuleEngine) -> list[dict[str, str]]:
        return [
            {"category": str(rule.category), "explanation": str(rule.explanation or "")[:300]}
            for rule in getattr(rule_engine, "rules", [])
        ]

    def compare_legacy(
        self,
        legacy: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> dict[str, Any]:
        legacy_category = str(legacy.get("最终建议类别") or legacy.get("final_category") or "").strip()
        current = evaluation.get("classification") or {}
        v2_category = str(current.get("final_category") or "").strip()
        return {
            "legacy_category": legacy_category,
            "v2_category": v2_category,
            "same_category": legacy_category == v2_category,
            "legacy_manual_review": bool(legacy.get("需人工复核") or legacy.get("need_manual_review", False)),
            "v2_manual_review": bool(current.get("need_manual_review", True)),
            "identity_status": str(
                (evaluation.get("identity_resolution") or {}).get("status")
                or (evaluation.get("identity") or {}).get("status")
                or "unresolved"
            ),
            "difference_reason": "same_category" if legacy_category == v2_category else "category_or_evidence_changed",
        }

    def evaluate_many(self, reagents: Iterable[dict[str, Any]], rule_engine: RuleEngine) -> list[dict[str, Any]]:
        rows = list(reagents)
        if len(rows) <= 1:
            return [self.evaluate(row, rule_engine) for row in rows]

        identities: list[IdentityRecord] = []
        sds_items: list[ProviderEvidence | None] = []
        for reagent in rows:
            identity = self.identity_resolver.resolve(
                str(reagent.get("试剂名称") or reagent.get("reagent_name") or reagent.get("name") or ""),
                cas=str(reagent.get("CAS号") or reagent.get("cas") or ""),
                specification=str(reagent.get("规格") or reagent.get("specification") or ""),
                unit=str(reagent.get("规格单位") or reagent.get("unit") or ""),
            )
            identities.append(identity)
            sds_items.append(self.supplier_sds_source.extract(identity, reagent))

        resolved_items = self.source.resolve_many(identities)
        fetch_positions = [
            index for index, evidence in enumerate(resolved_items)
            if evidence.diagnostic
            and evidence.diagnostic.status in {"verified", "name_only", "success"}
            and evidence.identity.provider_id("pubchem_cid")
        ]
        fetched_by_position: dict[int, ProviderEvidence] = {}
        if fetch_positions:
            fetched = self.source.fetch_evidence_many([resolved_items[index].identity for index in fetch_positions])
            fetched_by_position.update({index: evidence for index, evidence in zip(fetch_positions, fetched)})

        supplements_by_position: dict[int, list[ProviderEvidence]] = {index: [] for index in range(len(rows))}
        for source in self.supplementary_sources or []:
            supplemented = source.fetch_evidence_many([item.identity for item in resolved_items])
            for index, evidence in enumerate(supplemented):
                supplements_by_position[index].append(evidence)

        evaluations: list[dict[str, Any]] = []
        for index, reagent in enumerate(rows):
            erp_cas = str(reagent.get("CAS号") or reagent.get("cas") or "").strip()
            resolved_evidence = resolved_items[index]
            provider_evidence = [item for item in (sds_items[index], resolved_evidence, fetched_by_position.get(index), *supplements_by_position[index]) if item is not None]
            identity_resolution = self._resolve_identity_resolution(identities[index], resolved_evidence, erp_cas)
            provider_evidence.extend(self._hydrate_identity_candidates(identities[index], identity_resolution))
            legacy_items = [
                {"field": field, "value": value, "source": evidence.source, "source_url": evidence.source_url}
                for evidence in provider_evidence
                for field, value in evidence.fields.items()
                if field not in {"pubchem_cid", "detail"}
            ]
            normalized_evidence = self.evidence_resolver.normalize_legacy_items(legacy_items)
            properties = self.evidence_resolver.resolve(normalized_evidence)
            if self.llm_enabled and properties.missing_decision_fields():
                trusted_text = "\n".join(
                    str(evidence.fields[field])
                    for evidence in provider_evidence
                    if evidence.source != "LLM"
                    for field in evidence.fields
                    if field not in {"detail", "pubchem_cid"}
                )[:12000]
                if trusted_text.strip():
                    missing = properties.missing_decision_fields()
                    llm_fields = self.llm_extractor.extract_missing_fields(
                        trusted_text,
                        missing,
                        name=resolved_evidence.identity.standard_name_cn or resolved_evidence.identity.cleaned_base_name,
                        cas=resolved_evidence.identity.cas,
                    )
                    llm_items = [
                        {"field": field, "value": value, "source": "LLM", "evidence_span": " | ".join(llm_fields.get("evidence") or [])}
                        for field, value in llm_fields.items()
                        if field in missing and value not in (None, "", [])
                    ]
                    if llm_items:
                        normalized_evidence.extend(self.evidence_resolver.normalize_legacy_items(llm_items))
                        properties = self.evidence_resolver.resolve(normalized_evidence)
            resolved_identity = resolved_evidence.identity
            identity_status = str(identity_resolution.get("status") or resolved_identity.status)
            classification = rule_engine.classify_resolved_properties(
                properties,
                name=resolved_identity.standard_name_cn or resolved_identity.cleaned_base_name,
                cas=resolved_identity.cas,
                identity_status=identity_status,
            )
            self._force_identity_manual_review(classification, identity_resolution)
            llm_second_opinion = self._identity_second_opinion(
                reagent,
                identities[index],
                identity_resolution,
                provider_evidence,
                properties,
                classification,
                rule_engine,
            )
            evaluations.append({
                "identity": resolved_identity.to_dict(),
                "identity_resolution": identity_resolution,
                "properties": {
                    field: {"value": value.value, "unit": value.unit, "status": value.status, "evidence_refs": list(value.evidence_refs)}
                    for field, value in properties.fields.items()
                },
                "evidence": [item.to_dict() for item in normalized_evidence],
                "provider_diagnostics": [
                    {"provider": evidence.diagnostic.provider, "status": evidence.diagnostic.status, "elapsed_ms": evidence.diagnostic.elapsed_ms, "failure_kind": evidence.diagnostic.failure_kind}
                    for evidence in provider_evidence if evidence.diagnostic is not None
                ],
                "classification": classification,
                "llm_second_opinion": llm_second_opinion,
                "shadow_only": self.shadow_mode or not self.enabled,
            })
        return evaluations
