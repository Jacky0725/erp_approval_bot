from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from data_health import apply_data_health_repairs, run_data_health_audit  # noqa: E402


def load_settings(root_dir: Path) -> dict:
    config_path = root_dir / "config" / "settings.yaml"
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit reagent memory and manual-review data for mojibake and reuse-loop issues."
    )
    parser.add_argument("--root", default=str(ROOT_DIR), help="Project root directory.")
    parser.add_argument("--output", default="", help="Optional audit xlsx output path.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply safe repairs after exporting the audit. Defaults to dry-run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root_dir = Path(args.root).resolve()
    settings = load_settings(root_dir)
    output_path = Path(args.output).resolve() if args.output else None
    report = run_data_health_audit(root_dir, settings=settings, output_path=output_path)
    summary = report["summary"]
    print(f"Audit written: {report['output_path']}")
    print(
        "Summary: "
        f"memory_mojibake={summary['memory_mojibake_records']}, "
        f"reusable_mojibake={summary['reusable_mojibake_records']}, "
        f"unrecoverable_reusable={summary['unrecoverable_reusable_mojibake_records']}, "
        f"confirmed_missing_memory={summary['confirmed_review_missing_memory']}, "
        f"duplicate_review_items={summary['duplicate_review_items']}"
    )
    if not args.apply:
        print("Dry-run only. Re-run with --apply to repair recoverable issues after reviewing the audit.")
        return 0
    result = apply_data_health_repairs(root_dir, settings=settings)
    print(f"Applied repairs: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
