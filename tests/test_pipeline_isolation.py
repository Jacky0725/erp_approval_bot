import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from approval_flow import ApprovalFlowMixin
from web_app import run_options


class Bot(ApprovalFlowMixin):
    pass


class PipelineIsolationTest(unittest.TestCase):
    def test_v1_v2_v3_selectors_do_not_overlap(self):
        bot = Bot()
        bot.settings = {"approval": {"pipeline_version": "v1"}, "enrichment_v2": {"shadow_mode": True}}
        with patch.dict(os.environ, {"APPROVAL_PIPELINE_VERSION": "v1"}, clear=False):
            self.assertFalse(bot.v2_pipeline_enabled())
            self.assertFalse(bot.v3_pipeline_enabled())
        with patch.dict(os.environ, {"APPROVAL_PIPELINE_VERSION": "v2"}, clear=False):
            self.assertTrue(bot.v2_pipeline_enabled())
            self.assertFalse(bot.v3_pipeline_enabled())
            self.assertFalse(bot.enrichment_v2_shadow_enabled())
            self.assertTrue(bot.enrichment_v2_production_enabled())
        with patch.dict(os.environ, {"APPROVAL_PIPELINE_VERSION": "v3"}, clear=False):
            self.assertFalse(bot.v2_pipeline_enabled())
            self.assertTrue(bot.v3_pipeline_enabled())

    def test_run_options_accepts_v2_and_retires_doubao_to_v1(self):
        base = dict(target_list_numbers="", process_all_todos="false", process_all_todos_max="50", approval_write_mode="disabled", approval_write_min_confidence="0.8", approval_write_batch_size="3", erp_write_backend="web_ui", auto_pass="false")
        self.assertEqual(run_options(**base, pipeline_version="v2")["APPROVAL_PIPELINE_VERSION"], "v2")
        self.assertEqual(run_options(**base, pipeline_version="doubao_web_v2")["APPROVAL_PIPELINE_VERSION"], "v1")


if __name__ == "__main__":
    unittest.main()
