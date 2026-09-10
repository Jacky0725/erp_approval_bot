from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from reagent_identity import IdentityRecord


@dataclass(frozen=True)
class ProviderDiagnostic:
    provider: str
    status: str
    elapsed_ms: int
    attempts: int = 1
    failure_kind: str = ""


@dataclass(frozen=True)
class ProviderEvidence:
    identity: IdentityRecord
    source: str
    source_url: str
    fields: dict[str, str]
    raw: dict[str, Any] = field(default_factory=dict)
    diagnostic: ProviderDiagnostic | None = None


class ChemicalSourceAdapter(Protocol):
    name: str

    def resolve_many(self, identities: Sequence[IdentityRecord]) -> list[ProviderEvidence]:
        """Resolve local identity candidates into this provider's stable identity."""

    def fetch_evidence_many(self, identities: Sequence[IdentityRecord]) -> list[ProviderEvidence]:
        """Fetch structured fields only for provider-resolved identities."""
