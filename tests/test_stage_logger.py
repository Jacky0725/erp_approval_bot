from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from stage_logger import StageLogger  # noqa: E402


class StageLoggerTest(unittest.TestCase):
    def test_stage_logger_can_emit_jsonl_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            logger = StageLogger(jsonl_path=path)

            with logger.stage("chemical_search", "A"):
                logger.event("query started")

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([row["event"] for row in rows], ["stage_start", "event", "stage_end"])
        self.assertEqual(rows[0]["stage"], "chemical_search")
        self.assertEqual(rows[1]["message"], "query started")
        self.assertIn("elapsed_seconds", rows[2])

    def test_stage_logger_records_failures_in_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            logger = StageLogger(jsonl_path=path)

            with self.assertRaises(ValueError):
                with logger.stage("write"):
                    raise ValueError("could not select")

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(rows[-1]["event"], "stage_fail")
        self.assertEqual(rows[-1]["error"], "could not select")


if __name__ == "__main__":
    unittest.main()
