from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from approval_suggestion_metrics import (
    aggregate_suggestion_summaries,
    format_suggestion_summary,
    summarize_approval_suggestions,
)


def test_summarize_approval_suggestions_counts_writable_and_skips() -> None:
    suggestions = [
        {
            "最终建议类别": "普通类",
            "置信度": 0.95,
            "需人工复核": False,
            "查询来源": "reagent_memory",
        },
        {
            "最终建议类别": "易燃类",
            "置信度": 0.72,
            "需人工复核": False,
        },
        {
            "最终建议类别": "未知类",
            "置信度": 1.0,
            "需人工复核": True,
            "判定依据": "Chemsrc 和 ChemicalBook 都没有查到可信结果。",
        },
        {
            "最终建议类别": "",
            "置信度": 0.9,
            "需人工复核": False,
        },
    ]

    summary = summarize_approval_suggestions(
        suggestions,
        min_confidence=0.8,
        category_resolver=lambda category: category if category != "未知类" else "未知类",
    )

    assert summary["total"] == 4
    assert summary["writable"] == 1
    assert summary["memory_hit"] == 1
    assert summary["manual_review"] == 1
    assert summary["low_confidence"] == 1
    assert summary["missing_category"] == 1
    assert summary["search_failure"] == 1
    assert summary["skip_reasons"] == {
        "low_confidence": 1,
        "manual_review": 1,
        "missing_category": 1,
    }


def test_aggregate_suggestion_summaries_from_log_lines() -> None:
    summary = summarize_approval_suggestions(
        [
            {"最终建议类别": "普通类", "置信度": 0.91, "需人工复核": False},
            {
                "最终建议类别": "易燃类",
                "置信度": 0.91,
                "需人工复核": True,
                "是否使用LLM知识托底": True,
            },
        ],
        min_confidence=0.8,
        category_resolver=lambda category: category,
    )
    lines = [f"Page suggestion summary: {format_suggestion_summary(summary)}"]

    aggregate = aggregate_suggestion_summaries(lines)

    assert aggregate["suggestion_total"] == 2
    assert aggregate["writable_candidate_count"] == 1
    assert aggregate["manual_review_candidate_count"] == 1
    assert aggregate["llm_knowledge_fallback_count"] == 1
    assert aggregate["skipped_candidate_count"] == 1
