from __future__ import annotations

from dataclasses import dataclass
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
        classification = rule_engine.classify_resolved_properties(
            properties,
            name=resolved_identity.standard_name_cn or resolved_identity.cleaned_base_name,
            cas=resolved_identity.cas,
            identity_status=resolved_identity.status,
        )
        return {
            "identity": resolved_identity.to_dict(),
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
            "shadow_only": self.shadow_mode or not self.enabled,
        }

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
            "identity_status": str((evaluation.get("identity") or {}).get("status") or "unresolved"),
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
            resolved_evidence = resolved_items[index]
            provider_evidence = [item for item in (sds_items[index], resolved_evidence, fetched_by_position.get(index), *supplements_by_position[index]) if item is not None]
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
            classification = rule_engine.classify_resolved_properties(
                properties,
                name=resolved_identity.standard_name_cn or resolved_identity.cleaned_base_name,
                cas=resolved_identity.cas,
                identity_status=resolved_identity.status,
            )
            evaluations.append({
                "identity": resolved_identity.to_dict(),
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
                "shadow_only": self.shadow_mode or not self.enabled,
            })
        return evaluations
