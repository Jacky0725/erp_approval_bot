from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import closing
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

    def test_placeholder_cas_does_not_match_unrelated_memory(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(
                raw_name="PMSF solution",
                cleaned_name="PMSF solution",
                standard_name="PMSF",
                cas="-",
                final_category="test-category",
                confidence=0.95,
            )

            self.assertIsNone(memory.lookup(cas="-", raw_name="unknown bottle"))
            self.assertIsNone(memory.lookup(cas="-"))
            self.assertEqual(memory.lookup(raw_name="PMSF solution")["standard_name"], "PMSF")

    def test_duplicate_identity_updates_existing_record(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            self.assertTrue(
                memory.add_record(
                    raw_name="old reagent name",
                    cleaned_name="old clean",
                    standard_name="old standard",
                    cas="123-45-6",
                    final_category="normal",
                    confidence=0.85,
                    reason="first",
                    source="unit_test",
                )
            )
            self.assertTrue(
                memory.add_record(
                    raw_name="new reagent name",
                    cleaned_name="new clean",
                    standard_name="new standard",
                    cas="123-45-6",
                    final_category="normal",
                    confidence=0.95,
                    reason="second",
                    source="unit_test_2",
                )
            )

            self.assertEqual(memory.count_records(), 1)
            row = memory.lookup(cas="123-45-6")
            assert row is not None
            self.assertEqual(row["raw_name"], "new reagent name")
            self.assertEqual(row["standard_name"], "new standard")
            self.assertEqual(row["confidence"], 0.95)
            self.assertIn("first", row["reason"])
            self.assertIn("second", row["reason"])

    def test_duplicate_name_without_cas_updates_existing_record(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(
                raw_name="name only",
                cleaned_name="shared clean",
                standard_name="shared standard",
                final_category="normal",
                confidence=0.9,
            )
            memory.add_record(
                raw_name="name only changed",
                cleaned_name="shared clean",
                standard_name="shared standard",
                final_category="normal",
                confidence=0.91,
            )

            self.assertEqual(memory.count_records(), 1)
            row = memory.lookup(standard_name="shared standard")
            assert row is not None
            self.assertEqual(row["raw_name"], "name only changed")

    def test_conflicting_identity_keeps_real_category_conflict(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            self.assertTrue(
                memory.add_record(
                    raw_name="same cas one",
                    cas="456-78-9",
                    final_category="normal",
                    confidence=0.9,
                )
            )
            self.assertFalse(
                memory.add_record(
                    raw_name="same cas two",
                    cas="456-78-9",
                    final_category="flammable",
                    confidence=0.9,
                )
            )

            self.assertEqual(memory.count_records(), 2)
            self.assertEqual(memory.count_conflicting_records(), 2)
            self.assertIsNone(memory.lookup(cas="456-78-9"))

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

    def test_generic_unknown_name_is_reusable_unknown_class(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(
                raw_name="\u672a\u77e5\u7c89\u72b6\u7269",
                cleaned_name="\u672a\u77e5\u7c89\u72b6\u7269",
                standard_name="\u672a\u77e5\u7c89\u72b6\u7269",
                cas="-",
                final_category="\u666e\u901a\u7c7b",
                confidence=0.95,
                reason="old imported result",
            )
            row = memory.find_any(raw_name="\u672a\u77e5\u7c89\u72b6\u7269")

            self.assertEqual(row["final_category"], "\u672a\u77e5\u7c7b")
            self.assertEqual(row["reusable"], 1)
            self.assertEqual(row["need_manual_review"], 0)
            self.assertEqual(row["conflict"], 0)

    def test_business_normal_keyword_overrides_unknown_memory_rule(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(
                raw_name="\u8bd5\u5242\uff08\u672a\u77e5\uff09",
                cleaned_name="\u8bd5\u5242 \u672a\u77e5",
                standard_name="\u8bd5\u5242 \u672a\u77e5",
                cas="-",
                final_category="\u666e\u901a\u7c7b",
                confidence=0.95,
                reason="business normal keyword",
            )
            row = memory.find_any(raw_name="\u8bd5\u5242\uff08\u672a\u77e5\uff09")

            self.assertEqual(row["final_category"], "\u666e\u901a\u7c7b")
            self.assertEqual(row["reusable"], 1)
            self.assertEqual(row["need_manual_review"], 0)
            self.assertEqual(row["conflict"], 0)

            memory.add_record(
                raw_name="\u672a\u77e5\u836f\u7269",
                cleaned_name="\u672a\u77e5\u836f\u7269",
                standard_name="\u672a\u77e5\u836f\u7269",
                cas="-",
                final_category="\u666e\u901a\u7c7b",
                confidence=0.95,
                reason="business normal keyword",
            )
            drug_row = memory.find_any(raw_name="\u672a\u77e5\u836f\u7269")

            self.assertEqual(drug_row["final_category"], "\u666e\u901a\u7c7b")
            self.assertEqual(drug_row["reusable"], 1)
            self.assertEqual(drug_row["need_manual_review"], 0)
            self.assertEqual(drug_row["conflict"], 0)

            memory.add_record(
                raw_name="\u672a\u77e5\u76d0\u9178\u6587\u62c9\u6cd5\u8f9b",
                cleaned_name="\u76d0\u9178\u6587\u62c9\u6cd5\u8f9b",
                standard_name="\u76d0\u9178\u6587\u62c9\u6cd5\u8f9b",
                cas="-",
                final_category="\u666e\u901a\u7c7b",
                confidence=0.95,
                reason="pharmaceutical normal keyword",
            )
            venlafaxine_row = memory.find_any(raw_name="\u672a\u77e5\u76d0\u9178\u6587\u62c9\u6cd5\u8f9b")

            self.assertEqual(venlafaxine_row["final_category"], "\u666e\u901a\u7c7b")
            self.assertEqual(venlafaxine_row["reusable"], 1)
            self.assertEqual(venlafaxine_row["need_manual_review"], 0)
            self.assertEqual(venlafaxine_row["conflict"], 0)

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

    def test_deduplicate_conflicting_records_keeps_true_conflicts_only(self) -> None:
        tmp, memory = self.make_memory()
        with tmp:
            memory.add_record(raw_name="dup-a", cas="111-22-3", final_category="普通类", confidence=0.9)
            memory.add_record(raw_name="dup-b", cas="111-22-3", final_category="易燃类", confidence=0.9)
            with closing(memory._connect()) as conn:  # noqa: SLF001 - focused database cleanup regression test.
                with conn:
                    base = conn.execute(
                        "SELECT * FROM reagent_memory WHERE cas_key = ? AND final_category = ? LIMIT 1",
                        ("111-22-3", "普通类"),
                    ).fetchone()
                    assert base is not None
                    values = dict(base)
                    values.pop("id")
                    columns = ", ".join(values)
                    placeholders = ", ".join("?" for _ in values)
                    conn.execute(
                        f"INSERT INTO reagent_memory ({columns}) VALUES ({placeholders})",
                        list(values.values()),
                    )

            result = memory.deduplicate_conflicting_records()

            self.assertEqual(result["deleted"], 1)
            self.assertEqual(result["cleared"], 0)
            self.assertEqual(memory.count_records(), 2)
            self.assertEqual(memory.count_conflicting_records(), 2)

            memory.delete_conflicting_records()
            memory.add_record(raw_name="false-conflict", cas="222-33-4", final_category="普通类", confidence=0.9)
            row = memory.lookup(cas="222-33-4")
            assert row is not None
            memory.update_record(row["id"], {"conflict": True, "reusable": False})

            result = memory.deduplicate_conflicting_records()

            self.assertEqual(result["cleared"], 1)
            self.assertEqual(memory.count_conflicting_records(), 0)
            self.assertIsNotNone(memory.lookup(cas="222-33-4"))


if __name__ == "__main__":
    unittest.main()
