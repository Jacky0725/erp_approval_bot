from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from reagent_identity import ReagentIdentityResolver  # noqa: E402


class ReagentIdentityResolverTest(unittest.TestCase):
    def test_validates_cas_check_digit(self) -> None:
        self.assertTrue(ReagentIdentityResolver.is_valid_cas("64-17-5"))
        self.assertFalse(ReagentIdentityResolver.is_valid_cas("64-17-4"))

    def test_resolves_local_alias_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "aliases.yaml").write_text(
                yaml.safe_dump(
                    {"cas": {"64-17-5": {"standard_name": "乙醇", "english_name": "Ethanol"}}, "aliases": {"酒精": "乙醇"}},
                    allow_unicode=True,
                ),
                encoding="utf-8",
            )
            resolver = ReagentIdentityResolver(
                settings={"paths": {"name_aliases_yaml": "config/aliases.yaml"}},
                root_dir=root,
            )
            result = resolver.resolve("酒精 AR 500mL", cas="64-17-5")

        self.assertEqual(result.status, "verified")
        self.assertEqual(result.standard_name_cn, "乙醇")
        self.assertEqual(result.cas, "64-17-5")
        self.assertIn("local_alias:cas", result.evidence_refs)

    def test_marks_invalid_cas_as_conflict(self) -> None:
        resolver = ReagentIdentityResolver(root_dir=ROOT_DIR)
        result = resolver.resolve("乙醇", cas="64-17-4")
        self.assertEqual(result.status, "conflict")
        self.assertIn("invalid_erp_cas_checksum", result.conflicts)

    def test_marks_products_for_sds_review(self) -> None:
        resolver = ReagentIdentityResolver(root_dir=ROOT_DIR)
        result = resolver.resolve("蛋白提取试剂盒")
        self.assertEqual(result.mixture_state, "mixture_or_product")
        self.assertEqual(result.status, "ambiguous")
