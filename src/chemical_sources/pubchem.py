from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from chemical_sources.base import ProviderDiagnostic, ProviderEvidence
from reagent_identity import IdentityRecord


class PubChemAdapter:
    """Small, batch-oriented PUG REST adapter for the V2 enrichment path.

    Name searches remain individual because PUG REST treats names as individual
    identifiers.  Once CIDs are resolved, descriptor retrieval is genuinely
    batched, which avoids one detail request per reagent.
    """

    name = "PubChem"
    PROPERTY_FIELDS = "Title,IUPACName,MolecularFormula,MolecularWeight,CanonicalSMILES"

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_workers: int = 2,
        chunk_size: int = 50,
        include_hazard_details: bool = False,
    ) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_workers = max(1, min(8, int(max_workers)))
        self.chunk_size = max(1, min(100, int(chunk_size)))
        self.include_hazard_details = bool(include_hazard_details)

    def resolve_many(self, identities: Sequence[IdentityRecord]) -> list[ProviderEvidence]:
        results: list[ProviderEvidence | None] = [None] * len(identities)
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="pubchem-id") as executor:
            futures = {
                executor.submit(self._resolve_one, identity): index
                for index, identity in enumerate(identities)
                if identity.status in {"verified", "name_only"}
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as error:  # defensive boundary around provider failures
                    results[index] = self._empty(identities[index], "unavailable", "exception", str(error))
        return [result if result is not None else self._empty(identity, "not_found") for identity, result in zip(identities, results)]

    def resolve_identity_pair(self, identity: IdentityRecord) -> dict[str, Any]:
        """Resolve the name and CAS independently for audit/review purposes.

        The ordinary resolver intentionally returns one provider identity.  Review
        workflows need both sides of a possible mismatch, so this method keeps
        the candidate CID sets separate and never chooses a side.
        """
        name_query = identity.standard_name_en or identity.standard_name_cn or identity.cleaned_base_name
        name_cids, name_failure = self._cids(name_query) if name_query else ([], "")
        cas_query = identity.cas or ""
        cas_cids, cas_failure = self._cids(cas_query) if cas_query else ([], "")
        name_candidate = self._identity_candidate(identity, "name", name_query, name_cids)
        cas_candidate = self._identity_candidate(identity, "cas", cas_query, cas_cids)
        if not cas_query:
            status = "cas_missing"
        elif name_cids and cas_cids and not set(name_cids).intersection(cas_cids):
            status = "conflict"
        elif len(name_cids) > 1 or len(cas_cids) > 1:
            status = "ambiguous"
        elif name_cids and cas_cids:
            status = "verified"
        elif name_cids or cas_cids:
            status = "unresolved"
        else:
            status = "unresolved"
        return {
            "status": status,
            "name_query": name_query,
            "cas_query": cas_query,
            "name_candidate": name_candidate,
            "cas_candidate": cas_candidate,
            "name_candidates": [self._candidate_dict(identity, "name", name_query, cid) for cid in name_cids],
            "cas_candidates": [self._candidate_dict(identity, "cas", cas_query, cid) for cid in cas_cids],
            "diagnostics": {
                "name_failure_kind": name_failure,
                "cas_failure_kind": cas_failure,
            },
        }

    @staticmethod
    def _candidate_dict(identity: IdentityRecord, source: str, query: str, cid: str) -> dict[str, str]:
        return {
            "source": source,
            "query": query,
            "cid": str(cid),
            "cas": identity.cas if source == "cas" else "",
            "name": "",
            "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}" if cid else "",
        }

    def _identity_candidate(
        self,
        identity: IdentityRecord,
        source: str,
        query: str,
        cids: list[str],
    ) -> dict[str, Any] | None:
        return self._candidate_dict(identity, source, query, cids[0]) if cids else None

    def fetch_evidence_many(self, identities: Sequence[IdentityRecord]) -> list[ProviderEvidence]:
        by_cid = {identity.provider_id("pubchem_cid"): identity for identity in identities if identity.provider_id("pubchem_cid")}
        results: dict[str, ProviderEvidence] = {}
        for cids in self._chunks(list(by_cid), self.chunk_size):
            started = time.monotonic()
            data, failure_kind = self._get_json(
                "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
                f"{','.join(cids)}/property/{self.PROPERTY_FIELDS}/JSON"
            )
            properties = ((data.get("PropertyTable") or {}).get("Properties") or []) if data else []
            elapsed_ms = int((time.monotonic() - started) * 1000)
            returned = {str(row.get("CID") or ""): row for row in properties if isinstance(row, dict)}
            hazard_results: dict[str, tuple[dict[str, str], str]] = {}
            if self.include_hazard_details:
                with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="pubchem-view") as executor:
                    futures = {executor.submit(self._hazard_fields, cid): cid for cid in cids if cid in returned}
                    for future in as_completed(futures):
                        cid = futures[future]
                        try:
                            hazard_results[cid] = future.result()
                        except Exception:
                            hazard_results[cid] = ({}, "network_error")
            for cid in cids:
                identity = by_cid[cid]
                row = returned.get(cid)
                if not row:
                    results[cid] = self._empty(identity, "unavailable" if failure_kind else "not_found", failure_kind, elapsed_ms=elapsed_ms)
                    continue
                fields = {
                    "name": str(row.get("Title") or ""),
                    "iupac_name": str(row.get("IUPACName") or ""),
                    "molecular_formula": str(row.get("MolecularFormula") or ""),
                    "molecular_weight": str(row.get("MolecularWeight") or ""),
                    "canonical_smiles": str(row.get("ConnectivitySMILES") or row.get("CanonicalSMILES") or ""),
                }
                if self.include_hazard_details:
                    hazard_fields, hazard_failure = hazard_results.get(cid, ({}, "network_error"))
                    fields.update(hazard_fields)
                    if hazard_failure and not failure_kind:
                        failure_kind = hazard_failure
                results[cid] = ProviderEvidence(
                    identity=identity,
                    source=self.name,
                    source_url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                    fields={key: value for key, value in fields.items() if value},
                    raw=row,
                    diagnostic=ProviderDiagnostic(self.name, "success", elapsed_ms),
                )
        return [results.get(identity.provider_id("pubchem_cid"), self._empty(identity, "not_found")) for identity in identities]

    def _resolve_one(self, identity: IdentityRecord) -> ProviderEvidence:
        started = time.monotonic()
        cas_cids, cas_failure = self._cids(identity.cas) if identity.cas else ([], "")
        name_query = identity.standard_name_en or identity.standard_name_cn or identity.cleaned_base_name
        name_cids, name_failure = self._cids(name_query) if name_query else ([], "")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        failure_kind = cas_failure or name_failure
        if cas_cids and name_cids:
            common = next((cid for cid in name_cids if cid in set(cas_cids)), "")
            if not common:
                return self._empty(identity, "conflict", "identity_conflict", elapsed_ms=elapsed_ms)
            status, cid = "verified", common
        elif name_cids:
            if len(name_cids) > 1:
                return self._empty(identity, "ambiguous", "multiple_cids", elapsed_ms=elapsed_ms)
            status, cid = "name_only", name_cids[0]
        elif cas_cids:
            status, cid = "name_only", cas_cids[0]
        else:
            return self._empty(identity, "unavailable" if failure_kind else "not_found", failure_kind, elapsed_ms=elapsed_ms)

        resolved = replace(
            identity,
            status=status,
            confidence=0.99 if status == "verified" else min(identity.confidence, 0.75),
            evidence_refs=tuple([*identity.evidence_refs, "pubchem:cid"]),
            provider_ids=tuple([*identity.provider_ids, ("pubchem_cid", cid)]),
        )
        return ProviderEvidence(
            identity=resolved,
            source=self.name,
            source_url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
            fields={"pubchem_cid": cid},
            diagnostic=ProviderDiagnostic(self.name, "success", elapsed_ms, failure_kind=failure_kind),
        )

    def _cids(self, query: str) -> tuple[list[str], str]:
        if not query:
            return [], ""
        data, failure_kind = self._get_json(
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{quote(query, safe='')}/cids/JSON"
        )
        values = ((data.get("IdentifierList") or {}).get("CID") or []) if data else []
        return [str(value) for value in values[:5]], failure_kind

    def _get_json(self, url: str) -> tuple[dict[str, Any], str]:
        try:
            request = Request(url, headers={"Accept": "application/json", "User-Agent": "reagent-approval-bot/2"})
            with urlopen(request, timeout=self.timeout_seconds) as response:
                parsed = json.loads(response.read().decode("utf-8"))
            return (parsed if isinstance(parsed, dict) else {}), ""
        except HTTPError as error:
            return {}, f"http_{error.code}"
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            return {}, "network_error"

    def _hazard_fields(self, cid: str) -> tuple[dict[str, str], str]:
        data, failure_kind = self._get_json(
            f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON?heading=Safety%20and%20Hazards"
        )
        if not data:
            return {}, failure_kind
        fields: dict[str, list[str]] = {}

        def strings(value: Any) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, (int, float)):
                return [str(value)]
            if isinstance(value, list):
                return [item for child in value for item in strings(child)]
            if isinstance(value, dict):
                result: list[str] = []
                for key in ("String", "StringWithMarkup", "Number", "Value"):
                    if key in value:
                        result.extend(strings(value[key]))
                return result
            return []

        def target_field(heading: str) -> str:
            lowered = heading.casefold()
            for token, field in (
                ("flash point", "flash_point"),
                ("boiling point", "boiling_point"),
                ("tox", "toxicity"),
                ("corros", "corrosive"),
                ("oxid", "oxidizing"),
                ("flamm", "flammable"),
                ("explos", "explosive_risk"),
                ("water react", "water_reactive"),
            ):
                if token in lowered:
                    return field
            return "hazard_information"

        def walk(node: Any, parent_heading: str = "") -> None:
            if not isinstance(node, dict):
                return
            heading = str(node.get("TOCHeading") or node.get("Name") or parent_heading or "Safety and Hazards")
            for information in node.get("Information", []) or []:
                info_heading = str(information.get("Name") or heading)
                values = [re.sub(r"\s+", " ", item).strip() for item in strings(information.get("Value") or {})]
                values = [item for item in values if item]
                if values:
                    fields.setdefault(target_field(info_heading), []).extend(values[:5])
            for child in node.get("Section", []) or []:
                walk(child, heading)

        walk(data.get("Record") or {})
        return {field: " | ".join(dict.fromkeys(values))[:1200] for field, values in fields.items()}, failure_kind

    def _empty(
        self,
        identity: IdentityRecord,
        status: str,
        failure_kind: str = "",
        detail: str = "",
        *,
        elapsed_ms: int = 0,
    ) -> ProviderEvidence:
        return ProviderEvidence(
            identity=identity,
            source=self.name,
            source_url="",
            fields={"detail": detail} if detail else {},
            diagnostic=ProviderDiagnostic(self.name, status, elapsed_ms, failure_kind=failure_kind),
        )

    @staticmethod
    def _chunks(values: list[str], size: int) -> list[list[str]]:
        return [values[index : index + size] for index in range(0, len(values), size)]
