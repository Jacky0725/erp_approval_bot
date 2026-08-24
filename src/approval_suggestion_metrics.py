from __future__ import annotations

import json
from collections import Counter
from typing import Any, Callable


Suggestion = dict[str, Any]
CategoryResolver = Callable[[str], str]


def summarize_approval_suggestions(
    suggestions: list[Suggestion],
    *,
    min_confidence: float,
    category_resolver: CategoryResolver | None = None,
) -> dict[str, Any]:
    """Return a compact, log-friendly summary of one page of approval advice."""
    summary: dict[str, Any] = {
        "total": len(suggestions),
        "writable": 0,
        "manual_review": 0,
        "low_confidence": 0,
        "missing_category": 0,
        "unmapped_category": 0,
        "search_failure": 0,
        "memory_hit": 0,
        "local_basic_rule": 0,
        "llm_knowledge_fallback": 0,
        "skipped": 0,
        "skip_reasons": {},
        "categories": {},
        "writable_categories": {},
    }
    skip_reasons: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    writable_categories: Counter[str] = Counter()

    for suggestion in suggestions:
        category = _text(suggestion.get("最终建议类别"))
        if category:
            categories[category] += 1

        manual_review = _truthy(suggestion.get("需人工复核"))
        confidence = _float(suggestion.get("置信度"))
        source = _text(suggestion.get("查询来源"))
        fallback_source = _text(suggestion.get("兜底查询来源"))

        if manual_review:
            summary["manual_review"] += 1
        if confidence < min_confidence:
            summary["low_confidence"] += 1
        if not category:
            summary["missing_category"] += 1
        if source == "reagent_memory":
            summary["memory_hit"] += 1
        if source == "local_basic_reagent_rule":
            summary["local_basic_rule"] += 1
        if _truthy(suggestion.get("是否使用LLM知识托底")) or "llm knowledge" in fallback_source.lower():
            summary["llm_knowledge_fallback"] += 1
        if _looks_like_search_failure(suggestion):
            summary["search_failure"] += 1

        reason = write_skip_reason(
            suggestion,
            min_confidence=min_confidence,
            category_resolver=category_resolver,
        )
        if reason:
            summary["skipped"] += 1
            skip_reasons[reason] += 1
        else:
            summary["writable"] += 1
            writable_categories[category] += 1

    summary["skip_reasons"] = dict(sorted(skip_reasons.items()))
    summary["categories"] = dict(sorted(categories.items()))
    summary["writable_categories"] = dict(sorted(writable_categories.items()))
    return summary


def write_skip_reason(
    suggestion: Suggestion,
    *,
    min_confidence: float,
    category_resolver: CategoryResolver | None = None,
) -> str:
    category = _text(suggestion.get("最终建议类别"))
    if not category:
        return "missing_category"
    if category_resolver is not None and not category_resolver(category):
        return "unmapped_category"
    if _truthy(suggestion.get("需人工复核")):
        return "manual_review"
    if _float(suggestion.get("置信度")) < min_confidence:
        return "low_confidence"
    return ""


def format_suggestion_summary(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)


def parse_suggestion_summary_line(line: str) -> dict[str, Any] | None:
    marker = "Page suggestion summary:"
    if marker not in line:
        return None
    payload = line.split(marker, 1)[1].strip()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def aggregate_suggestion_summaries(lines: list[str]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {
        "suggestion_total": 0,
        "writable_candidate_count": 0,
        "manual_review_candidate_count": 0,
        "low_confidence_count": 0,
        "search_failure_count": 0,
        "memory_hit_count": 0,
        "local_basic_rule_count": 0,
        "llm_knowledge_fallback_count": 0,
        "skipped_candidate_count": 0,
        "skip_reasons": {},
    }
    skip_reasons: Counter[str] = Counter()
    seen = False

    for line in lines:
        summary = parse_suggestion_summary_line(str(line))
        if not summary:
            continue
        seen = True
        aggregate["suggestion_total"] += _int(summary.get("total"))
        aggregate["writable_candidate_count"] += _int(summary.get("writable"))
        aggregate["manual_review_candidate_count"] += _int(summary.get("manual_review"))
        aggregate["low_confidence_count"] += _int(summary.get("low_confidence"))
        aggregate["search_failure_count"] += _int(summary.get("search_failure"))
        aggregate["memory_hit_count"] += _int(summary.get("memory_hit"))
        aggregate["local_basic_rule_count"] += _int(summary.get("local_basic_rule"))
        aggregate["llm_knowledge_fallback_count"] += _int(summary.get("llm_knowledge_fallback"))
        aggregate["skipped_candidate_count"] += _int(summary.get("skipped"))
        for reason, count in (summary.get("skip_reasons") or {}).items():
            skip_reasons[str(reason)] += _int(count)

    aggregate["skip_reasons"] = dict(sorted(skip_reasons.items()))
    return aggregate if seen else {}


def _looks_like_search_failure(suggestion: Suggestion) -> bool:
    source = _text(suggestion.get("查询来源"))
    evidence = " ".join(
        [
            _text(suggestion.get("判定依据")),
            _text(suggestion.get("规则原因")),
            _text(suggestion.get("备注")),
            _text(suggestion.get("需人工复核原因")),
        ]
    ).lower()
    if not source and _truthy(suggestion.get("需人工复核")):
        return True
    failure_tokens = (
        "chemsrc 和 chemicalbook 都没有查到",
        "查询失败",
        "no trusted web evidence",
        "manual_review",
        "manual review",
        "没有查到可信结果",
    )
    return any(token in evidence for token in failure_tokens)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
