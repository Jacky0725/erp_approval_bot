from __future__ import annotations

import os
from typing import Sequence

from chemical_sources.base import ProviderDiagnostic, ProviderEvidence
from reagent_identity import IdentityRecord


class CompToxAdapter:
    """Credential-gated CompTox adapter placeholder with honest diagnostics.

    The production endpoint is intentionally not contacted until the operator
    supplies the organisation API key.  This prevents configuration from
    making a missing credential look like a source outage.
    """

    name = "EPA CompTox"

    def __init__(self, *, api_key_env: str = "EPA_CTX_API_KEY") -> None:
        self.api_key_env = api_key_env

    @property
    def configured(self) -> bool:
        return bool(os.getenv(self.api_key_env, "").strip())

    def fetch_evidence_many(self, identities: Sequence[IdentityRecord]) -> list[ProviderEvidence]:
        status = "not_configured" if not self.configured else "disabled_pending_endpoint_validation"
        kind = "disabled_missing_credentials" if not self.configured else "disabled_pending_endpoint_validation"
        return [
            ProviderEvidence(identity, self.name, "", {}, diagnostic=ProviderDiagnostic(self.name, status, 0, failure_kind=kind))
            for identity in identities
        ]
