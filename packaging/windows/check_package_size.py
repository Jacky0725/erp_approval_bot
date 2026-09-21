from __future__ import annotations

import argparse
import json
from pathlib import Path
import zipfile


LIMITS = {"core": 115, "setup": 130, "browser": 120}


def size_mb(path: Path) -> float:
    return path.stat().st_size / 1024 / 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=LIMITS, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    size = size_mb(args.artifact)
    report: dict[str, object] = {"artifact": args.artifact.name, "kind": args.kind, "size_mb": round(size, 2), "limit_mb": LIMITS[args.kind]}
    if args.artifact.suffix.lower() == ".zip":
        with zipfile.ZipFile(args.artifact) as bundle:
            report["largest_entries"] = [
                {"name": item.filename, "size": item.file_size, "compressed": item.compress_size}
                for item in sorted(bundle.infolist(), key=lambda item: item.file_size, reverse=True)[:30]
            ]
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if size > LIMITS[args.kind]:
        raise SystemExit(f"{args.kind} package is {size:.1f} MB; hard limit is {LIMITS[args.kind]} MB")
    return 0
