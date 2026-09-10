from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from enrichment_metrics import build_confirmed_benchmark


def build_from_review_queue(review_queue_path: Path, output_path: Path, limit: int | None = None) -> dict[str, Any]:
    frame = pd.read_excel(review_queue_path, dtype=str).fillna("")
    cases = build_confirmed_benchmark(frame.to_dict("records"), max_cases=limit)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "input": str(review_queue_path),
        "output": str(output_path),
        "case_count": len(cases),
        "categories": sorted({case["expected_category"] for case in cases}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a stable reagent-classification benchmark from confirmed reviews.")
    parser.add_argument("--review-queue", type=Path, default=Path("data/review_queue.xlsx"))
    parser.add_argument("--output", type=Path, default=Path("data/benchmarks/confirmed_reagents.json"))
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    result = build_from_review_queue(args.review_queue, args.output, args.limit or None)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
