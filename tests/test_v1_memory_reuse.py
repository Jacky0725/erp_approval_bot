from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))

from approval_flow import ApprovalFlowMixin  # noqa: E402
from reagent_memory import ReagentMemory  # noqa: E402


class _Bot(ApprovalFlowMixin):
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.settings = {
            "paths": {"reagent_memory_sqlite": "data/memory.sqlite"},
            "memory": {"min_confidence": 0.8},
            "approval": {"write_min_confidence": 0.7},
            "reagent": {"physicochemical_property_options": ["拒收类"]},
        }


class V1MemoryReuseTest(unittest.TestCase):
    def test_verified_pure_aqueous_solution_is_reused_and_is_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bot = _Bot(root)
            memory = ReagentMemory.from_settings(bot.settings, root)
            self.assertTrue(memory.add_record(
                raw_name="氟化氨",
                cleaned_name="氟化铵",
                standard_name="氟化铵",
                cas="12125-01-8",
                final_category="拒收类",
                confidence=1.0,
                source="verified_erp_write",
                reason=(
                    "命中规则：高毒类（规则ID: BUS-TOX-ACUTE-CAT3-001）。\n"
                    "ERP 接口稳定记录 ID 42 两次回读确认 拒收类。"
                ),
                manual_verified=True,
                track_conflicts=False,
                force_reusable=True,
            ))
            match = bot.lookup_reusable_memory(
                memory,
                raw_name="氟化铵40%水溶液",
                cleaned_name="氟化铵水溶液",
                standard_name="氟化铵水溶液",
            )
            self.assertIsNotNone(match)
            assert match is not None
            self.assertTrue(match["_memory_solution_reuse"])
            self.assertEqual(match["_memory_solution_primary_component"], "氟化铵")
            self.assertEqual(bot.memory_formal_rule_ids(match), ["BUS-TOX-ACUTE-CAT3-001"])

    def test_solution_with_named_non_water_solvent_never_reuses_pure_memory(self) -> None:
        self.assertEqual(_Bot.memory_solution_primary_component("氟化铵乙醇水溶液"), "")

    def test_verified_memory_rule_ids_enable_terminal_reject_write_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = _Bot(Path(temp_dir))
            suggestion = {
                "最终建议类别": "拒收类",
                "命中规则ID": "BUS-TOX-ACUTE-CAT3-001",
                "需人工复核": False,
                "置信度": 1.0,
                "身份状态": "name_verified",
            }
            self.assertEqual(bot.high_confidence_write_candidates([suggestion]), [suggestion])


if __name__ == "__main__":
    unittest.main()
