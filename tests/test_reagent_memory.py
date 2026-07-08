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

    def test_not_recommended_category_is_stored_as_reject_class(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(
                raw_name="reject sample",
                final_category="不建议接收类",
                confidence=0.95,
            )

            row = memory.lookup(raw_name="reject sample")

            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["final_category"], "拒收类")

    def test_update_normalizes_not_recommended_category_to_reject_class(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(
                raw_name="reject edit",
                final_category="普通类",
                confidence=0.95,
            )
            row = memory.lookup(raw_name="reject edit")
            assert row is not None

            updated = memory.update_record(row["id"], {"final_category": "不建议接收类"})

            self.assertEqual(updated["final_category"], "拒收类")

    def test_unknown_packaging_names_are_reusable_unknown_class(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(
                raw_name="Lot#L2107277",
                cleaned_name="Lot#L2107277",
                standard_name="Lot#L2107277",
                cas="-",
                final_category="\u5f3a\u53cd\u5e94",
                confidence=0.95,
                reason="old imported result",
            )
            row = memory.find_any(raw_name="Lot#L2107277")

            self.assertEqual(row["final_category"], "\u672a\u77e5\u7c7b")
            self.assertEqual(row["reusable"], 1)
            self.assertEqual(row["need_manual_review"], 0)
            self.assertEqual(row["conflict"], 0)
            self.assertIsNotNone(memory.lookup(raw_name="Lot#L2107277"))

    def test_update_record_keeps_unknown_packaging_names_reusable(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(raw_name="normal", final_category="普通类", confidence=0.9)
            row = memory.lookup(raw_name="normal")
            assert row is not None

            updated = memory.update_record(
                row["id"],
                {
                    "raw_name": "未知药品（白瓶红盖）",
                    "cleaned_name": "未知药品（白瓶红盖）",
                    "standard_name": "未知药品（白瓶红盖）",
                    "final_category": "强反应",
                    "reusable": True,
                    "conflict": False,
                    "need_manual_review": False,
                    "manual_verified": True,
                },
            )

            self.assertEqual(updated["final_category"], "未知类")
            self.assertEqual(updated["reusable"], 1)
            self.assertEqual(updated["conflict"], 0)
            self.assertEqual(updated["need_manual_review"], 0)
            self.assertEqual(updated["manual_verified"], 1)

    def test_list_records_supports_pagination_and_count(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            for index in range(25):
                memory.add_record(
                    raw_name=f"paged-{index:02d}",
                    final_category="普通类",
                    confidence=0.9,
                    source="unit_test",
                )

            self.assertEqual(memory.count_records(), 25)
            first_page = memory.list_records(limit=20, offset=0)
            second_page = memory.list_records(limit=20, offset=20)

            self.assertEqual(len(first_page), 20)
            self.assertEqual(len(second_page), 5)
            self.assertNotEqual(first_page[0]["id"], second_page[0]["id"])

    def test_delete_conflicting_records_removes_manual_confirmed_too(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(raw_name="delete-me", final_category="普通类", confidence=0.9)
            delete_row = memory.list_records(query="delete-me")[0]
            memory.update_record(delete_row["id"], {"conflict": True, "manual_verified": False})

            memory.add_record(raw_name="keep-confirmed", final_category="普通类", confidence=0.9)
            keep_row = memory.list_records(query="keep-confirmed")[0]
            memory.update_record(keep_row["id"], {"conflict": True, "manual_verified": True})

            memory.add_record(raw_name="keep-normal", final_category="普通类", confidence=0.9)

            self.assertEqual(memory.count_conflicting_records(), 2)
            self.assertEqual(memory.delete_conflicting_records(), 2)

            self.assertIsNone(memory.find_any(raw_name="delete-me"))
            self.assertIsNone(memory.find_any(raw_name="keep-confirmed"))
            self.assertIsNotNone(memory.find_any(raw_name="keep-normal"))


if __name__ == "__main__":
    unittest.main()
