from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from name_normalizer import NameNormalizer  # noqa: E402


class NameNormalizerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.normalizer = NameNormalizer(root_dir=ROOT_DIR, enable_llm=False)

    def test_cas_has_highest_priority(self) -> None:
        result = self.normalizer.normalize(
            raw_name="工业酒精 75% 500ml",
            cas="64-17-5",
            specification="500ml",
            unit="瓶",
        )

        self.assertEqual(result["standard_name"], "乙醇")
        self.assertEqual(result["english_name"], "ethanol")
        self.assertEqual(result["cas"], "64-17-5")
        self.assertEqual(result["concentration"], "75%")
        self.assertGreaterEqual(result["confidence"], 0.8)
        self.assertFalse(result["need_manual_review"])

    def test_alias_mapping_after_cleaning(self) -> None:
        result = self.normalizer.normalize(raw_name="无水乙醇 AR 500ml/瓶")

        self.assertEqual(result["standard_name"], "乙醇")
        self.assertEqual(result["english_name"], "ethanol")
        self.assertIn("乙醇", result["aliases"])
        self.assertFalse(result["need_manual_review"])

    def test_concentration_and_abbreviation(self) -> None:
        result = self.normalizer.normalize(raw_name="NaOH 0.1mol/L 分析纯")

        self.assertEqual(result["standard_name"], "氢氧化钠")
        self.assertEqual(result["english_name"], "sodium hydroxide")
        self.assertEqual(result["concentration"], "0.1mol/L")
        self.assertFalse(result["need_manual_review"])

    def test_specification_and_package_metadata_do_not_affect_identity(self) -> None:
        result = self.normalizer.normalize(
            raw_name="氢氧化钠",
            specification="0.1mol/L",
            unit="瓶",
            包装方式="箱装",
        )

        self.assertEqual(result["standard_name"], "氢氧化钠")
        self.assertEqual(result["concentration"], "")

    def test_llm_name_request_excludes_specification_unit_and_package_metadata(self) -> None:
        captured: dict[str, object] = {}

        class FakeCompletions:
            def create(self, **kwargs: object) -> SimpleNamespace:
                captured.update(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                    "standard_name": "测试化合物",
                    "english_name": "test compound",
                    "confidence": 0.8,
                    "reason": "fixture",
                }, ensure_ascii=False)))])

        normalizer = NameNormalizer(root_dir=ROOT_DIR, enable_llm=True)
        normalizer._has_api_key = lambda: True  # type: ignore[method-assign]
        normalizer._client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))  # type: ignore[method-assign]

        normalizer.normalize(
            raw_name="待标准化名称",
            specification="500 mL",
            unit="瓶",
            包装方式="箱装",
        )

        messages = captured["messages"]
        assert isinstance(messages, list)
        payload = json.loads(messages[1]["content"])
        self.assertNotIn("specification", payload)
        self.assertNotIn("unit", payload)
        self.assertNotIn("extra_fields", payload)

    def test_unmatched_name_needs_manual_review_without_llm(self) -> None:
        result = self.normalizer.normalize(raw_name="客户自编号未知混合液 1M")

        self.assertEqual(result["concentration"], "1M")
        self.assertLess(result["confidence"], 0.8)
        self.assertTrue(result["need_manual_review"])

    def test_non_informative_name_is_flagged_for_cas_based_resolution(self) -> None:
        result = self.normalizer.normalize(raw_name="没写")

        self.assertEqual(result["cleaned_name"], "没写")
        self.assertTrue(result["suspected_invalid_name"])
        self.assertIn("不是有效化学名称", result["suspected_invalid_reason"])
        self.assertTrue(result["need_manual_review"])

    def test_internal_code_suffix_after_colon_is_removed(self) -> None:
        result = self.normalizer.normalize(raw_name="卡马西平：AC-O-11-01")

        self.assertEqual(result["raw_name"], "卡马西平：AC-O-11-01")
        self.assertEqual(result["cleaned_name"], "卡马西平")
        self.assertEqual(result["standard_name"], "卡马西平")
        self.assertIn("Cleaned name by rules", result["reason"])

    def test_arsenic_reagent_alias_maps_to_silver_diethyldithiocarbamate(self) -> None:
        result = self.normalizer.normalize(raw_name="砷试剂")

        self.assertEqual(result["standard_name"], "二乙基二硫代氨基甲酸银")
        self.assertEqual(result["english_name"], "Silver diethyldithiocarbamate")
        self.assertEqual(result["cas"], "1470-61-7")
        self.assertFalse(result["need_manual_review"])

    def test_trailing_purity_grade_is_removed_with_or_without_space(self) -> None:
        for raw_name in ("硫酸 AR", "硫酸AR", "硫酸 GR", "硫酸GR", "硫酸 CP", "硫酸CP"):
            with self.subTest(raw_name=raw_name):
                result = self.normalizer.normalize(raw_name=raw_name)
                self.assertEqual(result["cleaned_name"], "硫酸")

    def test_update_aliases_after_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            aliases_path = Path(temp_dir) / "name_aliases.yaml"
            aliases_path.write_text("cas: {}\naliases: {}\nabbreviations: {}\n", encoding="utf-8")

            normalizer = NameNormalizer(root_dir=ROOT_DIR, aliases_path=aliases_path, enable_llm=False)
            updated = normalizer.update_aliases_after_approval(
                raw_name="甘氨酸（氨基乙酸）",
                cleaned_name="甘氨酸 氨基乙酸",
                standard_name="甘氨酸",
                english_name="glycine",
                cas="56-40-6",
                approved=True,
            )

            self.assertTrue(updated)
            data = yaml.safe_load(aliases_path.read_text(encoding="utf-8"))
            self.assertEqual(data["aliases"]["甘氨酸（氨基乙酸）"], "甘氨酸")
            self.assertEqual(data["aliases"]["甘氨酸 氨基乙酸"], "甘氨酸")
            self.assertEqual(data["cas"]["56-40-6"]["standard_name"], "甘氨酸")
            self.assertEqual(data["cas"]["56-40-6"]["english_name"], "glycine")

    def test_update_aliases_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            aliases_path = Path(temp_dir) / "name_aliases.yaml"
            aliases_path.write_text("cas: {}\naliases: {}\nabbreviations: {}\n", encoding="utf-8")

            normalizer = NameNormalizer(root_dir=ROOT_DIR, aliases_path=aliases_path, enable_llm=False)
            updated = normalizer.update_aliases_after_approval(
                raw_name="甘氨酸",
                standard_name="甘氨酸",
                english_name="glycine",
                approved=False,
            )

            self.assertFalse(updated)
            data = yaml.safe_load(aliases_path.read_text(encoding="utf-8"))
            self.assertEqual(data["aliases"], {})

    def test_manual_verified_alias_returns_cas_and_source_url(self) -> None:
        result = self.normalizer.normalize(raw_name="六水合三氯亚铁")

        self.assertEqual(result["standard_name"], "六水合三氯化铁")
        self.assertEqual(result["cas"], "10025-77-1")
        self.assertEqual(result["english_name"], "Iron(III) chloride hexahydrate")
        self.assertEqual(result["source_url"], "https://www.chemsrc.com/cas/10025-77-1_825977.html")


    def test_erp_cas_overrides_conflicting_alias_cas_and_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            aliases_path = Path(temp_dir) / "name_aliases.yaml"
            aliases_path.write_text(
                """
cas:
  64-17-5:
    standard_name: ethanol
    english_name: ethanol
    source_url: https://www.chemsrc.com/cas/64-17-5_123.html
    aliases: [wrong-alias]
aliases:
  wrong-alias:
    standard_name: ethanol
    english_name: ethanol
    cas: 64-17-5
    source_url: https://www.chemsrc.com/cas/64-17-5_123.html
abbreviations: {}
""".lstrip(),
                encoding="utf-8",
            )

            normalizer = NameNormalizer(root_dir=ROOT_DIR, aliases_path=aliases_path, enable_llm=False)
            result = normalizer.normalize(raw_name="wrong-alias", cas="1310-73-2")

        self.assertEqual(result["cas"], "1310-73-2")
        self.assertEqual(result["source_url"], "")
        self.assertIn("ERP provided CAS number has priority", result["reason"])


if __name__ == "__main__":
    unittest.main()
