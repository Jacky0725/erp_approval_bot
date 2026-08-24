from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from approval_flow import ApprovalFlowMixin  # noqa: E402
from llm_extractor import LlmExtractor  # noqa: E402
from rule_engine import Rule, RuleEngine  # noqa: E402
from web_runner import review_queue_summary  # noqa: E402


class AcidSaltGuardrailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RuleEngine(
            rules=[
                Rule(
                    category="特殊酸",
                    explanation="",
                    examples="",
                    explanation_keywords=(),
                    example_keywords=(),
                ),
                Rule(
                    category="常规酸",
                    explanation="",
                    examples="",
                    explanation_keywords=(),
                    example_keywords=(),
                ),
            ],
            priority=["特殊酸", "常规酸", "普通类"],
        )

    def test_acid_salt_names_do_not_match_regular_or_special_acid(self) -> None:
        names = (
            "五氯苯酚钠盐",
            "紫尿酸铵",
            "氨基磺酸铵",
            "氢磺酸氨",
            "苯肼盐酸盐",
            "硫酸铜",
            "硝酸镁",
            "盐酸苯肼",
            "magnesium nitrate",
            "copper sulfate",
            "phenylhydrazine hydrochloride",
            "sodium pentachlorophenolate",
        )

        for name in names:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "cleaned_name": name,
                        "suggested_categories": ["特殊酸", "常规酸"],
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], "普通类")
                self.assertNotIn("常规酸", result["matched_categories"])
                self.assertNotIn("特殊酸", result["matched_categories"])

    def test_true_mineral_acids_still_match_regular_acid(self) -> None:
        names = ("盐酸", "硝酸", "硫酸", "浓硫酸", "发烟盐酸", "10% HCl", "2mol HCl")

        for name in names:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "cleaned_name": name,
                        "suggested_categories": ["特殊酸"],
                    }
                )

                self.assertEqual(result["final_category"], "常规酸")
                self.assertIn("常规酸", result["matched_categories"])
                self.assertNotIn("特殊酸", result["matched_categories"])

    def test_llm_fallback_does_not_turn_salt_evidence_into_regular_acid(self) -> None:
        salt_evidence = (
            "ammonium sulfate salt mentions sulfuric acid",
            "sodium pentachlorophenolate source mentions sulfuric acid",
            "ammonium urate acid salt mentions nitric acid",
            "ammonium sulfamate / sulfamic acid ammonium salt",
            "sodium carboxylate salt mentions hydrochloric acid",
            "alkyl sulfonate salt mentions sulfuric acid",
            "五氯苯酚钠盐 资料中出现 sulfuric acid",
            "紫尿酸铵 资料中出现 硝酸",
            "氨基磺酸铵 资料中出现 硫酸",
        )

        for text in salt_evidence:
            with self.subTest(text=text):
                self.assertEqual(LlmExtractor._ordinary_mineral_acid_label(text), "")

    def test_llm_fallback_keeps_explicit_mineral_acid_solution(self) -> None:
        self.assertEqual(LlmExtractor._ordinary_mineral_acid_label("sulfuric acid solution"), "硫酸")
        self.assertEqual(LlmExtractor._ordinary_mineral_acid_label("hydrochloric acid solution"), "盐酸")

    def test_ambiguous_acid_direct_rule_skips_acid_salt_like_name(self) -> None:
        self.assertEqual(ApprovalFlowMixin.ambiguous_acid_reason("疑似氨基磺酸铵/硫酸"), "")
        self.assertEqual(ApprovalFlowMixin.ambiguous_acid_reason("可能紫尿酸铵或硫酸"), "")
        self.assertTrue(ApprovalFlowMixin.ambiguous_acid_reason("疑似硫酸或磷酸"))

    def test_review_queue_downgrades_acid_salt_suggestion_without_trusted_web(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir(parents=True)
            pd.DataFrame(
                [
                    {
                        "timestamp": "2026-08-24T10:00:00",
                        "list_number": "SJ1",
                        "chemical_name": "五氯苯酚钠盐",
                        "standard_name": "五氯苯酚钠盐",
                        "reason": "Known ordinary mineral acid (硫酸); classify as regular acid by business rule.",
                        "status": "pending",
                        "suggested_category": "常规酸",
                        "classification_confidence": "0.84",
                        "evidence_source_type": "none",
                    }
                ]
            ).to_excel(data_dir / "review_queue.xlsx", index=False)

            row = review_queue_summary(root)["preview"][0]

        self.assertEqual(row["display_suggestion"], "暂无可靠建议")
        self.assertEqual(row["evidence_status"], "证据不足")
        self.assertEqual(row["allow_suggestion_preselect"], "False")
        self.assertIn("酸盐/有机酸盐", row["display_reason"])

    def test_review_queue_keeps_non_acid_suggestion_for_acid_salt_like_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir(parents=True)
            pd.DataFrame(
                [
                    {
                        "timestamp": "2026-08-24T10:00:00",
                        "list_number": "SJ1",
                        "chemical_name": "重铬酸钾",
                        "standard_name": "重铬酸钾",
                        "reason": "oxidizer",
                        "status": "pending",
                        "suggested_category": "氧化剂",
                        "classification_confidence": "0.9",
                        "evidence_source_type": "none",
                    }
                ]
            ).to_excel(data_dir / "review_queue.xlsx", index=False)

            row = review_queue_summary(root)["preview"][0]

        self.assertEqual(row["display_suggestion"], "氧化剂")
        self.assertEqual(row["allow_suggestion_preselect"], "True")


if __name__ == "__main__":
    unittest.main()
