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

    def test_unknown_category_is_not_a_structured_manual_review_category(self) -> None:
        self.assertNotIn("未知类", self.engine.manual_review_categories)

        result = self.engine.classify(
            {"reagent_name": "未知样品", "text": "无标签，无法辨识"}
        )

        self.assertEqual(result["final_category"], "未知类")
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

    def test_mercury_name_overrides_business_normal_keywords(self) -> None:
        for name in [
            "三氟甲烷磺酸汞",
            "汞标准溶液",
            "Mercury standard solution",
        ]:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "english_name": name,
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

    def test_chromium_salt_matches_heavy_metal_class(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "\u5341\u4e8c\u6c34\u5408\u786b\u9178\u94ec\u94be",
                "standard_name": "\u5341\u4e8c\u6c34\u5408\u786b\u9178\u94ec\u94be",
                "text": "\u5341\u4e8c\u6c34\u5408\u786b\u9178\u94ec\u94be",
                "allow_default_normal": True,
            }
        )

        self.assertEqual(result["final_category"], "\u91cd\u91d1\u5c5e\u7c7b")
        self.assertFalse(result["need_manual_review"])

    def test_only_configured_elements_are_treated_as_heavy_metal_by_name(self) -> None:
        heavy_metal_cases = [
            "\u6c2f\u5316\u9549",
            "\u5341\u4e8c\u6c34\u5408\u786b\u9178\u94ec\u94be",
            "\u6c2f\u5316\u954d",
            "\u785d\u9178\u94f6",
            "\u504f\u9492\u9178\u94f5",
            "\u4e8c\u6c27\u5316\u7852",
            "\u6c2f\u5316\u94b4",
            "\u4e09\u6c1f\u7532\u78fa\u9178\u9521",
        ]
        for name in heavy_metal_cases:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "text": name,
                        "allow_default_normal": True,
                    }
                )

                self.assertIn("\u91cd\u91d1\u5c5e\u7c7b", result["matched_categories"])

        non_heavy_metal_cases = [
            "\u786b\u9178\u94dc",
            "\u785d\u9178\u9541",
            "\u6c2f\u5316\u950c",
            "\u6c2f\u5316\u94dd",
            "\u4e8c\u6c27\u5316\u9530",
            "\u4e09\u6c2f\u5316\u9511",
            "\u785d\u9178\u94cb",
        ]
        for name in non_heavy_metal_cases:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "text": name,
                        "allow_default_normal": True,
                    }
                )

                self.assertNotIn("\u91cd\u91d1\u5c5e\u7c7b", result["matched_categories"])
                self.assertNotEqual(result["final_category"], "\u91cd\u91d1\u5c5e\u7c7b")

    def test_heavy_metal_boolean_requires_configured_element(self) -> None:
        copper = self.engine.classify(
            {
                "reagent_name": "\u786b\u9178\u94dc",
                "standard_name": "\u786b\u9178\u94dc",
                "text": "source incorrectly says heavy metal",
                "heavy_metal": True,
                "allow_default_normal": True,
            }
        )
        cadmium = self.engine.classify(
            {
                "reagent_name": "\u6c2f\u5316\u9549",
                "standard_name": "\u6c2f\u5316\u9549",
                "text": "source says heavy metal",
                "heavy_metal": True,
                "allow_default_normal": True,
            }
        )

        self.assertNotIn("\u91cd\u91d1\u5c5e\u7c7b", copper["matched_categories"])
        self.assertNotEqual(copper["final_category"], "\u91cd\u91d1\u5c5e\u7c7b")
        self.assertIn("\u91cd\u91d1\u5c5e\u7c7b", cadmium["matched_categories"])

    def test_broad_business_normal_keywords_do_not_override_risk_classes(self) -> None:
        cases = [
            ("砷标准液", "高毒类"),
            ("砷标准溶液", "高毒类"),
            ("三氧化二砷试剂", "高毒类"),
            ("亚砷酸钠标准品", "高毒类"),
            ("铅ICP标准溶液", "不建议接收类"),
            ("汞标准液", "不建议接收类"),
            ("镉校准液", "重金属类"),
            ("铬标准液", "重金属类"),
            ("重铬酸钾标准液", "氧化剂"),
            ("吲哚标准溶液", "异味"),
            ("3-Bromo-6-nitroindole 试剂", "异味"),
        ]
        for name, expected in cases:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "english_name": name,
                        "text": name,
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], expected)
                self.assertFalse(result["need_manual_review"])

    def test_arsenic_reagent_alias_is_not_treated_as_arsenic_compound(self) -> None:
        names = [
            "砷试剂",
            "二乙基二硫代氨基甲酸银",
            "二乙基二硫代氨基甲酸银盐",
            "二乙氨基二硫代甲酸银",
            "二乙基氨荒酸银",
            "DDTC银盐",
            "DETC银盐",
            "AgDDC",
            "Ag-DDC",
            "AgDDTC",
            "Silver diethyldithiocarbamate",
        ]
        for name in names:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "english_name": name,
                        "text": name,
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], "普通类")
                self.assertNotIn("高毒类", result["matched_categories"])
                self.assertFalse(result["need_manual_review"])

    def test_hazard_evidence_overrides_business_normal_keywords(self) -> None:
        names = [
            "蛋白免疫抗体试剂",
            "一次性病毒采样管",
            "病毒保存液",
            "苏木素染色液",
            "卡马西平药物对照品",
            "盐酸文拉法辛",
        ]
        for name in names:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "text": "这里的证据提到含铅、溴和吲哚",
                        "allow_default_normal": True,
                    }
                )

                self.assertNotEqual(result["final_category"], "普通类")
                self.assertFalse(result["need_manual_review"])

    def test_unknown_keyword_overrides_business_normal_keywords_without_manual_review(self) -> None:
        for name in (
            "\u672a\u77e5\u7ec6\u80de\u57f9\u517b\u6db2",
            "\u8bd5\u5242\uff08\u672a\u77e5\uff09",
            "\u672a\u77e5\u4e00\u6b21\u6027\u75c5\u6bd2\u91c7\u6837\u7ba1",
        ):
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "text": name,
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], "\u672a\u77e5\u7c7b")
                self.assertFalse(result["need_manual_review"])

    def test_low_priority_business_normal_keywords_apply_without_other_matches(self) -> None:
        names = [
            "\u7eb3\u7c73\u6750\u6599",
            "\u4e19\u70ef\u9178\u5355\u4f53",
            "\u7845\u80f6\u62c5\u4f53",
            "\u52a9\u6ee4\u5242",
            "\u8131\u8272\u5242",
            "\u6a21\u62df\u6837\u54c1",
            "\u50ac\u5316\u5242",
            "\u4eba\u5de5\u6d77\u6c34",
        ]
        for name in names:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "text": name,
                    }
                )

                self.assertEqual(result["final_category"], "\u666e\u901a\u7c7b")
                self.assertFalse(result["need_manual_review"])

    def test_low_priority_business_normal_keywords_yield_to_other_categories(self) -> None:
        cases = [
            ("\u7eb3\u7c73\u786b\u9178\u94ec\u94be", "\u91cd\u91d1\u5c5e\u7c7b"),
            ("\u6c5e\u50ac\u5316\u5242", "\u4e0d\u5efa\u8bae\u63a5\u6536\u7c7b"),
            ("3-Bromo-6-nitroindole \u5355\u4f53", "\u5f02\u5473"),
            ("\u5432\u54da\u4eba\u5de5\u5408\u6210\u7269", "\u5f02\u5473"),
        ]
        for name, expected in cases:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "english_name": name,
                        "text": name,
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], expected)

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

    def test_odor_keyword_names_prefer_odor_class(self) -> None:
        for name in [
            "\u5432\u54da",
            "\u5f02\u5432\u54da",
            "\u5421\u5576",
            "2-\u7532\u57fa\u5421\u5576",
            "\u4e59\u786b\u9187",
            "4-\u5def\u57fa\u5421\u5576",
            "indole",
            "isoindole",
            "pyridine",
            "2-methylpyridine",
            "ethanethiol",
            "mercaptopyridine",
            "3-Bromo-6-nitroindole",
            "7-\u6eb4-5-\u6c1f-1H-\u5432\u54da-2,3-\u4e8c\u916e",
        ]:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "english_name": name,
                        "text": name,
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

    def test_mineral_acid_salts_do_not_match_regular_or_special_acid(self) -> None:
        for name in (
            "\u785d\u9178\u9541",
            "\u786b\u9178\u94dc",
            "\u82ef\u80bc\u76d0\u9178\u76d0",
            "\u76d0\u9178\u82ef\u80bc",
            "magnesium nitrate",
            "copper sulfate",
            "phenylhydrazine hydrochloride",
        ):
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "english_name": name,
                        "suggested_categories": ["\u7279\u6b8a\u9178", "\u5e38\u89c4\u9178"],
                        "allow_default_normal": True,
                    }
                )

                self.assertNotIn(result["final_category"], ("\u5e38\u89c4\u9178", "\u7279\u6b8a\u9178"))
                self.assertNotIn("\u5e38\u89c4\u9178", result["matched_categories"])
                self.assertNotIn("\u7279\u6b8a\u9178", result["matched_categories"])

    def test_plain_mineral_acids_still_match_regular_acid(self) -> None:
        for name in (
            "\u76d0\u9178",
            "\u785d\u9178",
            "\u786b\u9178",
            "\u53d1\u70df\u76d0\u9178",
            "10% HCl",
            "2mol HCl",
            "hydrochloric acid solution",
        ):
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "english_name": name,
                        "suggested_categories": ["\u7279\u6b8a\u9178"],
                        "allow_default_normal": True,
                    }
                )

                self.assertEqual(result["final_category"], "\u5e38\u89c4\u9178")
                self.assertIn("\u5e38\u89c4\u9178", result["matched_categories"])
                self.assertNotIn("\u7279\u6b8a\u9178", result["matched_categories"])

    def test_concentrated_sulfuric_and_nitric_acid_are_special_acids(self) -> None:
        for name in ("浓硫酸（>70%）", "浓硝酸（>65%）"):
            with self.subTest(name=name):
                result = self.engine.classify({"reagent_name": name, "text": name})
                self.assertEqual(result["final_category"], "特殊酸")

    def test_perchloric_acid_72_percent_has_no_boundary_gap(self) -> None:
        result = self.engine.classify({"reagent_name": "高氯酸72%", "text": "高氯酸72%"})
        self.assertEqual(result["final_category"], "特殊酸")

    def test_perchloric_acid_reads_separate_concentration_field(self) -> None:
        result = self.engine.classify({"reagent_name": "高氯酸", "concentration": "72%"})
        self.assertEqual(result["final_category"], "特殊酸")
        self.assertIn("SPECIAL-SPA-001", result["matched_rule_ids"])

    def test_ld50_value_is_not_concatenated_with_endpoint_number(self) -> None:
        result = self.engine.classify({
            "reagent_name": "测试物",
            "toxicity": "大鼠经口 LD50 4 mg/kg",
        })
        self.assertEqual(result["final_category"], "剧毒品")
        self.assertIn("T-TOX-001", result["matched_rule_ids"])
        self.assertTrue(result["need_manual_review"])

    def test_lc50_value_is_not_concatenated_with_endpoint_number(self) -> None:
        result = self.engine.classify({
            "reagent_name": "测试气体",
            "toxicity": "吸入 LC50 400 ppm（气体）",
        })
        self.assertEqual(result["final_category"], "高毒类")
        self.assertIn("T-TOX-INH-GAS-001", result["matched_rule_ids"])
        self.assertTrue(result["need_manual_review"])

    def test_hazard_keywords_take_priority_over_unknown_and_drug_words(self) -> None:
        for name in ("未知浓度叠氮化钠", "某药物叠氮化物"):
            result = self.engine.classify({"reagent_name": name, "text": name})
            self.assertEqual(result["final_category"], "易爆类")

    def test_negated_mercury_does_not_trigger_reject(self) -> None:
        result = self.engine.classify({"reagent_name": "无汞试剂", "text": "无汞试剂"})
        self.assertNotEqual(result["final_category"], "不建议接收类")

    def test_amino_sulfonic_acid_is_not_misread_as_ammonium_salt(self) -> None:
        result = self.engine.classify({"reagent_name": "对氨基苯磺酸", "text": "对氨基苯磺酸"})
        self.assertEqual(result["final_category"], "特殊酸")

    def test_structured_rule_id_and_confidence_are_executed(self) -> None:
        result = self.engine.classify({"reagent_name": "叠氮化钠", "text": "叠氮化钠"})
        self.assertIn("SPECIAL-EXP-001", result["matched_rule_ids"])
        self.assertEqual(result["confidence"], 0.92)

    def test_irritancy_requires_explicit_skin_or_eye_irritation_evidence(self) -> None:
        cases = (
            ("H315", "IRR-GHS-SKIN"),
            ("H319", "IRR-GHS-EYE"),
            ("造成皮肤刺激", "IRR-GHS-SKIN"),
            ("引起皮肤刺激", "IRR-GHS-SKIN"),
            ("Causes skin irritation", "IRR-GHS-SKIN"),
            ("造成严重眼刺激", "IRR-GHS-EYE"),
            ("引起严重眼刺激", "IRR-GHS-EYE"),
            ("Causes serious eye irritation", "IRR-GHS-EYE"),
            ("该物质具有催泪作用", "IRR-LACH"),
            ("Lachrymatory agent", "IRR-LACH"),
        )
        for evidence, rule_id in cases:
            with self.subTest(evidence=evidence):
                result = self.engine.classify({"reagent_name": "测试物", "evidence": evidence})
                self.assertEqual(result["final_category"], "刺激性")
                self.assertFalse(result["need_manual_review"])
                self.assertIn(rule_id, result["matched_rule_ids"])

    def test_irritancy_does_not_match_broad_or_negated_skin_text(self) -> None:
        for evidence in (
            "对皮肤无刺激性",
            "对皮肤有害",
            "不引起皮肤刺激",
            "No skin irritation was observed",
        ):
            with self.subTest(evidence=evidence):
                result = self.engine.classify({"reagent_name": "测试物", "evidence": evidence})
                self.assertNotEqual(result["final_category"], "刺激性")
                self.assertTrue(result["need_manual_review"])

    def test_non_irritation_ghs_hazards_force_manual_review(self) -> None:
        cases = (
            "H314 Causes severe skin burns and eye damage",
            "H318 Causes serious eye damage",
            "H317 May cause an allergic skin reaction",
            "H335 May cause respiratory irritation",
        )
        for evidence in cases:
            with self.subTest(evidence=evidence):
                result = self.engine.classify(
                    {
                        "reagent_name": "测试物",
                        "evidence": evidence,
                        "suggested_categories": ["刺激性"],
                        "allow_default_normal": True,
                    }
                )
                self.assertEqual(result["final_category"], "")
                self.assertTrue(result["need_manual_review"])
                self.assertIn("不能等同于皮肤/眼刺激", result["reason"])

    def test_severe_damage_overrides_concurrent_irritation_statement(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "测试物",
                "evidence": "H314; H315",
            }
        )
        self.assertEqual(result["final_category"], "")
        self.assertTrue(result["need_manual_review"])

    def test_llm_irritancy_hint_or_legacy_name_example_is_not_enough(self) -> None:
        for reagent_info in (
            {"reagent_name": "测试物", "suggested_categories": ["刺激性"]},
            {"reagent_name": "苯酚类"},
            {"reagent_name": "硫酸二甲酯"},
            {"reagent_name": "瓦斯"},
        ):
            with self.subTest(reagent_info=reagent_info):
                result = self.engine.classify(reagent_info)
                self.assertNotEqual(result["final_category"], "刺激性")

    def test_inhalation_thresholds_are_loaded_from_workbook(self) -> None:
        result = self.engine.classify({
            "reagent_name": "测试气体",
            "toxicity": "inhalation LC50 gas 400 ppm 4 h",
            "text": "inhalation LC50 gas 400 ppm 4 h",
        })
        self.assertEqual(result["final_category"], "高毒类")
        self.assertIn("T-TOX-INH-GAS-001", result["matched_rule_ids"])
        self.assertFalse(result["need_manual_review"])

    def test_generic_toxicity_or_oxidizing_text_does_not_become_special_acid(self) -> None:
        for text in ("该物质具有毒性", "该物质具有氧化性"):
            result = self.engine.classify({"reagent_name": "测试物", "text": text})
            self.assertNotEqual(result["final_category"], "特殊酸")

    def test_structured_hazard_booleans_are_executable(self) -> None:
        cases = {
            "explosive_risk": "易爆类",
            "water_reactive": "强反应性",
            "oxidizing": "氧化剂",
        }
        for field, expected in cases.items():
            with self.subTest(field=field):
                result = self.engine.classify({"reagent_name": "测试物", field: True})
                self.assertEqual(result["final_category"], expected)
                self.assertTrue(result["matched_rule_ids"])

        heavy_metal = self.engine.classify({"reagent_name": "氯化镉", "heavy_metal": True})
        self.assertEqual(heavy_metal["final_category"], "重金属类")
        self.assertTrue(heavy_metal["matched_rule_ids"])

    def test_structured_flammable_requires_liquid_context(self) -> None:
        liquid = self.engine.classify({"reagent_name": "测试液体", "flammable": True})
        solid = self.engine.classify({"reagent_name": "测试粉末", "flammable": True})
        self.assertEqual(liquid["final_category"], "易燃液体")
        self.assertNotEqual(solid["final_category"], "易燃液体")

    def test_dermal_ld50_boundary_is_non_overlapping(self) -> None:
        acute = self.engine.classify({"reagent_name": "测试物", "toxicity": "dermal LD50 50 mg/kg"})
        high = self.engine.classify({"reagent_name": "测试物", "toxicity": "dermal LD50 51 mg/kg"})
        self.assertEqual(acute["final_category"], "剧毒品")
        self.assertNotIn("高毒类", acute["matched_categories"])
        self.assertEqual(high["final_category"], "高毒类")

    def test_ordinary_auto_classification_requires_formal_completeness_gate(self) -> None:
        result = self.engine.classify({
            "reagent_name": "药典色度标准品",
            "ordinary_evidence_complete": False,
        })
        self.assertTrue(result["need_manual_review"])
        self.assertEqual(result["final_category"], "")

    def test_default_manual_review_category_blocks_auto_pass(self) -> None:
        result = self.engine.classify({"reagent_name": "\u6c30\u5316\u94a0", "text": "\u6c30\u5316\u94a0"})

        self.assertEqual(result["final_category"], "\u5267\u6bd2\u54c1")
        self.assertTrue(result["need_manual_review"])


if __name__ == "__main__":
    unittest.main()
