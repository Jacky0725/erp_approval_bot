from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from chemical_searcher import ChemicalSearcher
from reagent_identity import IdentityRecord, ReagentIdentityResolver


@dataclass(frozen=True)
class AliasCandidate:
    alias: str
    standard_name: str
    cas: str
    source: str
    source_url: str
    confidence: float
    evidence: str
    status: str = "pending"

    def to_row(self) -> dict[str, str]:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "alias": self.alias,
            "standard_name": self.standard_name,
            "cas": self.cas,
            "source": self.source,
            "source_url": self.source_url,
            "confidence": f"{self.confidence:.2f}",
            "evidence": self.evidence,
            "status": self.status,
            "reviewer": "",
            "reviewed_at": "",
        }


class ChineseAliasBackfill:
    """Background-only Chemsrc/ChemicalBook alias candidate collection.

    Results never update ``name_aliases.yaml``.  A candidate is emitted only
    after the legacy source matcher passes relevance checks and, when ERP has a
    CAS, the source returns that same CAS.  Existing provider circuit breakers,
    host limits and retries are reused through ``ChemicalSearcher``.
    """

    sources = ("Chemsrc", "ChemicalBook")

    def __init__(
        self,
        *,
        settings: dict[str, Any] | None = None,
        root_dir: Path | None = None,
        searcher_factory: Callable[..., ChemicalSearcher] = ChemicalSearcher,
    ) -> None:
        self.settings = settings or {}
        self.root_dir = root_dir or Path(__file__).resolve().parents[1]
        self.searcher_factory = searcher_factory
        self.identity_resolver = ReagentIdentityResolver(settings=self.settings, root_dir=self.root_dir)

    def collect(self, reagents: Iterable[dict[str, Any]]) -> list[AliasCandidate]:
        candidates: list[AliasCandidate] = []
        seen: set[tuple[str, str, str]] = set()
        for reagent in reagents:
            identity = self._identity(reagent)
            # A local CAS match proves the substance but does not prove that a
            # newly encountered Chinese wording is already an approved alias.
            # Keep both verified and name-only identities eligible; conflicts,
            # unresolved records and mixtures remain out of scope.
            if identity.status not in {"verified", "name_only"} or identity.mixture_state != "single_substance":
                continue
            candidate = self._collect_one(identity)
            if candidate is None:
                continue
            key = (candidate.alias.casefold(), candidate.standard_name.casefold(), candidate.cas)
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
        return candidates

    def write_candidates(self, candidates: Iterable[AliasCandidate]) -> int:
        rows = [candidate.to_row() for candidate in candidates]
        if not rows:
            return 0
        import pandas as pd

        columns = list(rows[0])
        path = self._candidates_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = pd.read_excel(path, dtype=str).fillna("") if path.exists() else pd.DataFrame(columns=columns)
        existing = existing.reindex(columns=columns).fillna("")
        additions = pd.DataFrame(rows).reindex(columns=columns).fillna("")
        duplicate = existing.apply(
            lambda row: (str(row["alias"]).strip(), str(row["standard_name"]).strip(), str(row["cas"]).strip()),
            axis=1,
        ) if not existing.empty else set()
        known = set(duplicate)
        additions = additions.loc[
            ~additions.apply(lambda row: (row["alias"], row["standard_name"], row["cas"]) in known, axis=1)
        ]
        if additions.empty:
            return 0
        pd.concat([existing, additions], ignore_index=True).to_excel(path, index=False)
        return len(additions)

    def _collect_one(self, identity: IdentityRecord) -> AliasCandidate | None:
        searcher = self.searcher_factory(settings=self.settings, root_dir=self.root_dir)
        queries = tuple(dict.fromkeys(value for value in (
            identity.cleaned_base_name,
            identity.raw_name,
            *identity.aliases,
        ) if value))[:2]
        validation_names = searcher._validation_names(
            identity.standard_name_cn or identity.cleaned_base_name,
            identity.standard_name_cn,
            identity.cleaned_base_name,
            identity.standard_name_en,
            list(identity.aliases),
        )
        for source in self.sources:
            provider = searcher._search_chemsrc if source == "Chemsrc" else searcher._search_chemicalbook
            for query in queries:
                result = searcher._run_provider(
                    provider,
                    name=query,
                    cas=identity.cas,
                    query=query,
                    validation_names=validation_names,
                )
                candidate = self._to_candidate(identity, result)
                if candidate is not None:
                    return candidate
        return None

    @staticmethod
    def _to_candidate(identity: IdentityRecord, result: dict[str, Any] | None) -> AliasCandidate | None:
        if not result or not result.get("relevance_passed"):
            return None
        returned_cas = str(result.get("cas") or "").strip()
        if identity.cas and returned_cas != identity.cas:
            return None
        standard_name = str(result.get("matched_site_name") or "").strip()
        alias = identity.cleaned_base_name or identity.raw_name
        if not returned_cas or not standard_name or not alias or alias.casefold() == standard_name.casefold():
            return None
        return AliasCandidate(
            alias=alias,
            standard_name=standard_name,
            cas=returned_cas,
            source=str(result.get("source") or "").strip(),
            source_url=str(result.get("url") or "").strip(),
            confidence=min(0.85, max(0.6, float(result.get("name_similarity") or 0.0))),
            evidence=f"background_alias_lookup; similarity={float(result.get('name_similarity') or 0.0):.2f}",
        )

    def _identity(self, reagent: dict[str, Any]) -> IdentityRecord:
        return self.identity_resolver.resolve(
            str(reagent.get("试剂名称") or reagent.get("reagent_name") or reagent.get("name") or ""),
            cas=str(reagent.get("CAS号") or reagent.get("cas") or ""),
            specification=str(reagent.get("规格") or reagent.get("specification") or ""),
            unit=str(reagent.get("规格单位") or reagent.get("unit") or ""),
        )

    def _candidates_path(self) -> Path:
        paths = self.settings.get("paths", {}) or {}
        return self.root_dir / str(paths.get("name_alias_candidates_excel", "config/name_alias_candidates.xlsx"))


def _load_settings(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Chinese alias candidates from background source lookups.")
    parser.add_argument("--input", required=True, type=Path, help="JSON array of reagent records")
    parser.add_argument("--write-candidates", action="store_true", help="Append pending candidates to the review workbook")
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    args = parser.parse_args()
    rows = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("input must be a JSON array of reagent records")
    service = ChineseAliasBackfill(settings=_load_settings(args.settings), root_dir=args.settings.resolve().parents[1])
    candidates = service.collect(rows)
    if args.write_candidates:
        print(json.dumps({"candidates_written": service.write_candidates(candidates)}, ensure_ascii=False))
    else:
        print(json.dumps([asdict(candidate) for candidate in candidates], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
