import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from approval_flow import ApprovalFlowMixin
from web_app import dingtalk_run_options, run_options
from web_runner import load_settings


class Bot(ApprovalFlowMixin):
    pass


class PipelineIsolationTest(unittest.TestCase):
    def test_historical_environment_and_settings_values_cannot_select_retired_pipelines(self):
        bot = Bot()
        for value in ("v1", "v2", "v3", "legacy"):
            bot.settings = {"approval": {"pipeline_version": value}}
            with self.subTest(value=value), patch.dict(os.environ, {"APPROVAL_PIPELINE_VERSION": value}, clear=False):
                self.assertEqual(bot._current_pipeline_version(), "v1")

    def test_all_run_options_are_fixed_to_v1(self):
        base = dict(target_list_numbers="", process_all_todos="false", process_all_todos_max="50", approval_write_mode="disabled", approval_write_min_confidence="0.8", approval_write_batch_size="3", erp_write_backend="web_ui", auto_pass="false")
        for value in ("v1", "v2", "v3", "legacy"):
            with self.subTest(value=value):
                self.assertEqual(run_options(**base, pipeline_version=value)["APPROVAL_PIPELINE_VERSION"], "v1")

    def test_default_pipeline_is_v1_for_dingtalk_runs(self):
        self.assertEqual(load_settings()["approval"]["pipeline_version"], "v1")
        with patch("web_app.runtime_config_snapshot", return_value={"approval_pipeline_version": "v1"}):
            self.assertEqual(dingtalk_run_options()["APPROVAL_PIPELINE_VERSION"], "v1")


if __name__ == "__main__":
    unittest.main()
