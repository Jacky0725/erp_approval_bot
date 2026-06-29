from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from reagent_memory import ReagentMemory  # noqa: E402


class ReagentMemoryTest(unittest.TestCase):
    def make_memory(self) -> tuple[tempfile.TemporaryDirectory[str], ReagentMemory]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        memory = ReagentMemory.from_settings(
            {
                "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
                "memory": {"min_confidence": 0.8},
            },
            root,
        )
        return tmp, memory

    def test_lookup_reuses_high_confidence_exact_match(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            added = memory.add_record(
                raw_name="PEG",
                cleaned_name="聚乙二醇",
                standard_name="聚乙二醇",
                cas="25322-68-3",
                final_category="普通类",
                confidence=0.95,
                reason="test",
                source="unit_test",
            )

            self.assertTrue(added)
            self.assertEqual(memory.lookup(cas="25322-68-3")["final_category"], "普通类")
            self.assertEqual(memory.lookup(standard_name="聚乙二醇")["final_category"], "普通类")
            self.assertEqual(memory.lookup(cleaned_name="聚乙二醇")["final_category"], "普通类")
            self.assertEqual(memory.lookup(raw_name="PEG")["final_category"], "普通类")

    def test_low_confidence_manual_review_and_conflict_are_not_reused(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            self.assertFalse(
                memory.add_record(
                    raw_name="low",
                    final_category="普通类",
                    confidence=0.5,
                    need_manual_review=False,
                )
            )
            self.assertIsNone(memory.lookup(raw_name="low"))

            self.assertFalse(
                memory.add_record(
                    raw_name="manual",
                    final_category="普通类",
                    confidence=1.0,
                    need_manual_review=True,
                )
            )
            self.assertIsNone(memory.lookup(raw_name="manual"))

            self.assertTrue(
                memory.add_record(
                    raw_name="same",
                    final_category="普通类",
                    confidence=0.95,
                )
            )
            self.assertFalse(
                memory.add_record(
                    raw_name="same",
                    final_category="易燃类",
                    confidence=0.95,
                )
            )
            self.assertIsNone(memory.lookup(raw_name="same"))

    def test_list_and_update_record_refresh_lookup_keys(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(
                raw_name="old name",
                cleaned_name="old clean",
                standard_name="old standard",
                cas="111-11-1",
                final_category="普通类",
                confidence=0.9,
            )
            rows = memory.list_records(query="old standard")
            self.assertEqual(len(rows), 1)

            updated = memory.update_record(
                rows[0]["id"],
                {
                    "raw_name": "new name",
                    "cleaned_name": "new clean",
                    "standard_name": "new standard",
                    "cas": "222-22-2",
                    "final_category": "易燃类",
                    "confidence": "0.95",
                    "reusable": True,
                    "conflict": False,
                    "manual_verified": True,
                    "reason": "manual correction",
                },
            )

            self.assertEqual(updated["final_category"], "易燃类")
            self.assertIsNone(memory.lookup(cas="111-11-1"))
            match = memory.lookup(standard_name="new standard")
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match["cas"], "222-22-2")
            self.assertEqual(match["final_category"], "易燃类")


if __name__ == "__main__":
    unittest.main()
