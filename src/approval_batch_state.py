from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


KeyFn = Callable[[dict[str, Any]], str]


def normalize_write_result(
    raw_result: dict[str, Any] | None,
    suggestions: list[dict[str, Any]],
    key_fn: KeyFn,
) -> dict[str, set[str]]:
    """Return a stable write-result shape for legacy and new writer paths."""
    if raw_result is None:
        raw_result = {}
    return {
        "attempted": set(raw_result.get("attempted") or set()),
        "handled": set(raw_result.get("handled") or {key_fn(suggestion) for suggestion in suggestions}),
        "failed": set(raw_result.get("failed") or set()),
        "deferred": set(raw_result.get("deferred") or set()),
    }


@dataclass
class MultiPageWriteState:
    max_attempts: int
    handled_keys: set[str] = field(default_factory=set)
    pending_write_suggestions: dict[str, dict[str, Any]] = field(default_factory=dict)
    not_found_after_reread_keys: set[str] = field(default_factory=set)
    write_attempts_by_key: dict[str, int] = field(default_factory=dict)

    def retry_allowed(self, key: str) -> bool:
        return self.write_attempts_by_key.get(key, 0) < self.max_attempts

    def unhandled_unmatched(
        self,
        reagents: list[dict[str, Any]],
        reagent_key_fn: KeyFn,
    ) -> list[dict[str, Any]]:
        return [
            reagent
            for reagent in reagents
            if reagent_key_fn(reagent) not in self.handled_keys
            and self.retry_allowed(reagent_key_fn(reagent))
        ]

    def register_writable_suggestions(
        self,
        suggestions: list[dict[str, Any]],
        suggestion_key_fn: KeyFn,
    ) -> None:
        for suggestion in suggestions:
            key = suggestion_key_fn(suggestion)
            if key not in self.handled_keys and self.retry_allowed(key):
                self.pending_write_suggestions[key] = suggestion

    def apply_write_result(self, write_result: dict[str, set[str]]) -> dict[str, set[str]]:
        for key in write_result.get("attempted", set()):
            self.write_attempts_by_key[key] = self.write_attempts_by_key.get(key, 0) + 1

        failed_keys = set(write_result.get("failed", set()))
        saved_or_terminal_keys = set(write_result.get("handled", set())) - failed_keys
        self.handled_keys.update(saved_or_terminal_keys)
        for key in saved_or_terminal_keys:
            self.pending_write_suggestions.pop(key, None)

        retry_limited_keys: set[str] = set()
        for key in failed_keys:
            if not self.retry_allowed(key):
                self.handled_keys.add(key)
                self.pending_write_suggestions.pop(key, None)
                retry_limited_keys.add(key)

        deferred_keys = set(write_result.get("deferred", set())) & set(self.pending_write_suggestions)
        return {
            "failed": failed_keys,
            "saved_or_terminal": saved_or_terminal_keys,
            "retry_limited": retry_limited_keys,
            "deferred": deferred_keys,
        }

    def mark_reagents_handled(self, reagents: list[dict[str, Any]], reagent_key_fn: KeyFn) -> None:
        for reagent in reagents:
            key = reagent_key_fn(reagent)
            self.handled_keys.add(key)
            self.pending_write_suggestions.pop(key, None)
