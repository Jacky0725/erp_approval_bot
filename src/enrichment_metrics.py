from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class EnrichmentMetrics:
    """Thread-safe, append-only instrumentation for reagent enrichment.

    Records intentionally exclude raw reagent names and source payloads.  The
    resulting JSONL can therefore be retained for operational performance
    analysis without duplicating ERP content or chemical source documents.
    """

    enabled: bool = False
    output_path: Path | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _events: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None, root_dir: Path) -> "EnrichmentMetrics":
        config = (settings or {}).get("enrichment_metrics", {}) or {}
        configured_path = str(config.get("jsonl_path") or "data/logs/enrichment_metrics.jsonl")
        return cls(
            enabled=_truthy(config.get("enabled")),
            output_path=Path(root_dir) / configured_path,
        )

    def record(self, event: str, **payload: Any) -> None:
        record = {
            "timestamp": _utc_now(),
            "run_id": self.run_id,
            "event": str(event or "unknown"),
            **{key: value for key, value in payload.items() if value is not None},
        }
        with self._lock:
            self._events.append(record)
            if self.enabled and self.output_path:
                self.output_path.parent.mkdir(parents=True, exist_ok=True)
                with self.output_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def record_cache(self, *, layer: str, outcome: str) -> None:
        self.record("cache", layer=layer, outcome=outcome)

    def record_provider(
        self,
        *,
        provider: str,
        status: str,
        elapsed_ms: int,
        attempts: int = 0,
        failure_kind: str = "",
    ) -> None:
        self.record(
            "provider",
            provider=provider,
            status=status,
            elapsed_ms=max(0, int(elapsed_ms or 0)),
            attempts=max(0, int(attempts or 0)),
            failure_kind=str(failure_kind or ""),
        )

    def record_llm(self, *, operation: str, status: str, elapsed_ms: int) -> None:
        self.record(
            "llm",
            operation=operation,
            status=status,
            elapsed_ms=max(0, int(elapsed_ms or 0)),
        )

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(event) for event in self._events]

    def summary(self) -> dict[str, Any]:
        events = self.snapshot()
        provider_events = [event for event in events if event.get("event") == "provider"]
        llm_events = [event for event in events if event.get("event") == "llm"]
        cache_events = [event for event in events if event.get("event") == "cache"]
        return {
            "run_id": self.run_id,
            "event_count": len(events),
            "provider_calls": len(provider_events),
            "provider_statuses": dict(Counter(str(event.get("status") or "") for event in provider_events)),
            "provider_elapsed_ms": sum(int(event.get("elapsed_ms") or 0) for event in provider_events),
            "llm_calls": len(llm_events),
            "llm_statuses": dict(Counter(str(event.get("status") or "") for event in llm_events)),
            "llm_elapsed_ms": sum(int(event.get("elapsed_ms") or 0) for event in llm_events),
            "cache_events": dict(
                Counter(f"{event.get('layer', '')}:{event.get('outcome', '')}" for event in cache_events)
            ),
        }


def build_confirmed_benchmark(
    rows: Iterable[dict[str, Any]],
    *,
    max_cases: int | None = None,
) -> list[dict[str, str]]:
    """Create stable, de-duplicated benchmark cases from confirmed review rows."""

    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        category = str(row.get("manual_result") or row.get("confirmed_category") or "").strip()
        name = str(row.get("chemical_name") or row.get("reagent_name") or "").strip()
        cas = str(row.get("cas") or row.get("CAS号") or "").strip()
        if status not in {"confirmed", "approved"} or not category or not name:
            continue
        key_text = f"{name.casefold()}|{cas.casefold()}|{category}"
        case_id = hashlib.sha256(key_text.encode("utf-8")).hexdigest()[:16]
        unique.setdefault(
            case_id,
            {
                "case_id": case_id,
                "reagent_name": name,
                "cas": cas,
                "expected_category": category,
                "source": "confirmed_review_queue",
            },
        )

    cases = sorted(unique.values(), key=lambda item: (item["expected_category"], item["case_id"]))
    return cases[:max_cases] if max_cases else cases


def compare_benchmark(
    cases: Iterable[dict[str, Any]],
    predict: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Run a supplied predictor without prescribing a production implementation."""

    results: list[dict[str, Any]] = []
    for case in cases:
        prediction = predict(dict(case)) or {}
        expected = str(case.get("expected_category") or "").strip()
        actual = str(prediction.get("final_category") or "").strip()
        results.append(
            {
                "case_id": str(case.get("case_id") or ""),
                "expected_category": expected,
                "actual_category": actual,
                "matched": expected == actual,
                "need_manual_review": bool(prediction.get("need_manual_review", True)),
            }
        )
    total = len(results)
    matches = sum(1 for item in results if item["matched"])
    return {
        "case_count": total,
        "match_count": matches,
        "match_rate": matches / total if total else 0.0,
        "manual_review_count": sum(1 for item in results if item["need_manual_review"]),
        "mismatches": [item for item in results if not item["matched"]],
    }
