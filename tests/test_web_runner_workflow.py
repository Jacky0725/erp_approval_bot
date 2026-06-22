import unittest

from web_runner import workflow_summary


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


if __name__ == "__main__":
    unittest.main()
