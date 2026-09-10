from __future__ import annotations

import re
import time
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from chemical_sources.base import ProviderDiagnostic, ProviderEvidence
from reagent_identity import IdentityRecord


class NistWebBookAdapter:
    """Optional NIST WebBook supplement for labelled temperature properties.

    It never resolves identity and never derives hazard classes.  A valid CAS
    is required and each extracted value retains the WebBook page as evidence.
    """

    name = "NIST"

    def __init__(self, *, timeout_seconds: float = 8.0) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def fetch_evidence_many(self, identities: Sequence[IdentityRecord]) -> list[ProviderEvidence]:
        return [self._fetch(identity) for identity in identities]

    def _fetch(self, identity: IdentityRecord) -> ProviderEvidence:
        if not identity.cas:
            return self._empty(identity, "not_found", "missing_cas")
        started = time.monotonic()
        url = f"https://webbook.nist.gov/cgi/cbook.cgi?ID=C{identity.cas.replace('-', '')}&Mask=4"
        try:
            request = Request(url, headers={"User-Agent": "reagent-approval-bot/2", "Accept": "text/html"})
            with urlopen(request, timeout=self.timeout_seconds) as response:
                html = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="ignore")
        except HTTPError as error:
            return self._empty(identity, "unavailable", f"http_{error.code}", elapsed_ms=self._elapsed(started))
        except (URLError, TimeoutError, OSError):
            return self._empty(identity, "unavailable", "network_error", elapsed_ms=self._elapsed(started))

        text = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))
        fields: dict[str, str] = {}
        for label, field in (("Boiling point", "boiling_point"), ("Flash point", "flash_point")):
            match = re.search(rf"{label}.{{0,160}}?([-+]?\d+(?:\.\d+)?\s*(?:&deg;|°)?\s*[CFK])", text, re.I)
            if match:
                fields[field] = match.group(1).replace("&deg;", "°")
        return ProviderEvidence(
            identity=identity,
            source=self.name,
            source_url=url,
            fields=fields,
            diagnostic=ProviderDiagnostic(self.name, "success" if fields else "not_found", self._elapsed(started)),
        )

    @staticmethod
    def _elapsed(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _empty(self, identity: IdentityRecord, status: str, failure_kind: str, *, elapsed_ms: int = 0) -> ProviderEvidence:
        return ProviderEvidence(identity, self.name, "", {}, diagnostic=ProviderDiagnostic(self.name, status, elapsed_ms, failure_kind=failure_kind))
