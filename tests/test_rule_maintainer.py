from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from rule_maintainer import RuleMaintainer  # noqa: E402


class RuleMaintainerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.tempdir.name)
        (self.root_dir / "config").mkdir()
        shutil.copyfile(ROOT_DIR / "config" / "rules_structured.xlsx", self.root_dir / "config" / "rules_structured.xlsx")
        self.settings = {
            "paths": {
                "structured_rules_excel": "config/rules_structured.xlsx",
                "rule_candidates_excel": "config/rule_candidates.xlsx",
            }
        }
        self.maintainer = RuleMaintainer.from_settings(self.settings, self.root_dir)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_records_pending_candidate_and_deduplicates(self) -> None:
        reagent = {"\u8bd5\u5242\u540d\u79f0": "\u6a21\u62df\u65b0\u8bd5\u5242", "CAS\u53f7": ""}
        name_result = {"standard_name": "\u6a21\u62df\u6807\u51c6\u540d"}
        search_result = {"need_manual_review": True, "cas": "", "url": "https://example.test/reagent"}
        extracted = {"evidence": ["source text"]}
        classification = {
            "final_category": "",
            "matched_categories": [],
            "reason": "\u9700\u4eba\u5de5\u590d\u6838",
            "need_manual_review": True,
        }

        self.assertTrue(
            self.maintainer.record_candidate(reagent, name_result, search_result, extracted, classification)
        )
        self.assertFalse(
            self.maintainer.record_candidate(reagent, name_result, search_result, extracted, classification)
        )

        candidates = pd.read_excel(self.maintainer.candidates_path, dtype=str).fillna("")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates.loc[0, "status"], "pending")
        self.assertEqual(candidates.loc[0, "standard_name"], "\u6a21\u62df\u6807\u51c6\u540d")

    def test_promotes_only_approved_candidates_to_structured_examples(self) -> None:
        self.maintainer.ensure_candidate_file()
        candidates = pd.DataFrame(
            [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "reagent_name": "\u4eba\u5de5\u5df2\u786e\u8ba4\u8bd5\u5242",
                    "standard_name": "\u4eba\u5de5\u5df2\u786e\u8ba4\u8bd5\u5242",
                    "cas": "",
                    "current_suggestion": "",
                    "manual_result": "\u6c27\u5316\u5242",
                    "candidate_category": "",
                    "reason": "\u4eba\u5de5\u786e\u8ba4",
                    "evidence": "",
                    "source_url": "",
                    "status": "approved",
                    "reviewer": "tester",
                    "reviewed_at": "2026-01-01",
                }
            ]
        )
        candidates.to_excel(self.maintainer.candidates_path, index=False)

        promoted = self.maintainer.promote_approved_candidates()

        self.assertEqual(promoted, 1)
        updated_candidates = pd.read_excel(self.maintainer.candidates_path, dtype=str).fillna("")
        self.assertEqual(updated_candidates.loc[0, "status"], "promoted")

        examples = pd.read_excel(
            self.maintainer.structured_rules_path,
            sheet_name="examples",
            dtype=str,
            engine="openpyxl",
        ).fillna("")
        added = examples[
            (examples["category"] == "\u6c27\u5316\u5242")
            & (examples["example_name"] == "\u4eba\u5de5\u5df2\u786e\u8ba4\u8bd5\u5242")
        ]
        self.assertEqual(len(added), 1)


if __name__ == "__main__":
    unittest.main()
