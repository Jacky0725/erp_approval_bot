from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web_runner import AutomationJobManager, parse_target_list_numbers, run_health, workflow_summary


class WorkflowSummaryTest(unittest.TestCase):
    def test_repeated_reagent_pipeline_marks_current_stage_active(self) -> None:
        lines = [
            "2026-06-22 12:39:48 [FLOW] START chemical_search - page 1 1/20 A",
            "2026-06-22 12:40:19 [FLOW] END   chemical_search (30.6s)",
            "2026-06-22 12:40:19 [FLOW] START llm_extract - page 1 1/20 A",
            "2026-06-22 12:40:31 [FLOW] END   llm_extract (12.2s)",
            "2026-06-22 12:40:31 [FLOW] START rule_classify - A",
            "2026-06-22 12:40:31 [FLOW] END   rule_classify (0.0s)",
            "2026-06-22 12:40:59 [FLOW] START chemical_search - page 1 2/20 B",
        ]

        result = workflow_summary(lines, running=True, success=None, error="")
        states = {step["id"]: step["state"] for step in result["steps"]}

        self.assertEqual(result["current_step"], "search")
        self.assertEqual(states["search"], "active")
        self.assertEqual(states["llm"], "waiting")
        self.assertEqual(states["rule"], "waiting")
        self.assertEqual(states["write"], "waiting")

    def test_manager_status_reports_finished_result(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = AutomationJobManager(root_dir=Path(tmp))
            manager.action = "suggestions"
            manager.started_at = "2026-06-22T12:00:00"
            manager.finished_at = "2026-06-22T12:02:00"
            manager.success = True
            manager.error = ""

            status = manager.status()

            self.assertEqual(status["result_label"], "成功")
            self.assertEqual(status["action"], "suggestions")

    def test_parse_target_list_numbers_deduplicates_values(self) -> None:
        result = parse_target_list_numbers("SJ1, SJ2;SJ1\nSJ3")

        self.assertEqual(result, ["SJ1", "SJ2", "SJ3"])

    def test_run_health_warns_on_business_failures(self) -> None:
        health = run_health(["Failed save operation(s): reagent_save_1"], True, "")

        self.assertEqual(health, "warning")


if __name__ == "__main__":
    unittest.main()
