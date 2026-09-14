from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Iterable


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def build_report(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(events)
    providers = [row for row in rows if row.get("event") == "provider"]
    llm = [row for row in rows if row.get("event") == "llm"]
    comparisons = [row for row in rows if row.get("event") == "shadow_comparison"]
    elapsed = sorted(int(row.get("elapsed_ms") or 0) for row in providers)
    return {
        "event_count": len(rows),
        "provider_calls": len(providers),
        "provider_statuses": dict(Counter(str(row.get("status") or "") for row in providers)),
        "provider_p50_ms": int(median(elapsed)) if elapsed else 0,
        "provider_p90_ms": elapsed[min(len(elapsed) - 1, int(len(elapsed) * 0.9))] if elapsed else 0,
        "llm_calls": len(llm),
        "llm_statuses": dict(Counter(str(row.get("status") or "") for row in llm)),
        "shadow_comparisons": len(comparisons),
        "same_category_count": sum(bool(row.get("same_category")) for row in comparisons),
        "v2_manual_review_count": sum(bool(row.get("v2_manual_review")) for row in comparisons),
        "shadow_failures": sum(1 for row in rows if row.get("event") == "shadow_failure"),
    }


def markdown_report(report: dict[str, Any]) -> str:
    comparisons = int(report["shadow_comparisons"])
    matched = int(report["same_category_count"])
    rate = f"{matched / comparisons:.1%}" if comparisons else "无样本"
    return "\n".join([
        "# 试剂信息影子运行报告",
        "",
        f"- 影子对比：{comparisons}；类别一致率：{rate}",
        f"- V2 人工复核：{report['v2_manual_review_count']}；影子失败：{report['shadow_failures']}",
        f"- Provider 调用：{report['provider_calls']}；P50/P90：{report['provider_p50_ms']} / {report['provider_p90_ms']} ms",
        f"- Provider 状态：{json.dumps(report['provider_statuses'], ensure_ascii=False, sort_keys=True)}",
        f"- LLM 调用：{report['llm_calls']}；状态：{json.dumps(report['llm_statuses'], ensure_ascii=False, sort_keys=True)}",
        "",
        "此报告用于运行监控；正式模式下仍须结合人工复核记录和高风险样本持续观察。",
        "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarise V2 shadow enrichment metrics.")
    parser.add_argument("--input", type=Path, default=Path("data/logs/enrichment_metrics.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/reports/enrichment_shadow_report.md"))
    args = parser.parse_args()
    report = build_report(load_events(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
