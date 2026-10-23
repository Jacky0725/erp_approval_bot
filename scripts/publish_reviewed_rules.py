"""Publish a separately prepared, reviewed workbook with stale-review protection."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import yaml
from rule_maintainer import RuleMaintainer

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("incoming", type=Path)
    parser.add_argument("--expected-sha256", required=True, help="审阅时正式规则的 SHA256，不能使用待发布文件的摘要")
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    result = RuleMaintainer(ROOT, settings).publish_reviewed_workbook(args.incoming, expected_sha256=args.expected_sha256, reviewer=args.reviewer, reason=args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
