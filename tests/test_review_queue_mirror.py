from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from review_queue_mirror import ReviewQueueMirror  # noqa: E402


class ReviewQueueMirrorTest(unittest.TestCase):
    def test_refresh_and_page_filters_review_queue_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            pd.DataFrame(
                [
                    {"试剂清单号": "SJ1", "序号": "1", "试剂名称": "A", "status": "pending"},
                    {"试剂清单号": "SJ1", "序号": "2", "试剂名称": "B", "status": "confirmed"},
                    {"试剂清单号": "SJ2", "序号": "1", "试剂名称": "C", "status": "pending"},
                ]
            ).to_excel(data_dir / "review_queue.xlsx", index=False)
            mirror = ReviewQueueMirror.from_settings(root, {})

            refreshed = mirror.refresh()
            page = mirror.page(list_number="SJ1", status="pending", limit=20)

        self.assertTrue(refreshed["refreshed"])
        self.assertEqual(refreshed["rows"], 3)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["rows"][0]["reagent_name"], "A")

    def test_missing_excel_is_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mirror = ReviewQueueMirror.from_settings(Path(tmp), {})

            result = mirror.refresh()

        self.assertFalse(result["refreshed"])
        self.assertEqual(result["rows"], 0)


if __name__ == "__main__":
    unittest.main()
