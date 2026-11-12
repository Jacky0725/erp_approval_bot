"""Read the observed WorkBuddy transcript format conservatively."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


def _events(path: Path):
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
    except (OSError, UnicodeError):
        return


def identify_dispatch(batch_path: Path, sent_at: float, projects: Path) -> dict[str, str] | None:
    """Require one user turn with the exact batch path and a fresh timestamp."""
    exact = re.compile(re.escape(str(batch_path).replace("\\", "/")) + r"(?=/input\.xlsx\b|[^\w/-]|$)", re.I)
    matches = []
    try:
        transcripts = sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in transcripts[:64]:
        for event in _events(path):
            timestamp = event.get("timestamp")
            if (event.get("type") != "message" or event.get("role") != "user" or not isinstance(timestamp, (int, float))
                    or not sent_at - 1 <= timestamp / 1000 <= sent_at + 60
                    or event.get("sessionId") != path.stem):
                continue
            blocks = event.get("content")
            if not isinstance(blocks, list):
                continue
            prompt = "\n".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict))
            prompt = re.sub(r"<system-reminder\b[^>]*>.*?</system-reminder>", "", prompt, flags=re.S)
            metadata = event.get("providerData")
            request = metadata.get("conversationRequestId") if isinstance(metadata, dict) else None
            if isinstance(request, str) and request and exact.search(prompt.replace("\\", "/")):
                matches.append({"session": path.stem, "request": request, "transcript": str(path.resolve())})
    return matches[0] if len(matches) == 1 else None


def dispatch_outcome(binding: dict[str, Any], projects: Path, batch_id: str) -> str:
    """A timeout by itself, an unfamiliar error, or a later turn cannot authorize replay."""
    try:
        path = Path(binding["transcript"]).resolve()
        if not path.is_relative_to(projects.resolve()) or path.stem != binding["session"]:
            return "unknown"
        request = binding["request"]
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"
    started = False
    outcome = "unknown"
    for event in _events(path):
        if event.get("type") != "message" or event.get("sessionId") != binding["session"]:
            continue
        metadata = event.get("providerData")
        if not isinstance(metadata, dict):
            metadata = {}
        if event.get("role") == "user":
            if started:
                return "completed" if outcome == "completed" else "unknown"
            started = metadata.get("conversationRequestId") == request
        if not started or metadata.get("conversationRequestId") != request or event.get("role") != "assistant":
            continue
        error = metadata.get("error") or {}
        blocks = event.get("content") or []
        if event.get("status") == "completed" and any(
                block.get("type") == "output_text"
                and re.search(r"(?m)^【批次完成】" + re.escape(batch_id) + r"\s*$", str(block.get("text", "")))
                for block in blocks if isinstance(block, dict)):
            outcome = "completed"
        elif (event.get("status") == "incomplete" and isinstance(error, dict) and error.get("isStreamTimeout") is True
            and str(error.get("message", "")).startswith(
                "Empty stream: upstream ended the turn with finish_reason=error after emitting reasoning but no text or tool call")):
            outcome = "failed"
        else:
            outcome = "unknown"
    return outcome
