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

    def test_promoted_drug_biology_polymer_families_keep_hazard_priority(self) -> None:
        for name, rule_id in (
            ("阿司匹林", "BUS-NOR-DRUG-DICT-001"),
            ("LB培养基", "BUS-NOR-BIO-CULTURE-001"),
            ("丁腈橡胶", "BUS-NOR-POLYMER-MATERIAL-001"),
        ):
            with self.subTest(name=name):
                result = self.engine.classify({"reagent_name": name, "ordinary_evidence_complete": True})
                self.assertEqual(result["final_category"], "普通类")
                self.assertIn(rule_id, result["matched_rule_ids"])

        hazardous_drug = self.engine.classify({
            "reagent_name": "阿司匹林", "evidence": "H225",
            "ordinary_evidence_complete": True,
        })
        self.assertEqual(hazardous_drug["final_category"], "易燃液体")
        self.assertIn("BUS-FLA-GHS-H224-226-001", hazardous_drug["matched_rule_ids"])

    def test_promoted_alias_maps_to_standard_name_without_overriding_valid_cas(self) -> None:
        result = self.engine._apply_structured_alias({"reagent_name": "阿司匹林", "cas": "123-45-6"})
        self.assertEqual(result["standard_name"], "乙酰水杨酸")
        self.assertEqual(result["cas"], "123-45-6")

    def test_unknown_category_is_not_a_structured_manual_review_category(self) -> None:
        self.assertNotIn("未知类", self.engine.manual_review_categories)

        result = self.engine.classify(
            {"reagent_name": "未知样品", "text": "无标签，无法辨识"}
        )

        self.assertEqual(result["final_category"], "未知类")
        self.assertFalse(result["need_manual_review"])

    def test_missing_sds_in_evidence_does_not_make_named_chemical_unknown(self) -> None:
        result = self.engine.classify({
            "reagent_name": "赤藓糖醇",
            "text": "网站暂不可用；无MSDS；毒性未知",
            "allow_default_normal": False,
        })
        self.assertNotEqual(result["final_category"], "未知类")

    def test_normal_hint_requires_complete_classification_evidence(self) -> None:
        result = self.engine.classify({
            "reagent_name": "待核验物质",
            "text": "高沸点，中性，但来源查询失败",
            "ordinary_evidence_complete": False,
            "allow_default_normal": True,
        })
        self.assertNotEqual(result["final_category"], "普通类")

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

    def test_acid_identity_rules_do_not_match_lithium_perchlorate_trihydrate(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "\u9ad8\u6c2f\u9178\u9502\u4e09\u6c34\u5408\u7269",
                "standard_name": "\u9ad8\u6c2f\u9178\u9502\u4e09\u6c34\u5408\u7269",
                "english_name": "lithium perchlorate trihydrate",
                "evidence": "H272; Oxidizing solid; UN 1481, class 5.1",
                "oxidizing": True,
            }
        )

        self.assertEqual(result["final_category"], "\u6c27\u5316\u5242")
        self.assertNotIn("\u6613\u7206\u7c7b", result["matched_categories"])
        self.assertNotIn("SPECIAL-EXP-004", result["matched_rule_ids"])

    def test_acid_identity_rules_exclude_other_mineral_acid_salts(self) -> None:
        cases = (
            ("\u9ad8\u6c2f\u9178\u94a0", "sodium perchlorate", "\u6c27\u5316\u5242"),
            ("\u786b\u9178\u9502", "lithium sulfate", "\u666e\u901a\u7c7b"),
            ("\u785d\u9178\u9502", "lithium nitrate", "\u6c27\u5316\u5242"),
        )
        for name, english_name, expected in cases:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {
                        "reagent_name": name,
                        "standard_name": name,
                        "english_name": english_name,
                        "evidence": "H272; oxidizing solid" if expected == "\u6c27\u5316\u5242" else "",
                        "oxidizing": expected == "\u6c27\u5316\u5242",
                        "allow_default_normal": True,
                    }
                )
                self.assertEqual(result["final_category"], expected)
                self.assertNotIn("\u7279\u6b8a\u9178", result["matched_categories"])
                self.assertNotIn("\u6613\u7206\u7c7b", result["matched_categories"])

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

    def test_controlled_common_salt_enzyme_and_culture_rules(self) -> None:
        ordinary_names = (
            "\u78f7\u9178\u6c22\u4e8c\u94be",
            "\u65e0\u6c34\u786b\u9178\u9541(\u5206\u6790\u7eaf)",
            "\u78f7\u9178\u4e8c\u6c22\u94a0\u00b7\u4e8c\u6c34",
            "\u65e0\u6c34\u78b3\u9178\u94a0(AR)",
            "\u4e59\u9178\u94f5",
            "\u7532\u9178\u94f5(\u5206\u6790\u7eaf)",
            "\u4e59\u4e8c\u80fa\u56db\u4e59\u9178\u4e8c\u94a0",
            "\u4e59\u4e8c\u9178\u56db\u4e59\u9178\u94a0",
            "\u66f2\u9709\u6dc0\u7c89\u9176(\u5206\u6790\u7eaf)",
            "\u86cb\u767d\u9176",
            "\u8102\u80aa\u9176",
            "\u751f\u7269\u9176\u5236\u5242",
            "\u9009\u62e9\u6027\u57f9\u517b\u57fa",
            "\u7ec6\u80de\u57f9\u517b\u6db2",
        )
        for name in ordinary_names:
            with self.subTest(name=name):
                result = self.engine.classify(
                    {"reagent_name": name, "standard_name": name, "text": name}
                )
                self.assertEqual(result["final_category"], "\u666e\u901a\u7c7b")
                self.assertFalse(result["need_manual_review"])

        self.assertFalse(self.engine._is_common_low_risk_salt_name({"reagent_name": "\u786b\u9178\u94dc"}))
        self.assertFalse(self.engine._is_common_low_risk_salt_name({"reagent_name": "\u785d\u9178\u94be"}))
        self.assertFalse(self.engine._is_enzyme_or_culture_normal_name({"reagent_name": "\u66f2\u9709"}))
        blocked = self.engine.classify(
            {
                "reagent_name": "\u6c5e\u57f9\u517b\u57fa",
                "standard_name": "\u6c5e\u57f9\u517b\u57fa",
                "text": "\u6c5e\u57f9\u517b\u57fa",
            }
        )
        self.assertEqual(blocked["final_category"], "\u4e0d\u5efa\u8bae\u63a5\u6536\u7c7b")

    def test_phosphorus_pentoxide_matches_reactivity_with_grade_suffix(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "\u4e94\u6c27\u5316\u4e8c\u78f7(AR)",
                "standard_name": "\u4e94\u6c27\u5316\u4e8c\u78f7",
                "text": "\u4e94\u6c27\u5316\u4e8c\u78f7(AR)",
            }
        )

        self.assertEqual(result["final_category"], "\u5f3a\u53cd\u5e94\u6027")

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

    def test_plain_sulfuric_and_nitric_follow_business_special_acid_policy(self) -> None:
        expected_categories = {
            "\u76d0\u9178": "\u5e38\u89c4\u9178",
            "\u785d\u9178": "\u7279\u6b8a\u9178",
            "\u786b\u9178": "\u7279\u6b8a\u9178",
            "\u53d1\u70df\u76d0\u9178": "\u5e38\u89c4\u9178",
            "10% HCl": "\u5e38\u89c4\u9178",
            "2mol HCl": "\u5e38\u89c4\u9178",
            "hydrochloric acid solution": "\u5e38\u89c4\u9178",
        }
        for name, expected_category in expected_categories.items():
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

                self.assertEqual(result["final_category"], expected_category)
                self.assertIn(expected_category, result["matched_categories"])

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
        self.assertFalse(result["need_manual_review"])

    def test_ghs_acute_tox_category_3_is_high_toxicity(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "测试物",
                "evidence": "H301; H311; H331; Acute Tox. 3",
            }
        )
        self.assertEqual(result["final_category"], "高毒类")
        self.assertIn("BUS-TOX-ACUTE-CAT3-001", result["matched_rule_ids"])

    def test_injection_route_ld50_never_triggers_oral_or_dermal_thresholds(self) -> None:
        evidence = (
            "H301; H311; H331. 造成皮肤灼伤。"
            "大鼠腹腔 LD50: 31mg/kg; 小鼠皮下 LDLo: 280mg/kg."
        )
        result = self.engine.classify({"reagent_name": "氟化铵", "evidence": evidence})
        self.assertEqual(result["final_category"], "高毒类")
        self.assertNotIn("T-ACU-DERM-001", result["matched_rule_ids"])
        self.assertNotIn("剧毒品", result["matched_categories"])

    def test_verified_dermal_ld50_still_uses_dermal_threshold(self) -> None:
        result = self.engine.classify(
            {
                "reagent_name": "测试物",
                "toxicity": "Rabbit dermal LD50: 31 mg/kg",
            }
        )
        self.assertEqual(result["final_category"], "剧毒品")
        self.assertIn("T-ACU-DERM-001", result["matched_rule_ids"])

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

    def test_irritancy_requires_the_uploaded_oral_ld50_band(self) -> None:
        for evidence in (
            "强烈刺激皮肤", "造成严重眼损伤", "H314", "H318",
            "该物质具有催泪作用", "Lachrymatory agent", "H315", "H319",
            "造成皮肤刺激", "Causes skin irritation", "R34", "R36",
        ):
            with self.subTest(evidence=evidence):
                result = self.engine.classify(
                    {"reagent_name": "测试物", "evidence": evidence, "allow_default_normal": True}
                )
                self.assertEqual(result["final_category"], "普通类")
                self.assertNotIn("刺激性", result.get("matched_categories", []))

    def test_legacy_acute_harm_r_codes_require_uploaded_oral_ld50_band(self) -> None:
        for evidence in ("R20", "R21", "R22", "R20/21/22", "R34; R20/21/22"):
            with self.subTest(evidence=evidence):
                result = self.engine.classify(
                    {"reagent_name": "测试物", "evidence": evidence, "allow_default_normal": True}
                )
                self.assertEqual(result["final_category"], "普通类")
                self.assertFalse(result["need_manual_review"])
                self.assertNotIn("BUS-IRR-TOX-HCODES-001", result.get("matched_rule_ids", []))

    def test_graphene_principal_material_is_normal_only_when_isolated(self) -> None:
        result = self.engine.classify({
            "reagent_name": "石墨烯量子点",
            "name": "石墨烯量子点",
            "standard_name": "石墨烯量子点",
            "cleaned_name": "石墨烯量子点",
        })
        self.assertEqual(result["final_category"], "普通类")
        self.assertIn("BUS-NOR-GRAPHENE-001", result["matched_rule_ids"])
        self.assertNotEqual(
            self.engine.classify({"reagent_name": "石墨烯量子点乙醇分散液"})["final_category"],
            "普通类",
        )

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

    def test_sensitization_and_respiratory_irritation_are_neutral_annotations(self) -> None:
        cases = (
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
                self.assertEqual(result["final_category"], "普通类")
                self.assertFalse(result["need_manual_review"])
                self.assertNotIn("刺激性", result.get("matched_categories", []))

    def test_h317_or_h335_does_not_mask_other_hazard_categories(self) -> None:
        cases = (
            ("H317; 具有强氧化性", "氧化剂"),
            ("H335; 与水反应", "强反应性"),
        )
        for evidence, category in cases:
            with self.subTest(evidence=evidence):
                result = self.engine.classify({"reagent_name": "测试物", "evidence": evidence})
                self.assertEqual(result["final_category"], category)
                self.assertFalse(result["need_manual_review"])

    def test_h317_h335_and_former_severe_codes_do_not_trigger_uploaded_irritancy_rule(self) -> None:
        for evidence in ("H314; H317", "H318; H335", "H314; H315"):
            with self.subTest(evidence=evidence):
                result = self.engine.classify(
                    {"reagent_name": "测试物", "evidence": evidence, "allow_default_normal": True}
                )
                self.assertEqual(result["final_category"], "普通类")
                self.assertNotIn("刺激性", result.get("matched_categories", []))

    def test_llm_irritancy_hint_and_paused_examples_do_not_trigger_irritancy(self) -> None:
        result = self.engine.classify({"reagent_name": "测试物", "suggested_categories": ["刺激性"]})
        self.assertNotEqual(result["final_category"], "刺激性")

        for name in ("苯酚类", "硫酸二甲酯", "瓦斯"):
            with self.subTest(name=name):
                result = self.engine.classify({"reagent_name": name, "allow_default_normal": True})
                self.assertEqual(result["final_category"], "普通类")

    def test_dilute_sulfuric_acid_is_regular_acid_without_numeric_concentration(self) -> None:
        result = self.engine.classify({"reagent_name": "稀硫酸", "text": "稀硫酸"})
        self.assertEqual(result["final_category"], "常规酸")

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

    def test_oxidizer_rule_ignores_incompatibility_text_but_keeps_positive_evidence(self) -> None:
        incompatible = self.engine.classify(
            {"reagent_name": "测试物", "evidence": "易燃；与强氧化剂不相容；避免接触氧化剂。"}
        )
        self.assertNotEqual(incompatible["final_category"], "氧化剂")
        self.assertNotIn("BUS-OXI-GHS-001", incompatible.get("matched_rule_ids", []))

        positive = self.engine.classify(
            {"reagent_name": "测试物", "evidence": "H272; 该物质具有强氧化性，为氧化性固体。"}
        )
        self.assertEqual(positive["final_category"], "氧化剂")
        self.assertIn("BUS-OXI-GHS-001", positive["matched_rule_ids"])

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

    def test_business_identity_rules_override_broad_halogen_and_name_gaps(self) -> None:
        cases = {
            "乙基溴化镁": ("强反应性", "BUS-REA-ORG-MG-001"),
            "单质溴": ("氧化剂", "BUS-OXI-BROMINE-001"),
            "单质碘": ("氧化剂", "BUS-OXI-IODINE-001"),
            "乙酸": ("常规酸", "BUS-ACID-ACETIC-001"),
            "甲酸": ("常规酸", "BUS-ACID-FORMIC-001"),
            "氢氧化钾滴定液1mol/l": ("常规碱", "BUS-BASE-TITRANT-001"),
            "无水硫酸铜": ("普通类", "BUS-NOR-ANHYD-CUSO4-001"),
        }
        for name, (category, rule_id) in cases.items():
            with self.subTest(name=name):
                result = self.engine.classify({"reagent_name": name, "text": name})
                self.assertEqual(result["final_category"], category)
                self.assertIn(rule_id, result["matched_rule_ids"])
                self.assertFalse(result["need_manual_review"])

        # Only the confirmed anhydrous product is whitelisted; no blanket rule
        # promotes all copper salts to the ordinary category.
        result = self.engine.classify({"reagent_name": "硫酸铜", "text": "硫酸铜"})
        self.assertTrue(result["need_manual_review"])

    def test_sulfuric_and_nitric_acid_concentration_boundaries(self) -> None:
        cases = (
            ({"reagent_name": "硫酸", "standard_name": "硫酸"}, "特殊酸", "BUS-ACID-SULFURIC-SPECIAL-001"),
            ({"reagent_name": "硫酸 AR", "standard_name": "硫酸", "cleaned_name": "硫酸"}, "特殊酸", "BUS-ACID-SULFURIC-SPECIAL-001"),
            ({"reagent_name": "硫酸 70%", "standard_name": "硫酸", "cleaned_name": "硫酸"}, "常规酸", "BUS-ACID-SULFURIC-REGULAR-001"),
            ({"reagent_name": "硫酸 98%", "standard_name": "硫酸", "cleaned_name": "硫酸"}, "特殊酸", "BUS-ACID-SULFURIC-SPECIAL-001"),
            ({"reagent_name": "硝酸", "standard_name": "硝酸"}, "特殊酸", "BUS-ACID-NITRIC-SPECIAL-001"),
            ({"reagent_name": "硝酸 65%", "standard_name": "硝酸", "cleaned_name": "硝酸"}, "常规酸", "BUS-ACID-NITRIC-REGULAR-001"),
            ({"reagent_name": "硝酸 68%", "standard_name": "硝酸", "cleaned_name": "硝酸"}, "特殊酸", "BUS-ACID-NITRIC-SPECIAL-001"),
        )
        for reagent, category, rule_id in cases:
            with self.subTest(reagent=reagent):
                result = self.engine.classify(reagent)
                self.assertEqual(result["final_category"], category)
                self.assertIn(rule_id, result["matched_rule_ids"])

        salt = self.engine.classify({"reagent_name": "硫酸钠", "standard_name": "硫酸钠"})
        self.assertNotIn(salt["final_category"], ("常规酸", "特殊酸"))

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

    def test_uploaded_irritancy_uses_only_oral_ld50_50_through_200(self) -> None:
        boundary = self.engine.classify({"reagent_name": "测试物", "toxicity": "oral LD50 50 mg/kg"})
        self.assertEqual(boundary["final_category"], "高毒类")
        self.assertIn("T-TOX-002", boundary["matched_rule_ids"])
        for toxicity in ("oral LD50 50.1 mg/kg", "oral LD50 200 mg/kg"):
            with self.subTest(toxicity=toxicity):
                result = self.engine.classify({"reagent_name": "测试物", "toxicity": toxicity})
                self.assertEqual(result["final_category"], "刺激性")
                self.assertFalse(result["need_manual_review"])

        for toxicity in (
            "oral LD50 201 mg/kg", "dermal LD50 500 mg/kg",
            "inhalation LC50 gas 1000 ppm 4 h", "inhalation LC50 vapor 5 mg/L 4 h",
            "inhalation LC50 dust 2 mg/L 4 h",
        ):
            with self.subTest(no_irritancy_toxicity=toxicity):
                result = self.engine.classify(
                    {"reagent_name": "测试物", "toxicity": toxicity, "allow_default_normal": True}
                )
                self.assertEqual(result["final_category"], "普通类")

        dermal_high = self.engine.classify({"reagent_name": "测试物", "toxicity": "dermal LD50 200 mg/kg"})
        self.assertEqual(dermal_high["final_category"], "高毒类")

    def test_formal_acute_harm_code_without_comparable_number_is_not_irritancy(self) -> None:
        without_number = self.engine.classify(
            {"reagent_name": "测试物", "evidence": "H302", "allow_default_normal": True}
        )
        self.assertEqual(without_number["final_category"], "普通类")
        self.assertNotIn("BUS-IRR-TOX-HCODES-001", without_number.get("matched_rule_ids", []))

        with_comparable_number = self.engine.classify(
            {"reagent_name": "测试物", "evidence": "H302; oral LD50 1000 mg/kg", "allow_default_normal": True}
        )
        self.assertEqual(with_comparable_number["final_category"], "普通类")
        self.assertNotIn("BUS-IRR-TOX-HCODES-001", with_comparable_number.get("matched_rule_ids", []))

    def test_h225_is_formal_flammable_liquid_evidence(self) -> None:
        result = self.engine.classify({"reagent_name": "测试物", "evidence": "H225"})
        self.assertEqual(result["final_category"], "易燃液体")
        self.assertIn("BUS-FLA-GHS-H224-226-001", result["matched_rule_ids"])
        self.assertFalse(result["need_manual_review"])

    def test_r36_is_risk_information_not_uploaded_irritancy_evidence(self) -> None:
        result = self.engine.classify({"reagent_name": "测试物", "evidence": "R36", "allow_default_normal": True})
        self.assertEqual(result["final_category"], "普通类")
        self.assertNotIn("BUS-IRR-EU-EYE-001", result.get("matched_rule_ids", []))

    def test_heavy_metal_precedes_the_absent_irritancy_match(self) -> None:
        result = self.engine.classify({"reagent_name": "氯化镍", "evidence": "H315"})
        self.assertEqual(result["final_category"], "重金属类")
        self.assertIn("重金属类", result["matched_categories"])
        self.assertNotIn("刺激性", result["matched_categories"])

    def test_uploaded_non_reagent_container_policy_overrides_embedded_chemical_words(self) -> None:
        for name, rule_id in (
            ("空瓶", "BUS-NOR-EMPTY-BOTTLE-001"),
            ("比色用硫酸桶", "BUS-NOR-NONREAGENT-CONTAINER-001"),
            ("乙醇桶", "BUS-NOR-NONREAGENT-CONTAINER-001"),
        ):
            with self.subTest(name=name):
                result = self.engine.classify({"reagent_name": name, "allow_default_normal": True})
                self.assertEqual(result["final_category"], "普通类")
                self.assertFalse(result["need_manual_review"])
                self.assertIn(rule_id, result["matched_rule_ids"])

        contained_chemical = self.engine.classify(
            {"reagent_name": "乙醇桶内含乙醇", "evidence": "H225", "allow_default_normal": True}
        )
        self.assertEqual(contained_chemical["final_category"], "易燃液体")

    def test_v5_acute_poison_category_does_not_force_manual_review(self) -> None:
        result = self.engine.classify({"reagent_name": "\u6c30\u5316\u94a0", "text": "\u6c30\u5316\u94a0"})

        self.assertEqual(result["final_category"], "\u5267\u6bd2\u54c1")
        self.assertFalse(result["need_manual_review"])


if __name__ == "__main__":
    unittest.main()
