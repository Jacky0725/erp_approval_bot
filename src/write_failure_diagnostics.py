from __future__ import annotations

from typing import Any, Callable


def build_write_failure_debug_payload(
    *,
    sequence: str,
    attempt: int,
    reason: str,
    category: str,
    failure_stage: str = "",
    dropdown_state_loader: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sequence": sequence,
        "attempt": attempt,
        "reason": reason,
        "target_category": category,
        "failure_stage": failure_stage,
        "dropdown": {},
    }
    if dropdown_state_loader is not None:
        try:
            payload["dropdown"] = dropdown_state_loader()
        except Exception as error:  # noqa: BLE001 - diagnostics should never mask the original failure.
            payload["dropdown"] = {"diagnostic_error": str(error)}
    return payload
