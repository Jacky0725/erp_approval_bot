from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workbuddy_recovery import dispatch_outcome, identify_dispatch  # noqa: E402


def inspect_batch(batch_dir: Path, projects: Path) -> dict:
    batch_dir = batch_dir.resolve()
    state = json.loads((batch_dir / "state.json").read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("batch_id") != batch_dir.name:
        raise ValueError("Invalid batch state: batch_id must match the directory name.")
    sent_at = state.get("dispatch_started_at")
    binding = None
    if isinstance(sent_at, (int, float)) and not isinstance(sent_at, bool):
        binding = identify_dispatch(batch_dir, sent_at, projects)
    return {
        "batch_id": state["batch_id"],
        "status": state.get("status"),
        "dispatch_state": state.get("dispatch_state"),
        "result_exists": (batch_dir / "result.xlsx").is_file(),
        "pending_result_exists": (batch_dir / "result.pending.xlsx").is_file(),
        "unique_transcript_match": binding is not None,
        "transcript_outcome": dispatch_outcome(binding, projects, state["batch_id"]) if binding else "unknown",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect a WorkBuddy batch without sending, importing, or changing state.")
    parser.add_argument("batch_dir", type=Path, help="Batch directory containing state.json.")
    parser.add_argument("--projects", type=Path, default=Path.home() / ".workbuddy" / "projects")
    args = parser.parse_args()
    try:
        report = inspect_batch(args.batch_dir, args.projects)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Cannot inspect batch: {error}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
