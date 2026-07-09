from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from rule_engine import RuleEngine  # noqa: E402


class StructuredRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = RuleEngine.from_settings(
            {
                "paths": {
                    "structured_rules_excel": "config/rules_structured.xlsx",
                    "rules_excel": "config/rules.xlsx",
                }
            },
            ROOT_DIR,
        )

    def test_azide_uses_explosive_class(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "\u53e0\u5316\u94a0",
                "text": "sodium azide",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "\u6613\u7206\u7c7b")
        self.assertFalse(result["need_manual_review"])

    def test_specific_reject_examples_override_generic_azide(self) -> None:
        for name in ["叠氮化铅", "雷汞", "TNT", "史蒂芬酸铅"]:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "text": name,
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], "不建议接收类")
                self.assertFalse(result["need_manual_review"])

    def test_reject_examples_from_structured_rules(self) -> None:
        for name in ["黑索今", "RDX", "太安", "PETN", "奥克托今", "HMX", "医疗废物", "放射性元素", "氚", "镭", "铀"]:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "text": name,
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], "不建议接收类")
                self.assertFalse(result["need_manual_review"])

    def test_lead_mercury_thallium_beryllium_examples_are_reject_class(self) -> None:
        for name in [
            "硝酸汞",
            "碘化汞",
            "溴化汞",
            "氰化汞",
            "硫氰酸汞",
            "氯化甲氧基乙基汞",
            "铊",
            "氧化亚铊",
            "氧化铊",
            "碳酸亚铊",
            "乙酸亚铊",
            "丙二酸铊",
            "铍类",
            "乙酸铅",
        ]:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "text": name,
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], "不建议接收类")
                self.assertFalse(result["need_manual_review"])

    def test_perchloric_acid_concentration_rules(self) -> None:
        low = self.engine.classify({"reagent_name": "70%\u9ad8\u6c2f\u9178", "allow_default_normal": True})
        high = self.engine.classify({"reagent_name": "75%\u9ad8\u6c2f\u9178", "allow_default_normal": True})
        missing = self.engine.classify({"reagent_name": "\u9ad8\u6c2f\u9178", "allow_default_normal": True})

        self.assertEqual(low["final_category"], "\u7279\u6b8a\u9178")
        self.assertEqual(high["final_category"], "\u6613\u7206\u7c7b")
        self.assertEqual(missing["final_category"], "\u6613\u7206\u7c7b")

    def test_bromine_iodine_keeps_priority_over_flammable(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "4-\u6eb4-2-\u7f9f\u57fa-6-\u7532\u57fa\u82ef\u7532\u9178\u7532\u916f",
                "text": "Flash point 50 C. Source says flammable liquid. bromo compound.",
                "suggested_categories": ["\u6eb4\u7898\u7c7b", "\u6613\u71c3\u6db2\u4f53"],
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "\u6eb4\u7898\u7c7b")

    def test_tin_compound_matches_heavy_metal_class(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "\u4e09\u6c1f\u7532\u78fa\u9178\u9521",
                "standard_name": "\u4e09\u6c1f\u7532\u78fa\u9178\u9521(II)",
                "text": "\u4e09\u6c1f\u7532\u78fa\u9178\u9521",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "\u91cd\u91d1\u5c5e\u7c7b")
        self.assertFalse(result["need_manual_review"])

    def test_indole_derivative_matches_odor_class(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "4-\u6c28\u57fa-N-\u7532\u57fa\u5432\u54da",
                "standard_name": "4-amino-1-methylindole",
                "text": "Chemsrc matched 4-amino-1-methylindole. Flash point 152.2 C.",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "\u5f02\u5473")
        self.assertFalse(result["need_manual_review"])

    def test_bromine_iodine_does_not_match_raw_text_noise(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "\u987a\u5f0f-3-\u7f9f\u57fa\u73af\u4e01\u57fa\u7fa7\u9178\u7532\u916f",
                "standard_name": "\u987a\u5f0f-3-\u7f9f\u57fa\u73af\u4e01\u57fa\u7fa7\u9178\u7532\u916f",
                "english_name": "methyl cis-3-hydroxycyclobutane carboxylate",
                "text": "web page footer mentions bromo bromide iodide unrelated terms",
                "evidence": ["bromide was found in an unrelated navigation block"],
                "allow_default_normal": True,
            }
        )

        self.assertNotEqual(result["final_category"], "\u6eb4\u7898\u7c7b")

    def test_normal_category_ignores_halogen_price_note(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "\u672a\u77e5\u4e2d\u95f4\u4f53",
                "text": "\u666e\u901a\u7c7b\u89e3\u91ca\u5907\u6ce8\uff1a\u542b\u6c1f\u6c2f\u6eb4\u7898\u7c7b\uff08\u5364\u4ee3\u70c3\u53ca\u884d\u751f\u7269\u9664\u5916\uff09\u4ef7\u683c\u7ffb\u500d",
                "allow_default_normal": False,
            }
        )

        self.assertNotEqual(result["final_category"], "\u666e\u901a\u7c7b")

    def test_hydrochloride_salt_does_not_match_special_acid(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "(S)-1-\u6c28\u57fa\u7425\u73c0\u91784-\u7532\u916f\u53d4\u4e01\u916f\u76d0\u9178\u76d0",
                "standard_name": "(S)-1-\u6c28\u57fa\u7425\u73c0\u91784-\u7532\u916f\u53d4\u4e01\u916f\u76d0\u9178\u76d0",
                "english_name": "(S)-1-amino succinic acid methyl tert-butyl ester hydrochloride",
                "suggested_categories": ["\u7279\u6b8a\u9178"],
                "text": "hydrochloride salt. source mentions hydrochloric acid in unrelated context.",
                "allow_default_normal": True,
            }
        )

        self.assertNotEqual(result["final_category"], "\u7279\u6b8a\u9178")

    def test_default_manual_review_category_blocks_auto_pass(self) -> None:
        result = self.engine.classify({"reagent_name": "\u6c30\u5316\u94a0", "text": "\u6c30\u5316\u94a0"})

        self.assertEqual(result["final_category"], "\u5267\u6bd2\u54c1")
        self.assertTrue(result["need_manual_review"])


if __name__ == "__main__":
    unittest.main()
