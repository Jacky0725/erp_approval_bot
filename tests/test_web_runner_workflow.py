from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web_runner import (
    AutomationJobManager,
    automation_failure_reason,
    atomic_write_text,
    normalize_web_write_mode,
    parse_target_list_numbers,
    repair_display_text,
    run_summary,
    run_health,
    todo_tasks_summary,
    workflow_summary,
)
import web_app
from web_app import artifact_path_for_download, run_options, web_ui_restart_command


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

            self.assertEqual(status["result_label"], "审批流程完成")
            self.assertEqual(status["action"], "suggestions")

    def test_todo_export_status_uses_business_label(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = AutomationJobManager(root_dir=Path(tmp))
            manager.action = "todo_export"
            manager.started_at = "2026-08-05T08:38:49"
            manager.finished_at = "2026-08-05T08:39:09"
            manager.success = True
            manager.error = ""

            status = manager.status()

            self.assertEqual(status["action_label"], "待办清单刷新")
            self.assertEqual(status["result_label"], "待办清单刷新成功")
            self.assertEqual(status["summary"]["outcome"], "待办清单刷新成功")

    def test_run_summary_counts_write_outcome_and_targets(self) -> None:
        with TemporaryDirectory() as tmp:
            lines = [
                "Read reagent page 1: 20 row(s).",
                "Opening target task detail: SJ202608040001",
                "Save verified for sequence 1",
                "Could not select physicochemical property 强反应 for sequence 2",
            ]

            summary = run_summary(
                lines,
                action="suggestions",
                options={"TARGET_LIST_NUMBERS": "SJ202608040001"},
                running=False,
                success=True,
                error="",
                root_dir=Path(tmp),
            )

            self.assertEqual(summary["target_list_numbers"], ["SJ202608040001"])
            self.assertEqual(summary["write_success_count"], 1)
            self.assertEqual(summary["write_failure_count"], 1)
            self.assertTrue(summary["has_write_warning"])

    def test_todo_tasks_summary_prefers_utf8_json(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "data" / "logs"
            log_dir.mkdir(parents=True)
            (log_dir / "todo_tasks.json").write_text(
                '[{"试剂清单号":"SJ202608040001","客户名称":"上海交通大学","申请人":"戚明燕"}]',
                encoding="utf-8",
            )

            summary = todo_tasks_summary(root)

            self.assertEqual(summary["rows"], 1)
            self.assertEqual(summary["tasks"][0]["list_number"], "SJ202608040001")
            self.assertEqual(summary["tasks"][0]["customer_name"], "上海交通大学")

    def test_parse_target_list_numbers_deduplicates_values(self) -> None:
        result = parse_target_list_numbers("SJ1, SJ2;SJ1\nSJ3")

        self.assertEqual(result, ["SJ1", "SJ2", "SJ3"])

    def test_repair_display_text_keeps_valid_chinese(self) -> None:
        self.assertEqual(repair_display_text("成功"), "成功")

    def test_repair_display_text_restores_gbk_decoded_utf8(self) -> None:
        mojibake = "成功".encode("utf-8").decode("gbk")

        self.assertEqual(repair_display_text(mojibake), "成功")

    def test_run_health_warns_on_business_failures(self) -> None:
        health = run_health(["Failed save operation(s): reagent_save_1"], True, "")

        self.assertEqual(health, "warning")

    def test_automation_failure_reason_detects_web_write_failure(self) -> None:
        reason = automation_failure_reason(
            [
                "Could not select physicochemical property 强反应 for sequence: 9",
                "2026-07-14T13:11:53 END suggestions",
            ]
        )

        self.assertIn("物化特性", reason)

    def test_stop_reports_not_stopped_when_idle(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = AutomationJobManager(root_dir=Path(tmp))

            result = manager.stop()

            self.assertFalse(result["stopped"])

    def test_worker_process_not_started_after_stop_requested(self) -> None:
        with TemporaryDirectory() as tmp:
            src_dir = Path(tmp) / "src"
            src_dir.mkdir(parents=True)
            manager = AutomationJobManager(root_dir=Path(tmp), running=True)
            manager._stop_requested = True

            result = manager._run_worker_process("suggestions", {}, writer=None)  # type: ignore[arg-type]

            self.assertEqual(result, 130)
            self.assertIsNone(manager._process)

    def test_source_restart_command_uses_configured_port(self) -> None:
        with patch.dict("os.environ", {"WEB_UI_HOST": "127.0.0.1", "WEB_UI_PORT": "8123"}):
            command, cwd = web_ui_restart_command(frozen=False)

        self.assertIn("uvicorn", command)
        self.assertEqual(command[-1], "8123")
        self.assertTrue(cwd.endswith("src"))

    def test_frozen_restart_command_restarts_current_executable(self) -> None:
        command, cwd = web_ui_restart_command(frozen=True)

        self.assertEqual(command, [sys.executable])
        self.assertFalse(cwd.endswith("src"))

    def test_artifact_download_path_stays_inside_log_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "data" / "logs"
            log_dir.mkdir(parents=True)
            artifact = log_dir / "ok.txt"
            artifact.write_text("ok", encoding="utf-8")
            outside = root / "data" / "logs_evil.txt"
            outside.write_text("no", encoding="utf-8")

            original = web_app.LOG_DIR
            web_app.LOG_DIR = log_dir
            try:
                self.assertEqual(artifact_path_for_download("ok.txt"), artifact.resolve())
                self.assertIsNone(artifact_path_for_download("../logs_evil.txt"))
            finally:
                web_app.LOG_DIR = original

    def test_write_mode_options_expose_only_production_modes(self) -> None:
        template_text = (Path(__file__).resolve().parents[1] / "src" / "templates" / "partials" / "run.html").read_text(
            encoding="utf-8"
        )
        settings_text = (
            Path(__file__).resolve().parents[1] / "src" / "templates" / "partials" / "settings.html"
        ).read_text(encoding="utf-8")
        combined = template_text + settings_text

        self.assertIn('value="disabled"', combined)
        self.assertIn('value="multi_page"', combined)
        self.assertIn('value="generate_library"', combined)
        for retired in ["test_one", "save_one", "single_page"]:
            self.assertNotIn(f'<option value="{retired}"', combined)

    def test_web_write_mode_normalizes_retired_values(self) -> None:
        self.assertEqual(normalize_web_write_mode("save_one"), "disabled")
        self.assertEqual(normalize_web_write_mode("unknown"), "disabled")
        self.assertEqual(normalize_web_write_mode("disabled"), "disabled")
        self.assertEqual(normalize_web_write_mode("generate_library"), "generate_library")

        options = run_options(
            target_list_numbers="SJ1",
            process_all_todos="",
            process_all_todos_max="50",
            approval_write_mode="disabled",
            approval_write_min_confidence="0.8",
            approval_write_batch_size="3",
            auto_pass="",
        )

        self.assertEqual(options["APPROVAL_WRITE_MODE"], "disabled")
        self.assertEqual(options["APPROVAL_WRITE_BATCH_SIZE"], "3")

    def test_atomic_write_text_replaces_existing_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.yaml"
            path.write_text("old: true\n", encoding="utf-8")

            atomic_write_text(path, "new: true\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "new: true\n")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
